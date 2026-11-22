"""Статистика дашборда и по районам.

Расчёт тяжёлый (таблица показаний около 15 млн строк, за 30 дней до 30+ секунд),
поэтому его делает планировщик (`warm_dashboard_cache`), а вьюха берёт готовый
результат из общего кэша. Если кэш пуст, считается на месте.
"""
from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

from .models import Device, Reading, SyncStatus

CATEGORIES = list(Device.CATEGORY_CHOICES)  # [(код, название)], порядок карточек на дашборде
HISTORY_DAYS = 7                            # длина линии активности на карточках категорий (последние 7 дней)
PERIODS = {'today': 0, '3d': 3, '7d': 7, '30d': 30}
# Ключи кэша живут дольше периода обновления, чтобы пользователь не попадал на «холодный» расчёт
CACHE_TTL = {'today': 30 * 60, '3d': 3 * 3600, '7d': 3 * 3600, '30d': 3 * 3600}


def normalize_period(period):
    return period if period in PERIODS else 'today'


def cache_key(period):
    return f'dashboard_stats_{period}'


def region_cache_key(period):
    return f'region_stats_{period}'


def no_reading_cache_key(period):
    return f'no_reading_ids_{period}'


AUTO_NOTES = [
    "Авто-сбор: База cEnergo",       # sync_mssql
    "Авто-сбор: Sanxing_old",        # sync_sanxing
    "Авто-сбор: SunRise",            # sync_sanrise
    "Авто-сбор: RiseSun API",        # sync_risesun_api
    "Авто-сбор: Inhemeter",          # sync_inhemeter
    "Hexing KUK",                    # sync_hexing
    "Hexing Raw sync",               # sync_hxn
    "RiseSun",                       # sync_risesun (старый)
    "Star sync",                     # sync_star (API)
    "Website sync",                  # sync_website (Sanxing 100A)
    "Website sync Period1 S34U18",   # sync_website_period1 (Sanxing 5A)
]


def category_code(category):
    """Прибор без категории (или с неизвестной) относится к «Прочим»."""
    return category if category in dict(CATEGORIES) else 'other'


def _bounds(period):
    today = timezone.now().date()
    start_date = today - timedelta(days=PERIODS[period])
    return datetime.combine(start_date, datetime.min.time()), datetime.combine(today, datetime.max.time())


def compute_period_stats(period):
    """Считает за один проход данные дашборда и статистику по районам.

    Возвращает (data, region_stats, no_reading_ids). Главный запрос один: множество устройств с показаниями
    за период (без JOIN к devices, он заставлял MySQL перебирать устройства по одному);
    всё остальное считается в Python по списку активных устройств.
    """
    from .views import get_producer_display
    period = normalize_period(period)
    start_dt, end_dt = _bounds(period)

    with_readings = set(Reading.objects.filter(
        timestamp__gte=start_dt, timestamp__lte=end_dt,
    ).values_list('device_id', flat=True).distinct())

    devices = list(Device.objects.filter(status='active').values_list(
        'id', 'region__id', 'region__name', 'model__device_type_str', 'model__device_type_id', 'category'))
    total_devices = len(devices)
    active_today = sum(1 for d in devices if d[0] in with_readings)
    active_percent = round((active_today / total_devices * 100), 1) if total_devices else 0

    manual_today = Reading.objects.filter(
        device__status='active', timestamp__gte=start_dt, timestamp__lte=end_dt,
    ).exclude(notes__in=AUTO_NOTES).count()

    producers, regions = {}, {}
    categories = {code: {'total': 0, 'active': 0} for code, _ in CATEGORIES}
    for dev_id, region_id, region_name, type_str, type_id, category in devices:
        is_active = dev_id in with_readings
        entry = categories[category_code(category)]
        entry['total'] += 1
        entry['active'] += is_active
        name = get_producer_display(type_str or "", type_id or "")
        entry = producers.setdefault(name, {'total': 0, 'active': 0})
        entry['total'] += 1
        entry['active'] += is_active
        entry = regions.setdefault((region_id, region_name), {'total': 0, 'active': 0})
        entry['total'] += 1
        entry['active'] += is_active

    producer_stats = [{
        'producer': name, 'total': st['total'], 'active': st['active'],
        'percent': round((st['active'] / st['total'] * 100), 1) if st['total'] else 0,
    } for name, st in producers.items()]
    producer_stats.sort(key=lambda x: x['producer'])

    region_stats = []
    # порядок как у MySQL: район без названия (NULL) первым, остальные по алфавиту
    for (region_id, region_name), st in sorted(regions.items(), key=lambda kv: (kv[0][1] is not None, kv[0][1] or '')):
        region_stats.append({
            'region_id': region_id,
            'region_name': region_name or 'Без района',
            'total': st['total'],
            'active': st['active'],
            'offline': st['total'] - st['active'],
            'percent': round((st['active'] / st['total'] * 100), 1) if st['total'] else 0,
        })

    category_stats = [{
        'code': code, 'name': name, 'total': categories[code]['total'], 'active': categories[code]['active'],
        'percent': round((categories[code]['active'] / categories[code]['total'] * 100), 1) if categories[code]['total'] else 0,
    } for code, name in CATEGORIES]

    data = {
        'category_stats': category_stats,
        'total_devices': total_devices,
        'active_today': active_today,
        'active_percent': active_percent,
        'manual_today': manual_today,
        'sync_statuses': list(SyncStatus.objects.all().order_by('-last_update')),
        'producer_stats': producer_stats,
        'period': period,
    }
    # Все приборы (любого статуса) без показаний за период: на них строятся отчёты
    # «с показаниями» / «без показаний», чтобы не гонять в SQL список из 130 тысяч id
    no_reading_ids = sorted(set(Device.objects.values_list('id', flat=True)) - with_readings)

    return data, region_stats, no_reading_ids


