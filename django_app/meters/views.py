import csv
import logging
import re
import subprocess
import threading
from datetime import datetime, timedelta

import openpyxl
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.db.models import Case, Count, IntegerField, Exists, OuterRef, Q, When
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from openpyxl.styles import Alignment, Font

from .models import Device, MeterModel, Reading, Region, SyncStatus

logger = logging.getLogger(__name__)


# ==========================================
# ГЛОБАЛЬНЫЙ МАППИНГ РАЙОНОВ
# ==========================================
from .region_mapping import REGION_MAPPING, REVERSE_REGION_MAPPING  # единый маппинг районов (1С -> БД)

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def is_allowed_user(user):
    return user.is_staff or user.groups.filter(name='operator').exists()

def get_producer_display(code, type_id):
    """Преобразует код производителя и ID типа в человекочитаемое название."""
    if code == "SX":
        if type_id == "18":
            return "Sanxing_new"
        elif type_id == "22":
            return "Sanxing_old"
        else:
            return "Sanxing (unknown)"
    mapping = {
        "RS": "RiseSun",
        "EM": "Energomera",
        "SR": "SunRise",
        "UK": "Hexing KUK",
        "ST": "Star",
        "HX": "Hexing",
        "IM": "InheMeter",
        "KF": "KAIFA",
        "8": "Others",
    }
    return mapping.get(code, code or "Неизвестно")

def producer_model_ids(name):
    """ID моделей ПУ, которые на дашборде показываются под названием производителя `name`."""
    pairs = MeterModel.objects.values_list('id', 'device_type_str', 'device_type_id')
    return [mid for mid, code, type_id in pairs if get_producer_display(code or "", type_id or "") == name]

def normalize_model_name(model):
    """Приводит название модели к каноническому виду для сравнения."""
    if not model:
        return ''
    # Удаляем лишние пробелы, приводим к нижнему регистру, убираем точки/дефисы/пробелы
    return re.sub(r'[^a-z0-9]', '', model.lower().strip())

def compare_models(model_1c, model_db):
    """
    Возвращает тип совпадения: 'exact', 'partial', 'none'
    """
    if not model_1c or not model_db:
        return 'none'
    norm_1c = normalize_model_name(model_1c)
    norm_db = normalize_model_name(model_db)
    if norm_1c == norm_db:
        return 'exact'
    if norm_1c in norm_db or norm_db in norm_1c:
        return 'partial'
    # Дополнительно: удаляем суффиксы типа -S31.543 и сравниваем
    # Удаляем всё после дефиса, если есть
    base_1c = norm_1c.split('-')[0] if '-' in norm_1c else norm_1c
    base_db = norm_db.split('-')[0] if '-' in norm_db else norm_db
    if base_1c == base_db and len(base_1c) > 3:
        return 'partial'
    return 'none'

def find_device(serial_number, model=None):
    """
    Поиск устройства по серийному номеру.
    Сначала точное совпадение, затем по суффиксу (последние символы).
    Возвращает объект Device или None.
    """
    if not serial_number:
        return None
    sn = str(serial_number).strip()
    qs = Device.objects.filter(serial_number=sn)
    if model:
        qs = qs.filter(model=model)
    if qs.exists():
        return qs.first()
    candidates = Device.objects.filter(serial_number__endswith=sn)
    if model:
        candidates = candidates.filter(model=model)
    if candidates.count() == 1:
        return candidates.first()
    return None

# ===== АВТОРИЗАЦИЯ =====
def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect('dashboard')
        else:
            messages.error(request, 'Неверный логин или пароль')
    return render(request, 'meters/login.html')

def logout_view(request):
    logout(request)
    return redirect('dashboard')

# ===== ДАШБОРД =====
def dashboard(request):
    from .dashboard_stats import get_dashboard_stats
    data = get_dashboard_stats(request.GET.get('period', 'today'), refresh=request.GET.get('refresh') == '1')
    return render(request, 'meters/dashboard.html', data)


# ===== МОДЕЛИ =====
@login_required
@user_passes_test(is_allowed_user)
def models(request):
    models_list = MeterModel.objects.all()
    context = {'models': models_list}
    return render(request, 'meters/models.html', context)

