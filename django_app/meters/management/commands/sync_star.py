import os
import time
import xml.etree.ElementTree as ET
import logging
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone as django_timezone
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv
from meters.models import Device, Reading, SyncStatus
from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync data from Star Power (HES) website'

    def add_arguments(self, parser):
        parser.add_argument('--date', type=str, help='Дата в формате YYYY-MM-DD (по умолчанию вчера)')
        parser.add_argument('--daemon', action='store_true', help='Режим демона с расписанием 03:00 и 15:00')
        parser.add_argument('--verbose', action='store_true', help='Подробный вывод')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        self.username = os.getenv('STAR_USERNAME')
        self.password = os.getenv('STAR_PASSWORD')
        self.base_url = os.getenv('STAR_URL', 'http://192.168.20.246:59101/hes')
        self.download_dir = '/app/downloads'
        os.makedirs(self.download_dir, exist_ok=True)
        self.verbose = options.get('verbose', False)

        if not self.username or not self.password:
            self.stderr.write(self.style.ERROR("STAR_USERNAME and STAR_PASSWORD must be set in cEnergo.env"))
            return

        if options.get('date'):
            try:
                self.sync_date = datetime.strptime(options['date'], '%Y-%m-%d').date()
            except ValueError:
                self.stderr.write(self.style.ERROR("Invalid date format. Use YYYY-MM-DD"))
                return
        else:
            self.sync_date = datetime.now().date() - timedelta(days=1)

        self.stdout.write(f"📅 Sync date: {self.sync_date}")

        if options.get('daemon'):
            self.stdout.write(self.style.SUCCESS("🤖 Star sync robot started in DAEMON mode (03:00 & 15:00)."))
            self.run_loop()
        else:
            self.stdout.write(self.style.SUCCESS("🤖 Star sync robot started (single run)."))
            try:
                self.sync()
                self.stdout.write(self.style.SUCCESS("✅ Sync completed successfully."))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"❌ Sync failed: {e}"))
                raise

    def run_loop(self):
        last_run = None
        while True:
            now = datetime.now()
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
        self.stdout.write("🔄 Starting Star sync...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.on('dialog', lambda dialog: dialog.accept())

            try:
                # -------- 1. ЛОГИН --------
                self.stdout.write("🌐 Logging in...")
                page.goto(f"{self.base_url}/login")
                page.wait_for_selector('input[name="username"]', timeout=30000)
                page.screenshot(path=os.path.join(self.download_dir, 'star_01_login.png'))

                username_input = page.locator('input[name="username"]')
                username_input.click()
                username_input.fill(self.username)

                password_input = page.locator('input[name="password"]')
                password_input.click()
                password_input.fill(self.password)

                page.click('button[type="submit"]')
                page.wait_for_selector('li[data-perm-no="sysMenu.hes.queryAnalysis"]', timeout=30000)
                self.stdout.write(self.style.SUCCESS("✅ Logged in."))
                page.screenshot(path=os.path.join(self.download_dir, 'star_02_after_login.png'))

                # -------- 2. ПЕРЕКЛЮЧЕНИЕ ЯЗЫКА --------
                self.stdout.write("🌍 Switching language to English...")
                lang_select = page.locator('select#langSelect')
                if lang_select.count():
                    lang_select.select_option('en-US')
                    time.sleep(2)
                    self.stdout.write(self.style.SUCCESS("✅ Language switched."))
                page.screenshot(path=os.path.join(self.download_dir, 'star_03_after_language.png'))

                # -------- 3. НАВИГАЦИЯ К QUERY ROW DATA --------
                self.stdout.write("📂 Navigating to Query Row Data...")
                menu_item = page.locator('li[data-perm-no="sysMenu.hes.queryAnalysis"]')
                menu_item.click()
                time.sleep(2)

                query_row_link = page.locator('a:has-text("Query Row Data")')
                if query_row_link.count() == 0:
                    query_row_link = page.locator('a.J_menuItem:has-text("Query Row Data")')
                if query_row_link.count() == 0:
                    query_row_link = page.locator('a[target="_blank"]:has-text("Query Row Data")')
                if query_row_link.count() == 0:
                    raise Exception("Query Row Data link not found")
                query_row_link.click()
                time.sleep(5)

                # -------- 4. РАБОТА С IFRAME --------
                self.stdout.write("🔍 Finding iframe...")
                page.wait_for_selector('iframe#iframe0', timeout=30000)
                iframe_element = page.query_selector('iframe#iframe0')
                if not iframe_element:
                    raise Exception("iframe not found")
                target_frame = iframe_element.content_frame()
                if not target_frame:
                    raise Exception("Could not get iframe content")

                if 'login' in target_frame.url:
                    self.stdout.write("⚠️ iframe shows login, reloading...")
                    target_frame.goto(f"{self.base_url}/hDatDayController/rowDataList")
                    target_frame.wait_for_load_state('networkidle', timeout=30000)

                self.stdout.write(self.style.SUCCESS(f"✅ Found frame: {target_frame.url}"))
                page.screenshot(path=os.path.join(self.download_dir, 'star_04_iframe_found.png'))

                # -------- 5. ОЖИДАНИЕ ЗАГРУЗКИ ДАННЫХ (по умолчанию) --------
                self.stdout.write("⏳ Waiting for default data to load...")
                try:
                    target_frame.wait_for_selector('table#hDatRowDataList tbody tr', timeout=60000)
                    self.stdout.write(self.style.SUCCESS("✅ Data already loaded."))
                except PlaywrightTimeout:
                    self.stdout.write("⚠️ No data found, clicking Query...")
                    query_btn = target_frame.locator('button#btnQueryhDatRowDataList')
                    if query_btn.count() > 0:
                        query_btn.click()
                    target_frame.wait_for_selector('table#hDatRowDataList tbody tr', timeout=60000)
                    self.stdout.write(self.style.SUCCESS("✅ Data loaded after Query."))

                page.screenshot(path=os.path.join(self.download_dir, 'star_06_data_table.png'))

                # -------- 6. ЭКСПОРТ XML (перехват по Content-Type) --------
                self.stdout.write("📥 Exporting XML...")
                export_btn = target_frame.locator('.btn_wrapper[title="Export XML File2"] .ui-title-btn a.fa-file-code-o')
                if export_btn.count() == 0:
                    export_btn = target_frame.locator('.btn_wrapper:has-text("Export XML File2")')
                if export_btn.count() == 0:
                    raise Exception("Export XML File2 button not found")

                with page.expect_response(
                    lambda resp: 'xml' in resp.headers.get('content-type', '').lower(),
                    timeout=60000
                ) as response_info:
                    export_btn.click(force=True)

                response = response_info.value
                xml_content = response.body()
                file_path = os.path.join(self.download_dir, f'star_{self.sync_date}.xml')
                with open(file_path, 'wb') as f:
                    f.write(xml_content)
                self.stdout.write(self.style.SUCCESS(f"📁 Downloaded XML: {file_path}"))

                # -------- 7. ИМПОРТ ДАННЫХ --------
                self.stdout.write("📦 Importing data into database...")
                imported = self._import_xml(file_path)
                self.stdout.write(self.style.SUCCESS(f"✅ Imported {imported} readings."))

                # -------- 8. ОБНОВЛЕНИЕ СТАТУСА --------
                self._update_sync_status('success', imported, None)

                # -------- 9. УДАЛЕНИЕ ФАЙЛА --------
                if os.path.exists(file_path):
                    os.remove(file_path)
                    self.stdout.write(f"🗑️ Deleted file: {file_path}")

                browser.close()
                self.stdout.write(self.style.SUCCESS("✅ All done."))

            except Exception as e:
                self.stderr.write(self.style.ERROR(f"❌ Sync error: {e}"))
                page.screenshot(path=os.path.join(self.download_dir, 'star_error.png'))
                with open(os.path.join(self.download_dir, 'star_error.html'), 'w', encoding='utf-8') as f:
                    f.write(page.content())
                self._update_sync_status('error', 0, str(e))
                raise
            finally:
                browser.close()

    def _import_xml(self, file_path):
        def import_task():
            close_old_connections()
            imported = 0
            try:
                tree = ET.parse(file_path)
                root = tree.getroot()

                # all_devices = Device.objects.filter(status='active').select_related('model')
                # devices_by_full = {}
                # devices_by_last8 = {}

                # for d in all_devices:
                #     devices_by_full[d.serial_number] = d
                #     is_star = False
                #     if d.askue_id and str(d.askue_id) == '14':
                #         is_star = True
                #     elif d.api_id and d.api_id.upper().startswith('ST'):
                #         is_star = True
                #     if is_star and len(d.serial_number) >= 8:
                #         last8 = d.serial_number[-8:]
                #         devices_by_last8[last8] = d

                from meters.utils import get_robot_devices
                devices = get_robot_devices('Star')
                devices_by_full = {d.serial_number: d for d in devices}
                devices_by_last8 = {}
                for d in devices:
                    if len(d.serial_number) >= 8:
                        last8 = d.serial_number[-8:]
                        devices_by_last8[last8] = d
                
                for mreading in root.findall('mreadings'):
                    meter_no = mreading.find('meterNo')
                    param_id = mreading.find('paramId')
                    value = mreading.find('value')
                    timestamp = mreading.find('timestamp')

                    if meter_no is None or value is None or timestamp is None:
                        continue

                    # Фильтр: paramId == 1 (общее показание)
                    if param_id is not None:
                        try:
                            p_id = int(param_id.text)
                            if p_id != 1:
                                continue
                        except:
                            continue
                    else:
                        # если paramId отсутствует, считаем что это общее (но лучше пропускать)
                        continue

                    sn = meter_no.text.strip()
                    if not sn:
                        continue

                    device = devices_by_full.get(sn)
                    if not device and len(sn) >= 8:
                        last8 = sn[-8:]
                        device = devices_by_last8.get(last8)

                    if not device:
                        if self.verbose:
                            self.stdout.write(f"Skipping serial {sn} (not found)")
                        continue

                    try:
                        val = float(value.text)
                    except:
                        continue
                    try:
                        dt_time = datetime.strptime(timestamp.text, '%Y-%m-%d %H:%M')
                    except ValueError:
                        try:
                            dt_time = datetime.strptime(timestamp.text, '%m/%d/%Y %H:%M:%S')
                        except:
                            continue

                    Reading.objects.update_or_create(
                        device=device,
                        timestamp=dt_time,
                        defaults={'reading_value': val, 'notes': 'Star sync', 'direction': 'aplus'}
                    )
                    imported += 1
                    if imported % 1000 == 0:
                        self.stdout.write(f"Imported {imported} readings")
                return imported
            except Exception as e:
                self.stderr.write(f"XML import error: {e}")
                raise

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(import_task)
            return future.result(timeout=600)

    def _update_sync_status(self, status, records_processed, error):
        """Обновляет статус синхронизации (в отдельном потоке)."""
        def sync_update():
            close_old_connections()
            SyncStatus.objects.update_or_create(
                robot_name='Star',
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