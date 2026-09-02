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
    help = 'Sync Raw Data from Hexing (Mini-MDM) website'

    def add_arguments(self, parser):
        parser.add_argument('--date', type=str, help='Дата в формате YYYY-MM-DD (по умолчанию вчера)')
        parser.add_argument('--daemon', action='store_true', help='Режим демона с расписанием 03:00 и 15:00')
        parser.add_argument('--verbose', action='store_true', help='Подробный вывод')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        self.username = os.getenv('HEXING_RAW_USERNAME')
        self.password = os.getenv('HEXING_RAW_PASSWORD')
        self.base_url = os.getenv('HEXING_RAW_URL', 'http://192.168.20.247:8080/Mini-MDM/common/view/init')
        self.download_dir = '/app/downloads'
        os.makedirs(self.download_dir, exist_ok=True)
        self.verbose = options.get('verbose', False)

        if not self.username or not self.password:
            self.stderr.write(self.style.ERROR("HEXING_RAW_USERNAME and HEXING_RAW_PASSWORD must be set in cEnergo.env"))
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
            self.stdout.write(self.style.SUCCESS("🤖 Hexing Raw sync robot started in DAEMON mode (03:00 & 15:00)."))
            self.run_loop()
        else:
            self.stdout.write(self.style.SUCCESS("🤖 Hexing Raw sync robot started (single run)."))
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
        self.stdout.write("🔄 Starting Hexing Raw sync...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.on('dialog', lambda dialog: dialog.accept())

            try:
                # -------- 1. ЛОГИН --------
                self.stdout.write("🌐 Logging in...")
                page.goto(self.base_url)
                page.wait_for_selector('input#czyId', timeout=30000)
                page.screenshot(path=os.path.join(self.download_dir, 'hxn_01_login.png'))

                username_input = page.locator('input#czyId')
                username_input.click()
                username_input.fill(self.username)

                password_input = page.locator('input#pwd')
                password_input.click()
                password_input.fill(self.password)

                page.click('.ui-button-login')
                try:
                    page.wait_for_selector('text="Your password will expire.Please modify it!"', timeout=10000)
                    self.stdout.write("⚠️ Password expiry dialog, clicking OK...")
                    page.click('button:has-text("OK")')
                except PlaywrightTimeout:
                    pass

                page.wait_for_selector('text="Data-Analysis"', timeout=30000)
                self.stdout.write(self.style.SUCCESS("✅ Logged in."))
                page.screenshot(path=os.path.join(self.download_dir, 'hxn_02_after_login.png'))

                # -------- 2. НАВИГАЦИЯ К RAW DATA REPORT --------
                self.stdout.write("📂 Navigating to Data-Analysis -> Raw Data Report...")
                page.click('text="Data-Analysis"')
                time.sleep(1)
                page.click('text="Raw Data Report"')
                time.sleep(3)

                # -------- 3. РАБОТА С IFRAME --------
                self.stdout.write("⏳ Finding iframe...")
                iframe_element = page.query_selector('iframe#mainFrame2')
                if not iframe_element:
                    iframe_element = page.query_selector('iframe[src*="rawDataReport"]')
                if not iframe_element:
                    raise Exception("Raw Data Report iframe not found")
                target_frame = iframe_element.content_frame()
                if not target_frame:
                    raise Exception("Could not get iframe content")
                self.stdout.write(self.style.SUCCESS(f"✅ Found frame: {target_frame.url}"))

                # -------- 4. ОЖИДАНИЕ ЗАГРУЗКИ ДАННЫХ (30 секунд) --------
                self.stdout.write("⏳ Waiting 30 seconds for data to load...")
                time.sleep(30)
                page.screenshot(path=os.path.join(self.download_dir, 'hxn_03_data_loaded.png'))

                # -------- 5. НАЖАТИЕ DOWNLOAD (открывает модальное окно с iframe) --------
                self.stdout.write("📥 Clicking Download...")
                download_btn = target_frame.locator('button:has-text("Download")')
                if download_btn.count() == 0:
                    download_btn = target_frame.locator('a:has-text("Download")')
                if download_btn.count() == 0:
                    raise Exception("Download button not found")
                download_btn.click()

                # Ждём модальное окно внутри iframe
                self.stdout.write("⏳ Waiting for modal window inside iframe...")
                target_frame.wait_for_selector('div#rawDataReportDownload.x-window', timeout=30000)
                modal = target_frame.locator('div#rawDataReportDownload.x-window')
                self.stdout.write(self.style.SUCCESS("✅ Modal window found."))

                # Находим iframe внутри модального окна
                self.stdout.write("🔍 Looking for iframe inside modal...")
                iframe_el = target_frame.query_selector('iframe#openwin')
                if not iframe_el:
                    iframe_el = target_frame.query_selector('iframe')
                if not iframe_el:
                    raise Exception("iframe inside modal not found")
                popup_frame = iframe_el.content_frame()
                if not popup_frame:
                    raise Exception("Could not get frame from iframe inside modal")
                self.stdout.write(self.style.SUCCESS("✅ Iframe inside modal found."))

                # -------- 6. ВЫБОР DATA RANGE = ALL --------
                self.stdout.write("🔧 Selecting Data Range = All (value=01)...")
                data_range_select = popup_frame.locator('select#dataRange')
                data_range_select.wait_for(state='visible', timeout=10000)
                data_range_select.select_option('01')
                self.stdout.write("✅ Data Range set to All.")

                # -------- 7. НАЖАТИЕ DOWNLOAD + ОБРАБОТКА ДИАЛОГА ПОДТВЕРЖДЕНИЯ (как в тесте) --------
                self.stdout.write("📥 Clicking Download inside modal...")
                download_in_modal = popup_frame.locator('button:has-text("Download")')
                if download_in_modal.count() == 0:
                    download_in_modal = popup_frame.locator('a:has-text("Download")')
                if download_in_modal.count() > 0:
                    # Кликаем по Download
                    download_in_modal.click()
                    time.sleep(2)

                    # Ищем диалог подтверждения и нажимаем OK
                    self.stdout.write("⏳ Looking for confirmation dialog...")
                    confirm_dialog = popup_frame.locator('.x-window:has-text("Confirm")')
                    if confirm_dialog.count() == 0:
                        confirm_dialog = page.locator('.x-window:has-text("Confirm")')
                    if confirm_dialog.count() == 0:
                        confirm_dialog = popup_frame.locator('div:has-text("Are you sure to download?")')
                    if confirm_dialog.count() == 0:
                        confirm_dialog = popup_frame.locator('div:has-text("Confirm")')

                    if confirm_dialog.count() > 0:
                        ok_btn = confirm_dialog.locator('button:has-text("OK")')
                        if ok_btn.count() == 0:
                            ok_btn = confirm_dialog.locator('button:has-text("Ok")')
                        if ok_btn.count() > 0:
                            # Перехватываем скачивание ДО клика по OK
                            with page.expect_download(timeout=120000) as download_info:
                                ok_btn.click()
                            download = download_info.value
                            file_path = os.path.join(self.download_dir, download.suggested_filename)
                            download.save_as(file_path)
                            self.stdout.write(self.style.SUCCESS(f"📁 Downloaded: {file_path}"))
                        else:
                            raise Exception("OK button not found in confirmation dialog.")
                    else:
                        raise Exception("Confirmation dialog not found.")
                else:
                    raise Exception("Download button in modal not found")

                # -------- 8. ИМПОРТ ДАННЫХ --------
                self.stdout.write("📦 Importing data into database...")
                imported = self._import_xml(file_path)
                self.stdout.write(self.style.SUCCESS(f"✅ Imported {imported} readings."))

                # -------- 9. ОБНОВЛЕНИЕ СТАТУСА --------
                self._update_sync_status('success', imported, None)

                # -------- 10. УДАЛЕНИЕ ФАЙЛА --------
                if os.path.exists(file_path):
                    os.remove(file_path)
                    self.stdout.write(f"🗑️ Deleted file: {file_path}")

                browser.close()
                self.stdout.write(self.style.SUCCESS("✅ All done."))

            except Exception as e:
                self.stderr.write(self.style.ERROR(f"❌ Sync error: {e}"))
                page.screenshot(path=os.path.join(self.download_dir, 'hxn_error.png'))
                with open(os.path.join(self.download_dir, 'hxn_error.html'), 'w', encoding='utf-8') as f:
                    f.write(page.content())
                self._update_sync_status('error', 0, str(e))
                raise
            finally:
                browser.close()

    def _import_xml(self, file_path):
        """Парсит XML и импортирует показания (paramId=1) в БД (в отдельном потоке)."""
        def import_task():
            close_old_connections()
            imported = 0
            try:
                tree = ET.parse(file_path)
                root = tree.getroot()

                # Загружаем все активные устройства Hexing
                # all_devices = Device.objects.filter(status='active').select_related('model')
                # devices_by_full = {}
                # devices_by_last8 = {}

                # for d in all_devices:
                #     devices_by_full[d.serial_number] = d
                #     is_hexing = False
                #     if d.askue_id and str(d.askue_id) == '5':
                #         is_hexing = True
                #     elif d.api_id and d.api_id.upper().startswith('HX'):
                #         is_hexing = True
                #     if is_hexing and len(d.serial_number) >= 8:
                #         last8 = d.serial_number[-8:]
                #         devices_by_last8[last8] = d

                from meters.utils import get_robot_devices
                devices = get_robot_devices('Hexing_POP')
                devices_by_full = {d.serial_number: d for d in devices}
                devices_by_last8 = {}
                for d in devices:
                    if len(d.serial_number) >= 8:
                        last8 = d.serial_number[-8:]
                        devices_by_last8[last8] = d

                for mreading in root.findall('mreadings'):
                    meter_no_elem = mreading.find('meterNo')
                    param_id_elem = mreading.find('paramId')
                    value_elem = mreading.find('value')
                    timestamp_elem = mreading.find('timestamp')
                    if meter_no_elem is None or param_id_elem is None or value_elem is None or timestamp_elem is None:
                        continue
                    # Берём только paramId = 1 (основное показание)
                    param_id = param_id_elem.text
                    if param_id != '1':
                        continue
                    sn = meter_no_elem.text.strip()
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
                        val = float(value_elem.text)
                    except:
                        continue
                    # Парсим дату (формат "15.07.2026 00:00:00")
                    try:
                        dt_time = datetime.strptime(timestamp_elem.text, '%d.%m.%Y %H:%M:%S')
                    except ValueError:
                        try:
                            dt_time = datetime.strptime(timestamp_elem.text, '%Y-%m-%d %H:%M')
                        except:
                            continue

                    Reading.objects.update_or_create(
                        device=device,
                        timestamp=dt_time,
                        defaults={'reading_value': val, 'notes': 'Hexing Raw sync', 'direction': 'aplus'}
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
        def sync_update():
            close_old_connections()
            SyncStatus.objects.update_or_create(
                robot_name='Hexing_POP',
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