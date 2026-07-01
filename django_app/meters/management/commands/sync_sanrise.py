from django.core.management.base import BaseCommand
from django.utils import timezone
from meters.models import Device, Reading, SyncStatus
from meters.utils import find_device  # <-- импорт новой функции
import pyodbc
import os
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync data from SunRise MS SQL to MySQL'

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        server = os.getenv("SANRISE_MSSQL_SERVER")
        user = os.getenv("SANRISE_MSSQL_USER")
        password = os.getenv("SANRISE_MSSQL_PASSWORD")
        db = os.getenv("SANRISE_MSSQL_DB")

        conn_str = f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};UID={user};PWD={password};Port=1433;TDS_Version=7.4;"

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

            device_sns = set(Device.objects.filter(status='active').values_list('serial_number', flat=True))
            if not device_sns:
                self.stdout.write("No devices in MySQL, sync skipped.")
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

            last_reading = Reading.objects.filter(notes="Авто-сбор: SunRise").order_by('-timestamp').first()
            if last_reading:
                last_time = last_reading.timestamp - timedelta(hours=2)
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_IMPORT_ABS
                    FROM DATA_C_DAILY D
                    INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                    WHERE D.DATA_TIME >= ? AND D.KWH_IMPORT_ABS IS NOT NULL
                    ORDER BY D.DATA_TIME ASC
                """
                cursor.execute(query, (last_time,))
            else:
                self.stdout.write("First run: fetching all history...")
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_IMPORT_ABS
                    FROM DATA_C_DAILY D
                    INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                    WHERE D.KWH_IMPORT_ABS IS NOT NULL
                    ORDER BY D.DATA_TIME ASC
                """
                cursor.execute(query)

            count = 0
            for row in cursor:
                db_sn = str(row.SerialNumber).strip()
                if not db_sn:
                    continue

                # Ищем устройство с помощью find_device (по суффиксу)
                device = find_device(db_sn)  # <-- заменяем прямой запрос
                if device is None:
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
                        'notes': 'Авто-сбор: SunRise'
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