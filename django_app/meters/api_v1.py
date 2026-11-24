"""Внешний API v1 (только чтение): приборы и показания. Авторизация: Authorization: Bearer <токен>."""
import hashlib
import secrets
from datetime import datetime, timedelta
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .models import ApiToken, Device, Reading

DEFAULT_DEVICES_PAGE = 100
MAX_DEVICES_PAGE = 1000
DEFAULT_READINGS_PAGE = 500
MAX_READINGS_PAGE = 5000
MAX_SERIALS = 100
MAX_PERIOD_DAYS = 92
TOUCH_INTERVAL = timedelta(minutes=1)


def hash_key(key):
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def create_token(name):
    """Возвращает (ApiToken, ключ). Ключ показывается один раз."""
    key = 'ask_' + secrets.token_urlsafe(32)
    token = ApiToken.objects.create(name=name, prefix=key[:8], key_hash=hash_key(key))
    return token, key


def error(message, status=400):
    return JsonResponse({'error': message}, status=status, json_dumps_params={'ensure_ascii': False})


def token_required(view):
    @csrf_exempt
    @require_GET
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        header = request.META.get('HTTP_AUTHORIZATION', '')
        scheme, _, key = header.partition(' ')
        if scheme.lower() != 'bearer' or not key.strip():
            return error('Нужен заголовок Authorization: Bearer <токен>', 401)
        token = ApiToken.objects.filter(key_hash=hash_key(key.strip()), is_active=True).first()
        if token is None:
            return error('Токен неверный или отозван', 401)
        now = timezone.now()
        if token.last_used_at is None or now - token.last_used_at > TOUCH_INTERVAL:
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return view(request, *args, **kwargs)
    return wrapper


def int_param(request, name, default, maximum):
    raw = request.GET.get(name)
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f'Параметр {name} должен быть числом')
    if value < 1:
        raise ValueError(f'Параметр {name} должен быть не меньше 1')
    return min(value, maximum)


def parse_moment(raw, name, end_of_day=False):
    raw = raw.strip()
    for fmt, date_only in (('%Y-%m-%d', True), ('%Y-%m-%d %H:%M:%S', False), ('%Y-%m-%dT%H:%M:%S', False), ('%Y-%m-%d %H:%M', False)):
        try:
            value = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if date_only and end_of_day:
            value = value.replace(hour=23, minute=59, second=59)
        return value
    raise ValueError(f'Параметр {name}: ожидается YYYY-MM-DD или YYYY-MM-DD HH:MM:SS')


def serials_param(request):
    serials = []
    for part in (request.GET.get('serial') or '').split(','):
        part = part.strip()
        if part and part not in serials:
            serials.append(part)
    if len(serials) > MAX_SERIALS:
        raise ValueError(f'Не больше {MAX_SERIALS} серийных номеров за запрос')
    return serials


def device_dict(d):
    return {
        'serial_number': d.serial_number,
        'model': d.model.model_name if d.model else None,
        'status': d.status,
        'category': d.category,
        'phase': d.phase,
        'nominal_current': d.nominal_current,
        'region': d.region.name if d.region else None,
        'substation': d.substation.name if d.substation else None,
        'feeder': d.feeder.name if d.feeder else None,
        'tp': d.tp.name if d.tp else None,
        'tt_ratio': str(d.tt_ratio) if d.tt_ratio is not None else None,
        'tn_ratio': str(d.tn_ratio) if d.tn_ratio is not None else None,
    }


def reading_dict(r, serial):
    return {
        'serial_number': serial,
        'timestamp': r.timestamp.isoformat(timespec='seconds'),
        'direction': r.direction,
        'value': str(r.reading_value),
        'notes': r.notes,
    }


def device_qs():
    return Device.objects.select_related('model', 'region', 'substation', 'feeder', 'tp')


@token_required
def devices_list(request):
    try:
        page = int_param(request, 'page', 1, 10 ** 9)
        size = int_param(request, 'page_size', DEFAULT_DEVICES_PAGE, MAX_DEVICES_PAGE)
    except ValueError as e:
        return error(str(e))
    qs = device_qs().order_by('id')
    if request.GET.get('region'):
        qs = qs.filter(region__name=request.GET['region'])
    if request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])
    serials = None
    try:
        serials = serials_param(request)
    except ValueError as e:
        return error(str(e))
    if serials:
        qs = qs.filter(serial_number__in=serials)
    rows = list(qs[(page - 1) * size: page * size + 1])
    return JsonResponse({
        'items': [device_dict(d) for d in rows[:size]],
        'page': page, 'page_size': size, 'has_next': len(rows) > size,
    }, json_dumps_params={'ensure_ascii': False})


@token_required
def device_detail(request, serial_number):
    d = device_qs().filter(serial_number=serial_number.strip()).first()
    if d is None:
        return error('Прибор не найден', 404)
    return JsonResponse(device_dict(d), json_dumps_params={'ensure_ascii': False})


@token_required
def readings_list(request):
    try:
        serials = serials_param(request)
        if not serials:
            return error('Параметр serial обязателен (один или несколько номеров через запятую)')
        page = int_param(request, 'page', 1, 10 ** 9)
        size = int_param(request, 'page_size', DEFAULT_READINGS_PAGE, MAX_READINGS_PAGE)
        date_to = parse_moment(request.GET['date_to'], 'date_to', end_of_day=True) if request.GET.get('date_to') else datetime.now().replace(microsecond=0)
        date_from = parse_moment(request.GET['date_from'], 'date_from') if request.GET.get('date_from') else date_to - timedelta(days=1)
    except ValueError as e:
        return error(str(e))
    if date_from > date_to:
        return error('date_from позже date_to')
    if date_to - date_from > timedelta(days=MAX_PERIOD_DAYS):
        return error(f'Период не больше {MAX_PERIOD_DAYS} дней')
    direction = request.GET.get('direction')
    if direction and direction not in ('aplus', 'aminus'):
        return error('direction: aplus или aminus')

    devices = {d.id: d.serial_number for d in Device.objects.filter(serial_number__in=serials)}
    not_found = [s for s in serials if s not in devices.values()]
    qs = Reading.objects.filter(device_id__in=devices.keys(), timestamp__gte=date_from, timestamp__lte=date_to)
    if direction:
        qs = qs.filter(direction=direction)
    rows = list(qs.order_by('device_id', 'timestamp', 'direction')[(page - 1) * size: page * size + 1])
    return JsonResponse({
        'items': [reading_dict(r, devices[r.device_id]) for r in rows[:size]],
        'page': page, 'page_size': size, 'has_next': len(rows) > size,
        'date_from': date_from.isoformat(sep=' '), 'date_to': date_to.isoformat(sep=' '),
        'not_found': not_found,
    }, json_dumps_params={'ensure_ascii': False})


@token_required
def readings_latest(request):
    try:
        serials = serials_param(request)
    except ValueError as e:
        return error(str(e))
    if not serials:
        return error('Параметр serial обязателен (один или несколько номеров через запятую)')
    devices = {d.serial_number: d for d in Device.objects.filter(serial_number__in=serials)}
    items = []
    for serial in serials:
        d = devices.get(serial)
        if d is None:
            continue
        for direction in ('aplus', 'aminus'):
            r = Reading.objects.filter(device_id=d.id, direction=direction).order_by('-timestamp').first()
            if r:
                items.append(reading_dict(r, serial))
    return JsonResponse({
        'items': items,
        'not_found': [s for s in serials if s not in devices],
    }, json_dumps_params={'ensure_ascii': False})
