import os
import json
import time
import io
import zipfile
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone as django_timezone
from django.db import connection, transaction
from dotenv import load_dotenv
from meters.models import SyncStatus
from meters.utils import get_robot_devices

# ==========================================
# НАСТРОЙКИ ДЛЯ SANXING_NEW_5A (только не-секретные)
# ==========================================
ROBOT_NAME = 'Sanxing_new_5A'
NOTES = 'Website sync Period1 S34U18'
DIRECTION = 'aplus'
CHANNEL = '0100070100630101FF02'   # Period 1 (для 100A был ...02FF02)
METER_TYPE_ID = 'ff80808194711e25019476072deb0004'  # S34U18 CT
BATCH_SIZE = 1000


class Command(BaseCommand):
    help = 'Sync Sanxing_new_5A (S34U18 CT) via HTTP API'

    def add_arguments(self, parser):
        parser.add_argument('--date', type=str, help='YYYY-MM-DD (по умолчанию вчера)')
        parser.add_argument('--once', action='store_true', help='Один прогон')
        parser.add_argument('--verbose', action='store_true', help='Подробный вывод')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')

        # ==========================================
        # ВСЁ ИЗ .env — БЕЗ ХАРДКОДА
        # ==========================================
        api_url = os.getenv('SANXING_NEW_API_URL')
        self.web_origin = os.getenv('SANXING_NEW_WEB_URL', 'http://192.168.20.252:8088').rstrip('/')
        account = os.getenv('SANXING_NEW_ACCOUNT')
        password = os.getenv('SANXING_NEW_PASSWORD_ENCRYPTED')

        missing = []
        if not api_url:
            missing.append('SANXING_NEW_API_URL')
        if not account:
            missing.append('SANXING_NEW_ACCOUNT')
        if not password:
            missing.append('SANXING_NEW_PASSWORD_ENCRYPTED')

        if missing:
            self.stderr.write(self.style.ERROR(
                f"❌ Не заданы в app/cEnergo.env: {', '.join(missing)}"
            ))
            return

        self.api_url = api_url.rstrip('/')
        self.account = account
        self.password = password
        self.verbose = options.get('verbose', False)

        # ==========================================
        # ДАТА
        # ==========================================
        if options.get('date'):
            try:
                self.sync_date = datetime.strptime(options['date'], '%Y-%m-%d').date()
            except ValueError:
                self.stderr.write(self.style.ERROR("Неверный формат даты. Используйте YYYY-MM-DD"))
                return
        else:
            self.sync_date = datetime.now().date()
        
        self.stdout.write(f"📅 Дата: {self.sync_date}")
        self.stdout.write(f"🤖 Робот: {ROBOT_NAME} (channel: {CHANNEL})")

        try:
            imported = self.sync()
            self._update_sync_status('success', imported, None)
            self.stdout.write(self.style.SUCCESS(f"✅ Импортировано: {imported:,}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ Ошибка: {e}"))
            self._update_sync_status('error', 0, str(e))
            raise

    def sync(self):
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Origin': self.web_origin,
            'Referer': self.web_origin + '/',
            'Accept': 'application/json, text/plain, */*',
        })

        # 1. ЛОГИН
        self.stdout.write("🌐 Логин...")
        login = session.post(f"{self.api_url}/auth/login", json={
            "account": self.account,
            "password": self.password,
            "loginType": "0",
            "emailCode": None,
            "inputFlag": False
        }, timeout=10)

        if login.status_code != 200:
            raise Exception(f"Ошибка логина: {login.text[:100]}")

        token = login.json().get('result', {}).get('token')
        if not token:
            raise Exception("Токен не найден в ответе")

        session.headers.update({'Authorization': token, 'Account': self.account})
        self.stdout.write(self.style.SUCCESS("✅ Логин успешен"))

        # 2. СОЗДАНИЕ ЗАДАЧИ НА ЭКСПОРТ
        self.stdout.write("📤 Создание задачи на экспорт...")
        date_str = self.sync_date.strftime('%Y-%m-%d')

        param_json = json.dumps([{
            "queryEndTime": f"{date_str} 23:59:59",
            "queryStartTime": f"{date_str} 00:00:00",
            "channel": CHANNEL,
            "queryType": "01",
            "orgNo": "1012",
            "meterTypeId": METER_TYPE_ID,
            "sideType": "02",
            "isNoTotal": True,
            "pageInfo": None,
            "page": 1,
            "size": 50
        }])

        column_data_json = json.dumps([
            {"paramId": "8", "label": "Active Energy Import (+A)(kWh)", "prop": "020100030100010800FF02"},
            {"paramId": "1", "label": "Active Energy (|+A|+|-A|)(kWh)", "prop": "0201000301000F0800FF02"}
        ])

        export = session.put(
            f"{self.api_url}/ami/ops/V1/exportAll/saveExportAllData",
            json={
                "columnData": column_data_json,
                "description": "var2",
                "fileNameWeb": f"var2: {datetime.now().strftime('%Y-%m-%d_%H_%M_%S')}",
                "fileType": "xml",
                "param": param_json,
                "url": "ami/collection/V1/profile/mutidataquery"
            },
            headers={'Content-Type': 'application/json;charset=UTF-8'},
            timeout=30
        )

        file_id = export.json().get('result', {}).get('id')
        if not file_id:
            raise Exception(f"Не удалось создать задачу: {export.text[:200]}")

        self.stdout.write(f"✅ Задача создана. ID: {file_id}")

        # 3. ОЖИДАНИЕ ГОТОВНОСТИ
        self.stdout.write("⏳ Ожидание генерации файла...")
        list_url = f"{self.api_url}/ami/ops/V1/exportAll/queryExportAllFileList"

        file_ready = False
        for i in range(180):
            try:
                resp = session.get(list_url, params={'fileName': '', 'size': 10, 'page': 1}, timeout=10)
                files = resp.json().get('result', {}).get('content', [])
                f = next((x for x in files if x.get('id') == file_id), None)

                if f and f.get('status') == '03':
                    self.stdout.write(self.style.SUCCESS(f"✅ Файл готов (за {i*2} сек)"))
                    file_ready = True
                    break

                if i % 10 == 0 and f:
                    pct = f.get('percentage', 0) or 0
                    self.stdout.write(f"   [{i*2}s] Генерация: {pct}%")

                time.sleep(2)
            except Exception as e:
                if self.verbose:
                    self.stderr.write(f"   ⚠️ {e}")
                time.sleep(2)

        if not file_ready:
            raise Exception("Таймаут генерации файла")

        time.sleep(5)

        # 4. ПОЛУЧАЕМ РАЗМЕР И СКАЧИВАЕМ
        self.stdout.write("📥 Скачивание файла...")
        file_size = 0
        for _ in range(10):
            try:
                sz = session.get(f"{self.api_url}/ami/ops/V1/exportAll/getFileSize/{file_id}", timeout=10)
                data = sz.json()
                if data.get('success') and data.get('result', 0) > 0:
                    file_size = data.get('result')
                    break
            except Exception:
                pass
            time.sleep(2)

        if file_size == 0:
            raise Exception("Не удалось получить размер файла")

        self.stdout.write(f"   Размер: {file_size:,} байт")

        dl = session.get(
            f"{self.api_url}/ami/ops/V1/exportAll/fileDownload/{file_id}",
            headers={
                'Accept': '*/*',
                'Accept-Encoding': 'identity',
                'Range': f'bytes=0-{file_size-1}'
            },
            timeout=180
        )

        if not dl.content.startswith(b'PK'):
            raise Exception(f"Получен не ZIP: {dl.content[:100]}")

        self.stdout.write(f"✅ Скачано {len(dl.content):,} байт")

        # 5. ПАРСИНГ И ИМПОРТ
        self.stdout.write("📦 Парсинг и импорт...")
        return self._parse_import(dl.content)

    def _parse_import(self, zip_content):
        devices = get_robot_devices(ROBOT_NAME)
        if not devices:
            self.stdout.write(f"⚠️ Нет устройств {ROBOT_NAME} в БД, пропуск")
            return 0

        by_full = {d.serial_number: d for d in devices}
        by_last8 = {}
        for d in devices:
            if len(d.serial_number) >= 8:
                by_last8[d.serial_number[-8:]] = d

        self.stdout.write(f"   Загружено устройств: {len(by_full):,}")

        sql = """
            INSERT INTO readings (device_id, timestamp, direction, reading_value, notes)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                reading_value = VALUES(reading_value),
                notes = VALUES(notes)
        """

        imported = 0
        stats = {'no_device': 0, 'empty': 0, 'param': 0, 'bad': 0}
        batch = []

        with zipfile.ZipFile(io.BytesIO(zip_content)) as z:
            xml_names = [n for n in z.namelist() if n.endswith('.xml')]
            if not xml_names:
                raise Exception("В ZIP-архиве не найдено XML-файлов")

            self.stdout.write(f"   XML в архиве: {xml_names[0]}")

            with z.open(xml_names[0]) as f:
                for event, elem in ET.iterparse(f, events=('end',)):
                    if elem.tag != 'mreadings':
                        continue

                    meter = elem.find('meterNo')
                    param = elem.find('paramId')
                    value = elem.find('value')
                    ts = elem.find('timestamp')

                    if meter is None or param is None or value is None or ts is None:
                        elem.clear()
                        continue

                    if param.text != '1':
                        stats['param'] += 1
                        elem.clear()
                        continue

                    val_str = (value.text or '').strip()
                    if not val_str:
                        stats['empty'] += 1
                        elem.clear()
                        continue

                    sn = (meter.text or '').strip()
                    if not sn:
                        elem.clear()
                        continue

                    device = by_full.get(sn)
                    if not device and len(sn) >= 8:
                        device = by_last8.get(sn[-8:])
                    if not device and len(sn) < 8:
                        device = by_last8.get(sn.zfill(8))

                    if not device:
                        stats['no_device'] += 1
                        elem.clear()
                        continue

                    try:
                        val = float(val_str)
                        dt_time = datetime.strptime(ts.text.strip(), '%d.%m.%Y %H:%M:%S')
                    except (ValueError, TypeError, AttributeError):
                        stats['bad'] += 1
                        elem.clear()
                        continue

                    batch.append((device.id, dt_time, DIRECTION, val, NOTES))
                    imported += 1

                    if len(batch) >= BATCH_SIZE:
                        with transaction.atomic(), connection.cursor() as cur:
                            cur.executemany(sql, batch)
                        if self.verbose:
                            self.stdout.write(f"   💾 {imported:,}")
                        batch = []

                    elem.clear()

        if batch:
            with transaction.atomic(), connection.cursor() as cur:
                cur.executemany(sql, batch)

        self.stdout.write(
            f"   📊 empty={stats['empty']:,}, param!=1={stats['param']:,}, "
            f"no_device={stats['no_device']:,}, bad={stats['bad']:,}"
        )

        return imported

    def _update_sync_status(self, status, records, error):
        SyncStatus.objects.update_or_create(
            robot_name=ROBOT_NAME,
            defaults={
                'status': status,
                'last_update': django_timezone.now(),
                'records_processed': records,
                'error': error,
            }
        )