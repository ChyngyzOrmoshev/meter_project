import os
import time
import requests
import urllib3
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction, connection
from django.utils import timezone
from dotenv import load_dotenv
from meters.models import Device, SyncStatus

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

NOTES = 'Авто-сбор: Kaifa'
DIRECTION = 'aplus'

# Сервер Kaifa на pageSize=5000 отвечает HTTP 200, но в теле code=500 (data пустой); 1000 работает (~0.4 сек на страницу).
PAGE_SIZE = 1000
CONNECT_TIMEOUT = 15      # сервер временами не завершает SSL-рукопожатие; без таймаута робот зависал бы насовсем
READ_TIMEOUT = 60
RETRIES = 3               # повторы на сетевую ошибку или code != 200 в теле, пауза растёт: 5, 10 сек
TOTAL_DEADLINE_SEC = 15 * 60   # общий потолок работы, чтобы не держать планировщик

class Command(BaseCommand):
    help = 'Синхронизация данных с сервера Kaifa (MDM)'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--verbose', action='store_true')
        parser.add_argument('--days', type=int, default=2)

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        self.verbose = options.get('verbose', False)
        self.days = options.get('days', 2)

        base_url = os.getenv('KAIFA_URL', 'https://192.168.80.254:30101')
        username = os.getenv('KAIFA_USERNAME')
        password = os.getenv('KAIFA_PASSWORD')
        region_id = os.getenv('KAIFA_REGION_ID', '0/58085/3190/')

        if not username or not password:
            self.stderr.write(self.style.ERROR('Не заданы KAIFA_USERNAME / KAIFA_PASSWORD в .env'))
            self._update_status('error', 0, 'Нет креденшелов')
            return

        self.stdout.write(f'🔄 Kaifa Sync (LAST): {self.days} дн., регион {region_id}')

        session = requests.Session()
        session.verify = False
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Token-Source': 'WEB'
        })

        # 1. Авторизация
        if not self._login(session, base_url, username, password):
            self._update_status('error', 0, 'Ошибка авторизации')
            return

        # 2. Подготовка дат
        now = timezone.now()
        start = now - timedelta(days=self.days)
        
        # Формат для API: DD/MM/YYYY HH:MM:SS
        start_str = start.strftime('%d/%m/%Y 00:00:00')
        end_str = now.strftime('%d/%m/%Y 23:59:59')

        # 3. Загрузка данных
        try:
            saved, errors = self._fetch_and_save(session, base_url, region_id, start_str, end_str)
            status = 'error' if errors > 0 else 'success'
            err_msg = 'Выгрузка прервана: ошибка или таймаут сервера Kaifa, см. лог' if errors else None
            self._update_status(status, saved, err_msg)
            self.stdout.write(self.style.SUCCESS(f'✅ Итого сохранено: {saved} записей'))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'❌ Ошибка: {e}'))
            self._update_status('error', 0, str(e))

    def _request(self, session, method, url, **kwargs):
        """Запрос с таймаутами и повторами. Возвращает JSON; бросает исключение, если после повторов нет успеха."""
        last = None
        for attempt in range(1, RETRIES + 1):
            try:
                resp = session.request(method, url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), **kwargs)
                if resp.status_code != 200:
                    raise Exception(f'HTTP {resp.status_code}: {resp.text[:200]}')
                body = resp.json()
                # сервер сообщает об ошибке в теле при HTTP 200
                if isinstance(body, dict) and body.get('code') not in (None, 200, '200'):
                    raise Exception(f"code {body.get('code')}: {body.get('desc')}")
                return body
            except Exception as e:
                last = e
                if attempt < RETRIES:
                    time.sleep(5 * attempt)
        raise Exception(f'{last} (попыток: {RETRIES})')

    def _login(self, session, base_url, username, password):
        self.stdout.write("🔐 Авторизация...")
        try:
            session.get(f"{base_url}/api/gateway-service/verify/code/login/verify?username={username}",
                        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            body = self._request(session, 'POST', f"{base_url}/api/gateway-service/tokens.json", json={
                "language": "en", "username": username, "password": password
            })
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Авторизация: {e}'))
            return False
        token = (body.get('data') or {}).get('access_token')
        if token:
            session.cookies.set('Access-Token', token)
            return True
        return False

    def _fetch_and_save(self, session, base_url, region_id, start_time, end_time):
        query_url = f"{base_url}/api/mdm-service/measure/row-data-report/query"
        
        page = 1
        page_size = PAGE_SIZE
        total_pages = 1
        
        our_devices = {
            str(d.serial_number).strip(): d 
            for d in Device.objects.filter(status='active', askue_id='30').only('id', 'serial_number')
        }
        self.stdout.write(f"  📚 Загружено Kaifa-приборов из БД: {len(our_devices)}")
        
        saved_total = 0
        errors = 0
        started = time.time()
        skipped_nodes = 0
        skipped_empty = 0

        while page <= total_pages:
            payload = {
                "pageNo": page,
                "pageSize": page_size,
                "fullRegionId": region_id,
                "profileType": "DAILY",
                "startTime": start_time,
                "endTime": end_time,
                "dataItems": ["PTCAP"],
                "decimalsMode": "DEFAULT",
                "displayUnit": "KWH",
                "recordsMode": "LAST",  # <--- ИЗМЕНЕНО НА LAST
                "rangeType": "CUSTOM",
                "advancedSearch": {"match": "ALL", "conditions": []}
            }

            if time.time() - started > TOTAL_DEADLINE_SEC:
                errors += 1
                self.stderr.write(self.style.ERROR(f'⏱ Превышено {TOTAL_DEADLINE_SEC // 60} мин, остановлено на стр. {page}/{total_pages}'))
                break

            try:
                body = self._request(session, 'POST', query_url, json=payload)
            except Exception as e:
                # частично сохранённое остаётся, ошибка отражается в статусе
                errors += 1
                self.stderr.write(self.style.ERROR(f'Стр. {page}/{total_pages}: {e}'))
                break

            data = (body.get('data') or {}).get('records') or {}
            if page == 1:
                total_pages = data.get('totalPage', 1)
                total_rows = data.get('totalRow', 0)
                self.stdout.write(f"  📥 Всего строк на сервере (LAST): {total_rows} ({total_pages} стр. по {page_size})")

            rows = data.get('pageData', [])
            if not rows:
                break

            readings = []
            
            for row in rows:
                device_no = row.get('deviceNo')
                freeze_time = row.get('freezeTime')
                
                if not device_no or not freeze_time:
                    continue
                
                dev = our_devices.get(str(device_no).strip())
                if not dev:
                    skipped_nodes += 1
                    continue

                val = None
                for item in row.get('itemValues', []):
                    if item.get('magnitudeCode') == 'PTCAP' and not item.get('missing'):
                        val = item.get('rawValue')
                        break
                
                if val is None:
                    skipped_empty += 1
                    continue

                try:
                    dt = datetime.strptime(freeze_time, '%d/%m/%Y %H:%M:%S')
                except ValueError:
                    skipped_empty += 1
                    continue

                readings.append((dev.id, dt, DIRECTION, val, NOTES))

            if readings:
                sql = """
                    INSERT INTO readings (device_id, timestamp, direction, reading_value, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        reading_value = VALUES(reading_value),
                        notes = VALUES(notes)
                """
                with transaction.atomic(), connection.cursor() as cur:
                    cur.executemany(sql, readings)
                saved_total += len(readings)

            if self.verbose:
                self.stdout.write(f'    Стр. {page}/{total_pages}: сохранено {len(readings)}')

            page += 1

        if self.verbose:
            self.stdout.write(f"  ℹ️ Пропущено (нет в БД с askue_id=30): {skipped_nodes}")
            self.stdout.write(f"  ℹ️ Пропущено (нет PTCAP/missing): {skipped_empty}")

        return saved_total, errors

    def _update_status(self, status, records, error):
        SyncStatus.objects.filter(robot_name='Kaifa').delete()
        SyncStatus.objects.update_or_create(
            robot_name='Kaifa',
            defaults={
                'status': status,
                'last_update': timezone.now(),
                'records_processed': records,
                'error': error
            }
        )