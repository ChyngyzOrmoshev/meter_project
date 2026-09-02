from django.core.management.base import BaseCommand
from django.utils import timezone
from meters.models import Device, Reading, SyncStatus
from meters.utils import find_device, get_robot_devices
import pyodbc
import os
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync data from SunRise MS SQL to MySQL'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Количество дней истории при первом запуске (по умолчанию 30)',
        )

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        server = os.getenv("SANRISE_MSSQL_SERVER")
        user = os.getenv("SANRISE_MSSQL_USER")
        password = os.getenv("SANRISE_MSSQL_PASSWORD")
        db = os.getenv("SANRISE_MSSQL_DB")

        conn_str = f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};UID={user};PWD={password};Port=1433;TDS_Version=7.4;"
        self.days = options.get('days', 30)

        while True:
            try:
                self.sync(conn_str)
            except Exception as e:
                logger.error(f"SunRise sync error: {e}")
                SyncStatus.objects.update_or_create(
                    robot_name='SunRise',
                    defaults={
                        'status': 'error',
                        'last_update': timezone.now(),
                        'error': str(e)
                    }
                )
            time.sleep(600)

    def sync(self, conn_str):
        self.stdout.write("🔄 Starting SunRise sync...")
        try:
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()
            self.stdout.write("Connected to SunRise MS SQL")

            # Получаем устройства SunRise через централизованную функцию
            devices = get_robot_devices('SunRise')
            device_sns = set(devices.values_list('serial_number', flat=True))
            if not device_sns:
                self.stdout.write("No SunRise devices found in MySQL.")
                SyncStatus.objects.update_or_create(
                    robot_name='SunRise',
                    defaults={
                        'status': 'idle',
                        'last_update': timezone.now(),
                        'records_processed': 0,
                        'error': None
                    }
                )
                return

            # Определяем дату начала загрузки
            last_reading = Reading.objects.filter(notes="Авто-сбор: SunRise").order_by('-timestamp').first()
            if last_reading:
                start_time = last_reading.timestamp - timedelta(hours=2)
                self.stdout.write(f"🔄 Incremental sync from {start_time}")
            else:
                start_time = datetime.now() - timedelta(days=self.days)
                self.stdout.write(f"🔄 First run: fetching last {self.days} days (from {start_time})")

            query = """
                SET NOCOUNT ON;
                SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_IMPORT_ABS
                FROM DATA_C_DAILY D
                INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                WHERE D.DATA_TIME >= ? AND D.KWH_IMPORT_ABS IS NOT NULL
                ORDER BY D.DATA_TIME ASC
            """
            cursor.execute(query, (start_time,))

            count = 0
            for row in cursor:
                db_sn = str(row.SerialNumber).strip()
                if not db_sn:
                    continue

                device = find_device(db_sn)
                if device is None:
                    continue

                # *** ВАЖНО: проверяем, что устройство относится к SunRise ***
                if device.serial_number not in device_sns:
                    continue

                dt = row.DATA_TIME
                if isinstance(dt, str):
                    dt = datetime.strptime(dt.split('.')[0], "%Y-%m-%d %H:%M:%S")
                val = row.KWH_IMPORT_ABS

                Reading.objects.update_or_create(
                    device=device,
                    timestamp=dt,
                    defaults={
                        'reading_value': val,
                        'notes': 'Авто-сбор: SunRise',
                        'direction': 'aplus'
                    }
                )
                count += 1
                if count % 1000 == 0:
                    self.stdout.write(f"Processed {count} readings")

            self.stdout.write(f"✅ SunRise sync done. Total: {count}")
            SyncStatus.objects.update_or_create(
                robot_name='SunRise',
                defaults={
                    'status': 'success',
                    'last_update': timezone.now(),
                    'records_processed': count,
                    'error': None
                }
            )

        except Exception as e:
            self.stdout.write(f"❌ Error: {e}")
            SyncStatus.objects.update_or_create(
                robot_name='SunRise',
                defaults={
                    'status': 'error',
                    'last_update': timezone.now(),
                    'error': str(e)
                }
            )
            raise