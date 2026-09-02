from django.core.management.base import BaseCommand
from django.utils import timezone
from meters.models import Device, Reading, SyncStatus
import subprocess
import csv
import io
import os
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync data from RiseSun PostgreSQL (only last reading per day)'

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        self.host = os.getenv("RISESUN_PG_HOST")
        self.port = os.getenv("RISESUN_PG_PORT", "5432")
        self.db = os.getenv("RISESUN_PG_DB")
        self.user = os.getenv("RISESUN_PG_USER")
        self.password = os.getenv("RISESUN_PG_PASSWORD")
        self.encoding = "cp1251"

        while True:
            try:
                self.sync()
            except Exception as e:
                logger.error(f"RiseSun sync error: {e}")
                SyncStatus.objects.update_or_create(
                    robot_name='RiseSun',
                    defaults={
                        'status': 'error',
                        'last_update': timezone.now(),
                        'error': str(e)
                    }
                )
            time.sleep(600)

    def run_psql_query(self, query):
        cmd = [
            "psql",
            "-h", self.host,
            "-p", self.port,
            "-U", self.user,
            "-d", self.db,
            "-t", "-A", "-F", ",",
            "-c", query
        ]
        env = os.environ.copy()
        env["PGPASSWORD"] = self.password
        env["PGSSLMODE"] = "disable"
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, timeout=120)
            if result.returncode != 0:
                self.stdout.write(f"psql error: {result.stderr.decode(self.encoding, errors='replace')}")
                return []
            output = result.stdout.decode(self.encoding, errors='replace').strip()
            if not output:
                return []
            reader = csv.reader(io.StringIO(output))
            return list(reader)
        except Exception as e:
            self.stdout.write(f"psql exception: {e}")
            return []

    def sync(self):
        self.stdout.write("🔄 Starting RiseSun sync (only last reading per day)...")
        try:
            # device_sns = set(Device.objects.filter(status='active').values_list('serial_number', flat=True))
            from meters.utils import get_robot_devices
            devices = get_robot_devices('RiseSun')
            device_sns = set(devices.values_list('serial_number', flat=True))
            if not device_sns:
                self.stdout.write("No active devices, sync skipped.")
                SyncStatus.objects.update_or_create(
                    robot_name='RiseSun',
                    defaults={
                        'status': 'idle',
                        'last_update': timezone.now(),
                        'records_processed': 0,
                        'error': None
                    }
                )
                return

            last_reading = Reading.objects.filter(notes="RiseSun").order_by('-timestamp').first()
            if last_reading:
                last_time = last_reading.timestamp - timedelta(hours=1)
                last_time_str = last_time.strftime("%Y-%m-%d %H:%M:%S")
                # Используем оконную функцию для получения последней записи за день
                query = f"""
                    WITH ranked AS (
                        SELECT 
                            m.device_id AS serial_number,
                            r.date AS timestamp,
                            r.value AS reading_value,
                            ROW_NUMBER() OVER (PARTITION BY m.device_id, DATE(r.date) ORDER BY r.date DESC) as rn
                        FROM ami.readings r
                        JOIN ami.meters m ON r.meter = m.id
                        JOIN ami.models mo ON m.model = mo.id
                        WHERE r.date >= '{last_time_str}'
                          AND r.type = 10
                          AND r.value IS NOT NULL
                          AND mo.producer = 5
                    )
                    SELECT serial_number, timestamp, reading_value
                    FROM ranked
                    WHERE rn = 1
                    ORDER BY timestamp ASC
                """
            else:
                self.stdout.write("First run: fetching only last reading per day for all history...")
                query = """
                    WITH ranked AS (
                        SELECT 
                            m.device_id AS serial_number,
                            r.date AS timestamp,
                            r.value AS reading_value,
                            ROW_NUMBER() OVER (PARTITION BY m.device_id, DATE(r.date) ORDER BY r.date DESC) as rn
                        FROM ami.readings r
                        JOIN ami.meters m ON r.meter = m.id
                        JOIN ami.models mo ON m.model = mo.id
                        WHERE r.date >= '2026-06-01'
                          AND r.type = 10
                          AND r.value IS NOT NULL
                          AND mo.producer = 5
                    )
                    SELECT serial_number, timestamp, reading_value
                    FROM ranked
                    WHERE rn = 1
                    ORDER BY timestamp ASC
                """

            rows = self.run_psql_query(query)
            count = 0
            if rows:
                for row in rows:
                    if len(row) < 3:
                        continue
                    sn = row[0].strip()
                    if sn not in device_sns:
                        continue
                    try:
                        dt = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
                    except:
                        try:
                            dt = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S.%f")
                        except:
                            continue
                    try:
                        val = float(row[2])
                    except:
                        continue
                    device = Device.objects.filter(serial_number=sn, status='active').first()
                    if not device:
                        continue
                    Reading.objects.update_or_create(
                        device=device,
                        timestamp=dt,
                        defaults={
                            'reading_value': val,
                            'notes': 'RiseSun',
                            'direction': 'aplus'
                        }
                    )
                    count += 1
                    if count % 1000 == 0:
                        self.stdout.write(f"Processed {count} readings")

            self.stdout.write(f"✅ RiseSun sync done. Total: {count}")
            SyncStatus.objects.update_or_create(
                robot_name='RiseSun',
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
                robot_name='RiseSun',
                defaults={
                    'status': 'error',
                    'last_update': timezone.now(),
                    'error': str(e)
                }
            )
            raise