@login_required
@user_passes_test(is_allowed_user)
def add_model(request):
    if request.method == 'POST':
        try:
            model = MeterModel(
                catalog_code=request.POST.get('catalog_code'),
                model_name=request.POST.get('model_name'),
                digit_capacity=request.POST.get('digit_capacity'),
                phases=request.POST.get('phases'),
                nominal_current=request.POST.get('nominal_current'),
                nominal_voltage=request.POST.get('nominal_voltage'),
                system_type=request.POST.get('system_type'),
                period=request.POST.get('period'),
                device_type_id=request.POST.get('device_type_id'),
                device_type_str=request.POST.get('device_type_str'),
            )
            model.save()
            messages.success(request, f"Тип ПУ {model.model_name} добавлена.")
        except Exception as e:
            messages.error(request, f"Ошибка: {e}")
        return redirect('models')
    return render(request, 'meters/add_model.html')

@login_required
@user_passes_test(is_allowed_user)
def edit_model(request, model_id):
    model = get_object_or_404(MeterModel, id=model_id)
    if request.method == 'POST':
        try:
            model.catalog_code = request.POST.get('catalog_code')
            model.model_name = request.POST.get('model_name')
            model.digit_capacity = request.POST.get('digit_capacity')
            model.phases = request.POST.get('phases')
            model.nominal_current = request.POST.get('nominal_current')
            model.nominal_voltage = request.POST.get('nominal_voltage')
            model.system_type = request.POST.get('system_type')
            model.period = request.POST.get('period')
            model.device_type_id = request.POST.get('device_type_id')
            model.device_type_str = request.POST.get('device_type_str')
            model.save()
            messages.success(request, f"Тип ПУ {model.model_name} обновлена.")
        except Exception as e:
            messages.error(request, f"Ошибка: {e}")
        return redirect('models')
    context = {'model': model}
    return render(request, 'meters/edit_model.html', context)

@login_required
@user_passes_test(is_allowed_user)
def delete_model(request, model_id):
    model = get_object_or_404(MeterModel, id=model_id)
    if request.method == 'POST':
        model.delete()
        messages.success(request, "Тип ПУ удалена.")
        return redirect('models')
    return render(request, 'meters/confirm_delete.html', {'object': model, 'type': 'Тип ПУ'})

# Доступ без входа (гостевая страница)
def producer_stats_table(request):
    period = request.GET.get('period', 'today')
    region_id = request.GET.get('region_id')
    if not region_id:
        # Все районы: берём прогретый кэш дашборда (его считает планировщик)
        from .dashboard_stats import get_dashboard_stats, normalize_period
        period = normalize_period(period)
        stats = get_dashboard_stats(period)['producer_stats']
        return HttpResponse(render_to_string('meters/_producer_table.html', {'producer_stats': stats, 'period': period}))
    cache_key = f'producer_stats_table_{period}_{region_id}'
    html = cache.get(cache_key)
    if html is None:
        today = timezone.now().date()
        if period == 'today':
            start_date = today
        elif period == '3d':
            start_date = today - timedelta(days=3)
        elif period == '7d':
            start_date = today - timedelta(days=7)
        elif period == '30d':
            start_date = today - timedelta(days=30)
        else:
            start_date = today

        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(today, datetime.max.time())

        devices = Device.objects.filter(status='active')
        if region_id:
            devices = devices.filter(region_id=region_id)

        active_device_ids = list(Reading.objects.filter(
            device__status='active',
            timestamp__gte=start_dt,
            timestamp__lte=end_dt
        ).values_list('device_id', flat=True).distinct())

        grouped = devices.values(
            'model__device_type_str', 'model__device_type_id'
        ).annotate(
            total=Count('id'),
            active=Count('id', filter=Q(id__in=active_device_ids)),
        )

        producer_stats_dict = {}
        for row in grouped:
            code = row['model__device_type_str'] or ""
            type_id = row['model__device_type_id'] or ""
            display_name = get_producer_display(code, type_id)
            entry = producer_stats_dict.setdefault(display_name, {'total': 0, 'active': 0})
            entry['total'] += row['total']
            entry['active'] += row['active']

        producer_stats = []
        for name, stats in producer_stats_dict.items():
            percent = round((stats['active'] / stats['total'] * 100), 1) if stats['total'] else 0
            producer_stats.append({
                'producer': name,
                'total': stats['total'],
                'active': stats['active'],
                'percent': percent,
            })
        producer_stats.sort(key=lambda x: x['producer'])

        html = render_to_string('meters/_producer_table.html', {'producer_stats': producer_stats})
        cache.set(cache_key, html, 600)
    return HttpResponse(html)


