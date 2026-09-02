import os
import time
import zipfile
import io
import xml.etree.ElementTree as ET
from datetime import datetime as dt
from django.core.management.base import BaseCommand
from django.utils import timezone as django_timezone
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv
from meters.models import Device, Reading, SyncStatus
import logging
from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections
import re

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync data from external website (every 12 hours)'

    def add_arguments(self, parser):
        parser.add_argument('--verbose', action='store_true', help='Подробный вывод')
        parser.add_argument('--data-type', choices=['all', 'current'], default='all', help='Тип данных')
        parser.add_argument('--daemon', action='store_true', help='Режим демона с расписанием (03:00 и 15:00)')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        self.username = os.getenv('WEBSITE_USERNAME')
        self.password = os.getenv('WEBSITE_PASSWORD')
        self.url = os.getenv('WEBSITE_URL', 'http://192.168.20.252:8088/#/login')
        self.download_dir = '/app/downloads'
        os.makedirs(self.download_dir, exist_ok=True)
        self.verbose = options.get('verbose', False)
        self.data_type = options.get('data_type', 'all')
        daemon = options.get('daemon', False)

        if not self.username or not self.password:
            self.stderr.write(self.style.ERROR("❌ WEBSITE_USERNAME and WEBSITE_PASSWORD must be set in cEnergo.env"))
            return

        if daemon:
            self.stdout.write(self.style.SUCCESS("🤖 Website sync robot started in DAEMON mode (03:00 & 15:00)."))
            self.run_loop()
        else:
            self.stdout.write(self.style.SUCCESS("🤖 Website sync robot started (single run)."))
            try:
                self.sync()
                self.stdout.write(self.style.SUCCESS("✅ Sync completed successfully."))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"❌ Sync failed: {e}"))
                raise

    def run_loop(self):
        last_run = None
        while True:
            now = dt.now()
            if (now.hour in [3, 15] and now.minute < 5) or last_run is None:
                if last_run and last_run.hour == now.hour and last_run.day == now.day:
                    time.sleep(60)
                    continue
                self.stdout.write(f"🔄 Scheduled sync at {now}")
                try:
                    self.sync()
                    self.stdout.write(self.style.SUCCESS("✅ Scheduled sync completed."))
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"❌ Scheduled sync failed: {e}"))
                last_run = now
            time.sleep(60)

    def sync(self):
        self.stdout.write("🔄 Starting website sync...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            file_path = None
            try:
                # -------- 1. ЛОГИН --------
                self.stdout.write("🔐 Logging in...")
                page.goto(self.url, wait_until='networkidle')
                page.wait_for_selector('input[placeholder="User Name"]', timeout=30000)
                page.fill('input[placeholder="User Name"]', self.username)
                page.fill('input[placeholder="Password"]', self.password)
                page.click('button.login-btn')
                page.wait_for_selector('.container', timeout=30000)
                self.stdout.write(self.style.SUCCESS("✅ Logged in."))
                time.sleep(1)

                # -------- 2. НАВИГАЦИЯ --------
                self.stdout.write("📂 Navigating to data query page...")
                page.locator('.el-submenu__title:has-text("Data Collection")').first.click()
                time.sleep(0.5)
                page.locator('.el-submenu__title:has-text("Data Query")').first.click()
                time.sleep(0.5)
                page.locator('li.el-menu-item:has-text("Proflie Data (Multiple Meter)")').click()
                page.wait_for_selector('button#query', state='visible', timeout=30000)
                self.stdout.write(self.style.SUCCESS("✅ Navigated."))
                time.sleep(1)

                # -------- 3. ВЫБОР ПАРАМЕТРОВ --------
                self.stdout.write("🔧 Selecting parameters...")
                profile_select = page.locator('.el-select[name="channelId"]')
                profile_select.click()
                page.wait_for_selector('div.el-select-dropdown.el-popper:not([style*="display: none"])', state='visible', timeout=10000)
                page.locator('li:has-text("Load Profile with Period 2")').click()
                page.wait_for_selector('div.el-select-dropdown.el-popper', state='hidden', timeout=5000)
                time.sleep(0.5)
                side_select = page.locator('.el-select[name="sideType"]')
                side_select.click()
                page.wait_for_selector('div.el-select-dropdown.el-popper:not([style*="display: none"])', state='visible', timeout=5000)
                page.locator('.el-select-dropdown__item:has-text("Secondary Side")').click()
                page.wait_for_selector('div.el-select-dropdown.el-popper', state='hidden', timeout=3000)
                time.sleep(0.5)
                page.locator('label.el-radio:has(input[value="2"])').click()
                time.sleep(0.5)

                # -------- 4. QUERY + FETCH TOTAL --------
                self.stdout.write("🔍 Clicking Query...")
                page.click('button#query')
                page.wait_for_selector('.el-table__body tr', state='attached', timeout=60000)
                row_count = page.locator('.el-table__body tr').count()
                if row_count == 0:
                    raise Exception("No data rows found after query.")
                self.stdout.write(self.style.SUCCESS(f"✅ Data loaded, found {row_count} rows."))

                try:
                    fetch_btn = page.locator('button:has-text("Fetch Total")')
                    if fetch_btn.count() > 0:
                        fetch_btn.click()
                        self.stdout.write("🔍 Clicked Fetch Total.")
                        time.sleep(2)
                except Exception as e:
                    self.stdout.write(f"ℹ️ Fetch Total not found or error: {e}")

                # -------- 5. СКАЧИВАНИЕ --------
                self.stdout.write("📥 Downloading Type1...")
                download_btn = page.locator('.el-dropdown-link:has-text("Download Type1")')
                download_btn.wait_for(state='visible', timeout=10000)
                download_btn.click(force=True)

                self.stdout.write("⏳ Waiting for file list dialog...")
                page.wait_for_selector('span:has-text(".xml")', state='visible', timeout=180000)

                file_spans = page.locator('span:has-text(".xml")').all()
                if not file_spans:
                    raise Exception("No XML files found in the dialog.")

                # Выбираем самый свежий
                latest_span = None
                latest_time = None
                date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2}_\d{2}_\d{2}_\d{2})')

                for span in file_spans:
                    text = span.text_content().strip()
                    match = date_pattern.search(text)
                    if match:
                        try:
                            file_dt = dt.strptime(match.group(1), '%Y-%m-%d_%H_%M_%S')
                            if latest_time is None or file_dt > latest_time:
                                latest_time = file_dt
                                latest_span = span
                        except ValueError:
                            continue

                if latest_span is None:
                    latest_span = file_spans[0]
                    self.stdout.write(self.style.WARNING("⚠️ Could not parse dates, using first XML file."))

                file_name = latest_span.text_content().strip()
                self.stdout.write(self.style.SUCCESS(f"✅ Selected latest file: {file_name}"))

                parent = latest_span.locator('xpath=ancestor::div[contains(@class, "el-dialog") or contains(@class, "el-form-item") or contains(@class, "el-row")]').first
                download_in_parent = parent.locator('button:has-text("Download")')
                download_in_parent.wait_for(state='visible', timeout=60000)

                with page.expect_download(timeout=180000) as download_info:
                    download_in_parent.click(force=True)

                download = download_info.value
                file_path = os.path.join(self.download_dir, download.suggested_filename)
                download.save_as(file_path)
                self.stdout.write(self.style.SUCCESS(f"📁 Downloaded: {file_path}"))

                # -------- 6. ИМПОРТ ДАННЫХ --------
                self.stdout.write("📦 Importing data into database...")
                imported = self._import_data_safe(file_path)

                if imported == 0:
                    self.stdout.write(self.style.WARNING("⚠️ No readings were imported (empty data)."))
                else:
                    self.stdout.write(self.style.SUCCESS(f"✅ Imported {imported} readings."))

                # -------- 7. ОБНОВЛЕНИЕ СТАТУСА --------
                self._update_sync_status_safe('success', imported, None)

                # -------- 8. УДАЛЕНИЕ СКАЧАННОГО ФАЙЛА --------
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
                    self.stdout.write(f"🗑️ Deleted downloaded file: {file_path}")

                browser.close()
                self.stdout.write(self.style.SUCCESS("✅ All done."))

            except Exception as e:
                self.stderr.write(self.style.ERROR(f"❌ Sync error: {e}"))
                page.screenshot(path=os.path.join(self.download_dir, 'error_screenshot.png'))
                self.stdout.write(f"📸 Screenshot saved to {self.download_dir}/error_screenshot.png")
                with open(os.path.join(self.download_dir, 'error_page.html'), 'w', encoding='utf-8') as f:
                    f.write(page.content())
                self.stdout.write(f"📄 Page HTML saved to {self.download_dir}/error_page.html")
                self._update_sync_status_safe('error', 0, str(e))
                raise
            finally:
                browser.close()

    # -------- ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ --------
    def _import_data_safe(self, file_path):
        def import_task():
            close_old_connections()
            imported = 0
            try:
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    for name in zip_ref.namelist():
                        if name.endswith('.xml'):
                            with zip_ref.open(name) as f:
                                content = f.read()
                                imported = self.parse_xml_and_import(content)
                            break
            except Exception as e:
                self.stderr.write(f"Import error: {e}")
                raise
            return imported

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(import_task)
            return future.result(timeout=600)

    def parse_xml_and_import(self, content):
        close_old_connections()
        # all_devices = Device.objects.select_related('model').all()
        # devices_by_full = {}
        # devices_by_last8 = {}

        # for d in all_devices:
        #     devices_by_full[d.serial_number] = d
        #     is_sanxing = False
        #     if hasattr(d, 'api_id') and d.api_id and d.api_id.upper().startswith('SX'):
        #         is_sanxing = True
        #     if not is_sanxing and hasattr(d, 'model') and d.model:
        #         if hasattr(d.model, 'manufacturer') and d.model.manufacturer:
        #             if 'Sanxing' in d.model.manufacturer or 'sanxing' in d.model.manufacturer.lower():
        #                 is_sanxing = True
        #         elif hasattr(d.model, 'name') and d.model.name:
        #             if 'Sanxing' in d.model.name or 'sanxing' in d.model.name.lower():
        #                 is_sanxing = True
        #     if is_sanxing and len(d.serial_number) >= 8:
        #         last8 = d.serial_number[-8:]
        #         if last8 not in devices_by_last8:
        #             devices_by_last8[last8] = d

        from meters.utils import get_robot_devices
        devices = get_robot_devices('Sanxing_new_100A')
        devices_by_full = {d.serial_number: d for d in devices}
        devices_by_last8 = {}
        for d in devices:
            if len(d.serial_number) >= 8:
                last8 = d.serial_number[-8:]
                devices_by_last8[last8] = d

        count = 0
        try:
            tree = ET.parse(io.BytesIO(content))
            root = tree.getroot()
            for elem in root.iter():
                serialno_elem = elem.find('serialno')
                time_elem = elem.find('time')
                value_elem = elem.find('value')
                if serialno_elem is not None and time_elem is not None:
                    sn = serialno_elem.text.strip()
                    if not sn:
                        continue

                    # --- ПРОВЕРКА НА НАЛИЧИЕ ЗНАЧЕНИЯ ---
                    if value_elem is None or not value_elem.text or not value_elem.text.strip():
                        # Нет значения – пропускаем запись
                        if self.verbose:
                            self.stdout.write(f"ℹ️ Skipping record with empty value for serial {sn}")
                        continue
                    try:
                        val = float(value_elem.text.strip())
                    except ValueError:
                        if self.verbose:
                            self.stdout.write(f"⚠️ Invalid number format for serial {sn}: '{value_elem.text}'")
                        continue

                    device = devices_by_full.get(sn)
                    if not device and len(sn) >= 8:
                        last8 = sn[-8:]
                        device = devices_by_last8.get(last8)
                    if not device:
                        if self.verbose:
                            self.stdout.write(f"⚠️ Device with serial {sn} not found, skipping.")
                        continue
                    try:
                        dt_time = dt.strptime(time_elem.text, '%Y-%m-%d %H:%M:%S')
                    except:
                        continue

                    Reading.objects.update_or_create(
                        device=device,
                        timestamp=dt_time,
                        defaults={'reading_value': val, 'notes': 'Website sync', 'direction': 'aplus'}
                    )
                    count += 1
            return count
        except Exception as e:
            self.stderr.write(f"XML parsing error: {e}")
            raise

    def _update_sync_status_safe(self, status, records_processed, error):
        def sync_update():
            close_old_connections()
            SyncStatus.objects.update_or_create(
                robot_name='Sanxing_new_100A',
                defaults={
                    'status': status,
                    'last_update': django_timezone.now(),
                    'records_processed': records_processed,
                    'error': error,
                }
            )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(sync_update)
            future.result(timeout=10)