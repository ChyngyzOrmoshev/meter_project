from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.db.models import OuterRef, Subquery, Exists, Count, Q
from django.utils import timezone
from django.core.cache import cache
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.db import transaction
from datetime import datetime, timedelta
import csv
import re
import json
import openpyxl
import pandas as pd
import logging
import threading
from decimal import Decimal
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import subprocess
from .models import Device, Reading, SyncStatus, MeterModel, Region, Substation, Feeder, TransformerSubstation, DeviceGroup
from .relay_queue import supersede_tasks, inflight_tasks
from django.core.paginator import Paginator
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger


logger = logging.getLogger(__name__)

# ==========================================
# ГЛОБАЛЬНЫЙ МАППИНГ РАЙОНОВ
# ==========================================
REGION_MAPPING = {
    'Манас РЭС': '01 Манас',
    'Кок-Арт': '02 Кок-Арт',
    'Сузак ЭС': '03 Сузак',
    'Базар-Коргон РЭС': '04 Базар-Коргон',
    'Ноокен': '05 Ноокен',
    'Майлуу-Суу': '06 Майлуу-Суу',
    'Таш-Комур': '07 Таш-Комур',
    'Аксы РЭС': '08 Аксы',
    'Ала-Бука РЭС': '09 Ала-Бука',
    'Чаткал': '10 Чаткал',
    'Кара-Кол РЭС': '11 Кара-Куль',
    'Токтогул': '12 Токтогул',
    'ЖПЭС': 'ЖПЭС'
}
REVERSE_REGION_MAPPING = {v: k for k, v in REGION_MAPPING.items()}

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
        "8": "Others",
    }
    return mapping.get(code, code or "Неизвестно")

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
    period = request.GET.get('period', 'today')
    cache_key = f'dashboard_stats_{period}'
    data = cache.get(cache_key)
    if data is None or request.GET.get('refresh') == '1':
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

        total_devices = Device.objects.filter(status='active').count()

        active_device_ids = list(Reading.objects.filter(
            device__status='active',
            timestamp__gte=start_dt,
            timestamp__lte=end_dt
        ).values_list('device_id', flat=True).distinct())
        active_today = len(active_device_ids)
        active_percent = round((active_today / total_devices * 100), 1) if total_devices else 0

        auto_notes = [
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
        manual_today = Reading.objects.filter(
            device__status='active',
            timestamp__gte=start_dt,
            timestamp__lte=end_dt
        ).exclude(notes__in=auto_notes).count()

        grouped = Device.objects.filter(status='active').values(
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

        data = {
            'total_devices': total_devices,
            'active_today': active_today,
            'active_percent': active_percent,
            'manual_today': manual_today,
            'sync_statuses': list(SyncStatus.objects.all().order_by('-last_update')),
            'producer_stats': producer_stats,
            'period': period,
        }
        cache.set(cache_key, data, 600)
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
        messages.success(request, f"Тип ПУ удалена.")
        return redirect('models')
    return render(request, 'meters/confirm_delete.html', {'object': model, 'type': 'Тип ПУ'})

def producer_stats_table(request):
    period = request.GET.get('period', 'today')
    region_id = request.GET.get('region_id')
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


# @login_required
# @user_passes_test(is_allowed_user)
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
        # 'producers_list': producers_list,
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


# ===== ОТЧЁТ БАЛАНСА =====
from decimal import Decimal

@login_required
@user_passes_test(is_allowed_user)
def balance_report(request):
    from django.db import connection
    from datetime import datetime

    date_str = request.GET.get('date', datetime.now().strftime('%Y-%m-%d'))
    level = request.GET.get('level', 'tp')
    export = request.GET.get('export', '')

    try:
        report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = datetime.now().date()
        date_str = report_date.strftime('%Y-%m-%d')

    # Формируем запрос в зависимости от уровня
    if level == 'tp':
        query = """
            SELECT
                t.id AS object_id,
                t.name AS object_name,
                f.name AS parent_name,
                r.name AS region_name,
                hd.serial_number AS head_serial,
                hr.reading_value AS head_reading,
                COALESCE(SUM(cr.reading_value), 0) AS sum_children,
                (COALESCE(hr.reading_value, 0) - COALESCE(SUM(cr.reading_value), 0)) AS balance
            FROM transformer_substations t
            JOIN feeders f ON f.id = t.feeder_id
            JOIN substations s ON s.id = f.substation_id
            JOIN regions r ON r.id = s.region_id
            LEFT JOIN devices hd ON hd.id = t.head_meter_id
            LEFT JOIN readings hr ON hr.device_id = hd.id AND DATE(hr.timestamp) = %s
            LEFT JOIN devices cd ON cd.tp_id = t.id AND cd.id != t.head_meter_id
            LEFT JOIN readings cr ON cr.device_id = cd.id AND DATE(cr.timestamp) = %s
            GROUP BY t.id, t.name, f.name, r.name, hd.serial_number, hr.reading_value
            ORDER BY t.name
        """
        params = [report_date, report_date]
        columns = ['ТП', 'Фидер', 'Район', 'Головной счётчик', 'Показание', 'Сумма подчинённых', 'Баланс']
        header = ['object_name', 'parent_name', 'region_name', 'head_serial', 'head_reading', 'sum_children', 'balance']
    elif level == 'feeder':
        query = """
            SELECT
                f.id AS object_id,
                f.name AS object_name,
                s.name AS parent_name,
                r.name AS region_name,
                hd.serial_number AS head_serial,
                hr.reading_value AS head_reading,
                COALESCE(SUM(cr.reading_value), 0) AS sum_children,
                (COALESCE(hr.reading_value, 0) - COALESCE(SUM(cr.reading_value), 0)) AS balance
            FROM feeders f
            JOIN substations s ON s.id = f.substation_id
            JOIN regions r ON r.id = s.region_id
            LEFT JOIN devices hd ON hd.id = f.head_meter_id
            LEFT JOIN readings hr ON hr.device_id = hd.id AND DATE(hr.timestamp) = %s
            LEFT JOIN devices cd ON cd.feeder_id = f.id AND cd.id != f.head_meter_id
            LEFT JOIN readings cr ON cr.device_id = cd.id AND DATE(cr.timestamp) = %s
            GROUP BY f.id, f.name, s.name, r.name, hd.serial_number, hr.reading_value
            ORDER BY f.name
        """
        params = [report_date, report_date]
        columns = ['Фидер', 'Подстанция', 'Район', 'Головной счётчик', 'Показание', 'Сумма подчинённых', 'Баланс']
        header = ['object_name', 'parent_name', 'region_name', 'head_serial', 'head_reading', 'sum_children', 'balance']
    elif level == 'substation':
        query = """
            SELECT
                s.id AS object_id,
                s.name AS object_name,
                r.name AS parent_name,
                '' AS region_name,
                hd.serial_number AS head_serial,
                hr.reading_value AS head_reading,
                COALESCE(SUM(cr.reading_value), 0) AS sum_children,
                (COALESCE(hr.reading_value, 0) - COALESCE(SUM(cr.reading_value), 0)) AS balance
            FROM substations s
            JOIN regions r ON r.id = s.region_id
            LEFT JOIN devices hd ON hd.id = s.head_meter_id
            LEFT JOIN readings hr ON hr.device_id = hd.id AND DATE(hr.timestamp) = %s
            LEFT JOIN devices cd ON cd.substation_id = s.id AND cd.id != s.head_meter_id
            LEFT JOIN readings cr ON cr.device_id = cd.id AND DATE(cr.timestamp) = %s
            GROUP BY s.id, s.name, r.name, hd.serial_number, hr.reading_value
            ORDER BY s.name
        """
        params = [report_date, report_date]
        columns = ['Подстанция', 'Район', 'Головной счётчик', 'Показание', 'Сумма подчинённых', 'Баланс']
        header = ['object_name', 'parent_name', 'head_serial', 'head_reading', 'sum_children', 'balance']
    else:  # region
        query = """
            SELECT
                r.id AS object_id,
                r.name AS object_name,
                '' AS parent_name,
                '' AS region_name,
                hd.serial_number AS head_serial,
                hr.reading_value AS head_reading,
                COALESCE(SUM(cr.reading_value), 0) AS sum_children,
                (COALESCE(hr.reading_value, 0) - COALESCE(SUM(cr.reading_value), 0)) AS balance
            FROM regions r
            LEFT JOIN devices hd ON hd.id = r.head_meter_id
            LEFT JOIN readings hr ON hr.device_id = hd.id AND DATE(hr.timestamp) = %s
            LEFT JOIN devices cd ON cd.region_id = r.id AND cd.id != r.head_meter_id
            LEFT JOIN readings cr ON cr.device_id = cd.id AND DATE(cr.timestamp) = %s
            GROUP BY r.id, r.name, hd.serial_number, hr.reading_value
            ORDER BY r.name
        """
        params = [report_date, report_date]
        columns = ['Район', 'Головной счётчик', 'Показание', 'Сумма подчинённых', 'Баланс']
        header = ['object_name', 'head_serial', 'head_reading', 'sum_children', 'balance']

    with connection.cursor() as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()

    data = []
    for row in rows:
        item = {}
        for i, col in enumerate(header):
            val = row[i]
            if isinstance(val, Decimal):
                val = float(val)
            item[col] = val if val is not None else 0
        data.append(item)

    # Экспорт
    if export == 'excel':
        import openpyxl
        from openpyxl.styles import Font, Alignment
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = f"Баланс {report_date}"
        for col, title in enumerate(columns, 1):
            cell = ws.cell(row=1, column=col, value=title)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal='center')
        for row_idx, item in enumerate(data, 2):
            for col_idx, key in enumerate(header, 1):
                ws.cell(row=row_idx, column=col_idx, value=item.get(key, ''))
        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = max_length + 2
        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="balance_{report_date}_{level}.xlsx"'
        wb.save(response)
        return response

    if export == 'csv':
        import csv
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="balance_{report_date}_{level}.csv"'
        writer = csv.writer(response)
        writer.writerow(columns)
        for item in data:
            row = [item.get(key, '') for key in header]
            writer.writerow(row)
        return response

    levels = [
        ('tp', 'ТП', level == 'tp'),
        ('feeder', 'Фидер', level == 'feeder'),
        ('substation', 'Подстанция', level == 'substation'),
        ('region', 'Район', level == 'region'),
    ]

    context = {
        'data': data,
        'columns': columns,
        'header': header,
        'date': date_str,
        'level': level,
        'levels': levels,
        'report_date': report_date,
    }
    return render(request, 'meters/balance.html', context)

import subprocess
import threading
import logging
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

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


from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from .registration import register_devices_for_robot, ROBOT_CONFIGS


from django.utils import timezone
from datetime import datetime


# @login_required
# @user_passes_test(is_allowed_user)
def region_stats_table(request):
    period = request.GET.get('period', 'today')
    cache_key = f'region_stats_table_{period}'
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

        # Получаем все активные устройства
        devices = Device.objects.filter(status='active')

        # Всего устройств по районам
        totals_qs = devices.values('region__id', 'region__name').annotate(
            total=Count('id')
        ).order_by('region__name')

        # Устройства с показанием за период — быстрый запрос БЕЗ JOIN к devices,
        # используя только индекс по timestamp (JOIN с devices заставлял MySQL
        # перебирать по циклу каждое активное устройство отдельно, что было медленно)
        device_ids_with_readings = set(Reading.objects.filter(
            timestamp__gte=start_dt,
            timestamp__lte=end_dt
        ).values_list('device_id', flat=True).distinct())

        # Активных устройств по районам считаем в Python (быстрая проверка через set),
        # чтобы не передавать в SQL гигантский список IN(...) при широких диапазонах дат,
        # где почти все устройства попадают в выборку
        active_by_region = {}
        for device_id, region_id in devices.values_list('id', 'region__id'):
            if device_id in device_ids_with_readings:
                active_by_region[region_id] = active_by_region.get(region_id, 0) + 1

        # Преобразуем для шаблона
        stats = []
        for stat in totals_qs:
            total = stat['total']
            active = active_by_region.get(stat['region__id'], 0)
            offline = total - active
            percent = round((active / total * 100), 1) if total else 0
            stats.append({
                'region_id': stat['region__id'],
                'region_name': stat['region__name'] if stat['region__name'] else 'Без района',
                'total': total,
                'active': active,
                'offline': offline,
                'percent': percent,
            })

        html = render_to_string('meters/_region_table.html', {'region_stats': stats})
        cache.set(cache_key, html, 600)
    return HttpResponse(html)


from django.http import StreamingHttpResponse
import csv


from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render, get_object_or_404, redirect
from django.core.paginator import Paginator
from django.contrib import messages
from django.db.models import Q
from .models import CompareResult, Device, Region, Reading
import pandas as pd
import re


# @login_required
# @user_passes_test(is_allowed_user)
import json


from django.http import JsonResponse
from django.core.management import call_command
from django.contrib.auth.decorators import login_required


from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from django.http import HttpResponse


from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required


from django.http import JsonResponse
from django.views.decorators.http import require_POST
from .models import Device, ReadingFetchTask
import threading


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
        from django.db.models import Exists, OuterRef, Count, Case, When, IntegerField
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


from django.http import JsonResponse
from django.views.decorators.http import require_GET


# ========== БАЛАНС ==========

from .balance_calculator import (
    calculate_balance_for_substation,
    get_or_create_period,
    get_reading_for_date,
    get_coefficient_for_date,
)
from .models import (
    Device, Reading, SyncStatus, MeterModel, Region, 
    Substation, Feeder, TransformerSubstation, DeviceGroup,
    CompareResult, ReadingFetchTask,
    # Новые модели для баланса
    MeterConnection, CoefficientHistory, BalanceResult
)
from decimal import Decimal

@login_required
@user_passes_test(is_allowed_user)
def balance_readings(request):
    substation_id = request.GET.get('substation')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    # Если даты не заданы, используем 1-е число месяца и сегодня
    if not start_date_str:
        start_date = timezone.now().date().replace(day=1)
    else:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        except:
            start_date = timezone.now().date().replace(day=1)

    if not end_date_str:
        end_date = timezone.now().date()
    else:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except:
            end_date = timezone.now().date()

    substations = Substation.objects.filter(is_active=True).order_by('name')
    selected_substation = None
    data = []
    substations_with_selected = []

    if substation_id:
        selected_substation = get_object_or_404(Substation, id=substation_id)
        feeders = selected_substation.feeders.all().order_by('feeder_type', 'name')
        for feeder in feeders:
            if not feeder.head_meter:
                continue
            device = feeder.head_meter
            # Для каждого направления
            for direction in ['aplus', 'aminus']:
                dir_display = 'A+' if direction == 'aplus' else 'A-'
                # Получаем показания на начальную и конечную даты
                reading_start = get_reading_for_date(device, start_date, direction)
                reading_end = get_reading_for_date(device, end_date, direction)
                if reading_start is None or reading_end is None:
                    continue
                # Разность
                diff = reading_end - reading_start
                # Коэффициент на конечную дату
                coeff = get_coefficient_for_date(device, end_date)
                # Объём
                energy = diff * coeff
                data.append({
                    'feeder': feeder,
                    'device': device,
                    'direction': dir_display,
                    'reading_start': reading_start,
                    'reading_end': reading_end,
                    'diff': diff,
                    'coefficient': coeff,
                    'energy': energy,
                })

    for sub in substations:
        selected = (selected_substation and selected_substation.id == sub.id)
        substations_with_selected.append((sub, selected))

    # Вычисляем итоги
    total_input = sum(item['energy'] for item in data if item['feeder'].feeder_type == 'input')
    total_output = sum(item['energy'] for item in data if item['feeder'].feeder_type == 'output')
    imbalance = total_input - total_output
    imbalance_percent = (imbalance / total_input * 100) if total_input else Decimal(0)

    context = {
        'substations_with_selected': substations_with_selected,
        'selected_substation': selected_substation,
        'start_date': start_date,
        'end_date': end_date,
        'data': data,
        'total_input': total_input,
        'total_output': total_output,
        'imbalance': imbalance,
        'imbalance_percent': imbalance_percent,
    }
    return render(request, 'meters/balance_readings.html', context)

@login_required
@user_passes_test(is_allowed_user)
def balance_summary(request):
    region_id = request.GET.get('region')
    year = int(request.GET.get('year', timezone.now().year))
    month = int(request.GET.get('month', timezone.now().month))

    substations = Substation.objects.filter(is_active=True)
    if region_id:
        substations = substations.filter(region_id=region_id)

    results = []
    total_decades = {
        'decade1': {'input': 0, 'output': 0, 'imbalance': 0},
        'decade2': {'input': 0, 'output': 0, 'imbalance': 0},
        'decade3': {'input': 0, 'output': 0, 'imbalance': 0},
        'month': {'input': 0, 'output': 0, 'imbalance': 0, 'percent': 0},
    }

    for sub in substations:
        balance = calculate_balance_for_substation(sub, year, month)
        results.append({
            'substation': sub,
            'decades': balance,
        })
        for key in ['decade1', 'decade2', 'decade3', 'month']:
            total_decades[key]['input'] += balance[key]['input']
            total_decades[key]['output'] += balance[key]['output']
            total_decades[key]['imbalance'] += balance[key]['imbalance']
        if total_decades['month']['input'] > 0:
            total_decades['month']['percent'] = (total_decades['month']['imbalance'] / total_decades['month']['input']) * 100

    regions = Region.objects.all().order_by('name')
    regions_with_selected = [(r, str(r.id) == region_id) for r in regions]

    context = {
        'regions_with_selected': regions_with_selected,
        'results': results,
        'year': year,
        'month': month,
        'total_decades': total_decades,
    }
    return render(request, 'meters/balance_summary.html', context)


@login_required
@user_passes_test(is_allowed_user)
def balance_report_all(request):
    year = int(request.GET.get('year', timezone.now().year))
    month = int(request.GET.get('month', timezone.now().month))

    substations = Substation.objects.filter(is_active=True)
    data = []
    total_input = 0
    total_output = 0
    total_imbalance = 0

    for sub in substations:
        period = get_or_create_period(sub, year, month, 'month')
        try:
            balance = BalanceResult.objects.get(substation=sub, period=period)
        except BalanceResult.DoesNotExist:
            calculate_balance_for_substation(sub, year, month)  # расчёт
            balance = BalanceResult.objects.get(substation=sub, period=period)

        data.append({
            'substation': sub,
            'input': balance.input_energy,
            'output': balance.output_energy,
            'imbalance': balance.imbalance_kwh,
            'percent': balance.imbalance_percent,
        })
        total_input += balance.input_energy
        total_output += balance.output_energy
        total_imbalance += balance.imbalance_kwh

    total_percent = (total_imbalance / total_input * 100) if total_input else 0

    chart_labels = [d['substation'].name for d in data]
    chart_imbalance = [float(d['imbalance']) for d in data]
    chart_input = [float(d['input']) for d in data]

    context = {
        'data': data,
        'year': year,
        'month': month,
        'chart_labels': json.dumps(chart_labels),
        'chart_imbalance': json.dumps(chart_imbalance),
        'chart_input': json.dumps(chart_input),
        'total_input': total_input,
        'total_output': total_output,
        'total_imbalance': total_imbalance,
        'total_percent': total_percent,
    }
    return render(request, 'meters/balance_report_all.html', context)


from decimal import Decimal
from .models import CoefficientHistory

@login_required
@user_passes_test(is_allowed_user)
def device_coefficients(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    
    if request.method == 'POST':
        tt = request.POST.get('tt_ratio')
        tn = request.POST.get('tn_ratio')
        start_date = request.POST.get('start_date')
        
        if not tt or not tn:
            messages.error(request, 'Введите оба коэффициента (ТТ и ТН)')
            return redirect('device_coefficients', device_id=device.id)
        
        try:
            tt = Decimal(tt)
            tn = Decimal(tn)
        except:
            messages.error(request, 'Введите корректные числовые значения')
            return redirect('device_coefficients', device_id=device.id)
        
        if not start_date:
            start_date = timezone.now().date()
        else:
            try:
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
            except:
                start_date = timezone.now().date()
        
        # Закрываем предыдущую активную запись (если есть)
        active_history = CoefficientHistory.objects.filter(
            device=device,
            end_date__isnull=True
        ).first()
        if active_history:
            active_history.end_date = start_date
            active_history.save()
        
        # Создаём новую запись
        CoefficientHistory.objects.create(
            device=device,
            tt_ratio=tt,
            tn_ratio=tn,
            start_date=start_date,
            end_date=None
        )
        
        # Обновляем текущие значения в Device
        device.tt_ratio = tt
        device.tn_ratio = tn
        device.save()
        
        messages.success(request, f'Коэффициенты обновлены (ТТ={tt}, ТН={tn}, с {start_date})')
        return redirect('device_coefficients', device_id=device.id)
    
    # GET – показываем форму и историю
    history = CoefficientHistory.objects.filter(device=device).order_by('-start_date')
    
    # ВЫЧИСЛЯЕМ КОЭФФИЦИЕНТ ДЛЯ КАЖДОЙ ЗАПИСИ
    history_with_calc = []
    for item in history:
        # Добавляем вычисленное поле calc_coeff
        item.calc_coeff = item.tt_ratio * item.tn_ratio
        history_with_calc.append(item)
    
    context = {
        'device': device,
        'history': history_with_calc,  # передаём уже с вычисленным коэффициентом
    }
    return render(request, 'meters/device_coefficients.html', context)
    
import subprocess
from django.http import FileResponse
import os
from django.http import FileResponse, HttpResponseForbidden

import json
from datetime import datetime


from .sunrise_service import SunRiseService
from .models import RelayCommandHistory
from django.views.decorators.http import require_POST
import json
