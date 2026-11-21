"""Общие хелперы для страниц-отчётов со списками приборов.

Тяжёлые вычисления (последнее показание, «есть ли показание за 7 дней») считаются
только для приборов текущей страницы, а не для всех 137 тысяч: иначе MySQL считает
подзапрос для каждой строки ещё до сортировки и LIMIT.
"""
from datetime import timedelta

from django.db.models import Exists, OuterRef, Subquery
from django.utils import timezone

from .models import Reading


def annotate_reading_flags(qs):
    """Добавляет last_reading_date и has_recent_reading (показание за 7 дней)."""
    seven_days_ago = timezone.now() - timedelta(days=7)
    last = Reading.objects.filter(device=OuterRef('pk')).order_by('-timestamp').values('timestamp')[:1]
    return qs.annotate(
        last_reading_date=Subquery(last),
        has_recent_reading=Exists(Reading.objects.filter(device=OuterRef('pk'), timestamp__gte=seven_days_ago)),
    )


def page_devices(qs, page, per_page, annotated=False):
    """Приборы страницы по serial_number; флаги чтения считаются только для них."""
    start, end = (page - 1) * per_page, page * per_page
    if annotated:
        return list(qs.order_by('serial_number')[start:end])
    ids = list(qs.order_by('serial_number').values_list('id', flat=True)[start:end])
    order = {pk: i for i, pk in enumerate(ids)}
    rows = annotate_reading_flags(qs.model.objects.select_related('model').filter(pk__in=ids))
    return sorted(rows, key=lambda d: order[d.id])


def window_page_range(page, total_pages):
    """Номера страниц для пагинатора: первая, последняя и окно вокруг текущей.

    Шаблоны сами рисуют «…» у границ окна (page±3), поэтому возвращаем эти номера
    и не перебираем тысячи страниц.
    """
    pages = {1, total_pages}
    pages.update(p for p in range(page - 3, page + 4) if 1 <= p <= total_pages)
    return sorted(pages)


def filter_by_period(qs, period, missing):
    """Приборы без показаний (missing=True) или с показаниями за период.

    Список из кэша прогрева берётся в том виде, в каком он короче: «IN» на
    100+ тысяч id работает в разы медленнее, чем на 10 тысяч.
    """
    from .dashboard_stats import get_no_reading_ids, get_with_reading_ids
    no_ids = get_no_reading_ids(period)
    with_ids = get_with_reading_ids(period)
    if len(no_ids) <= len(with_ids):
        return qs.filter(id__in=no_ids) if missing else qs.exclude(id__in=no_ids)
    return qs.exclude(id__in=with_ids) if missing else qs.filter(id__in=with_ids)
