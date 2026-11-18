"""
Синхронизация показаний RiseSun через MMWS API (раздел 4.1 регламента).
Только stdlib (urllib) — requests не требуется.

- Период: с последнего показания (или --days при первом запуске).
- Пагинация: page_size = 2000.
- Retry: экспоненциальная задержка на 429/5xx (раздел 7).
- Время: API отдаёт ISO 8601 с TZ (+00:00 / UTC).
        Сохраняем в БД как naive Bishkek local (UTC+6),
        чтобы совпадать с другими роботами проекта.
- Запись в Reading: direction='aplus', notes='Авто-сбор: RiseSun API'.

Запуск:
    python manage.py sync_risesun_api --once --verbose
    python manage.py sync_risesun_api --once --cleanup --days 60   # очистить + залить
    python manage.py sync_risesun_api                              # демон
"""

import os
import ssl
import json
import time
import urllib.parse
import urllib.request
import urllib.error
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.utils import timezone
from dotenv import load_dotenv

from meters.models import Reading, SyncStatus
from meters.utils import get_robot_devices, is_risesun_device

logger = logging.getLogger(__name__)

MMWS_PAGE_SIZE = 2000
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 2
FIXED_429_DELAY = 30

ROBOT_NAME = 'RiseSun'
NOTES = 'Авто-сбор: RiseSun API'
DIRECTION = 'aplus'

# Бишкек = UTC+6 без перехода на летнее время
BISHKEK_TZ = dt_timezone(timedelta(hours=6))


