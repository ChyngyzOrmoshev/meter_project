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
    help = 'Sync data from Sanxing MS SQL to MySQL (only Sanxing devices)'

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
                    robot_name='Sanxing_old',
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

            # ---- 1. Получаем только активные устройства Sanxing ----
            # devices = []
            # for d in Device.objects.filter(status='active').select_related('model'):
            #     is_sanxing = False

            #     # 1. Проверяем manufacturer в модели (содержит 'Sanxing')
            #     if d.model and d.model.manufacturer and 'Sanxing_old' in d.model.manufacturer:
            #         is_sanxing = True

            #     # # 2. Резерв: api_id начинается с 'SX'
            #     # if not is_sanxing and d.api_id and d.api_id.upper().startswith('SX'):
            #     #     is_sanxing = True

            #     # # 3. Резерв: askue_id == 18
            #     # if not is_sanxing and d.askue_id and str(d.askue_id) == "18":
            #     #     is_sanxing = True

            #     if is_sanxing:
            #         devices.append(d)
            from meters.utils import get_robot_devices
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
            devices_map = {d.serial_number: d for d in devices}

            # ---- 2. Определяем время начала: всегда с 00:00:00 сегодня ----
            start_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            self.stdout.write(f"Fetching data from {start_time} (today 00:00:00)")

            # ---- 3. Запрос к MS SQL ----
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

            # ---- 4. Обработка строк ----
            count = 0
            skipped = 0
            for row in rows:
                sn = row.SerialNumber.strip()
                device = devices_map.get(sn)
                if not device:
                    # Пробуем с ведущими нулями
                    if len(sn) < 8:
                        sn_padded = sn.zfill(8)
                        device = devices_map.get(sn_padded)
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

                Reading.objects.update_or_create(
                    device=device,
                    timestamp=dt,
                    defaults={
                        'reading_value': val,
                        'notes': 'Авто-сбор: Sanxing_old',
                        'direction': 'aplus'
                    }
                )
                count += 1
                if count % 1000 == 0:
                    self.stdout.write(f"Imported {count} readings")

            self.stdout.write(f"✅ Sanxing sync done. Imported: {count}, skipped: {skipped}")
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