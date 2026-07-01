import os
import time
import zipfile
import io
import xml.etree.ElementTree as ET
from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone as django_timezone
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv
from meters.models import Device, Reading, SyncStatus
from meters.utils import find_device
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync data from external website (every 12 hours)'

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        self.username = os.getenv('WEBSITE_USERNAME')
        self.password = os.getenv('WEBSITE_PASSWORD')
        self.url = os.getenv('WEBSITE_URL', 'http://192.168.20.252:8088/#/login')
        self.download_dir = '/app/downloads'
        os.makedirs(self.download_dir, exist_ok=True)

        if not self.username or not self.password:
            logger.error("WEBSITE_USERNAME and WEBSITE_PASSWORD must be set in cEnergo.env")
            return

        # logger.info("🤖 Website sync robot started. Will run every 12 hours at 03:00 and 15:00.")
        # self.last_run = None

        # while True:
        #     now = datetime.now()
        #     if (now.hour in [3, 15] and now.minute < 5) or self.last_run is None:
        #         if self.last_run and self.last_run.hour == now.hour and self.last_run.day == now.day:
        #             time.sleep(60)
        #             continue
        #         logger.info(f"🔄 Starting sync at {now}")
        #         self.sync()
        #         self.last_run = now
        #     time.sleep(60)
        logger.info("🤖 Website sync robot started (single run).")
        self.sync()
        logger.info("✅ Sync completed.")
        return  # или break, если внутри цикла

    def sync(self):
        logger.info("🔄 Starting website sync...")
        error = None
        imported = 0
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()

                try:
                    # 1. Логин
                    logger.info("🔐 Logging in...")
                    page.goto(self.url, wait_until='networkidle')
                    page.wait_for_selector('input[placeholder="User Name"]', timeout=30000)
                    page.fill('input[placeholder="User Name"]', self.username)
                    page.fill('input[placeholder="Password"]', self.password)
                    page.click('button.login-btn')
                    page.wait_for_selector('.container', timeout=30000)
                    logger.info("✅ Logged in.")
                    time.sleep(1)

                    # 2. Навигация
                    logger.info("📂 Navigating to data query page...")
                    page.locator('.el-submenu__title:has-text("Data Collection")').first.click()
                    time.sleep(0.5)
                    page.locator('.el-submenu__title:has-text("Data Query")').first.click()
                    time.sleep(0.5)
                    page.locator('li.el-menu-item:has-text("Proflie Data (Multiple Meter)")').click()
                    page.wait_for_selector('button#query', state='visible', timeout=30000)
                    logger.info("✅ Navigated.")
                    time.sleep(1)

                    # 3. Выбор параметров
                    # Profile
                    profile_select = page.locator('.el-select[name="channelId"]')
                    profile_select.click()
                    page.wait_for_selector('div.el-select-dropdown.el-popper:not([style*="display: none"])', state='visible', timeout=10000)
                    page.locator('li:has-text("Load Profile with Period 2")').click()
                    page.wait_for_selector('div.el-select-dropdown.el-popper', state='hidden', timeout=5000)
                    time.sleep(0.5)
                    # Side Type
                    side_select = page.locator('.el-select[name="sideType"]')
                    side_select.click()
                    page.wait_for_selector('div.el-select-dropdown.el-popper:not([style*="display: none"])', state='visible', timeout=5000)
                    page.locator('.el-select-dropdown__item:has-text("Secondary Side")').click()
                    page.wait_for_selector('div.el-select-dropdown.el-popper', state='hidden', timeout=3000)
                    time.sleep(0.5)
                    # Radio button '2'
                    page.locator('label.el-radio:has(input[value="2"])').click()
                    time.sleep(0.5)

                    # 4. Query
                    logger.info("🔍 Clicking Query...")
                    page.click('button#query')
                    page.wait_for_selector('.el-table__body tr', state='attached', timeout=60000)
                    row_count = page.locator('.el-table__body tr').count()
                    if row_count == 0:
                        raise Exception("No data rows found after query.")
                    logger.info(f"✅ Data loaded, found {row_count} rows.")

                    # 5. Скачивание
                    logger.info("📥 Downloading Type1...")
                    download_btn = page.locator('.el-dropdown-link:has-text("Download Type1")')
                    download_btn.wait_for(state='visible', timeout=10000)
                    download_btn.click(force=True)

                    # Ждём появления диалога экспорта (увеличенный таймаут)
                    logger.info("⏳ Waiting for export dialog to appear...")
                    try:
                        page.wait_for_selector('div.el-dialog', state='visible', timeout=180000)  # 3 минуты
                        logger.info("✅ Export dialog appeared.")
                    except PlaywrightTimeout:
                        logger.warning("Dialog not found, waiting for .xml span...")
                        page.wait_for_selector('span:has-text(".xml")', state='visible', timeout=60000)
                        logger.info("✅ .xml span found (fallback).")

                    # Ищем span с именем файла
                    file_span = page.locator('span:has-text(".xml")').first
                    file_span.wait_for(state='visible', timeout=10000)
                    file_name = file_span.text_content()
                    logger.info(f"✅ Export completed, file name: {file_name}")

                    time.sleep(2)

                    # Находим родительский диалог и кнопку Download
                    dialog = page.locator('div.el-dialog:has(span:has-text(".xml"))').first
                    if not dialog.is_visible():
                        dialog = file_span.locator('xpath=ancestor::div[contains(@class, "el-dialog")]').first
                    if dialog.count() == 0:
                        raise Exception("Could not find dialog containing file name.")

                    download_in_dialog = dialog.locator('button:has-text("Download")')
                    download_in_dialog.wait_for(state='visible', timeout=30000)
                    if download_in_dialog.is_disabled():
                        logger.warning("Download button is disabled, waiting...")
                        time.sleep(5)

                    # Запускаем скачивание (увеличенный таймаут)
                    logger.info("⏳ Waiting for download to start...")
                    with page.expect_download(timeout=300000) as download_info:  # 5 минут
                        download_in_dialog.click(force=True)

                    download = download_info.value
                    file_path = os.path.join(self.download_dir, download.suggested_filename)
                    download.save_as(file_path)
                    logger.info(f"📁 Downloaded: {file_path}")

                    # 6. Импорт данных
                    imported = self.import_data(file_path)
                    logger.info(f"✅ Imported {imported} readings.")

                    browser.close()

                except Exception as e:
                    error = str(e)
                    logger.error(f"❌ Sync error: {e}")
                    # Сохраняем скриншот
                    screenshot_path = os.path.join(self.download_dir, 'error_screenshot.png')
                    page.screenshot(path=screenshot_path)
                    logger.info(f"📸 Screenshot saved: {screenshot_path}")
                    browser.close()
                    raise  # пробрасываем, чтобы выйти из контекста playwright
        except Exception as e:
            # Если ошибка произошла вне блока playwright
            if not error:
                error = str(e)
            logger.error(f"Outer error: {e}")

        # Обновляем статус СИНХРОННО (вне контекста playwright)
        if error:
            SyncStatus.objects.update_or_create(
                robot_name='Website',
                defaults={
                    'status': 'error',
                    'last_update': django_timezone.now(),
                    'error': error
                }
            )
        else:
            SyncStatus.objects.update_or_create(
                robot_name='Website',
                defaults={
                    'status': 'success',
                    'last_update': django_timezone.now(),
                    'records_processed': imported,
                    'error': None
                }
            )
        logger.info("✅ Sync status updated.")

    def import_data(self, file_path):
        """Распаковывает ZIP, парсит XML и импортирует показания в БД."""
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
            logger.error(f"Import error: {e}")
            raise
        return imported

    def parse_xml_and_import(self, content):
        """Парсит XML и сохраняет показания."""
        try:
            tree = ET.parse(io.BytesIO(content))
            root = tree.getroot()
            count = 0
            for elem in root.iter():
                serialno_elem = elem.find('serialno')
                time_elem = elem.find('time')
                value_elem = elem.find('value')
                if serialno_elem is not None and time_elem is not None:
                    sn = serialno_elem.text.strip()
                    if not sn:
                        continue
                    device = find_device(sn)
                    if not device:
                        continue
                    try:
                        dt = datetime.strptime(time_elem.text, '%Y-%m-%d %H:%M:%S')
                    except:
                        continue
                    val = float(value_elem.text) if value_elem is not None and value_elem.text else 0.0
                    Reading.objects.update_or_create(
                        device=device,
                        timestamp=dt,
                        defaults={'reading_value': val, 'notes': 'Website sync'}
                    )
                    count += 1
            return count
        except Exception as e:
            logger.error(f"XML parsing error: {e}")
            raise