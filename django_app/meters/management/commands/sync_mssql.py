from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import connection, transaction
from meters.models import Device, Reading, SyncStatus
from meters.utils import get_robot_devices
import pyodbc
import os
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Sync data from cEnergo MS SQL to MySQL'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true',
                            help='Один прогон без бесконечного цикла')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        server = os.getenv("DB_MSSQL_SERVER")
        user = os.getenv("DB_MSSQL_USER")
        password = os.getenv("DB_MSSQL_PASSWORD")
        db = os.getenv("DB_MSSQL_NAME")

        conn_str = (
            f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};"
            f"UID={user};PWD={password};Port=1433;TDS_Version=7.4;"
        )

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
            if options.get('once'):
                break
            time.sleep(600)

    def sync(self, conn_str):
        self.stdout.write("🔄 Starting cEnergo sync...")
        try:
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()
            self.stdout.write("Connected to cEnergo MS SQL")

            # ---- Устройства cEnergo ----
            devices = list(get_robot_devices('cEnergo'))
            if not devices:
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

            devices_map = {}
            devices_map_last8 = {}
            for d in devices:
                devices_map[d.serial_number] = d
                if len(d.serial_number) >= 8:
                    devices_map_last8.setdefault(d.serial_number[-8:], d)

            # ---- Период ----
            last_reading = Reading.objects.filter(
                notes="Авто-сбор: База cEnergo"
            ).order_by('-timestamp').first()
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
                sn = row.SerialNumber.strip()
                device = devices_map.get(sn)
                if not device and len(sn) >= 8:
                    device = devices_map_last8.get(sn[-8:])
                if not device and len(sn) < 8:
                    device = devices_map_last8.get(sn.zfill(8))
                if not device:
                    skipped += 1
                    continue

                dt = row.DT
                if isinstance(dt, str):
                    dt = datetime.strptime(dt.split('.')[0], "%Y-%m-%d %H:%M:%S")
                val = row.Val

                batch.append((device.id, dt, 'aplus', val, 'Авто-сбор: База cEnergo'))
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
                f"✅ cEnergo sync done. Total: {count}, skipped: {skipped}"
            )
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