import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone as django_timezone
from django.db import connection, transaction
from dotenv import load_dotenv

from meters.models import SyncStatus
from meters.utils import get_robot_devices

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Sync data from Star Power (HES) via HTTP requests'

    def add_arguments(self, parser):
        parser.add_argument('--date', type=str, help='Дата в формате YYYY-MM-DD (по умолчанию вчера)')
        parser.add_argument('--once', action='store_true', help='Один прогон и выход')
        parser.add_argument('--verbose', action='store_true', help='Подробный вывод')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')

        username = os.getenv('STAR_USERNAME')
        password = os.getenv('STAR_PASSWORD')
        raw_url = os.getenv('STAR_URL', 'http://192.168.20.246:59101/hes/login')

        if not username or not password:
            self.stderr.write(self.style.ERROR("❌ Не заданы STAR_USERNAME / STAR_PASSWORD"))
            return

        self.root_url = raw_url[:-6] if raw_url.endswith('/login') else raw_url.rstrip('/')
        self.verbose = options.get('verbose', False)

        # Если дата задана вручную — берем только её
        if options.get('date'):
            try:
                dates_to_try = [datetime.strptime(options['date'], '%Y-%m-%d').date()]
            except ValueError:
                self.stderr.write(self.style.ERROR("Неверный формат даты. Используйте YYYY-MM-DD"))
                return
        else:
            # Если нет — пробуем СЕГОДНЯ и ВЧЕРА
            today = datetime.now().date()
            yesterday = today - timedelta(days=1)
            dates_to_try = [today, yesterday]

        self.stdout.write(f"📅 Даты для проверки: {', '.join(d.strftime('%Y-%m-%d') for d in dates_to_try)}")

        imported = 0
        successful_date = None
        last_error = None

        for try_date in dates_to_try:
            self.stdout.write(f"\n{'='*50}")
            self.stdout.write(f"🗓️ Попытка за {try_date}")
            self.stdout.write(f"{'='*50}")
            try:
                imported = self.sync(try_date, username, password)
                successful_date = try_date
                break
            except Exception as e:
                last_error = e
                self.stderr.write(self.style.WARNING(f"⚠️ {try_date}: {e}"))
                continue

        if successful_date is None:
            self.stderr.write(self.style.ERROR(f"❌ Все даты провалились. Последняя ошибка: {last_error}"))
            self._update_sync_status('error', 0, str(last_error))
            return

        self.stdout.write(self.style.SUCCESS(
            f"✅ Успех за {successful_date}. Импортировано: {imported:,}"
        ))
        self._update_sync_status('success', imported, None)

    def sync(self, sync_date, username, password):
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        })

        # 1. Логин
        self.stdout.write("🌐 Авторизация...")
        session.get(f"{self.root_url}/login", timeout=10)
        resp_post = session.post(
            f"{self.root_url}/dologin", 
            data={'username': username, 'password': password}, 
            timeout=10,
            allow_redirects=True 
        )
        if 'login' in resp_post.url.lower():
            raise Exception("Логин не удался")

        # 2. Скачивание
        self.stdout.write(f"📥 Запрос XML за {sync_date}...")
        target_date = sync_date.strftime('%Y-%m-%d')
        
        export_url = f"{self.root_url}/hDatDayController/exportXmlFile2"
        
        # ТОЧНЫЕ ПАРАМЕТРЫ ИЗ ВАШЕГО УСПЕШНОГО ТЕСТА
        params = {
            'meterNo': '',
            'deptId': '',
            'deptNo': '',
            'regNos': '11010000',
            'start': f"{target_date} 00:00:00",
            'end': f"{target_date} 23:59:59",
            'transformerId': '',  # <--- ИСПРАВЛЕНО (было transformId)
            'gridNo': '',
            'regionId': '',
            'regionNo': '',
            'groupId': '',
            'bLast': '1',
            'custNo': '',
            'dataType': '1'
        }

        export_headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f"{self.root_url}/hDatDayController/rowDataList"
        }

        resp_dl = session.get(export_url, params=params, headers=export_headers, timeout=180, stream=True)
        
        if resp_dl.status_code == 500:
            raise Exception(f"HTTP 500: нет данных за {sync_date} (сервер вернул ошибку)")
        if resp_dl.status_code != 200:
            raise Exception(f"HTTP {resp_dl.status_code}: ошибка скачивания")

        # 3. Парсинг и импорт
        self.stdout.write("📦 Парсинг XML и импорт в БД...")
        imported = self._import_xml_stream(resp_dl.raw)
        return imported

    def _import_xml_stream(self, response_stream):
        devices = get_robot_devices('Star')
        if not devices:
            self.stdout.write("⚠️ Нет устройств Star в БД")
            return 0

        devices_by_full = {d.serial_number: d for d in devices}
        devices_by_last8 = {}
        for d in devices:
            if len(d.serial_number) >= 8:
                devices_by_last8.setdefault(d.serial_number[-8:], d)

        sql = """
            INSERT INTO readings (device_id, timestamp, direction, reading_value, notes)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                reading_value = VALUES(reading_value),
                notes = VALUES(notes)
        """

        imported = 0
        batch = []
        BATCH_SIZE = 1000

        for event, elem in ET.iterparse(response_stream, events=('end',)):
            if elem.tag == 'mreadings':
                param_id_elem = elem.find('paramId')
                
                # Фильтруем только Total kWh (paramId=1)
                if param_id_elem is not None and param_id_elem.text == '1':
                    meter_no_elem = elem.find('meterNo')
                    value_elem = elem.find('value')
                    timestamp_elem = elem.find('timestamp')
                    
                    if meter_no_elem is not None and value_elem is not None and timestamp_elem is not None:
                        meter_no = meter_no_elem.text.strip()
                        val_str = value_elem.text
                        ts_str = timestamp_elem.text
                        
                        device = devices_by_full.get(meter_no)
                        if not device and len(meter_no) >= 8:
                            device = devices_by_last8.get(meter_no[-8:])
                        if not device and len(meter_no) < 8:
                            device = devices_by_last8.get(meter_no.zfill(8))
                        
                        if device:
                            try:
                                val = float(val_str)
                                dt_time = datetime.strptime(ts_str, '%Y-%m-%d %H:%M')
                                batch.append((device.id, dt_time, 'aplus', val, 'Star sync'))
                                imported += 1
                                
                                if len(batch) >= BATCH_SIZE:
                                    with transaction.atomic(), connection.cursor() as cur:
                                        cur.executemany(sql, batch)
                                    batch = []
                                    if self.verbose:
                                        self.stdout.write(f"   💾 {imported:,}")
                            except (ValueError, TypeError):
                                pass
                elem.clear()

        # Хвост
        if batch:
            with transaction.atomic(), connection.cursor() as cur:
                cur.executemany(sql, batch)

        return imported

    def _update_sync_status(self, status, records_processed, error):
        SyncStatus.objects.update_or_create(
            robot_name='Star',
            defaults={
                'status': status,
                'last_update': django_timezone.now(),
                'records_processed': records_processed,
                'error': error,
            }
        )