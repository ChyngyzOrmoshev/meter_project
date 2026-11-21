"""Статистика дашборда и по районам.

Расчёт тяжёлый (таблица показаний около 15 млн строк, за 30 дней до 30+ секунд),
поэтому его делает планировщик (`warm_dashboard_cache`), а вьюха берёт готовый
результат из общего кэша. Если кэш пуст, считается на месте.
"""
from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

from .models import Device, Reading, SyncStatus

PERIODS = {'today': 0, '3d': 3, '7d': 7, '30d': 30}
# Ключи кэша живут дольше периода обновления, чтобы пользователь не попадал на «холодный» расчёт
CACHE_TTL = {'today': 30 * 60, '3d': 3 * 3600, '7d': 3 * 3600, '30d': 3 * 3600}


def normalize_period(period):
    return period if period in PERIODS else 'today'


def cache_key(period):
    return f'dashboard_stats_{period}'


def region_cache_key(period):
    return f'region_stats_{period}'


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


def _bounds(period):
    today = timezone.now().date()
    start_date = today - timedelta(days=PERIODS[period])
    return datetime.combine(start_date, datetime.min.time()), datetime.combine(today, datetime.max.time())


def compute_period_stats(period):
    """Считает за один проход данные дашборда и статистику по районам.

    Возвращает (data, region_stats). Главный запрос один: множество устройств с показаниями
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
        'id', 'region__id', 'region__name', 'model__device_type_str', 'model__device_type_id'))
    total_devices = len(devices)
    active_today = sum(1 for d in devices if d[0] in with_readings)
    active_percent = round((active_today / total_devices * 100), 1) if total_devices else 0

    manual_today = Reading.objects.filter(
        device__status='active', timestamp__gte=start_dt, timestamp__lte=end_dt,
    ).exclude(notes__in=AUTO_NOTES).count()

    producers, regions = {}, {}
    for dev_id, region_id, region_name, type_str, type_id in devices:
        is_active = dev_id in with_readings
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

    data = {
        'total_devices': total_devices,
        'active_today': active_today,
        'active_percent': active_percent,
        'manual_today': manual_today,
        'sync_statuses': list(SyncStatus.objects.all().order_by('-last_update')),
        'producer_stats': producer_stats,
        'period': period,
    }
    return data, region_stats


def store_period_stats(period):
    """Считает и кладёт в кэш обе статистики; возвращает (data, region_stats)."""
    period = normalize_period(period)
    data, region_stats = compute_period_stats(period)
    cache.set(cache_key(period), data, CACHE_TTL[period])
    cache.set(region_cache_key(period), region_stats, CACHE_TTL[period])
    return data, region_stats


def get_dashboard_stats(period, refresh=False):
    period = normalize_period(period)
    data = None if refresh else cache.get(cache_key(period))
    if data is None:
        data, _ = store_period_stats(period)
    return data


def get_region_stats(period):
    period = normalize_period(period)
    region_stats = cache.get(region_cache_key(period))
    if region_stats is None:
        _, region_stats = store_period_stats(period)
    return region_stats
