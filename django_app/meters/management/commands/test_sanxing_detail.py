from django.core.management.base import BaseCommand
from django.utils import timezone
from meters.models import Device, Reading, SyncStatus
import pyodbc
import os
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync data from Sanxing MS SQL to MySQL'

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        server = os.getenv("SANXING_SERVER")
        user = os.getenv("SANXING_USER")
        password = os.getenv("SANXING_PASSWORD")
        db = os.getenv("SANXING_DB")

        conn_str = f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};UID={user};PWD={password};Port=1433;TDS_Version=7.4;"

        while True:
            try:
                self.sync(conn_str)
            except Exception as e:
                logger.error(f"Sanxing sync error: {e}")
                SyncStatus.objects.update_or_create(
                    robot_name='Sanxing',
                    defaults={
                        'status': 'error',
                        'last_update': timezone.now(),
                        'error': str(e)
                    }
                )
            time.sleep(600)

    def sync(self, conn_str):
        self.stdout.write("🔄 Starting Sanxing sync...")
        try:
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()
            self.stdout.write("Connected to Sanxing MS SQL")

            devices_map = {d.serial_number: d for d in Device.objects.filter(status='active')}
            if not devices_map:
                self.stdout.write("No devices in MySQL, sync skipped.")
                SyncStatus.objects.update_or_create(
                    robot_name='Sanxing',
                    defaults={
                        'status': 'idle',
                        'last_update': timezone.now(),
                        'records_processed': 0,
                        'error': None
                    }
                )
                return

            last_reading = Reading.objects.filter(notes="Авто-сбор: Sanxing_old").order_by('-timestamp').first()
            if last_reading:
                last_time = last_reading.timestamp - timedelta(hours=2)
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_ABS
                    FROM DATA_C_ELEC D
                    INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                    WHERE D.DATA_TIME >= ? AND D.TARIFF_ID = 0 AND D.KWH_ABS IS NOT NULL
                    ORDER BY D.DATA_TIME ASC
                """
                cursor.execute(query, (last_time,))
            else:
                self.stdout.write("First run: fetching all history...")
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_ABS
                    FROM DATA_C_ELEC D
                    INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                    WHERE D.TARIFF_ID = 0 AND D.KWH_ABS IS NOT NULL
                    ORDER BY D.DATA_TIME ASC
                """
                cursor.execute(query)

            count = 0
            for row in cursor:
                sn = row.SerialNumber.strip()
                if sn not in devices_map:
                    continue
                dt = row.DATA_TIME
                if isinstance(dt, str):
                    dt = datetime.strptime(dt.split('.')[0], "%Y-%m-%d %H:%M:%S")
                raw_val = float(row.KWH_ABS)  # явное преобразование в float
                device = devices_map[sn]
                nominal_current = device.nominal_current
                if nominal_current and "100" in nominal_current:
                    val = raw_val / 100.0
                else:
                    val = raw_val / 1000.0
                Reading.objects.update_or_create(
                    device=device,
                    timestamp=dt,
                    defaults={
                        'reading_value': val,
                        'notes': 'Авто-сбор: Sanxing_old'
                    }
                )
                count += 1
                if count % 1000 == 0:
                    self.stdout.write(f"Processed {count} readings")

            self.stdout.write(f"✅ Sanxing sync done. Total: {count}")
            SyncStatus.objects.update_or_create(
                robot_name='Sanxing',
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
                robot_name='Sanxing',
                defaults={
                    'status': 'error',
                    'last_update': timezone.now(),
                    'error': str(e)
                }
            )
            raise