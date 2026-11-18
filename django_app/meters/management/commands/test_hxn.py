import os
import time
import requests
import xml.etree.ElementTree as ET
from django.core.management.base import BaseCommand
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

class Command(BaseCommand):
    help = 'Тест с параметрами браузера (xmlType=00)'

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        
        username = os.getenv('HEXING_RAW_USERNAME')
        password = os.getenv('HEXING_RAW_PASSWORD')
        raw_url = os.getenv('HEXING_RAW_URL', 'http://192.168.20.247:8080/Mini-MDM/common/view/init')
        root_url = raw_url.split('/Mini-MDM')[0] + '/Mini-MDM' if '/Mini-MDM' in raw_url else raw_url.rstrip('/')
        
        self.stdout.write("1️⃣ Авторизация через Playwright...")
        cookies = {}
        user_agent = None
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.on("dialog", lambda dialog: dialog.accept())
            
            try:
                # Логин
                page.goto(f"{root_url}/common/login/init", timeout=30000)
                page.wait_for_selector('input#czyId', timeout=15000)
                page.fill('input#czyId', username)
                page.fill('input#pwd', password)
                page.click('.ui-button-login')
                page.wait_for_timeout(3000)
                
                try:
                    ok_btn = page.locator('button:has-text("OK")').first
                    if ok_btn.is_visible(timeout=3000):
                        ok_btn.click()
                        page.wait_for_timeout(3000)
                except PlaywrightTimeout:
                    pass
                
                # Страница отчета
                page.goto(
                    f"{root_url}/PS/amiPSPakistan/controller/rawDataReport/init"
                    f"?menuid=AmiPS_RawDataReport&menupath=Raw Data Report&curTabId=AmiPS_RawDataReport",
                    timeout=30000
                )
                page.wait_for_load_state('networkidle', timeout=30000)
                page.wait_for_timeout(2000)
                
                # Принудительный DWR вызов
                page.evaluate("""
                    () => new Promise(res => {
                        if (!window.dwr) return res(false);
                        dwr.engine._execute(dwr.engine._pathToDwrServlet, '__System', 'generateId',
                            [{callback: () => res(true), errorHandler: () => res(false)}]);
                    })
                """)
                page.wait_for_timeout(2000)
                
                for c in context.cookies():
                    cookies[c['name']] = c['value']
                user_agent = page.evaluate("() => navigator.userAgent")
                
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"❌ Ошибка Playwright: {e}"))
                browser.close()
                return
            
            browser.close()

        # ============================================================
        # СКАЧИВАНИЕ С ПАРАМЕТРАМИ ИЗ БРАУЗЕРА (xmlType=00)
        # ============================================================
        self.stdout.write("\n2️⃣ Скачивание через requests (xmlType=00)...")
        
        session = requests.Session()
        session.cookies.update(cookies)
        session.headers.update({
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Encoding': 'identity',
        })

        start_date = '2026-11-14'
        end_date = '2026-11-16'
        
        # initDownload
        init_dl_url = f"{root_url}/PS/amiPSPakistan/controller/rawDataReport/initDownload"
        session.get(init_dl_url, params={
            'dateType': '01', 'tariffType': '00', 'startDate': start_date, 'endDate': end_date,
            'nodeType': 'dw', 'nodeId': '105008002', 'meterNo': '', 'CTPT': '', 'last': 'last', 'start': '0', 'limit': '30'
        }, timeout=15)
        
        # downloadFile (с xmlType=00)
        download_url = f"{root_url}/PS/amiPSPakistan/controller/rawDataReport/downloadFile"
        resp = session.post(download_url, data={
            'dateType': '01', 
            'tariffType': '00', 
            'startDate': start_date, 
            'endDate': end_date,
            'nodeType': 'dw', 
            'nodeId': '105008002', 
            'meterNo': '', 
            'CTPT': '',
            'last': 'last', 
            'start': '0', 
            'limit': '30', 
            'xmlType': '00',  # <--- ИЗМЕНЕНО НА 00
            'dataRange': '01'
        }, timeout=120)
        
        self.stdout.write(f"   Статус: {resp.status_code}")
        self.stdout.write(f"   Content-Type: {resp.headers.get('Content-Type')}")
        self.stdout.write(f"   Размер: {len(resp.content)} байт")
        
        if len(resp.content) == 0 or resp.content.startswith(b'<html'):
            self.stderr.write(self.style.ERROR("❌ Сервер вернул HTML или пустоту."))
            return

        self.stdout.write(self.style.SUCCESS("   ✅ Файл успешно скачан!"))
        
        os.makedirs('/app/exports', exist_ok=True)
        filepath = '/app/exports/test_hxn_raw_00.xml'
        with open(filepath, 'wb') as f:
            f.write(resp.content)
            
        self.stdout.write(f"\n3️⃣ Парсинг XML (сохранен в {filepath})...")
        
        try:
            root = ET.fromstring(resp.content)
            items = root.findall('.//mreadings')
            if not items:
                items = root.findall('.//mval2')
                
            self.stdout.write(f"   Найдено записей: {len(items)}")
            
            if items:
                # Выводим первый элемент, чтобы увидеть структуру
                first_item = ET.tostring(items[0], encoding='unicode').strip()
                self.stdout.write("\n--- ПРИМЕР ПЕРВОЙ ЗАПИСИ ---")
                self.stdout.write(first_item)
                self.stdout.write("----------------------------\n")
                
                # Ищем paramId
                param_id = items[0].findtext('paramId')
                if param_id:
                    self.stdout.write(f"   paramId первой записи: {param_id}")
                    
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ Ошибка парсинга XML: {e}"))