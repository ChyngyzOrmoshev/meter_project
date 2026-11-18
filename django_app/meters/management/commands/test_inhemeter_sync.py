"""
Тестовая синхронизация Inhemeter (smartview.amr_read).

Отличия от боевой:
  • Пишет показания ТОЛЬКО для устройств, уже зарегистрированных у нас
    (совпадение по нормализованному серийнику: цифры без ведущих нулей).
  • НЕ регистрирует новые устройства.
  • НЕ пишет в SyncStatus.

Назначение — проверить маппинг колонок и качество данных перед боем.

Запуск:
    python manage.py test_inhemeter_sync --days 30
    python manage.py test_inhemeter_sync --days 30 --verbose
    python manage.py test_inhemeter_sync --days 30 --cleanup
"""

import os
import re
from datetime import datetime, timedelta

import mysql.connector
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone
from dotenv import load_dotenv

from meters.models import Device, Reading


NOTES = 'Авто-сбор: Inhemeter'
DIRECTION = 'aplus'
FIELD_NAME = '03000002'   # единственное значение показания у Inhemeter
BATCH_SIZE = 1000


def norm_serial(s):
    """'037143544692' -> '37143544692'"""
    if s is None:
        return ''
    digits = re.sub(r'\D', '', str(s))
    return digits.lstrip('0') or '0'


class Command(BaseCommand):
    help = 'Тестовая синхронизация Inhemeter — только для уже зарегистрированных устройств'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='Глубина истории при первом запуске (по умолчанию 30)')
        parser.add_argument('--verbose', action='store_true')
        parser.add_argument('--cleanup', action='store_true',
                            help='Перед синхронизацией удалить Reading Inhemeter из нашей БД')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')

        host = os.getenv('INHEMETER_MYSQL_HOST')
        port = int(os.getenv('INHEMETER_MYSQL_PORT', 8001))
        user = os.getenv('INHEMETER_MYSQL_USER')
        password = os.getenv('INHEMETER_MYSQL_PASSWORD')
        db = os.getenv('INHEMETER_MYSQL_DB', 'smartview')

        if not host or not user:
            self.stderr.write(self.style.ERROR(
                'INHEMETER_MYSQL_HOST / INHEMETER_MYSQL_USER не заданы'
            ))
            return

        self.verbose = options.get('verbose', False)
        self.days = options.get('days', 30)

        if options.get('cleanup'):
            qs = Reading.objects.filter(notes=NOTES)
            cnt = qs.count()
            qs.delete()
            self.stdout.write(self.style.WARNING(f'🗑️ Удалено записей {NOTES}: {cnt:,}'))

        # ---- Lookup наших активных устройств (по нормализованному серийнику) ----
        our = {}
        for d in Device.objects.filter(status='active').only('id', 'serial_number'):
            our[norm_serial(d.serial_number)] = d
        self.stdout.write(f'🔎 Загружено устройств из нашей БД: {len(our):,}')

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
            host=host, port=port, user=user, password=password,
            database=db, connect_timeout=10,
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

        self.stdout.write(f'📥 Получено строк из источника: {len(rows):,}')
        if not rows:
            self.stdout.write(self.style.WARNING('ℹ️ Нечего синхронизировать'))
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

            sn_norm = norm_serial(device_num)
            device = our.get(sn_norm)
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

        # ---- Итог ----
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('✅ Тестовый прогон завершён'))
        self.stdout.write(f'   Всего строк из источника:  {len(rows):,}')
        self.stdout.write(f'   Upsert-нуто в readings:    {saved:,}')
        self.stdout.write(f'   Не найдено устройств:      {skipped_no_device:,}')
        self.stdout.write(f'   Битых записей:             {skipped_bad:,}')

        if seen_unmatched and self.verbose:
            sample = sorted(seen_unmatched)[:10]
            self.stdout.write(f'   Примеры несовпавших DeviceNum (10): {sample}')

        # Что реально попало в БД по этому источнику
        total_in_db = Reading.objects.filter(notes=NOTES).count()
        unique_devices = Reading.objects.filter(notes=NOTES).values('device_id').distinct().count()
        self.stdout.write(
            f'   Сейчас в readings [{NOTES}]: '
            f'{total_in_db:,} записей по {unique_devices} устройств(ам)'
        )