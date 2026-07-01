from django.core.management.base import BaseCommand
from django.utils import timezone
from meters.models import Device, Reading, SyncStatus
import mysql.connector
import os
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync data from Hexing KUK MySQL to MySQL'

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        host = os.getenv("HEXING_KUK_MYSQL_HOST")
        port = int(os.getenv("HEXING_KUK_MYSQL_PORT", 3306))
        user = os.getenv("HEXING_KUK_MYSQL_USER")
        password = os.getenv("HEXING_KUK_MYSQL_PASSWORD")
        db = os.getenv("HEXING_KUK_MYSQL_DB")

        while True:
            try:
                self.sync(host, port, user, password, db)
            except Exception as e:
                logger.error(f"Hexing KUK sync error: {e}")
                SyncStatus.objects.update_or_create(
                    robot_name='Hexing KUK',
                    defaults={
                        'status': 'error',
                        'last_update': timezone.now(),
                        'error': str(e)
                    }
                )
            time.sleep(600)

    def sync(self, host, port, user, password, db):
        self.stdout.write("🔄 Starting Hexing KUK sync...")
        try:
            conn = mysql.connector.connect(
                host=host, port=port, user=user, password=password, database=db,
                connect_timeout=10
            )
            cursor = conn.cursor()
            self.stdout.write("Connected to Hexing KUK MySQL")

            device_sns = set(Device.objects.filter(status='active').values_list('serial_number', flat=True))
            if not device_sns:
                self.stdout.write("No devices in MySQL, sync skipped.")
                SyncStatus.objects.update_or_create(
                    robot_name='Hexing KUK',
                    defaults={
                        'status': 'idle',
                        'last_update': timezone.now(),
                        'records_processed': 0,
                        'error': None
                    }
                )
                return

            last_reading = Reading.objects.filter(notes="Hexing KUK").order_by('-timestamp').first()
            if last_reading:
                last_time = last_reading.timestamp - timedelta(hours=1)
                query = """
                    SELECT 
                        m.METER_NO AS serial_number,
                        t.TV AS timestamp,
                        t.CA AS reading_value
                    FROM biz_pub_data_t_energy_d t
                    JOIN a_data_catalogue c ON t.DATA_ID = c.DATA_ID
                    JOIN a_equip_meter m ON c.METER_ID = m.METER_ID
                    WHERE t.TV >= %s
                      AND t.CA IS NOT NULL
                    ORDER BY t.TV ASC
                """
                cursor.execute(query, (last_time,))
            else:
                self.stdout.write("First run: fetching all history...")
                query = """
                    SELECT 
                        m.METER_NO AS serial_number,
                        t.TV AS timestamp,
                        t.CA AS reading_value
                    FROM biz_pub_data_t_energy_d t
                    JOIN a_data_catalogue c ON t.DATA_ID = c.DATA_ID
                    JOIN a_equip_meter m ON c.METER_ID = m.METER_ID
                    WHERE t.CA IS NOT NULL
                    ORDER BY t.TV ASC
                """
                cursor.execute(query)

            count = 0
            for row in cursor:
                sn = row[0].strip()
                if sn not in device_sns:
                    continue
                dt = row[1]
                if isinstance(dt, str):
                    dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
                val = row[2]
                device = Device.objects.get(serial_number=sn)
                Reading.objects.update_or_create(
                    device=device,
                    timestamp=dt,
                    defaults={
                        'reading_value': val,
                        'notes': 'Hexing KUK'
                    }
                )
                count += 1
                if count % 1000 == 0:
                    self.stdout.write(f"Processed {count} readings")

            self.stdout.write(f"✅ Hexing KUK sync done. Total: {count}")
            SyncStatus.objects.update_or_create(
                robot_name='Hexing KUK',
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
                robot_name='Hexing KUK',
                defaults={
                    'status': 'error',
                    'last_update': timezone.now(),
                    'error': str(e)
                }
            )
            raise