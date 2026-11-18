"""
Тестовое подключение к MMWS API (раздел 4.1 документа).
Использует только stdlib (urllib). Поддерживает автофолбэк http<->https.

Примеры:
    python manage.py test_mmws_api --ping
    python manage.py test_mmws_api --ping --scheme http
    python manage.py test_mmws_api --ping --scheme https
    python manage.py test_mmws_api --days 7 --compare
"""

import os
import re
import json
import time
import ssl
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from dotenv import load_dotenv
from django.core.management.base import BaseCommand
from django.utils import timezone

from meters.models import Device, Reading


def normalize_url(url):
    """https://host:port//api/v1/billing → https://host:port/api/v1/billing"""
    url = url.strip()
    # Убираем дублирующиеся слэши в пути, но сохраняем "://"
    m = re.match(r'^(https?://)(.*)$', url, re.IGNORECASE)
    if not m:
        return url
    scheme, rest = m.group(1), m.group(2)
    rest = re.sub(r'/+', '/', rest)   # схлопываем подряд идущие слэши в один
    return scheme + rest


def swap_scheme(url):
    """Меняет http<->https."""
    if url.lower().startswith('https://'):
        return 'http://' + url[8:]
    if url.lower().startswith('http://'):
        return 'https://' + url[7:]
    return url


