import os
import re
from datetime import datetime, timedelta

import requests
import xml.etree.ElementTree as ET
from django.core.management.base import BaseCommand
from django.db import transaction, connection, close_old_connections
from django.utils import timezone
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from meters.models import Device, SyncStatus

NOTES = 'Hexing Raw sync'
DIRECTION = 'aplus'
BATCH_SIZE = 1000

class Command(BaseCommand):
    help = 'Синхронизация Hexing Mini-MDM (Playwright для логина + Requests для данных)'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Один запуск и выход')
        parser.add_argument('--verbose', action='store_true')
        parser.add_argument('--days', type=int, default=2, help='Глубина истории (дней)')
        parser.add_argument('--nodes', type=str, default=None, help='nodeId через запятую (иначе env)')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        self.verbose = options.get('verbose', False)
        self.days = options.get('days', 2)

        username = os.getenv('HEXING_RAW_USERNAME')
        password = os.getenv('HEXING_RAW_PASSWORD')
        raw_url = os.getenv('HEXING_RAW_URL', 'http://192.168.20.247:8080/Mini-MDM/common/view/init')
        root_url = raw_url.split('/Mini-MDM')[0] + '/Mini-MDM' if '/Mini-MDM' in raw_url else raw_url.rstrip('/')

        nodes_str = options.get('nodes') or os.getenv('HEXING_RAW_NODES', '')
        nodes = [n.strip() for n in nodes_str.split(',') if n.strip()] or ['105008002']

        self.stdout.write(f'🔄 Hexing Raw: {len(nodes)} узлов, {self.days} дн.')

        auth = self._login(root_url, username, password)
        if not auth:
            self._update_status('error', 0, 'Не удалось авторизоваться')
            return

        cookies, user_agent = auth
        saved_total, errors = 0, 0

        for node in nodes:
            close_old_connections()
            try:
                n = self._process_node(root_url, cookies, user_agent, node)
                saved_total += n
                if self.verbose:
                    self.stdout.write(f'  ✅ node={node}: {n} записей')
            except Exception as e:
                errors += 1
                self.stderr.write(f'  ❌ node={node}: {e}')

        self._update_status('error' if errors else 'success', saved_total,
                            f'Ошибок: {errors}' if errors else None)
        self.stdout.write(self.style.SUCCESS(f'✅ Итого: {saved_total} записей'))

    # ---------- Авторизация (Playwright) ----------
    def _login(self, root_url, username, password):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context()
                page = ctx.new_page()
                page.on("dialog", lambda d: d.accept())

                page.goto(f"{root_url}/common/login/init", timeout=30000)
                page.wait_for_selector('input#czyId', timeout=15000)
                page.fill('input#czyId', username)
                page.fill('input#pwd', password)
                page.click('.ui-button-login')
                page.wait_for_timeout(3000)

                try:
                    ok = page.locator('button:has-text("OK")').first
                    if ok.is_visible(timeout=3000):
                        ok.click()
                        page.wait_for_timeout(3000)
                except PlaywrightTimeout:
                    pass

                page.goto(
                    f"{root_url}/PS/amiPSPakistan/controller/rawDataReport/init"
                    f"?menuid=AmiPS_RawDataReport&menupath=Raw Data Report&curTabId=AmiPS_RawDataReport",
                    timeout=30000,
                )
                page.wait_for_load_state('networkidle', timeout=30000)
                page.wait_for_timeout(2000)

                page.evaluate("""
                    () => new Promise(res => {
                        if (!window.dwr) return res(false);
                        dwr.engine._execute(dwr.engine._pathToDwrServlet, '__System', 'generateId',
                            [{callback: () => res(true), errorHandler: () => res(false)}]);
                    })
                """)
                page.wait_for_timeout(2000)

                cookies = {c['name']: c['value'] for c in ctx.cookies()}
                ua = page.evaluate("() => navigator.userAgent")
                browser.close()

                if 'DWRSESSIONID' not in cookies:
                    self.stderr.write('❌ DWRSESSIONID не получен')
                    return None
                return cookies, ua
        except Exception as e:
            self.stderr.write(f'❌ Playwright: {e}')
            return None

    # ---------- Скачивание (Requests) ----------
    def _process_node(self, root_url, cookies, user_agent, node_id):
        s = requests.Session()
        s.cookies.update(cookies)
        s.headers.update({
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Encoding': 'identity',
        })

        end_dt = timezone.now()
        start_dt = end_dt - timedelta(days=self.days)
        
        start_s = start_dt.strftime('%Y-%m-%d')
        end_s = end_dt.strftime('%Y-%m-%d')

        s.get(f"{root_url}/PS/amiPSPakistan/controller/rawDataReport/initDownload", params={
            'dateType': '01', 'tariffType': '00',
            'startDate': start_s, 'endDate': end_s,
            'nodeType': 'dw', 'nodeId': node_id,
            'meterNo': '', 'CTPT': '', 'last': 'last', 'start': '0', 'limit': '30'
        }, timeout=30)

        resp = s.post(f"{root_url}/PS/amiPSPakistan/controller/rawDataReport/downloadFile", data={
            'dateType': '01', 'tariffType': '00',
            'startDate': start_s, 'endDate': end_s,
            'nodeType': 'dw', 'nodeId': node_id,
            'meterNo': '', 'CTPT': '', 'last': 'last', 'start': '0', 'limit': '30',
            'xmlType': '00', 'dataRange': '01'
        }, timeout=180)

        if not resp.content or resp.content.startswith(b'<html'):
            raise Exception(f'HTML/пусто ({len(resp.content)} байт)')

        return self._parse_and_import(resp.content)

    # ---------- Парсинг + импорт ----------
    def _parse_and_import(self, xml_bytes):
        exact = {}
        suffix = {}
        ambiguous = set()

        # Строим индекс: точные совпадения и суффиксы от 8 до длины серийника
        for d in Device.objects.filter(status='active').only('id', 'serial_number'):
            sn = str(d.serial_number).strip()
            exact[sn] = d
            for i in range(8, len(sn) + 1):
                suf = sn[-i:]
                if suf in suffix and suffix[suf].id != d.id:
                    ambiguous.add(suf)
                else:
                    suffix[suf] = d

        for suf in ambiguous:
            suffix.pop(suf, None)

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            raise Exception(f'XML parse error: {e}')

        readings = []
        skip_nodev, skip_empty = 0, 0
        matched_samples = []

        for it in root.findall('.//mreadings'):
            meter_no = it.findtext('meterNo')
            param_id = it.findtext('paramId')
            value = it.findtext('value')
            ts = it.findtext('timestamp')

            if not meter_no or not value or not ts:
                skip_empty += 1
                continue

            if param_id != '1':
                continue

            meter_no = meter_no.strip()

            # 1. Точное совпадение
            dev = exact.get(meter_no)
            
            # 2. Без ведущих нулей
            if not dev:
                stripped = meter_no.lstrip('0')
                dev = exact.get(stripped)
                
                # 3. Поиск по суффиксу (например, '41013041' -> '083141013041')
                if not dev:
                    dev = suffix.get(stripped)
                    
                # 4. Последние 8 цифр
                if not dev and len(stripped) >= 8:
                    dev = suffix.get(stripped[-8:])
                    
                # 5. Оригинальный суффикс
                if not dev:
                    dev = suffix.get(meter_no)

            if not dev:
                skip_nodev += 1
                continue

            try:
                v = float(value.replace(',', '.'))
            except (ValueError, TypeError):
                skip_empty += 1
                continue

            # Формат даты: DD.MM.YYYY HH:MM:SS
            ts_raw = ts.strip()
            dt = None
            for fmt in ('%d.%m.%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
                try:
                    dt = datetime.strptime(ts_raw, fmt)
                    break
                except ValueError:
                    continue

            if dt is None:
                # Попытка с микросекундами: 2026-11-16 00:00:00.000
                try:
                    dt = datetime.strptime(ts_raw, '%Y-%m-%d %H:%M:%S.%f')
                except ValueError:
                    if self.verbose and skip_empty < 3:  # покажем первые 3 для отладки
                        self.stderr.write(f'  ⚠️ Не удалось распарсить дату: "{ts_raw}"')
                    skip_empty += 1
                    continue

            readings.append((dev.id, dt, DIRECTION, v, NOTES))
            
            if len(matched_samples) < 5:
                matched_samples.append((meter_no, dev.serial_number, v, ts))

        if not readings:
            if self.verbose:
                self.stdout.write(f'  ℹ️ Нет новых данных (пропущено: {skip_nodev} не найдено, {skip_empty} пустых)')
            return 0

        # Bulk Upsert
        sql = """
            INSERT INTO readings (device_id, timestamp, direction, reading_value, notes)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                reading_value = VALUES(reading_value),
                notes = VALUES(notes)
        """
        
        saved = 0
        for i in range(0, len(readings), BATCH_SIZE):
            chunk = readings[i:i + BATCH_SIZE]
            with transaction.atomic(), connection.cursor() as cur:
                cur.executemany(sql, chunk)
            saved += len(chunk)

        if self.verbose:
            self.stdout.write(f'  💾 {saved} сохр., не найдено: {skip_nodev}, пустых: {skip_empty}')
            if matched_samples:
                self.stdout.write("  🔍 Примеры сопоставления:")
                for xml_sn, db_sn, val, ts in matched_samples:
                    self.stdout.write(f"    XML: {xml_sn} -> DB: {db_sn} | {val} кВтч | {ts}")
        
        return saved

    # ---------- Обновление статуса ----------
    def _update_status(self, status, records, error):
        SyncStatus.objects.update_or_create(
            robot_name='Hexing Raw sync',
            defaults={
                'status': status,
                'last_update': timezone.now(),
                'records_processed': records,
                'error': error
            }
        )