def store_period_stats(period):
    """Считает и кладёт в кэш обе статистики; возвращает (data, region_stats)."""
    period = normalize_period(period)
    data, region_stats, no_reading_ids = compute_period_stats(period)
    cache.set(cache_key(period), data, CACHE_TTL[period])
    cache.set(region_cache_key(period), region_stats, CACHE_TTL[period])
    cache.set(no_reading_cache_key(period),
              {'date': timezone.now().date(), 'ids': no_reading_ids}, CACHE_TTL[period])
    return data, region_stats


def get_dashboard_stats(period, refresh=False):
    period = normalize_period(period)
    data = None if refresh else cache.get(cache_key(period))
    if data is None:
        data, _ = store_period_stats(period)
    return data


def get_no_reading_ids(period):
    """ID приборов без показаний за период (кэш прогрева; за вчерашний день не берётся)."""
    period = normalize_period(period)
    payload = cache.get(no_reading_cache_key(period))
    if payload is None or payload['date'] != timezone.now().date():
        store_period_stats(period)
        payload = cache.get(no_reading_cache_key(period))
    return payload['ids']


def get_with_reading_ids(period):
    """ID приборов, у которых есть показания за период (множество)."""
    no_reading = set(get_no_reading_ids(period))
    return set(Device.objects.values_list('id', flat=True)) - no_reading


def get_region_stats(period):
    period = normalize_period(period)
    region_stats = cache.get(region_cache_key(period))
    if region_stats is None:
        _, region_stats = store_period_stats(period)
    return region_stats


# ---------- Линия активности по категориям (динамика по дням) ----------

def day_cache_key(day):
    return f'category_day_active_{day.isoformat()}'


def _day_category_counts(day, devices):
    """Сколько активных приборов каждой категории передали показания в указанный день."""
    start = datetime.combine(day, datetime.min.time())
    end = datetime.combine(day, datetime.max.time())
    with_readings = set(Reading.objects.filter(
        timestamp__gte=start, timestamp__lte=end,
    ).values_list('device_id', flat=True).distinct())
    counts = {code: 0 for code, _ in CATEGORIES}
    for dev_id, category in devices:
        if dev_id in with_readings:
            counts[category_code(category)] += 1
    return counts


def store_category_history(days=HISTORY_DAYS):
    """Дополняет кэш недостающими днями и пересчитывает сегодняшний; возвращает {день: {категория: активных}}."""
    today = timezone.now().date()
    devices = list(Device.objects.filter(status='active').values_list('id', 'category'))
    history = {}
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        counts = None if back == 0 else cache.get(day_cache_key(day))
        if counts is None:
            counts = _day_category_counts(day, devices)
            # прошедшие дни меняются мало (редкие поздние показания): держим сутки, сегодня обновляет прогрев
            cache.set(day_cache_key(day), counts, 30 * 60 if back == 0 else 26 * 3600)
        history[day] = counts
    return history


def get_category_history(category_stats, days=HISTORY_DAYS):
    """Процент активных по дням для каждой категории: {код: [проценты от старых дням к новым]}, список дат.

    Знаменатель, как в карточке, берётся из текущего числа активных приборов категории.
    """
    today = timezone.now().date()
    day_list = [today - timedelta(days=back) for back in range(days - 1, -1, -1)]
    history = {}
    missing = False
    for day in day_list:
        counts = cache.get(day_cache_key(day))
        if counts is None:
            missing = True
            break
        history[day] = counts
    if missing:
        history = store_category_history(days)
    totals = {c['code']: c['total'] for c in category_stats}
    series = {code: [round(history[day][code] / totals[code] * 100, 1) if totals[code] else 0 for day in day_list]
              for code in totals}
    return series, day_list


def build_sparkline(values, width=100, height=40, pad=6, min_span=10):
    """Геометрия линии для SVG: точки, заливка под линией и последняя точка.

    Шкала по Y берётся от минимума до максимума ряда, но не уже `min_span` пунктов,
    чтобы шум в пределах процента не выглядел как обвал.
    """
    n = len(values)
    if n == 0:
        return None
    low, high = min(values), max(values)
    if high - low < min_span:
        mid = (high + low) / 2
        low, high = mid - min_span / 2, mid + min_span / 2
    step = width / (n - 1) if n > 1 else 0
    pts = [(round(i * step, 2), round(pad + (height - 2 * pad) * (1 - (v - low) / (high - low)), 2))
           for i, v in enumerate(values)]
    line = ' '.join(f'{x},{y}' for x, y in pts)
    area = f'0,{height} {line} {pts[-1][0]},{height}'
    return {'line': line, 'area': area, 'points': pts[:-1], 'last_x': pts[-1][0], 'last_y': pts[-1][1],
            'width': width, 'height': height, 'top': pad, 'bottom': height - pad}


def get_category_cards(period):
    """Данные карточек категорий: активность за период и линия по дням."""
    period = normalize_period(period)
    data = get_dashboard_stats(period)
    if 'category_stats' not in data:  # кэш прежней версии без категорий
        data = get_dashboard_stats(period, refresh=True)
    stats = data['category_stats']
    series, days = get_category_history(stats)
    cards = []
    for c in stats:
        values = series[c['code']]
        cards.append({**c, 'spark': build_sparkline(values),
                      'delta': round(values[-1] - values[-2], 1) if len(values) > 1 else 0,
                      'first_day': days[0], 'last_day': days[-1], 'days': len(days)})
    return cards