class Command(BaseCommand):
    help = 'Тестовое подключение к MMWS API'

    def add_arguments(self, parser):
        parser.add_argument('--ping', action='store_true')
        parser.add_argument('--days', type=int, default=7)
        parser.add_argument('--date-from', type=str, default=None)
        parser.add_argument('--date-to', type=str, default=None)
        parser.add_argument('--meters', type=str, default=None)
        parser.add_argument('--page-size', type=int, default=500)
        parser.add_argument('--max-pages', type=int, default=20)
        parser.add_argument('--compare', action='store_true')
        parser.add_argument('--save-json', type=str, default=None)
        parser.add_argument('--insecure', action='store_true')
        parser.add_argument('--scheme', choices=['http', 'https'], default=None,
                            help='Принудительно задать схему')
        parser.add_argument('--no-fallback', action='store_true',
                            help='Не пытаться переключить схему автоматически')

    # ---------- HTTP ----------

    def _ssl_context(self):
        if self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context()

    def _do_get(self, base_url, path, params):
        query = urllib.parse.urlencode(params, doseq=True)
        url = f'{base_url}{path}?{query}'
        req = urllib.request.Request(url, method='GET')
        req.add_header('Authorization', f'Bearer {self.api_key}')
        req.add_header('Accept', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._ssl_context()) as resp:
                return resp.status, resp.read().decode('utf-8', errors='replace'), None
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', errors='replace'), None
        except urllib.error.URLError as e:
            return None, None, str(e.reason)
        except ssl.SSLError as e:
            return None, None, f'SSL: {e}'

    def _http_get(self, path, params):
        """Пытается текущий base_url, при ошибке — со сменой схемы."""
        status, body, err = self._do_get(self.base_url, path, params)

        # Проверяем типичные ошибки схемы
        err_str = (err or '').lower()
        scheme_problem = (
            'wrong_version_number' in err_str
            or 'connection refused' in err_str
            or 'unknown protocol' in err_str
            or 'ssl' in err_str and 'wrong' in err_str
        )

        if scheme_problem and not self.no_fallback:
            alt_url = swap_scheme(self.base_url)
            self.stdout.write(self.style.WARNING(
                f'⚠️ Ошибка при {self.base_url}: {err}'
            ))
            self.stdout.write(self.style.WARNING(
                f'🔄 Пробуем альтернативную схему: {alt_url}'
            ))
            status, body, err = self._do_get(alt_url, path, params)
            if status is not None:
                self.base_url = alt_url  # запоминаем рабочую схему

        if status is None:
            self.stderr.write(self.style.ERROR(f'❌ Ошибка сети: {err}'))
        return status, body

    # ---------- Основной цикл ----------

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')

        raw_url = os.getenv('MMWS_API_URL', '')
        self.base_url = normalize_url(raw_url).rstrip('/')
        self.api_key = os.getenv('MMWS_API_KEY', '')
        self.timeout = int(os.getenv('MMWS_API_TIMEOUT', 30))
        self.insecure = options.get('insecure', False)
        self.no_fallback = options.get('no_fallback', False)

        if not self.base_url or not self.api_key:
            self.stderr.write(self.style.ERROR(
                'Не заданы MMWS_API_URL или MMWS_API_KEY'
            ))
            return

        # Принудительная схема
        if options.get('scheme'):
            forced = options['scheme']
            old = self.base_url
            if forced == 'http' and old.startswith('https://'):
                self.base_url = 'http://' + old[8:]
            elif forced == 'https' and old.startswith('http://'):
                self.base_url = 'https://' + old[7:]
            if old != self.base_url:
                self.stdout.write(f'🔧 Схема принудительно: {self.base_url}')

        self.stdout.write(f'🌐 Base URL: {self.base_url}')
        self.stdout.write(f'🔑 API key:  {self.api_key[:8]}...{self.api_key[-4:]}')

        if options['ping']:
            self._ping()
            return

        date_from, date_to = self._resolve_dates(options)
        self.stdout.write(f'📅 Период: {date_from} → {date_to}')

        meters = None
        if options['meters']:
            meters = [m.strip() for m in options['meters'].split(',') if m.strip()]
            self.stdout.write(f'🎯 Счётчики: {len(meters)} шт.')

        items = self._fetch_all_readings(
            date_from, date_to, meters,
            options['page_size'], options['max_pages'],
        )
        if items is None:
            return

        self._print_stats(items)

        if options['save_json']:
            with open(options['save_json'], 'w', encoding='utf-8') as f:
                json.dump(items, f, ensure_ascii=False, indent=2, default=str)
            self.stdout.write(self.style.SUCCESS(f'💾 Сохранено в {options["save_json"]}'))

        if options['compare']:
            self._compare_with_db(items)

    def _resolve_dates(self, options):
        if options['date_from'] and options['date_to']:
            return options['date_from'], options['date_to']
        now = timezone.now()
        return (now - timedelta(days=options['days'])).isoformat(), now.isoformat()

    def _ping(self):
        params = {
            'date_from': (timezone.now() - timedelta(days=1)).isoformat(),
            'date_to': timezone.now().isoformat(),
            'page': 1,
            'page_size': 1,
        }
        status, body = self._http_get('/readings', params)
        if status is None:
            return

        self.stdout.write(f'📡 HTTP {status} (рабочий URL: {self.base_url})')

        if status == 200:
            try:
                data = json.loads(body)
                self.stdout.write(self.style.SUCCESS('✅ Авторизация OK'))
                self.stdout.write(f'   total записей за 1 день: {data.get("total", "?")}')
            except json.JSONDecodeError:
                self.stdout.write(self.style.WARNING('⚠️ Ответ не JSON:'))
                self.stdout.write(body[:500])
        elif status in (401, 403):
            self.stderr.write(self.style.ERROR(f'❌ HTTP {status} — проблема с API-ключом'))
            self.stdout.write(body[:500])
        else:
            self.stderr.write(self.style.ERROR(f'❌ HTTP {status}'))
            self.stdout.write(body[:500])

    def _fetch_all_readings(self, date_from, date_to, meters, page_size, max_pages):
        page_size = min(page_size, 2000)
        all_items = []
        page = 1
        while page <= max_pages:
            params = {
                'date_from': date_from,
                'date_to': date_to,
                'page': page,
                'page_size': page_size,
            }
            if meters:
                params['meter_ids'] = ','.join(meters)

            t0 = time.time()
            status, body = self._http_get('/readings', params)
            elapsed = time.time() - t0
            if status is None:
                return all_items if all_items else None
            if status != 200:
                self.stderr.write(self.style.ERROR(
                    f'❌ HTTP {status} на стр. {page}: {(body or "")[:300]}'
                ))
                return all_items if all_items else None

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.stderr.write(self.style.ERROR(f'❌ Не JSON: {body[:300]}'))
                return all_items if all_items else None

            items = data.get('items', [])
            total = data.get('total', 0)
            all_items.extend(items)
            self.stdout.write(
                f'  📄 Стр. {page}: {len(items)} (всего {len(all_items)} из {total}) — {elapsed:.2f}с'
            )
            if len(all_items) >= total or not items:
                break
            page += 1
        return all_items

    def _print_stats(self, items):
        if not items:
            self.stdout.write(self.style.WARNING('⚠️ Ни одной записи'))
            return
        self.stdout.write(self.style.SUCCESS(f'✅ Получено записей: {len(items)}'))
        uniq_m = set()
        obis = set()
        min_dt = max_dt = None
        for it in items:
            if it.get('meter_serial'):
                uniq_m.add(it['meter_serial'])
            if it.get('obis_code'):
                obis.add(it['obis_code'])
            ts = it.get('read_at')
            if ts:
                min_dt = ts if min_dt is None or ts < min_dt else min_dt
                max_dt = ts if max_dt is None or ts > max_dt else max_dt
        self.stdout.write(f'   Уникальных счётчиков: {len(uniq_m)}')
        self.stdout.write(f'   OBIS-кодов: {sorted(obis)}')
        self.stdout.write(f'   Диапазон дат: {min_dt} → {max_dt}')
        self.stdout.write('   Пример записей:')
        for it in items[:5]:
            self.stdout.write(
                f'     • {it.get("meter_serial")} | {it.get("obis_code")} | '
                f'{it.get("value")} {it.get("unit")} | {it.get("read_at")}'
            )

    def _compare_with_db(self, api_items):
        self.stdout.write('')
        self.stdout.write('🔍 Сравнение с БД:')
        self.stdout.write('-' * 60)
        api_map = {}
        for it in api_items:
            sn = (it.get('meter_serial') or '').strip()
            ts = it.get('read_at')
            if not sn or not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            except ValueError:
                continue
            api_map[(sn, dt.date())] = float(it.get('value', 0))

        matched = 0
        only_api = []
        mismatch = []
        for (sn, day), api_val in api_map.items():
            device = Device.objects.filter(serial_number=sn).first()
            if not device:
                last8 = sn[-8:] if len(sn) >= 8 else sn
                device = Device.objects.filter(serial_number__endswith=last8).first()
            if not device:
                only_api.append((sn, day, api_val))
                continue
            db_r = Reading.objects.filter(
                device=device, timestamp__date=day, direction='aplus'
            ).order_by('-timestamp').first()
            if not db_r:
                only_api.append((sn, day, api_val))
                continue
            matched += 1
            if abs(float(db_r.reading_value) - api_val) > 0.01:
                mismatch.append((sn, day, float(db_r.reading_value), api_val))

        self.stdout.write(f'  Записей в API:           {len(api_map)}')
        self.stdout.write(f'  Совпало с БД:            {matched}')
        self.stdout.write(f'  Только в API:            {len(only_api)}')
        self.stdout.write(f'  Расхождение значений:    {len(mismatch)}')
        for sn, day, val in only_api[:5]:
            self.stdout.write(self.style.WARNING(f'  ⚠️ Только в API: {sn} | {day} | {val}'))
        for sn, day, dbv, av in mismatch[:5]:
            self.stdout.write(self.style.WARNING(f'  ⚠️ Расхождение: {sn} | {day} | БД={dbv} | API={av}'))