# Доступ без входа (гостевая страница)
def device_types_report(request):
    period = request.GET.get('period', '7d')
    specific_date = request.GET.get('date', '')
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    region_filter = request.GET.get('region', '')
    category_filter = request.GET.get('category', '')
    online_filter = request.GET.get('online', '')
    export = request.GET.get('export', '')

    today = timezone.now().date()

    # --- Определяем окно «онлайн» ---
    if specific_date:
        try:
            target_date = datetime.strptime(specific_date, '%Y-%m-%d').date()
        except ValueError:
            target_date = today
        start_dt = datetime.combine(target_date, datetime.min.time())
        end_dt = start_dt + timedelta(days=1)
    else:
        if period == 'today':
            start_date = today
        elif period == '3d':
            start_date = today - timedelta(days=3)
        elif period == '7d':
            start_date = today - timedelta(days=7)
        elif period == '30d':
            start_date = today - timedelta(days=30)
        else:
            start_date = today - timedelta(days=7)
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(today + timedelta(days=1), datetime.min.time())

    # --- Базовый queryset с фильтрами ---
    devices_qs = Device.objects.select_related('model')
    if status_filter:
        devices_qs = devices_qs.filter(status=status_filter)
    if region_filter:
        devices_qs = devices_qs.filter(region_id=region_filter)
    if category_filter == '__none__':
        devices_qs = devices_qs.filter(category__isnull=True)
    elif category_filter:
        devices_qs = devices_qs.filter(category=category_filter)
    if producer_filter:
        devices_qs = devices_qs.filter(model__device_type_str=producer_filter)
    if model_filter:
        devices_qs = devices_qs.filter(model_id=model_filter)

    # --- Множество id «онлайн» (есть показание за окно) ---
    if not specific_date:
        # период из прогрева кэша (неизвестный период, как раньше, = 7 дней)
        from .dashboard_stats import PERIODS, get_with_reading_ids
        online_ids = get_with_reading_ids(period if period in PERIODS else '7d')
    else:
        online_ids = set(Reading.objects.filter(
            timestamp__gte=start_dt,
            timestamp__lt=end_dt
        ).values_list('device_id', flat=True).distinct())

    # --- Группировка по модели ---
    agg = {}
    for dev in devices_qs.values(
        'id', 'model_id',
        'model__model_name', 'model__manufacturer',
        'model__device_type_str', 'model__device_type_id',
        'model__nominal_current', 'model__phases',
    ):
        mid = dev['model_id']
        if mid not in agg:
            agg[mid] = {
                'model_name': dev['model__model_name'] or 'Без модели',
                'manufacturer': dev['model__manufacturer'] or '',
                'device_type_str': dev['model__device_type_str'] or '',
                'device_type_id': dev['model__device_type_id'] or '',
                'nominal_current': dev['model__nominal_current'] or '',
                'phases': dev['model__phases'],
                'total': 0,
                'online': 0,
            }
        agg[mid]['total'] += 1
        if dev['id'] in online_ids:
            agg[mid]['online'] += 1

    models_list = []
    for mid, d in agg.items():
        offline = d['total'] - d['online']
        percent = round((d['online'] / d['total']) * 100, 1) if d['total'] else 0
        models_list.append({
            'id': mid,
            'producer_display': get_producer_display(d['device_type_str'], d['device_type_id']),
            'model_name': d['model_name'],
            'manufacturer': d['manufacturer'],
            'nominal_current': d['nominal_current'],
            'phases': d['phases'],
            'total': d['total'],
            'online': d['online'],
            'offline': offline,
            'percent': percent,
        })

    # Фильтр «Только онлайн / оффлайн» на уровне модели
    if online_filter == 'yes':
        models_list = [m for m in models_list if m['online'] > 0]
    elif online_filter == 'no':
        models_list = [m for m in models_list if m['offline'] > 0]

    models_list.sort(key=lambda x: (x['producer_display'] or '', x['model_name'] or ''))

    total_devices = sum(m['total'] for m in models_list)
    total_online = sum(m['online'] for m in models_list)
    total_offline = total_devices - total_online
    total_percent = round((total_online / total_devices) * 100, 1) if total_devices else 0

    # --- Экспорт CSV ---
    if export == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition'] = f'attachment; filename="device_types_{today}.csv"'
        writer = csv.writer(response, delimiter=';')
        writer.writerow(['Производитель', 'Тип ПУ', 'Ток', 'Фазность',
                         'Всего', 'Онлайн', 'Оффлайн', '% чтения'])
        for m in models_list:
            writer.writerow([
                m['producer_display'], m['model_name'],
                m['nominal_current'], m['phases'] or '',
                m['total'], m['online'], m['offline'], f"{m['percent']}%",
            ])
        writer.writerow([])
        writer.writerow(['ИТОГО', '', '', '', total_devices, total_online, total_offline, f"{total_percent}%"])
        return response

    # --- Экспорт XLSX ---
    if export == 'xlsx':
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Типы ПУ"
        headers = ['Производитель', 'Тип ПУ', 'Ток', 'Фазность',
                   'Всего', 'Онлайн', 'Оффлайн', '% чтения']
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal='center')
        for ridx, m in enumerate(models_list, 2):
            ws.cell(row=ridx, column=1, value=m['producer_display'])
            ws.cell(row=ridx, column=2, value=m['model_name'])
            ws.cell(row=ridx, column=3, value=m['nominal_current'])
            ws.cell(row=ridx, column=4, value=m['phases'])
            ws.cell(row=ridx, column=5, value=m['total'])
            ws.cell(row=ridx, column=6, value=m['online'])
            ws.cell(row=ridx, column=7, value=m['offline'])
            ws.cell(row=ridx, column=8, value=f"{m['percent']}%")
        last = len(models_list) + 2
        ws.cell(row=last, column=1, value='ИТОГО').font = Font(bold=True)
        ws.cell(row=last, column=5, value=total_devices)
        ws.cell(row=last, column=6, value=total_online)
        ws.cell(row=last, column=7, value=total_offline)
        ws.cell(row=last, column=8, value=f"{total_percent}%")
        for col in ws.columns:
            w = max((len(str(c.value)) for c in col if c.value), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(w + 2, 40)
        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="device_types_{today}.xlsx"'
        wb.save(response)
        return response

    # --- Списки для фильтров ---
    codes = MeterModel.objects.values_list('device_type_str', flat=True).distinct().order_by('device_type_str')
    producers_with_selected = []
    for code in codes:
        display = get_producer_display(code, '')
        if display == 'Неизвестно' and code:
            display = code
        producers_with_selected.append((display, code, code == producer_filter))

    models_with_selected = [
        (m, str(m.id) == model_filter)
        for m in MeterModel.objects.all().order_by('model_name')
    ]

    statuses_with_selected = [
        (v, l, v == status_filter)
        for v, l in [('active', 'Активен'), ('repair', 'Ремонт'),
                     ('inactive', 'Отключен'), ('temporary_off', 'Временно отключен')]
    ]

    regions_with_selected = [
        (r, str(r.id) == region_filter)
        for r in Region.objects.all().order_by('name')
    ]

    categories_with_selected = [(c[0], c[1], c[0] == category_filter) for c in Device.CATEGORY_CHOICES]
    categories_with_selected.insert(0, ('', 'Все', category_filter == ''))
    categories_with_selected.append(('__none__', 'Не указано', category_filter == '__none__'))

    online_choices = [
        ('', 'Все', online_filter == ''),
        ('yes', 'Только онлайн', online_filter == 'yes'),
        ('no', 'Только оффлайн', online_filter == 'no'),
    ]

    periods = [
        ('today', 'Сегодня', period == 'today' and not specific_date),
        ('3d', '3 дня', period == '3d' and not specific_date),
        ('7d', '7 дней', period == '7d' and not specific_date),
        ('30d', '30 дней', period == '30d' and not specific_date),
    ]

    context = {
        'period': period,
        'specific_date': specific_date,
        'periods': periods,
        'models_list': models_list,
        'total_devices': total_devices,
        'total_online': total_online,
        'total_offline': total_offline,
        'total_percent': total_percent,
        'producer_filter': producer_filter,
        'model_filter': model_filter,
        'status_filter': status_filter,
        'region_filter': region_filter,
        'category_filter': category_filter,
        'online_filter': online_filter,
        'producers_with_selected': producers_with_selected,
        'models_with_selected': models_with_selected,
        'statuses_with_selected': statuses_with_selected,
        'regions_with_selected': regions_with_selected,
        'categories_with_selected': categories_with_selected,
        'online_choices': online_choices,
    }
    return render(request, 'meters/device_types_report.html', context)


@login_required
@user_passes_test(lambda u: u.is_staff)
@csrf_exempt
def restart_robot(request, robot_name):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    robot_config = {
        'RiseSun': 'sync_risesun_api',
        'SunRise': 'sync_sanrise',
        'cEnergo': 'sync_mssql',
        'Hexing_KUK': 'sync_hexing',
        'Hexing_POP': 'sync_hxn',
        'Sanxing_old': 'sync_sanxing',
        'Sanxing_new_100A': 'sync_website',
        'Sanxing_new_5A': 'sync_website_period1',
        'Star': 'sync_star',
        'Inhemeter': 'sync_inhemeter',
        'Kaifa': 'sync_kaifa',
        'RelayWorker': 'process_relay_tasks',
    }

    command = robot_config.get(robot_name)
    if not command:
        return JsonResponse({'error': f'Unknown robot: {robot_name}'}, status=400)

    def run_command():
        try:
            subprocess.Popen(
                ['python', 'manage.py', command, '--once', '--verbose'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd='/app',
                start_new_session=True,   # ← отвязываем от Django, чтобы не было зомби
            )
            logger.info(f"Manual restart of {robot_name} initiated")
        except Exception as e:
            logger.error(f"Manual restart of {robot_name} failed: {e}")

    thread = threading.Thread(target=run_command)
    thread.daemon = True
    thread.start()

    return JsonResponse({'message': f'Robot {robot_name} restart initiated'})


# Доступ без входа (гостевая страница)
def region_stats_table(request):
    from .dashboard_stats import get_region_stats, normalize_period
    period = normalize_period(request.GET.get('period', 'today'))
    stats = get_region_stats(period)
    return HttpResponse(render_to_string('meters/_region_table.html', {'region_stats': stats, 'period': period}))


# Доступ без входа (гостевая страница)
def category_cards(request):
    from .dashboard_stats import get_category_cards, normalize_period
    period = normalize_period(request.GET.get('period', 'today'))
    return HttpResponse(render_to_string('meters/_category_cards.html', {'cards': get_category_cards(period), 'period': period}))


# Доступ без входа (гостевая страница)
def category_stats_table(request):
    """Возвращает HTML-таблицу со статистикой по категориям для выбранного района."""
    region_id = request.GET.get('region_id')
    period = request.GET.get('period', 'today')
    cache_key = f'category_stats_{period}_{region_id}'
    html = cache.get(cache_key)
    if html is None:
        today = timezone.now().date()
        if period == 'today':
            start_date = today
        elif period == '3d':
            start_date = today - timedelta(days=3)
        elif period == '7d':
            start_date = today - timedelta(days=7)
        elif period == '30d':
            start_date = today - timedelta(days=30)
        else:
            start_date = today

        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(today, datetime.max.time())

        devices = Device.objects.filter(status='active')
        if region_id:
            devices = devices.filter(region_id=region_id)

        # Аннотируем has_reading
        readings_subquery = Reading.objects.filter(
            device=OuterRef('pk'),
            timestamp__gte=start_dt,
            timestamp__lte=end_dt
        ).values('device_id').annotate(cnt=Count('id')).values('cnt')
        devices = devices.annotate(
            has_reading=Exists(readings_subquery)
        )

        # Группируем по категориям
        category_stats = devices.values('category').annotate(
            total=Count('id'),
            active=Count(Case(When(has_reading=True, then=1), output_field=IntegerField()))
        ).order_by('category')

        # Преобразуем для шаблона
        stats = []
        for stat in category_stats:
            cat = stat['category']
            total = stat['total']
            active = stat['active']
            percent = round((active / total * 100), 1) if total else 0
            stats.append({
                'category': cat,
                'category_display': dict(Device.CATEGORY_CHOICES).get(cat, cat or 'Не указана'),
                'total': total,
                'active': active,
                'percent': percent,
            })

        html = render_to_string('meters/_category_table.html', {'category_stats': stats})
        cache.set(cache_key, html, 600)
    return HttpResponse(html)
