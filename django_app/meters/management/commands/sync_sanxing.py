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
    help = 'Sync data from Sanxing MS SQL to MySQL (only Sanxing devices)'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true',
                            help='Один прогон без бесконечного цикла')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        server = os.getenv("SANXING_SERVER")
        user = os.getenv("SANXING_USER")
        password = os.getenv("SANXING_PASSWORD")
        db = os.getenv("SANXING_DB")

        conn_str = (
            f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};"
            f"UID={user};PWD={password};Port=1433;TDS_Version=7.4;"
        )

        while True:
            try:
                self.sync(conn_str)
            except Exception as e:
                logger.error(f"Sanxing sync error: {e}")
                SyncStatus.objects.update_or_create(
                    robot_name='Sanxing_old',
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
        self.stdout.write("🔄 Starting Sanxing sync...")
        try:
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()
            self.stdout.write("Connected to Sanxing MS SQL")

            # ---- Загружаем устройства Sanxing ----
            devices = list(get_robot_devices('Sanxing_old'))
            if not devices:
                self.stdout.write("No active Sanxing devices found in MySQL.")
                SyncStatus.objects.update_or_create(
                    robot_name='Sanxing_old',
                    defaults={
                        'status': 'idle',
                        'last_update': timezone.now(),
                        'records_processed': 0,
                        'error': None
                    }
                )
                return

            self.stdout.write(f"Found {len(devices)} active Sanxing devices.")

            # Lookup: точное + last8
            devices_map = {}
            devices_map_last8 = {}
            for d in devices:
                devices_map[d.serial_number] = d
                if len(d.serial_number) >= 8:
                    devices_map_last8.setdefault(d.serial_number[-8:], d)

            # ---- Период: всегда с 00:00:00 сегодня ----
            start_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            self.stdout.write(f"Fetching data from {start_time} (today 00:00:00)")

            query = """
                SET NOCOUNT ON;
                SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_ABS
                FROM DATA_C_ELEC D
                INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                WHERE D.DATA_TIME >= ?
                  AND D.TARIFF_ID = 0
                  AND D.KWH_ABS IS NOT NULL
                ORDER BY D.DATA_TIME ASC
            """
            cursor.execute(query, (start_time,))
            rows = cursor.fetchall()
            self.stdout.write(f"Total rows fetched from MS SQL: {len(rows)}")

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

            for row in rows:
                sn = row.SerialNumber.strip()
                device = devices_map.get(sn)
                if not device and len(sn) >= 8:
                    device = devices_map_last8.get(sn[-8:])
                if not device and len(sn) < 8:
                    device = devices_map_last8.get(sn.zfill(8))
                if not device:
                    skipped += 1
                    if skipped <= 10:
                        self.stdout.write(f"Skipping serial {sn} (not in Sanxing list)")
                    continue

                dt = row.DATA_TIME
                if isinstance(dt, str):
                    dt = datetime.strptime(dt.split('.')[0], "%Y-%m-%d %H:%M:%S")

                raw_val = float(row.KWH_ABS)
                nominal_current = device.nominal_current
                if nominal_current and ("80" in str(nominal_current) or "100" in str(nominal_current)):
                    val = raw_val / 100.0
                else:
                    val = raw_val / 1000.0

                batch.append((device.id, dt, 'aplus', val, 'Авто-сбор: Sanxing_old'))
                count += 1

                if len(batch) >= BATCH_SIZE:
                    with transaction.atomic(), connection.cursor() as cur:
                        cur.executemany(sql, batch)
                    self.stdout.write(f"Bulk-upserted {count} readings")
                    batch = []

            if batch:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.executemany(sql, batch)
                self.stdout.write(f"Bulk-upserted tail {len(batch)} readings")

            self.stdout.write(
                f"✅ Sanxing sync done. Imported: {count}, skipped: {skipped}"
            )
            SyncStatus.objects.update_or_create(
                robot_name='Sanxing_old',
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
                robot_name='Sanxing_old',
                defaults={
                    'status': 'error',
                    'last_update': timezone.now(),
                    'error': str(e)
                }
            )
            raise