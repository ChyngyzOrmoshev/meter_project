from django.core.management.base import BaseCommand
from django.utils import timezone
from meters.models import Device, Reading, SyncStatus
import pyodbc
import os
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync data from cEnergo MS SQL to MySQL'

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        server = os.getenv("DB_MSSQL_SERVER")
        user = os.getenv("DB_MSSQL_USER")
        password = os.getenv("DB_MSSQL_PASSWORD")
        db = os.getenv("DB_MSSQL_NAME")

        conn_str = f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};UID={user};PWD={password};Port=1433;TDS_Version=7.4;"

        while True:
            try:
                self.sync(conn_str)
            except Exception as e:
                logger.error(f"cEnergo sync error: {e}")
                SyncStatus.objects.update_or_create(
                    robot_name='cEnergo',
                    defaults={
                        'status': 'error',
                        'last_update': timezone.now(),
                        'error': str(e)
                    }
                )
            time.sleep(600)

    def sync(self, conn_str):
        self.stdout.write("🔄 Starting cEnergo sync...")
        try:
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()
            self.stdout.write("Connected to cEnergo MS SQL")

            device_sns = set(Device.objects.filter(status='active').values_list('serial_number', flat=True))
            if not device_sns:
                self.stdout.write("No devices in MySQL, sync skipped.")
                SyncStatus.objects.update_or_create(
                    robot_name='cEnergo',
                    defaults={
                        'status': 'idle',
                        'last_update': timezone.now(),
                        'records_processed': 0,
                        'error': None
                    }
                )
                return

            last_reading = Reading.objects.filter(notes="Авто-сбор: База cEnergo").order_by('-timestamp').first()
            if last_reading:
                last_time = last_reading.timestamp - timedelta(hours=1)
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.SerialNumber)) as SerialNumber, V.DT, V.Val
                    FROM [Values] V
                    INNER JOIN Meters M ON V.MeterId = M.MeterId
                    WHERE V.DT >= ? AND V.PropertyId = 12
                    ORDER BY V.DT ASC
                """
                cursor.execute(query, (last_time,))
            else:
                self.stdout.write("First run: fetching all history...")
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.SerialNumber)) as SerialNumber, V.DT, V.Val
                    FROM [Values] V
                    INNER JOIN Meters M ON V.MeterId = M.MeterId
                    WHERE V.PropertyId = 12
                    ORDER BY V.DT ASC
                """
                cursor.execute(query)

            count = 0
            for row in cursor:
                sn = row.SerialNumber.strip()
                if sn not in device_sns:
                    continue
                dt = row.DT
                if isinstance(dt, str):
                    dt = datetime.strptime(dt.split('.')[0], "%Y-%m-%d %H:%M:%S")
                val = row.Val
                device = Device.objects.get(serial_number=sn)
                Reading.objects.update_or_create(
                    device=device,
                    timestamp=dt,
                    defaults={
                        'reading_value': val,
                        'notes': 'Авто-сбор: База cEnergo'
                    }
                )
                count += 1
                if count % 1000 == 0:
                    self.stdout.write(f"Processed {count} readings")

            self.stdout.write(f"✅ cEnergo sync done. Total: {count}")
            SyncStatus.objects.update_or_create(
                robot_name='cEnergo',
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
                robot_name='cEnergo',
                defaults={
                    'status': 'error',
                    'last_update': timezone.now(),
                    'error': str(e)
                }
            )
            raise