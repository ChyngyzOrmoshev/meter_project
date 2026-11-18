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
    help = 'Sync data from SunRise MS SQL to MySQL'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Количество дней истории при первом запуске (по умолчанию 30)',
        )
        parser.add_argument(
            '--once',
            action='store_true',
            help='Один прогон без бесконечного цикла',
        )

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        server = os.getenv("SANRISE_MSSQL_SERVER")
        user = os.getenv("SANRISE_MSSQL_USER")
        password = os.getenv("SANRISE_MSSQL_PASSWORD")
        db = os.getenv("SANRISE_MSSQL_DB")

        conn_str = (
            f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};"
            f"UID={user};PWD={password};Port=1433;TDS_Version=7.4;"
        )
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
            if options.get('once'):
                break
            time.sleep(600)

    def sync(self, conn_str):
        self.stdout.write("🔄 Starting SunRise sync...")
        try:
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()
            self.stdout.write("Connected to SunRise MS SQL")

            # ---- Загружаем устройства SunRise один раз ----
            devices = list(get_robot_devices('SunRise'))
            if not devices:
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

            # Lookup: точное совпадение + последние 8 символов
            lookup_exact = {}
            lookup_last8 = {}
            for d in devices:
                sn = d.serial_number
                lookup_exact[sn] = d
                if len(sn) >= 8:
                    lookup_last8.setdefault(sn[-8:], d)

            self.stdout.write(
                f"Built lookup: exact={len(lookup_exact)}, last8={len(lookup_last8)}"
            )

            # ---- Определяем период ----
            last_reading = Reading.objects.filter(
                notes="Авто-сбор: SunRise"
            ).order_by('-timestamp').first()
            if last_reading:
                start_time = last_reading.timestamp - timedelta(hours=2)
                self.stdout.write(f"🔄 Incremental sync from {start_time}")
            else:
                start_time = datetime.now() - timedelta(days=self.days)
                self.stdout.write(
                    f"🔄 First run: fetching last {self.days} days (from {start_time})"
                )

            query = """
                SET NOCOUNT ON;
                SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_IMPORT_ABS
                FROM DATA_C_DAILY D
                INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                WHERE D.DATA_TIME >= ? AND D.KWH_IMPORT_ABS IS NOT NULL
                ORDER BY D.DATA_TIME ASC
            """
            cursor.execute(query, (start_time,))

            # ---- Пакетная вставка через executemany + ON DUPLICATE KEY ----
            count = 0
            skipped_not_found = 0
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
                db_sn = str(row.SerialNumber).strip()
                if not db_sn:
                    continue

                # Быстрый lookup вместо find_device
                device = lookup_exact.get(db_sn)
                if device is None and len(db_sn) >= 8:
                    device = lookup_last8.get(db_sn[-8:])
                if device is None and len(db_sn) < 8:
                    device = lookup_last8.get(db_sn.zfill(8))
                if device is None:
                    skipped_not_found += 1
                    continue

                dt = row.DATA_TIME
                if isinstance(dt, str):
                    dt = datetime.strptime(dt.split('.')[0], "%Y-%m-%d %H:%M:%S")
                val = row.KWH_IMPORT_ABS

                batch.append((device.id, dt, 'aplus', val, 'Авто-сбор: SunRise'))
                count += 1

                if len(batch) >= BATCH_SIZE:
                    with transaction.atomic(), connection.cursor() as cur:
                        cur.executemany(sql, batch)
                    self.stdout.write(f"Bulk-upserted {count}")
                    batch = []

            # Хвост
            if batch:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.executemany(sql, batch)

            self.stdout.write(
                f"✅ SunRise sync done. Total: {count}, "
                f"not_found: {skipped_not_found}"
            )
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