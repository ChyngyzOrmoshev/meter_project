import os
import time
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

class Command(BaseCommand):
    help = 'Test Hexing Raw Data Report: login, wait for data, download XML via response capture'

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        username = os.getenv('HEXING_RAW_USERNAME')
        password = os.getenv('HEXING_RAW_PASSWORD')
        base_url = os.getenv('HEXING_RAW_URL', 'http://192.168.20.247:8080/Mini-MDM/common/view/init')
        download_dir = '/app/downloads'
        os.makedirs(download_dir, exist_ok=True)

        if not username or not password:
            self.stdout.write(self.style.ERROR("HEXING_RAW_USERNAME and HEXING_RAW_PASSWORD must be set in cEnergo.env"))
            return

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.on('dialog', lambda dialog: dialog.accept())

            try:
                # -------- 1. ЛОГИН --------
                self.stdout.write("🌐 Navigating to login page...")
                page.goto(base_url)
                page.wait_for_selector('input#czyId', timeout=30000)
                page.screenshot(path=os.path.join(download_dir, 'test_hxn_01_login.png'))

                self.stdout.write("🔐 Filling login form...")
                username_input = page.locator('input#czyId')
                username_input.click()
                username_input.fill(username)

                password_input = page.locator('input#pwd')
                password_input.click()
                password_input.fill(password)

                page.click('.ui-button-login')
                try:
                    page.wait_for_selector('text="Your password will expire.Please modify it!"', timeout=10000)
                    self.stdout.write("⚠️ Password expiry dialog, clicking OK...")
                    page.click('button:has-text("OK")')
                except PlaywrightTimeout:
                    pass

                page.wait_for_selector('text="Data-Analysis"', timeout=30000)
                self.stdout.write(self.style.SUCCESS("✅ Logged in."))
                page.screenshot(path=os.path.join(download_dir, 'test_hxn_02_after_login.png'))

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
                page.screenshot(path=os.path.join(download_dir, 'test_hxn_03_data_loaded.png'))

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
                    # Попробуем найти любой iframe внутри модалки
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

                # -------- 7. НАЖАТИЕ DOWNLOAD + ОБРАБОТКА ДИАЛОГА ПОДТВЕРЖДЕНИЯ --------
                self.stdout.write("📥 Clicking Download inside modal...")
                download_in_modal = popup_frame.locator('button:has-text("Download")')
                if download_in_modal.count() == 0:
                    download_in_modal = popup_frame.locator('a:has-text("Download")')
                if download_in_modal.count() > 0:
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
                            file_path = os.path.join(download_dir, download.suggested_filename)
                            download.save_as(file_path)
                            self.stdout.write(self.style.SUCCESS(f"📁 Downloaded: {file_path}"))
                        else:
                            raise Exception("OK button not found in confirmation dialog.")
                    else:
                        raise Exception("Confirmation dialog not found.")
                else:
                    raise Exception("Download button in modal not found")

                # -------- 8. ПАРСИНГ XML --------
                self.stdout.write("📄 Parsing XML...")
                tree = ET.parse(file_path)
                root = tree.getroot()
                readings = root.findall('mreadings')
                self.stdout.write(f"✅ Found {len(readings)} readings in XML")
                for i, mreading in enumerate(readings[:5]):
                    meter_no = mreading.find('meterNo').text if mreading.find('meterNo') is not None else 'N/A'
                    value = mreading.find('value').text if mreading.find('value') is not None else 'N/A'
                    timestamp = mreading.find('timestamp').text if mreading.find('timestamp') is not None else 'N/A'
                    self.stdout.write(f"  {i+1}. Meter: {meter_no}, Value: {value}, Time: {timestamp}")

                os.remove(file_path)
                self.stdout.write("🗑️ Deleted test file.")
                self.stdout.write(self.style.SUCCESS("✅ Test completed successfully."))

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"❌ Error: {e}"))
                page.screenshot(path=os.path.join(download_dir, 'test_hxn_error.png'))
                with open(os.path.join(download_dir, 'test_hxn_error.html'), 'w', encoding='utf-8') as f:
                    f.write(page.content())
            finally:
                browser.close()