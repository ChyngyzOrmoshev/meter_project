"""
Синхронизация показаний Inhemeter (smartview.amr_read → readings).

Только для устройств, которые УЖЕ зарегистрированы у нас.
Регистрацию новых робот НЕ делает.

Особенности источника:
  • Одна колонка FieldName='03000002' — это и есть основное показание.
  • DeviceNum приходит с ведущими нулями ('037143544692'),
    у нас хранится без ('37143544692') — нормализуем через lstrip('0').
  • FieldValue — строка с точкой, всегда парсится как float.

Запуск:
    python manage.py sync_inhemeter --once
    python manage.py sync_inhemeter --once --verbose
    python manage.py sync_inhemeter               # демон (на случай ручного запуска)
"""

import os
import re
import time
import logging
from datetime import datetime, timedelta

import mysql.connector
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone
from dotenv import load_dotenv

from meters.models import Device, Reading, SyncStatus

logger = logging.getLogger(__name__)


ROBOT_NAME = 'Inhemeter'
NOTES = 'Авто-сбор: Inhemeter'
DIRECTION = 'aplus'
FIELD_NAME = '03000002'
BATCH_SIZE = 1000


def norm_serial(s):
    """'037143544692' → '37143544692'. Без ведущих нулей, только цифры."""
    if s is None:
        return ''
    digits = re.sub(r'\D', '', str(s))
    return digits.lstrip('0') or '0'


class Command(BaseCommand):
    help = 'Sync Inhemeter readings from smartview.amr_read'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='Глубина истории при первом запуске (по умолчанию 30)')
        parser.add_argument('--once', action='store_true',
                            help='Один прогон без бесконечного цикла')
        parser.add_argument('--verbose', action='store_true')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')

        self.host = os.getenv('INHEMETER_MYSQL_HOST')
        self.port = int(os.getenv('INHEMETER_MYSQL_PORT', 8001))
        self.user = os.getenv('INHEMETER_MYSQL_USER')
        self.password = os.getenv('INHEMETER_MYSQL_PASSWORD')
        self.db = os.getenv('INHEMETER_MYSQL_DB', 'smartview')
        self.days = options.get('days', 30)
        self.verbose = options.get('verbose', False)

        if not self.host or not self.user:
            self.stderr.write(self.style.ERROR(
                'INHEMETER_MYSQL_HOST / INHEMETER_MYSQL_USER не заданы в cEnergo.env'
            ))
            return

        while True:
            try:
                self.sync()
            except Exception as e:
                logger.exception('Inhemeter sync error: %s', e)
                self._update_status('error', 0, str(e))

            if options.get('once'):
                break
            time.sleep(600)

    # ------------------------------------------------------------------

    def sync(self):
        self.stdout.write('🔄 Starting Inhemeter sync...')

        # ---- Lookup наших активных устройств по нормализованному номеру ----
        our = {}
        for d in Device.objects.filter(status='active').only('id', 'serial_number'):
            our[norm_serial(d.serial_number)] = d
        self.stdout.write(f'🔎 Устройств в нашей БД: {len(our):,}')

        # ---- Период ----
        last_reading = Reading.objects.filter(notes=NOTES).order_by('-timestamp').first()
        if last_reading:
            start_dt = last_reading.timestamp - timedelta(hours=1)
            self.stdout.write(f'🔄 Инкрементально с {start_dt} (Bishkek)')
        else:
            start_dt = timezone.now() - timedelta(days=self.days)
            self.stdout.write(f'🔄 Первый запуск: последние {self.days} дней (с {start_dt})')

        # ---- Читаем источник ----
        conn = mysql.connector.connect(
            host=self.host, port=self.port, user=self.user,
            password=self.password, database=self.db, connect_timeout=10,
        )
        cursor = conn.cursor()
        cursor.execute("""
            SELECT CDate, DeviceNum, FieldValue
            FROM amr_read
            WHERE CDate >= %s
              AND FieldName = %s
              AND FieldValue IS NOT NULL
            ORDER BY CDate ASC
        """, (start_dt, FIELD_NAME))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        self.stdout.write(f'📥 Строк из источника: {len(rows):,}')
        if not rows:
            self.stdout.write('ℹ️ Нечего синхронизировать')
            self._update_status('success', 0, None)
            return

        # ---- Bulk upsert ----
        sql = """
            INSERT INTO readings (device_id, timestamp, direction, reading_value, notes)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                reading_value = VALUES(reading_value),
                notes = VALUES(notes)
        """

        saved = 0
        skipped_no_device = 0
        skipped_bad = 0
        batch = []
        seen_unmatched = set()

        for cdate, device_num, field_value in rows:
            if cdate is None or device_num is None or field_value is None:
                skipped_bad += 1
                continue

            device = our.get(norm_serial(device_num))
            if device is None:
                skipped_no_device += 1
                if self.verbose:
                    seen_unmatched.add(str(device_num).strip())
                continue

            try:
                val = float(str(field_value).replace(',', '.'))
            except (TypeError, ValueError):
                skipped_bad += 1
                if self.verbose:
                    self.stdout.write(
                        f'  ⚠️ Плохое значение DeviceNum={device_num}: {field_value!r}'
                    )
                continue

            if isinstance(cdate, str):
                try:
                    cdate = datetime.strptime(cdate.split('.')[0], '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    skipped_bad += 1
                    continue

            batch.append((device.id, cdate, DIRECTION, val, NOTES))
            if len(batch) >= BATCH_SIZE:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.executemany(sql, batch)
                saved += len(batch)
                if self.verbose:
                    self.stdout.write(f'  💾 upsert {saved}')
                batch = []

        if batch:
            with transaction.atomic(), connection.cursor() as cur:
                cur.executemany(sql, batch)
            saved += len(batch)

        self.stdout.write(self.style.SUCCESS(
            f'✅ Inhemeter sync done. '
            f'saved: {saved}, no_device: {skipped_no_device}, bad: {skipped_bad}'
        ))
        if seen_unmatched and self.verbose:
            sample = sorted(seen_unmatched)[:10]
            self.stdout.write(f'   Примеры несовпавших DeviceNum: {sample}')

        self._update_status('success', saved, None)

    # ------------------------------------------------------------------

    def _update_status(self, status, records, error):
        SyncStatus.objects.update_or_create(
            robot_name=ROBOT_NAME,
            defaults={
                'status': status,
                'last_update': timezone.now(),
                'records_processed': records,
                'error': error,
            }
        )