class Command(BaseCommand):
    help = 'Sync RiseSun readings from MMWS API'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='Глубина истории при первом запуске (по умолчанию 30)')
        parser.add_argument('--once', action='store_true',
                            help='Один прогон без бесконечного цикла')
        parser.add_argument('--verbose', action='store_true')
        parser.add_argument('--cleanup', action='store_true',
                            help='Перед синхронизацией удалить ВСЕ записи RiseSun и RiseSun API')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')

        self.base_url = (os.getenv('MMWS_API_URL') or '').rstrip('/')
        self.api_key = os.getenv('MMWS_API_KEY') or ''
        self.timeout = int(os.getenv('MMWS_API_TIMEOUT', 30))
        self.days = options.get('days', 30)
        self.once = options.get('once', False)
        self.verbose = options.get('verbose', False)

        if not self.base_url or not self.api_key:
            self.stderr.write(self.style.ERROR(
                'MMWS_API_URL / MMWS_API_KEY не заданы в cEnergo.env'
            ))
            return

        self.stdout.write(f'🌐 MMWS: {self.base_url}')
        self.stdout.write(f'🔑 Key:  {self.api_key[:8]}...{self.api_key[-4:]}')

        if options.get('cleanup'):
            qs = Reading.objects.filter(notes__in=['RiseSun', NOTES])
            count = qs.count()
            qs.delete()
            self.stdout.write(self.style.WARNING(f'🗑️ Удалено записей: {count}'))
            SyncStatus.objects.filter(robot_name=ROBOT_NAME).update(
                status='idle', records_processed=0, error=None
            )

        while True:
            try:
                self.sync()
            except Exception as e:
                logger.exception('RiseSun API sync error: %s', e)
                self._update_status('error', 0, str(e))
            if self.once:
                break
            time.sleep(600)

    def sync(self):
        self.stdout.write('🔄 Starting RiseSun API sync...')

        devices = list(get_robot_devices('RiseSun'))
        if not devices:
            self.stdout.write('No RiseSun devices in DB, skipping')
            self._update_status('idle', 0, None)
            return
        device_map = {}
        device_map_last8 = {}
        for d in devices:
            device_map[d.serial_number] = d
            if len(d.serial_number) >= 8:
                device_map_last8.setdefault(d.serial_number[-8:], d)

        # ---- Период в Bishkek naive ----
        last_reading = Reading.objects.filter(notes=NOTES).order_by('-timestamp').first()
        now_bishkek = datetime.now(dt_timezone.utc).astimezone(BISHKEK_TZ).replace(tzinfo=None)

        if last_reading:
            start_local = last_reading.timestamp - timedelta(hours=2)
            self.stdout.write(f'🔄 Incremental from {start_local} (Bishkek)')
        else:
            start_local = now_bishkek - timedelta(days=self.days)
            self.stdout.write(
                f'🔄 First run: last {self.days} days (from {start_local} Bishkek)'
            )

        end_local = now_bishkek

        # Bishkek naive → UTC aware → ISO 8601 для API
        date_from = start_local.replace(tzinfo=BISHKEK_TZ).astimezone(dt_timezone.utc).isoformat()
        date_to = end_local.replace(tzinfo=BISHKEK_TZ).astimezone(dt_timezone.utc).isoformat()

        self.stdout.write(f'📅 API query: {date_from} → {date_to}')

        # ---- Получаем данные ----
        items = self._fetch_all(date_from, date_to)
        if items is None:
            return

        if not items:
            self.stdout.write('ℹ️ API вернул 0 записей')
            self._update_status('success', 0, None)
            return

        # ---- Сохраняем ----
        saved = 0
        updated = 0
        skipped_no_device = 0
        skipped_bad = 0

        for it in items:
            sn = (it.get('meter_serial') or '').strip()
            if not sn:
                skipped_bad += 1
                continue

            # Быстрый lookup: точное → last8
            device = device_map.get(sn)
            if device is None and len(sn) >= 8:
                device = device_map_last8.get(sn[-8:])
            if device is None and len(sn) < 8:
                device = device_map_last8.get(sn.zfill(8))

            if device is None or not is_risesun_device(device):
                skipped_no_device += 1
                if self.verbose:
                    self.stdout.write(f'  ⚠️ skip {sn} (not RiseSun)')
                continue

            try:
                value = float(it['value'])
            except (KeyError, TypeError, ValueError):
                skipped_bad += 1
                continue

            # API ISO 8601 (+00:00) → Bishkek naive
            try:
                ts_api = datetime.fromisoformat(it['read_at'].replace('Z', '+00:00'))
                if ts_api.tzinfo is None:
                    ts_api = ts_api.replace(tzinfo=dt_timezone.utc)
                ts_local = ts_api.astimezone(BISHKEK_TZ).replace(tzinfo=None)
            except (KeyError, ValueError, AttributeError):
                skipped_bad += 1
                continue

            obj, created = Reading.objects.update_or_create(
                device=device,
                timestamp=ts_local,
                direction=DIRECTION,
                defaults={
                    'reading_value': value,
                    'notes': NOTES,
                }
            )
            if created:
                saved += 1
            else:
                updated += 1

            if (saved + updated) % 1000 == 0:
                self.stdout.write(f'  💾 processed {saved + updated}')

        self.stdout.write(
            f'✅ RiseSun API sync done. '
            f'Saved: {saved}, updated: {updated}, '
            f'skipped (no device): {skipped_no_device}, bad: {skipped_bad}'
        )
        self._update_status('success', saved + updated, None)

    # ---------- HTTP ----------

    def _fetch_all(self, date_from, date_to):
        all_items = []
        page = 1
        while True:
            params = {
                'date_from': date_from,
                'date_to': date_to,
                'page': page,
                'page_size': MMWS_PAGE_SIZE,
            }
            status, body = self._http_get_with_retry('/readings', params)
            if status is None:
                self.stderr.write(self.style.ERROR('❌ Сеть недоступна'))
                return all_items if all_items else None
            if status != 200:
                self.stderr.write(self.style.ERROR(
                    f'❌ HTTP {status}: {(body or "")[:300]}'
                ))
                return all_items if all_items else None

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.stderr.write(self.style.ERROR(f'❌ Non-JSON: {body[:300]}'))
                return all_items if all_items else None

            items = data.get('items', [])
            total = data.get('total', 0)
            all_items.extend(items)
            self.stdout.write(
                f'  📄 page {page}: +{len(items)} (total {len(all_items)}/{total})'
            )

            if len(all_items) >= total or not items:
                break
            page += 1
        return all_items

    def _http_get_with_retry(self, path, params):
        delay = INITIAL_RETRY_DELAY
        for attempt in range(1, MAX_RETRIES + 1):
            status, body, err = self._do_get(path, params)

            if status is None:
                self.stderr.write(f'  ⚠️ attempt {attempt}: {err}')
                time.sleep(delay)
                delay *= 2
                continue

            if status == 429:
                self.stderr.write(f'  ⚠️ 429 Too Many Requests, sleep {FIXED_429_DELAY}s')
                time.sleep(FIXED_429_DELAY)
                continue

            if 500 <= status < 600:
                self.stderr.write(f'  ⚠️ {status}, retry in {delay}s')
                time.sleep(delay)
                delay *= 2
                continue

            return status, body

        return None, None

    def _do_get(self, path, params):
        query = urllib.parse.urlencode(params, doseq=True)
        url = f'{self.base_url}{path}?{query}'
        req = urllib.request.Request(url, method='GET')
        req.add_header('Authorization', f'Bearer {self.api_key}')
        req.add_header('Accept', 'application/json')

        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                return resp.status, resp.read().decode('utf-8', errors='replace'), None
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', errors='replace'), None
        except urllib.error.URLError as e:
            return None, None, str(e.reason)
        except Exception as e:
            return None, None, str(e)

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