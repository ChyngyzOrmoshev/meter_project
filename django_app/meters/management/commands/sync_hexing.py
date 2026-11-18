from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import connection, transaction
from meters.models import Device, Reading, SyncStatus
from meters.utils import get_robot_devices
import mysql.connector
import os
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Sync data from Hexing KUK MySQL to MySQL'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true',
                            help='Один прогон без бесконечного цикла')

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
                    robot_name='Hexing_KUK',
                    defaults={
                        'status': 'error',
                        'last_update': timezone.now(),
                        'error': str(e)
                    }
                )
            if options.get('once'):
                break
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

            # ---- Устройства Hexing_KUK ----
            devices = list(get_robot_devices('Hexing_KUK'))
            if not devices:
                self.stdout.write("No devices in MySQL, sync skipped.")
                SyncStatus.objects.update_or_create(
                    robot_name='Hexing_KUK',
                    defaults={
                        'status': 'idle',
                        'last_update': timezone.now(),
                        'records_processed': 0,
                        'error': None
                    }
                )
                return

            devices_map = {}
            devices_map_last8 = {}
            for d in devices:
                devices_map[d.serial_number] = d
                if len(d.serial_number) >= 8:
                    devices_map_last8.setdefault(d.serial_number[-8:], d)

            # ---- Период ----
            last_reading = Reading.objects.filter(
                notes="Hexing KUK"
            ).order_by('-timestamp').first()
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

            # ---- Пакетная вставка ----
            count = 0
            skipped = 0
            batch = []
            BATCH_SIZE = 1000

            sql = """
                INSERT INTO readings (device_id, timestamp, direction, reading_value, notes)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    reading_value = VALUES(reading_value),
                    notes = VALUES(notes)
            """

            for row in cursor:
                sn = row[0].strip()
                device = devices_map.get(sn)
                if not device and len(sn) >= 8:
                    device = devices_map_last8.get(sn[-8:])
                if not device and len(sn) < 8:
                    device = devices_map_last8.get(sn.zfill(8))
                if not device:
                    skipped += 1
                    continue

                dt = row[1]
                if isinstance(dt, str):
                    dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
                val = row[2]

                batch.append((device.id, dt, 'aplus', val, 'Hexing KUK'))
                count += 1

                if len(batch) >= BATCH_SIZE:
                    with transaction.atomic(), connection.cursor() as cur:
                        cur.executemany(sql, batch)
                    self.stdout.write(f"Bulk-upserted {count}")
                    batch = []

            if batch:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.executemany(sql, batch)

            self.stdout.write(
                f"✅ Hexing KUK sync done. Total: {count}, skipped: {skipped}"
            )
            SyncStatus.objects.update_or_create(
                robot_name='Hexing_KUK',
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
                robot_name='Hexing_KUK',
                defaults={
                    'status': 'error',
                    'last_update': timezone.now(),
                    'error': str(e)
                }
            )
            raise