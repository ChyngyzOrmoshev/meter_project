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

# ===== УСТРОЙСТВА (Реестр) – ОБНОВЛЁННАЯ ВЕРСИЯ =====
@login_required
@user_passes_test(is_allowed_user)
def devices(request):
    # Получаем фильтры из GET
    search = request.GET.get('search', '')
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    region_filter = request.GET.get('region', '')
    substation_filter = request.GET.get('substation', '')
    feeder_filter = request.GET.get('feeder', '')
    tp_filter = request.GET.get('tp', '')
    group_filter = request.GET.get('group', '')
    category_filter = request.GET.get('category', '')

    # Все группы для выпадающих списков (в модалку)
    groups_all = DeviceGroup.objects.all().order_by('name')

    # Пагинация
    page = int(request.GET.get('page', 1))
    per_page = 10

    # Базовый запрос с предзагрузкой связей
    qs = Device.objects.select_related('model', 'region', 'substation', 'feeder', 'tp').prefetch_related('groups')

    if search:
        qs = qs.filter(Q(serial_number__icontains=search) | Q(model__model_name__icontains=search))
    if producer_filter:
        qs = qs.filter(model__device_type_str=producer_filter)
    if model_filter:
        qs = qs.filter(model__id=model_filter)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if region_filter:
        qs = qs.filter(region_id=region_filter)
    if substation_filter:
        qs = qs.filter(substation_id=substation_filter)
    if feeder_filter:
        qs = qs.filter(feeder_id=feeder_filter)
    if tp_filter:
        qs = qs.filter(tp_id=tp_filter)
    if group_filter:
        qs = qs.filter(groups__id=group_filter)
    if category_filter == '__none__':
        qs = qs.filter(category__isnull=True)
    elif category_filter:
        qs = qs.filter(category=category_filter)

    from django.db.models import Exists, OuterRef
    from django.utils import timezone
    from datetime import timedelta
    seven_days_ago = timezone.now() - timedelta(days=7)
    qs = qs.annotate(
        has_recent_reading=Exists(
            Reading.objects.filter(
                device=OuterRef('pk'),
                timestamp__gte=seven_days_ago
            )
        )
    )

    total = qs.count()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
    devices_list = qs.order_by('serial_number')[(page-1)*per_page:page*per_page]
    page_range = range(1, total_pages + 1)

    # Получаем списки для выпадающих фильтров
    codes = MeterModel.objects.values_list('device_type_str', flat=True).distinct().order_by('device_type_str')
    producers_with_selected = []
    for code in codes:
        display = get_producer_display(code, '')
        if display == "Неизвестно" and code:
            display = code
        selected = (code == producer_filter)
        producers_with_selected.append((display, code, selected))

    models_list = MeterModel.objects.all().order_by('model_name')
    models_with_selected = [(m, str(m.id) == model_filter) for m in models_list]

    statuses = [
        ('active', 'Активен'),
        ('repair', 'Ремонт'),
        ('inactive', 'Отключен'),
        ('temporary_off', 'Временно отключен'),
    ]
    statuses_with_selected = [(value, label, value == status_filter) for value, label in statuses]

    # Списки для иерархии
    regions_with_selected = [(r, str(r.id) == region_filter) for r in Region.objects.all().order_by('name')]
    substations_with_selected = [(s, str(s.id) == substation_filter) for s in Substation.objects.all().order_by('name')]
    feeders_with_selected = [(f, str(f.id) == feeder_filter) for f in Feeder.objects.all().order_by('name')]
    tps_with_selected = [(t, str(t.id) == tp_filter) for t in TransformerSubstation.objects.all().order_by('name')]
    groups_with_selected = [(g, str(g.id) == group_filter) for g in DeviceGroup.objects.all().order_by('name')]

    categories = list(Device.CATEGORY_CHOICES)
    categories_with_selected = [(c[0], c[1], c[0] == category_filter) for c in categories]
    categories_with_selected.insert(0, ('', 'Все', category_filter == ''))
    categories_with_selected.append(('__none__', 'Не указано', category_filter == '__none__'))

    # Обработка POST-запроса
    if request.method == 'POST':
        # Массовое обновление привязки к иерархии (существующий код)
        if 'update_hierarchy' in request.POST:
            selected_ids = request.POST.getlist('selected_devices')
            if not selected_ids:
                messages.warning(request, 'Выберите хотя бы одно устройство.')
                return redirect(request.path)

            target_region = request.POST.get('target_region')
            target_substation = request.POST.get('target_substation')
            target_feeder = request.POST.get('target_feeder')
            target_tp = request.POST.get('target_tp')

            update_fields = {}
            if target_region and target_region != '':
                update_fields['region_id'] = target_region
            if target_substation and target_substation != '':
                update_fields['substation_id'] = target_substation
            if target_feeder and target_feeder != '':
                update_fields['feeder_id'] = target_feeder
            if target_tp and target_tp != '':
                update_fields['tp_id'] = target_tp

            if not update_fields:
                messages.warning(request, 'Выберите хотя бы одно поле для обновления.')
                return redirect(request.path)

            updated_count = Device.objects.filter(id__in=selected_ids).update(**update_fields)
            messages.success(request, f'Обновлено {updated_count} устройств.')
            return redirect(request.path)

        # НОВОЕ: массовое добавление устройств в группы через модалку
        elif 'add_to_groups_modal' in request.POST:
            selected_ids = request.POST.getlist('selected_devices')
            group_ids = request.POST.getlist('group_ids')

            if not selected_ids:
                messages.warning(request, 'Выберите хотя бы одно устройство.')
            elif not group_ids:
                messages.warning(request, 'Выберите хотя бы одну группу.')
            else:
                devices = Device.objects.filter(id__in=selected_ids)
                groups_to_add = DeviceGroup.objects.filter(id__in=group_ids)
                for device in devices:
                    device.groups.add(*groups_to_add)
                messages.success(request, f'Устройства добавлены в {len(groups_to_add)} группу(ы).')
            return redirect(request.path)
        
        # НОВОЕ: массовое изменение устройств категории
        elif 'update_category' in request.POST:
            selected_ids = request.POST.getlist('selected_devices')
            new_category = request.POST.get('new_category')
            if not selected_ids:
                messages.warning(request, 'Выберите хотя бы одно устройство.')
            elif not new_category:
                messages.warning(request, 'Выберите категорию.')
            else:
                Device.objects.filter(id__in=selected_ids).update(category=new_category)
                messages.success(request, f'Категория обновлена для {len(selected_ids)} устройств.')
            return redirect(request.path)

    context = {
        'devices': devices_list,
        'total': total,
        'page': page,
        'per_page': per_page,
        'search': search,
        'total_pages': total_pages,
        'page_range': page_range,
        'producers_with_selected': producers_with_selected,
        'models_with_selected': models_with_selected,
        'statuses_with_selected': statuses_with_selected,
        'producer_filter': producer_filter,
        'model_filter': model_filter,
        'status_filter': status_filter,
        # 'regions': regions,
        # 'substations': substations,
        # 'feeders': feeders,
        # 'tps': tps,
        'groups_all': groups_all,
        'regions_with_selected': regions_with_selected,
        'substations_with_selected': substations_with_selected,
        'feeders_with_selected': feeders_with_selected,
        'tps_with_selected': tps_with_selected,
        'groups_with_selected': groups_with_selected,                     
        'selected_region': region_filter,
        'selected_substation': substation_filter,
        'selected_feeder': feeder_filter,
        'selected_tp': tp_filter,
        'selected_group': group_filter,
        'categories_with_selected': categories_with_selected,
        'category_filter': category_filter,
    }

    current_params = request.GET.copy()
    if 'page' in current_params:
        current_params.pop('page')
    current_params = current_params.urlencode()
    context['current_params'] = current_params

    return render(request, 'meters/devices.html', context)

# ===== УПРАВЛЕНИЕ ГРУППАМИ ДЛЯ ОТДЕЛЬНОГО УСТРОЙСТВА =====
@login_required
@user_passes_test(is_allowed_user)
def device_add_to_groups(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == 'POST':
        group_ids = request.POST.getlist('group_ids')
        if group_ids:
            groups = DeviceGroup.objects.filter(id__in=group_ids)
            device.groups.add(*groups)
            messages.success(request, f'Устройство {device.serial_number} добавлено в выбранные группы.')
        else:
            messages.warning(request, 'Не выбрано ни одной группы.')
        return redirect('devices')
    groups = DeviceGroup.objects.all().order_by('name')
    return render(request, 'meters/device_add_to_groups.html', {'device': device, 'groups': groups})

@login_required
@user_passes_test(is_allowed_user)
def device_remove_from_groups(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == 'POST':
        group_ids = request.POST.getlist('group_ids')
        if group_ids:
            groups = DeviceGroup.objects.filter(id__in=group_ids)
            device.groups.remove(*groups)
            messages.success(request, f'Устройство {device.serial_number} удалено из выбранных групп.')
        else:
            messages.warning(request, 'Не выбрано ни одной группы.')
        return redirect('devices')
    groups = device.groups.all().order_by('name')
    return render(request, 'meters/device_remove_from_groups.html', {'device': device, 'groups': groups})

@login_required
@user_passes_test(is_allowed_user)
def edit_device(request, serial_number):
    device = get_object_or_404(Device, serial_number=serial_number)
    if request.method == 'POST':
        new_serial = request.POST.get('serial_number', '').strip()
        if not new_serial:
            messages.error(request, 'Номер не может быть пустым.')
        elif new_serial == device.serial_number:
            messages.warning(request, 'Номер не изменён.')
        elif Device.objects.filter(serial_number=new_serial).exclude(id=device.id).exists():
            messages.error(request, f'Номер "{new_serial}" уже используется другим устройством.')
        else:
            device.serial_number = new_serial
            device.save()
            messages.success(request, f'Заводской номер изменён на {new_serial}.')
            return redirect('devices')
    context = {'device': device}
    return render(request, 'meters/edit_device.html', context)

@login_required
@user_passes_test(is_allowed_user)
def delete_device(request, serial_number):
    device = get_object_or_404(Device, serial_number=serial_number)
    if request.method == 'POST':
        device.delete()
        messages.success(request, f'Устройство {serial_number} удалено.')
        return redirect('devices')
    return render(request, 'meters/confirm_delete.html', {'object': device, 'type': 'устройство'})

@login_required
@user_passes_test(is_allowed_user)
def add_device(request):
    if request.method == 'POST':
        if 'single_submit' in request.POST:
            serial = request.POST.get('serial_number', '').strip()
            model_id = request.POST.get('model')
            status = request.POST.get('status', 'active')
            if not serial:
                messages.error(request, 'Серийный номер обязателен.')
            elif Device.objects.filter(serial_number=serial).exists():
                messages.error(request, f'Прибор с номером {serial} уже существует.')
            else:
                try:
                    model = MeterModel.objects.get(id=model_id) if model_id else None
                    if model:
                        nominal_current = model.nominal_current
                        phase = model.phases
                        askue_id = model.device_type_id or ''
                        api_id = model.device_type_str or ''
                    else:
                        nominal_current = ''
                        phase = None
                        askue_id = ''
                        api_id = ''
                    jpes_region, _ = Region.objects.get_or_create(name='ЖПЭС')
                    Device.objects.create(
                        serial_number=serial,
                        model=model,
                        status=status,
                        region=jpes_region,
                        nominal_current=nominal_current,
                        phase=phase,
                        askue_id=askue_id,
                        api_id=api_id,
                    )
                    messages.success(request, f'✅ Отчет: Прибор {serial} успешно добавлен в реестр.')
                    return redirect('devices')
                except Exception as e:
                    messages.error(request, f'Ошибка: {e}')
        elif 'bulk_submit' in request.POST:
            serials_text = request.POST.get('bulk_serials', '').strip()
            model_id = request.POST.get('bulk_model')
            status = request.POST.get('bulk_status', 'active')
            if not serials_text:
                messages.error(request, 'Введите хотя бы один номер.')
            else:
                serials = [s.strip() for s in serials_text.split('\n') if s.strip()]
                added = 0
                skipped = []
                try:
                    model = MeterModel.objects.get(id=model_id) if model_id else None
                    if model:
                        nominal_current = model.nominal_current
                        phase = model.phases
                        askue_id = model.device_type_id or ''
                        api_id = model.device_type_str or ''
                    else:
                        nominal_current = ''
                        phase = None
                        askue_id = ''
                        api_id = ''
                    for sn in serials:
                        if Device.objects.filter(serial_number=sn).exists():
                            skipped.append(sn)
                            continue
                        Device.objects.create(
                            serial_number=sn,
                            model=model,
                            status=status,
                            nominal_current=nominal_current,
                            phase=phase,
                            askue_id=askue_id,
                            api_id=api_id,
                        )
                        added += 1
                except Exception as e:
                    skipped.append(f"Ошибка: {e}")
                                # Формируем красивый отчет
                if added > 0:
                    messages.success(request, f'✅ Отчет по импорту: Успешно добавлено {added} приборов.')
                if skipped:
                    # Если пропущенных много, показываем первых 5
                    skipped_str = ", ".join(skipped[:5])
                    if len(skipped) > 5:
                        skipped_str += f" и еще {len(skipped) - 5}..."
                    messages.warning(request, f'⚠️ Пропущено {len(skipped)} дубликатов: {skipped_str}')
                if not added and not skipped:
                    messages.info(request, 'Ни один прибор не был добавлен. Проверьте данные.')
                
                return redirect('devices')
    context = {
        'models': MeterModel.objects.all().order_by('model_name'),
    }
    return render(request, 'meters/add_device.html', context)

@login_required
@user_passes_test(is_allowed_user)
def upload_devices(request):
    if request.method == 'POST' and request.FILES.get('excel_file'):
        file = request.FILES['excel_file']
        try:
            wb = openpyxl.load_workbook(file)
            ws = wb.active
            added = 0
            skipped = []
            errors = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                sn = str(row[0]).strip() if row[0] else None
                model_name = str(row[1]).strip() if row[1] else None
                status = str(row[2]).strip() if row[2] else 'active'
                nominal_current = str(row[3]).strip() if row[3] else ''
                phase = row[4]
                askue_id = str(row[5]).strip() if row[5] else ''
                api_id = str(row[6]).strip() if row[6] else ''
                if not sn:
                    continue
                if Device.objects.filter(serial_number=sn).exists():
                    skipped.append(sn)
                    continue
                try:
                    model = MeterModel.objects.filter(model_name=model_name).first() if model_name else None
                    jpes_region, _ = Region.objects.get_or_create(name='ЖПЭС')
                    Device.objects.create(
                        serial_number=sn,
                        model=model,
                        status=status,
                        region=jpes_region,
                        nominal_current=nominal_current,
                        phase=int(phase) if phase else None,
                        askue_id=askue_id,
                        api_id=api_id,
                    )
                    added += 1
                except Exception as e:
                    errors.append(f"{sn}: {e}")
            messages.success(request, f'Добавлено: {added}. Пропущено (дубликаты): {len(skipped)}. Ошибок: {len(errors)}')
            if errors:
                messages.warning(request, f'Ошибки: {"; ".join(errors[:3])}')
        except Exception as e:
            messages.error(request, f'Ошибка обработки файла: {e}')
        return redirect('devices')
    return redirect('add_device')

@login_required
@user_passes_test(is_allowed_user)
def download_template(request):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Devices"
    headers = ["serial_number", "model_name", "status", "nominal_current", "phase", "askue_id", "api_id"]
    ws.append(headers)
    ws.append(["084600001431", "DTZY217", "active", "5(80)", "3", "", ""])
    ws.append(["084600001432", "P34S02", "active", "10(80)", "3", "", ""])
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=device_template.xlsx'
    wb.save(response)
    return response

# ===== ПОКАЗАНИЯ =====
def readings(request):
    serial = request.GET.get('serial', '').strip() # Добавляем .strip() на случай лишних пробелов
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    
    device = None
    readings_list = []
    is_online = False
    last_reading_date = None

    today = timezone.now().date()
    start_3d = (today - timedelta(days=3)).strftime('%Y-%m-%d')
    start_7d = (today - timedelta(days=7)).strftime('%Y-%m-%d')
    start_30d = (today - timedelta(days=30)).strftime('%Y-%m-%d')
    end_today = today.strftime('%Y-%m-%d')

    if serial:
        # 1. Точное совпадение
        device = Device.objects.filter(serial_number=serial).first()
        
        # 2. Если не нашли — ищем по окончанию (спасет, если ввели без ведущего нуля)
        if not device:
            candidates = Device.objects.filter(serial_number__endswith=serial).order_by('-id')
            if candidates.exists():
                device = candidates.first()
                
        # 3. Если всё еще нет — пробуем обрезать ведущие нули с введенного номера
        if not device:
            clean_serial = serial.lstrip('0')
            if clean_serial and clean_serial != serial:
                candidates = Device.objects.filter(serial_number__endswith=clean_serial).order_by('-id')
                if candidates.exists():
                    device = candidates.first()

        if device:
            qs = Reading.objects.filter(device=device)
            if start_date_str:
                try:
                    start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
                    qs = qs.filter(timestamp__gte=start_dt)
                except:
                    pass
            if end_date_str:
                try:
                    end_dt = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)
                    qs = qs.filter(timestamp__lt=end_dt)
                except:
                    pass

            if not start_date_str and not end_date_str:
                readings_list = qs.order_by('-timestamp')[:10]
            else:
                readings_list = qs.order_by('-timestamp')

            last_reading = Reading.objects.filter(device=device).order_by('-timestamp').first()
            if last_reading:
                last_reading_date = last_reading.timestamp
                days_since_last = (timezone.now() - last_reading.timestamp).days
                is_online = days_since_last < 7

            # ---- ВЫЧИСЛЕНИЕ ОТОБРАЖАЕМЫХ ЗНАЧЕНИЙ ИЕРАРХИИ ----
            display_region = device.region.name if device.region else "—"
            
            if device.substation:
                display_substation = device.substation.name
            else:
                conn_sub = MeterConnection.objects.filter(device=device, connected_object_type='substation').first()
                display_substation = conn_sub.connected_object.name if conn_sub else "—"
            
            if device.feeder:
                display_feeder = device.feeder.name
            else:
                conn_feeder = MeterConnection.objects.filter(device=device, connected_object_type='feeder').first()
                display_feeder = conn_feeder.connected_object.name if conn_feeder else "—"
            
            if device.tp:
                display_tp = device.tp.name
            else:
                conn_tp = MeterConnection.objects.filter(device=device, connected_object_type='tp').first()
                display_tp = conn_tp.connected_object.name if conn_tp else "—"

    context = {
        'device': device,
        'readings': readings_list,
        'serial': serial,
        'is_online': is_online,
        'last_reading_date': last_reading_date,
        'start_date': start_date_str,
        'end_date': end_date_str,
        'today': today,
        'start_3d': start_3d,
        'start_7d': start_7d,
        'start_30d': start_30d,
        'end_today': end_today,
        'display_region': display_region if device else "—",
        'display_substation': display_substation if device else "—",
        'display_feeder': display_feeder if device else "—",
        'display_tp': display_tp if device else "—",
    }
    return render(request, 'meters/readings.html', context)

# ===== ГРУППОВОЙ ВВОД И ИМПОРТ EXCEL (С ФОНОМ) =====
def run_import_background(session_key):
    from django.contrib.sessions.models import Session
    try:
        session = Session.objects.get(session_key=session_key)
        data = session.get_decoded()
        import_data = data.get('import_data', [])
        params = data.get('import_params', {})
        if not import_data or not params:
            logger.warning("Нет данных для фонового импорта")
            return

        col_serial = params.get('col_serial')
        col_timestamp = params.get('col_timestamp')
        col_value = params.get('col_value')
        col_notes = params.get('col_notes')
        date_format = params.get('date_format', 'auto')
        col_direction = params.get('col_direction')
        default_direction = params.get('default_direction', 'aplus')

        success = 0
        errors = []
        skipped = []
        skipped_empty = []
        updated = 0

        with transaction.atomic():
            for row in import_data:
                sn = str(row.get(col_serial, '')).strip()
                if not sn:
                    continue
                device = find_device(sn)
                if not device:
                    skipped.append(sn)
                    continue

                # Направление: из колонки файла (A-/aminus => aminus), иначе по умолчанию
                if col_direction and row.get(col_direction):
                    direction = str(row[col_direction]).strip().lower()
                    direction = 'aminus' if direction in ['a-', 'aminus'] else 'aplus'
                else:
                    direction = default_direction

                raw_value = row.get(col_value)
                if pd.isna(raw_value) or raw_value == '':
                    skipped_empty.append(sn)
                    continue
                try:
                    val = float(raw_value)
                except:
                    errors.append(f"Ошибка преобразования значения для {sn}: {raw_value}")
                    continue

                raw_date = row.get(col_timestamp)
                if pd.isna(raw_date):
                    continue

                try:
                    if isinstance(raw_date, datetime):
                        dt = raw_date
                    elif isinstance(raw_date, str):
                        if date_format == 'auto':
                            raw_date_clean = raw_date
                            if 'T' in raw_date:
                                if '+' in raw_date:
                                    raw_date_clean = raw_date.split('+')[0]
                                elif 'Z' in raw_date:
                                    raw_date_clean = raw_date.replace('Z', '')
                                raw_date_clean = raw_date_clean.replace('T', ' ')
                                try:
                                    dt = datetime.strptime(raw_date_clean, '%Y-%m-%d %H:%M:%S')
                                except ValueError:
                                    for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d.%m.%Y %H:%M:%S', '%d.%m.%Y', '%m/%d/%Y %H:%M:%S', '%m/%d/%Y']:
                                        try:
                                            dt = datetime.strptime(raw_date, fmt)
                                            break
                                        except:
                                            continue
                                    else:
                                        errors.append(f"Не удалось распознать дату: {raw_date}")
                                        continue
                            else:
                                for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d.%m.%Y %H:%M:%S', '%d.%m.%Y', '%m/%d/%Y %H:%M:%S', '%m/%d/%Y']:
                                    try:
                                        dt = datetime.strptime(raw_date, fmt)
                                        break
                                    except:
                                        continue
                                else:
                                    errors.append(f"Не удалось распознать дату: {raw_date}")
                                    continue
                        else:
                            dt = datetime.strptime(raw_date, date_format)
                    else:
                        errors.append(f"Неподдерживаемый тип даты: {type(raw_date)}")
                        continue
                except Exception as e:
                    errors.append(f"Ошибка парсинга даты для {sn}: {raw_date} -> {e}")
                    continue

                notes = str(row.get(col_notes, '')).strip() if col_notes else ''

                obj, created = Reading.objects.update_or_create(
                    device=device,
                    timestamp=dt,
                    direction=direction,
                    defaults={'reading_value': val, 'notes': notes}
                )
                if created:
                    success += 1
                else:
                    updated += 1

        session_data = session.get_decoded()
        session_data['import_result'] = {
            'success': success,
            'updated': updated,
            'errors': errors[:10],
            'skipped': skipped[:10],
            'skipped_empty': skipped_empty[:10],
            'error_count': len(errors),
            'skipped_count': len(skipped),
            'empty_count': len(skipped_empty),
        }
        session_data.pop('import_data', None)
        session_data.pop('import_params', None)
        session.save()
        logger.info(f"Фоновый импорт завершён: добавлено {success}, обновлено {updated}")
    except Exception as e:
        logger.error(f"Ошибка в фоновом импорте: {e}")

@login_required
@user_passes_test(is_allowed_user)
def bulk_readings(request):
    # Очистка результата импорта
    if request.GET.get('clear_result'):
        request.session.pop('import_result', None)
        return redirect('bulk_readings')

    # Ручной ввод
    if request.method == 'POST' and 'manual_submit' in request.POST:
        date_str = request.POST.get('date')
        readings_text = request.POST.get('readings_text', '')
        notes = request.POST.get('notes', '')
        direction = request.POST.get('direction', 'aplus')
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
            except:
                dt = datetime.now()
        else:
            dt = datetime.now()
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)

        lines = readings_text.strip().split('\n')
        added = 0
        errors = []
        not_found = []
        for line in lines:
            parts = line.strip().split()
            if len(parts) < 2:
                errors.append(f"Неверный формат: {line}")
                continue
            sn = parts[0]
            val = parts[1]
            device = find_device(sn)
            if not device:
                not_found.append(sn)
                continue
            try:
                Reading.objects.update_or_create(
                    device=device,
                    timestamp=dt,
                    direction=direction,
                    defaults={'reading_value': float(val), 'notes': notes}
                )
                added += 1
            except Exception as e:
                errors.append(f"Ошибка для {sn}: {e}")
        messages.success(request, f"Добавлено показаний: {added}. Ошибок: {len(errors)}. Не найдено: {len(not_found)}")
        if errors:
            messages.warning(request, "Ошибки: " + "; ".join(errors[:3]))
        if not_found:
            messages.warning(request, "Не найдены: " + ", ".join(not_found[:3]))
        return redirect('bulk_readings')

    # Загрузка Excel (первый шаг – предпросмотр)
    if request.method == 'POST' and 'excel_submit' in request.POST and request.FILES.get('excel_file'):
        file = request.FILES['excel_file']
        try:
            df = pd.read_excel(file, engine='openpyxl')
        except Exception as e:
            messages.error(request, f'Ошибка чтения файла: {e}')
            return redirect('bulk_readings')

        if df.empty:
            messages.error(request, 'Файл пуст.')
            return redirect('bulk_readings')

        request.session['import_data'] = df.to_dict('records')
        request.session['import_columns'] = list(df.columns)
        request.session['import_shape'] = df.shape

        sample_data = df.head(10).values.tolist()
        return render(request, 'meters/import_readings_preview.html', {
            'columns': list(df.columns),
            'sample': sample_data,
            'total_rows': df.shape[0],
        })

    # Подтверждение импорта (запуск фона)
    if request.method == 'POST' and 'confirm_import' in request.POST:
        request.session['import_params'] = {
            'col_serial': request.POST.get('col_serial'),
            'col_timestamp': request.POST.get('col_timestamp'),
            'col_value': request.POST.get('col_value'),
            'col_notes': request.POST.get('col_notes'),
            'col_direction': request.POST.get('col_direction'),
            'default_direction': request.POST.get('default_direction', 'aplus'),
            'date_format': request.POST.get('date_format', 'auto'),
        }
        thread = threading.Thread(target=run_import_background, args=(request.session.session_key,))
        thread.daemon = True
        thread.start()
        messages.info(request, 'Импорт запущен в фоновом режиме. Обновите страницу через некоторое время для просмотра результатов.')
        return redirect('bulk_readings')

    return render(request, 'meters/bulk_readings.html')

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
def missing_readings_report(request):
    period = request.GET.get('period', 'today')
    specific_date = request.GET.get('date', '')          # НОВОЕ: конкретная дата
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    region_filter = request.GET.get('region', '')
    group_filter = request.GET.get('group', '')
    export = request.GET.get('export', False)
    online_filter = request.GET.get('online', '')
    category_filter = request.GET.get('category', '')

    today = timezone.now().date()

    # Базовый запрос
    devices_qs = Device.objects.select_related('model')

    # --- Если указана конкретная дата ---
    if specific_date:
        try:
            target_date = datetime.strptime(specific_date, '%Y-%m-%d').date()
        except ValueError:
            target_date = today

        # Аннотация: есть ли показание на эту дату
        reading_exists = Exists(
            Reading.objects.filter(
                device=OuterRef('pk'),
                timestamp__gte=target_date,
                timestamp__lt=target_date + timedelta(days=1)
            )
        )
        devices_qs = devices_qs.annotate(
            has_reading_on_date=reading_exists
        ).filter(has_reading_on_date=False)

        # Для отображения последнего показания (можно оставить как есть)
        last_reading_subquery = Reading.objects.filter(
            device=OuterRef('pk')
        ).order_by('-timestamp').values('timestamp')[:1]
        devices_qs = devices_qs.annotate(
            last_reading_date=Subquery(last_reading_subquery)
        )
        # Статус связи (последние 7 дней)
        seven_days_ago = timezone.now() - timedelta(days=7)
        devices_qs = devices_qs.annotate(
            has_recent_reading=Exists(
                Reading.objects.filter(
                    device=OuterRef('pk'),
                    timestamp__gte=seven_days_ago
                )
            )
        )
        # Для пагинации и шаблона будем использовать start_date = target_date (для совместимости)
        start_date = target_date
    else:
        # --- Старый режим: период ---
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

        last_reading_subquery = Reading.objects.filter(
            device=OuterRef('pk')
        ).order_by('-timestamp').values('timestamp')[:1]
        devices_qs = devices_qs.annotate(
            last_reading_date=Subquery(last_reading_subquery)
        )

        seven_days_ago = timezone.now() - timedelta(days=7)
        devices_qs = devices_qs.annotate(
            has_recent_reading=Exists(
                Reading.objects.filter(
                    device=OuterRef('pk'),
                    timestamp__gte=seven_days_ago
                )
            )
        )

        # Подсчёт количества показаний за период (материализуем id отдельным быстрым запросом,
        # чтобы MySQL не выбирал плохой план для вложенного подзапроса)
        qualifying_device_ids = list(Reading.objects.filter(
            timestamp__gte=start_date,
            timestamp__lt=today + timedelta(days=1)
        ).values_list('device_id', flat=True).distinct())

        devices_qs = devices_qs.exclude(id__in=qualifying_device_ids)

    # --- Применяем остальные фильтры (статус, Тип ПУ, производитель, регион, группа) ---
    if status_filter:
        devices_qs = devices_qs.filter(status=status_filter)
    if model_filter:
        devices_qs = devices_qs.filter(model__id=model_filter)
    if producer_filter:
        devices_qs = devices_qs.filter(model__device_type_str=producer_filter)
    if region_filter:
        devices_qs = devices_qs.filter(region_id=region_filter)
    if group_filter:
        devices_qs = devices_qs.filter(groups__id=group_filter)
    if category_filter == '__none__':
        devices_qs = devices_qs.filter(category__isnull=True)
    elif category_filter:
        devices_qs = devices_qs.filter(category=category_filter)
    if online_filter == 'yes':
        devices_qs = devices_qs.filter(has_recent_reading=True)
    elif online_filter == 'no':
        devices_qs = devices_qs.filter(has_recent_reading=False)
    

    # --- Экспорт ---
    if export == '1':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="missing_readings_{specific_date or period}_{today}.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Заводской номер', 'Тип ПУ', 'Статус реестра',
            'Статус связи', 'Производитель', 'Ток', 'Фазность', 'Категория', 'Последнее показание'
        ])
        for device in devices_qs:
            producer = device.model.device_type_str if device.model else ''
            writer.writerow([
                device.serial_number,
                device.model.model_name if device.model else '',
                device.status,
                'Онлайн' if device.has_recent_reading else 'Оффлайн',
                producer,
                device.nominal_current,
                device.model.phases if device.model else '',
                device.get_category_display() or '',
                device.last_reading_date.strftime('%Y-%m-%d %H:%M:%S') if device.last_reading_date else '',
            ])
        return response

    if export == 'xlsx':
        import openpyxl
        from openpyxl.styles import Font, Alignment
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Счетчики без показаний"
        headers = [
            'Заводской номер', 'Тип ПУ', 'Статус реестра',
            'Статус связи', 'Производитель', 'Ток', 'Фазность', 'Категория', 'Последнее показание'
        ]
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal='center')
        for row_idx, device in enumerate(devices_qs, 2):
            producer = device.model.device_type_str if device.model else ''
            ws.cell(row=row_idx, column=1, value=device.serial_number)
            ws.cell(row=row_idx, column=2, value=device.model.model_name if device.model else '')
            ws.cell(row=row_idx, column=3, value=device.status)
            ws.cell(row=row_idx, column=4, value='Онлайн' if device.has_recent_reading else 'Оффлайн')
            ws.cell(row=row_idx, column=5, value=producer)
            ws.cell(row=row_idx, column=6, value=device.nominal_current)
            ws.cell(row=row_idx, column=7, value=device.model.phases if device.model else '')
            ws.cell(row=row_idx, column=8, value=device.get_category_display() or '')
            ws.cell(row=row_idx, column=9, value=device.last_reading_date.strftime('%Y-%m-%d %H:%M:%S') if device.last_reading_date else '')
        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = max_length + 2
        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="missing_readings_{specific_date or period}_{today}.xlsx"'
        wb.save(response)
        return response

    # --- Пагинация ---
    page = int(request.GET.get('page', 1))
    per_page = 10
    total = devices_qs.count()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
    devices_list = devices_qs.order_by('serial_number')[(page-1)*per_page:page*per_page]
    page_range = range(1, total_pages + 1)

    # Добавляем producer_display для отображения
    for device in devices_list:
        if device.model:
            code = device.model.device_type_str or ""
            type_id = device.model.device_type_id or ""
            device.producer_display = get_producer_display(code, type_id)
        else:
            device.producer_display = "Неизвестно"

    # Подготовка данных для select-ов
    periods = [
        ('today', 'Сегодня', period == 'today'),
        ('3d', '3 дня', period == '3d'),
        ('7d', '7 дней', period == '7d'),
        ('30d', '30 дней', period == '30d'),
    ]

    codes = MeterModel.objects.values_list('device_type_str', flat=True).distinct().order_by('device_type_str')
    producers_with_selected = []
    for code in codes:
        display = get_producer_display(code, '')
        if display == "Неизвестно" and code:
            display = code
        selected = (code == producer_filter)
        producers_with_selected.append((display, code, selected))

    models_list = MeterModel.objects.all().order_by('model_name')
    models_with_selected = [(m, str(m.id) == model_filter) for m in models_list]

    statuses_with_selected = [(s, s == status_filter) for s in ['active', 'repair', 'inactive', 'temporary_off']]

    regions = Region.objects.all().order_by('name')
    regions_with_selected = [(r, str(r.id) == region_filter) for r in regions]
    groups = DeviceGroup.objects.all().order_by('name')
    groups_with_selected = [(g, str(g.id) == group_filter) for g in groups]

    categories = list(Device.CATEGORY_CHOICES)
    categories_with_selected = [(c[0], c[1], c[0] == category_filter) for c in categories]
    categories_with_selected.insert(0, ('', 'Все', category_filter == ''))
    categories_with_selected.append(('__none__', 'Не указано', category_filter == '__none__'))

    context = {
        'devices': devices_list,
        'total': total,
        'page': page,
        'total_pages': total_pages,
        'page_range': page_range,
        'period': period,
        'specific_date': specific_date,          # передаём в шаблон
        'producer_filter': producer_filter,
        'model_filter': model_filter,
        'status_filter': status_filter,
        'start_index': (page - 1) * per_page,
        'periods': periods,
        'producers_with_selected': producers_with_selected,
        'models_with_selected': models_with_selected,
        'statuses_with_selected': statuses_with_selected,
        'start_date': start_date,
        'regions_with_selected': regions_with_selected,
        'groups_with_selected': groups_with_selected,
        'selected_region': region_filter,
        'selected_group': group_filter,
        'categories_with_selected': categories_with_selected,
    }

    current_params = request.GET.copy()
    if 'page' in current_params:
        current_params.pop('page')
    current_params = current_params.urlencode()
    context['current_params'] = current_params

    return render(request, 'meters/missing_readings.html', context)


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

# исправление статуса active, inactive
@login_required
@user_passes_test(is_allowed_user)
def bulk_update_status_page(request):
    if request.method == 'POST':
        serials_text = request.POST.get('serials', '').strip()
        new_status = request.POST.get('status', 'active')
        if not serials_text:
            messages.error(request, 'Список серийных номеров пуст.')
            return redirect('bulk_update_status_page')
        serials = [s.strip() for s in serials_text.split('\n') if s.strip()]
        updated = 0
        not_found = []
        for sn in serials:
            device = Device.objects.filter(serial_number=sn).first()
            if device:
                device.status = new_status
                device.save()
                updated += 1
            else:
                not_found.append(sn)
        if updated:
            messages.success(request, f'Обновлено устройств: {updated}')
        if not_found:
            messages.warning(request, f'Не найдены: {", ".join(not_found[:10])}')
        return redirect('bulk_update_status_page')
    return render(request, 'meters/bulk_update_status.html')

# импорт показания Exsel
@login_required
@user_passes_test(is_allowed_user)
def import_readings_excel(request):
    import logging
    logger = logging.getLogger(__name__)

    if request.method == 'POST' and request.FILES.get('excel_file'):
        file = request.FILES['excel_file']
        try:
            import pandas as pd
            df = pd.read_excel(file, engine='openpyxl')
        except Exception as e:
            messages.error(request, f'Ошибка чтения файла: {e}')
            return redirect('import_readings_excel')

        if df.empty:
            messages.error(request, 'Файл пуст.')
            return redirect('import_readings_excel')

        # Сохраняем данные в сессию
        request.session['import_data'] = df.to_dict('records')
        request.session['import_columns'] = list(df.columns)

        sample_data = df.head(10).values.tolist()
        return render(request, 'meters/import_readings_preview.html', {
            'columns': list(df.columns),
            'sample': sample_data,
            'total_rows': df.shape[0],
        })

    if request.method == 'POST' and 'confirm' in request.POST:
        col_serial = request.POST.get('col_serial')
        col_timestamp = request.POST.get('col_timestamp')
        col_value = request.POST.get('col_value')
        col_notes = request.POST.get('col_notes')
        date_format = request.POST.get('date_format', 'auto')

        if not all([col_serial, col_timestamp, col_value]):
            messages.error(request, 'Необходимо указать колонки для серийного номера, даты и значения.')
            return redirect('import_readings_excel')

        data = request.session.get('import_data', [])
        if not data:
            messages.error(request, 'Данные не найдены, попробуйте загрузить файл заново.')
            return redirect('import_readings_excel')

        success = 0
        errors = []
        skipped = []
        updated = 0
        import pandas as pd
        from datetime import datetime

        logger.info(f"Начинаем импорт {len(data)} строк из Excel")

        for idx, row in enumerate(data):
            sn = str(row.get(col_serial, '')).strip()
            if not sn:
                errors.append(f"Строка {idx+1}: пустой серийный номер")
                continue

            device = find_device(sn)
            if not device:
                skipped.append(f"{sn} (строка {idx+1})")
                continue

            raw_date = row.get(col_timestamp)
            if pd.isna(raw_date):
                errors.append(f"Строка {idx+1} ({sn}): пустая дата")
                continue

            try:
                if isinstance(raw_date, datetime):
                    dt = raw_date
                elif isinstance(raw_date, str):
                    if date_format == 'auto':
                        # Пробуем форматы
                        if 'T' in raw_date:
                            if '+' in raw_date:
                                raw_date_clean = raw_date.split('+')[0]
                            elif 'Z' in raw_date:
                                raw_date_clean = raw_date.replace('Z', '')
                            else:
                                raw_date_clean = raw_date
                            for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d']:
                                try:
                                    dt = datetime.strptime(raw_date_clean, fmt)
                                    break
                                except:
                                    continue
                            else:
                                for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d.%m.%Y %H:%M:%S', '%d.%m.%Y', '%m/%d/%Y %H:%M:%S', '%m/%d/%Y']:
                                    try:
                                        dt = datetime.strptime(raw_date, fmt)
                                        break
                                    except:
                                        continue
                                else:
                                    errors.append(f"Строка {idx+1} ({sn}): не удалось распознать дату '{raw_date}'")
                                    continue
                        else:
                            for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d.%m.%Y %H:%M:%S', '%d.%m.%Y', '%m/%d/%Y %H:%M:%S', '%m/%d/%Y']:
                                try:
                                    dt = datetime.strptime(raw_date, fmt)
                                    break
                                except:
                                    continue
                            else:
                                errors.append(f"Строка {idx+1} ({sn}): не удалось распознать дату '{raw_date}'")
                                continue
                    else:
                        dt = datetime.strptime(raw_date, date_format)
                else:
                    errors.append(f"Строка {idx+1} ({sn}): неподдерживаемый тип даты {type(raw_date)}")
                    continue
            except Exception as e:
                errors.append(f"Строка {idx+1} ({sn}): ошибка парсинга даты '{raw_date}': {e}")
                continue

            try:
                val = float(row.get(col_value))
            except Exception as e:
                errors.append(f"Строка {idx+1} ({sn}): ошибка преобразования значения '{row.get(col_value)}': {e}")
                continue

            notes = str(row.get(col_notes, '')).strip() if col_notes else ''

            direction = 'aplus'  # в этой форме колонки направления нет
            obj, created = Reading.objects.update_or_create(
                device=device,
                timestamp=dt,
                direction=direction,
                defaults={'reading_value': val, 'notes': notes}
            )
            if created:
                success += 1
            else:
                updated += 1

        logger.info(f"Импорт завершён: добавлено {success}, обновлено {updated}, ошибок {len(errors)}, пропущено {len(skipped)}")

        messages.success(request, f'✅ Импорт завершён. Добавлено: {success}, обновлено: {updated}.')
        if errors:
            messages.warning(request, f'⚠️ Ошибок при импорте: {len(errors)} (первые 5 показаны).')
            # Покажем первые 5 ошибок в отдельном сообщении
            for err in errors[:5]:
                messages.warning(request, f'  • {err}')
        if skipped:
            messages.warning(request, f'⚠️ Пропущено (устройство не найдено): {len(skipped)} (первые 5 показаны).')
            for sk in skipped[:5]:
                messages.warning(request, f'  • {sk}')

        request.session.pop('import_data', None)
        request.session.pop('import_columns', None)
        return redirect('bulk_readings')  # или 'devices', но лучше на страницу импорта

    return render(request, 'meters/import_readings_excel.html')

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

# ===== ОТЧЁТ: СЧЁТЧИКИ С ПОКАЗАНИЯМИ (НОВЫЙ) =====
# @login_required
# @user_passes_test(is_allowed_user)
def readings_with_report(request):
    period = request.GET.get('period', 'today')
    specific_date = request.GET.get('date', '')          # новая дата
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    region_filter = request.GET.get('region', '')
    group_filter = request.GET.get('group', '')
    export = request.GET.get('export', False)
    online_filter = request.GET.get('online', '')
    category_filter = request.GET.get('category', '')

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

    # Базовый запрос
    devices_qs = Device.objects.select_related('model')

    # Если указана конкретная дата – используем её, иначе работаем с периодом
    if specific_date:
        try:
            target_date = datetime.strptime(specific_date, '%Y-%m-%d').date()
        except ValueError:
            target_date = today
        # Аннотируем значение показания на эту дату (берём первое за день, можно последнее)
        reading_subquery = Reading.objects.filter(
            device=OuterRef('pk'),
            timestamp__gte=target_date,
            timestamp__lt=target_date + timedelta(days=1)
        ).order_by('-timestamp').values('reading_value')[:1]
        devices_qs = devices_qs.annotate(
            reading_on_date=Subquery(reading_subquery)
        )
        # Фильтруем только те, у которых есть показание на эту дату
        devices_qs = devices_qs.filter(reading_on_date__isnull=False)
        # Для отображения в таблице будем использовать это значение
        show_reading_value = True
        # Для статуса связи используем последние 7 дней
        seven_days_ago = timezone.now() - timedelta(days=7)
        devices_qs = devices_qs.annotate(
            has_recent_reading=Exists(
                Reading.objects.filter(
                    device=OuterRef('pk'),
                    timestamp__gte=seven_days_ago
                )
            )
        )
        # Последнее показание – тоже на эту дату (или можно оставить last_reading_date)
        last_reading_subquery = Reading.objects.filter(
            device=OuterRef('pk'),
            timestamp__gte=target_date,
            timestamp__lt=target_date + timedelta(days=1)
        ).order_by('-timestamp').values('timestamp')[:1]
        devices_qs = devices_qs.annotate(
            last_reading_date=Subquery(last_reading_subquery)
        )
    else:
        # Старый режим: период
        last_reading_subquery = Reading.objects.filter(
            device=OuterRef('pk')
        ).order_by('-timestamp').values('timestamp')[:1]
        devices_qs = devices_qs.annotate(
            last_reading_date=Subquery(last_reading_subquery)
        )
        seven_days_ago = timezone.now() - timedelta(days=7)
        devices_qs = devices_qs.annotate(
            has_recent_reading=Exists(
                Reading.objects.filter(
                    device=OuterRef('pk'),
                    timestamp__gte=seven_days_ago
                )
            )
        )
        # Подсчёт количества показаний за период (материализуем id отдельным быстрым запросом,
        # чтобы MySQL не выбирал плохой план для вложенного подзапроса)
        qualifying_device_ids = list(Reading.objects.filter(
            timestamp__gte=start_date,
            timestamp__lt=today + timedelta(days=1)
        ).values_list('device_id', flat=True).distinct())
        devices_qs = devices_qs.filter(id__in=qualifying_device_ids)
        # Поле значения на дату не нужно
        show_reading_value = False

    # Применяем остальные фильтры
    if status_filter:
        devices_qs = devices_qs.filter(status=status_filter)
    if model_filter:
        devices_qs = devices_qs.filter(model__id=model_filter)
    if producer_filter:
        devices_qs = devices_qs.filter(model__device_type_str=producer_filter)
    if region_filter:
        devices_qs = devices_qs.filter(region_id=region_filter)
    if group_filter:
        devices_qs = devices_qs.filter(groups__id=group_filter)
    if category_filter == '__none__':
        devices_qs = devices_qs.filter(category__isnull=True)
    elif category_filter:
        devices_qs = devices_qs.filter(category=category_filter)
    if online_filter == 'yes':
        devices_qs = devices_qs.filter(has_recent_reading=True)
    elif online_filter == 'no':
        devices_qs = devices_qs.filter(has_recent_reading=False)

    # Экспорт (CSV/Excel) – аналогично missing_readings, но с дополнительной колонкой "Значение"
    if export == '1':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="readings_with_{period}_{today}.csv"'
        writer = csv.writer(response)
        headers = [
            'Заводской номер', 'Тип ПУ', 'Статус реестра',
            'Статус связи', 'Производитель', 'Ток', 'Фазность', 'Категория'
        ]
        if show_reading_value and specific_date:
            headers.append(f'Значение на {specific_date}')
        else:
            headers.append('Последнее показание (дата)')
        writer.writerow(headers)

        for device in devices_qs:
            producer = device.model.device_type_str if device.model else ''
            row = [
                device.serial_number,
                device.model.model_name if device.model else '',
                device.status,
                'Онлайн' if device.has_recent_reading else 'Оффлайн',
                producer,
                device.nominal_current,
                device.model.phases if device.model else '',
                device.get_category_display() or '',
            ]
            if show_reading_value and specific_date:
                row.append(str(device.reading_on_date) if device.reading_on_date is not None else '')
            else:
                row.append(device.last_reading_date.strftime('%Y-%m-%d %H:%M:%S') if device.last_reading_date else '')
            writer.writerow(row)
        return response

    if export == 'xlsx':
        import openpyxl
        from openpyxl.styles import Font, Alignment
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Счетчики с показаниями"
        headers = [
            'Заводской номер', 'Тип ПУ', 'Статус реестра',
            'Статус связи', 'Производитель', 'Ток', 'Фазность', 'Категория'
        ]
        if show_reading_value and specific_date:
            headers.append(f'Значение на {specific_date}')
        else:
            headers.append('Последнее показание (дата)')
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal='center')

        for row_idx, device in enumerate(devices_qs, 2):
            producer = device.model.device_type_str if device.model else ''
            ws.cell(row=row_idx, column=1, value=device.serial_number)
            ws.cell(row=row_idx, column=2, value=device.model.model_name if device.model else '')
            ws.cell(row=row_idx, column=3, value=device.status)
            ws.cell(row=row_idx, column=4, value='Онлайн' if device.has_recent_reading else 'Оффлайн')
            ws.cell(row=row_idx, column=5, value=producer)
            ws.cell(row=row_idx, column=6, value=device.nominal_current)
            ws.cell(row=row_idx, column=7, value=device.model.phases if device.model else '')
            ws.cell(row=row_idx, column=8, value=device.get_category_display() or '')
            if show_reading_value and specific_date:
                ws.cell(row=row_idx, column=8, value=float(device.reading_on_date) if device.reading_on_date is not None else '')
            else:
                ws.cell(row=row_idx, column=8, value=device.last_reading_date.strftime('%Y-%m-%d %H:%M:%S') if device.last_reading_date else '')
        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = max_length + 2
        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="readings_with_{period}_{today}.xlsx"'
        wb.save(response)
        return response

    # Пагинация
    page = int(request.GET.get('page', 1))
    per_page = 10
    total = devices_qs.count()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
    page_ids = list(
        devices_qs.order_by('serial_number').values_list('id', flat=True)[(page-1)*per_page:page*per_page]
    )
    order_map = {pk: i for i, pk in enumerate(page_ids)}
    devices_list = sorted(devices_qs.filter(pk__in=page_ids), key=lambda d: order_map.get(d.id, 0))
    page_range = range(1, total_pages + 1)

    for device in devices_list:
        if device.model:
            code = device.model.device_type_str or ""
            type_id = device.model.device_type_id or ""
            device.producer_display = get_producer_display(code, type_id)
        else:
            device.producer_display = "Неизвестно"

    periods = [
        ('today', 'Сегодня', period == 'today'),
        ('3d', '3 дня', period == '3d'),
        ('7d', '7 дней', period == '7d'),
        ('30d', '30 дней', period == '30d'),
    ]

    codes = MeterModel.objects.values_list('device_type_str', flat=True).distinct().order_by('device_type_str')
    producers_with_selected = []
    for code in codes:
        display = get_producer_display(code, '')
        if display == "Неизвестно" and code:
            display = code
        selected = (code == producer_filter)
        producers_with_selected.append((display, code, selected))

    models_list = MeterModel.objects.all().order_by('model_name')
    models_with_selected = [(m, str(m.id) == model_filter) for m in models_list]

    statuses_with_selected = [(s, s == status_filter) for s in ['active', 'repair', 'inactive', 'temporary_off']]

    regions = Region.objects.all().order_by('name')
    regions_with_selected = [(r, str(r.id) == region_filter) for r in regions]
    groups = DeviceGroup.objects.all().order_by('name')
    groups_with_selected = [(g, str(g.id) == group_filter) for g in groups]

    categories = list(Device.CATEGORY_CHOICES)
    categories_with_selected = [(c[0], c[1], c[0] == category_filter) for c in categories]
    categories_with_selected.insert(0, ('', 'Все', category_filter == ''))
    categories_with_selected.append(('__none__', 'Не указано', category_filter == '__none__'))

    context = {
        'devices': devices_list,
        'total': total,
        'page': page,
        'total_pages': total_pages,
        'page_range': page_range,
        'period': period,
        'specific_date': specific_date,
        'producer_filter': producer_filter,
        'model_filter': model_filter,
        'status_filter': status_filter,
        'start_index': (page - 1) * per_page,
        'periods': periods,
        'producers_with_selected': producers_with_selected,
        'models_with_selected': models_with_selected,
        'statuses_with_selected': statuses_with_selected,
        'start_date': start_date,
        'regions_with_selected': regions_with_selected,
        'groups_with_selected': groups_with_selected,
        'selected_region': region_filter,
        'selected_group': group_filter,
        'show_reading_value': show_reading_value,  # флаг для шаблона
        'categories_with_selected': categories_with_selected,
    }

    current_params = request.GET.copy()
    if 'page' in current_params:
        current_params.pop('page')
    current_params = current_params.urlencode()
    context['current_params'] = current_params

    return render(request, 'meters/readings_with.html', context)

# ===== УПРАВЛЕНИЕ ГРУППАМИ =====
@login_required
@user_passes_test(is_allowed_user)
def groups_list(request):
    groups = DeviceGroup.objects.all().order_by('name')
    return render(request, 'meters/groups_list.html', {'groups': groups})

@login_required
@user_passes_test(is_allowed_user)
def group_create(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        if not name:
            messages.error(request, 'Название группы обязательно.')
        elif DeviceGroup.objects.filter(name=name).exists():
            messages.error(request, 'Группа с таким названием уже существует.')
        else:
            DeviceGroup.objects.create(name=name, description=description)
            messages.success(request, f'Группа "{name}" создана.')
            return redirect('groups_list')
    return render(request, 'meters/group_form.html', {'action': 'create'})

@login_required
@user_passes_test(is_allowed_user)
def group_edit(request, group_id):
    group = get_object_or_404(DeviceGroup, id=group_id)
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        if not name:
            messages.error(request, 'Название группы обязательно.')
        elif DeviceGroup.objects.exclude(id=group_id).filter(name=name).exists():
            messages.error(request, 'Группа с таким названием уже существует.')
        else:
            group.name = name
            group.description = description
            group.save()
            messages.success(request, f'Группа "{name}" обновлена.')
            return redirect('groups_list')
    return render(request, 'meters/group_form.html', {'group': group, 'action': 'edit'})

@login_required
@user_passes_test(is_allowed_user)
def group_delete(request, group_id):
    group = get_object_or_404(DeviceGroup, id=group_id)
    if request.method == 'POST':
        group.delete()
        messages.success(request, f'Группа "{group.name}" удалена.')
        return redirect('groups_list')
    return render(request, 'meters/confirm_delete.html', {'object': group, 'type': 'группу'})

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from .registration import register_devices_for_robot, ROBOT_CONFIGS

@login_required
@user_passes_test(is_allowed_user)
def register_devices(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    robot_name = request.POST.get('robot_name')
    limit = request.POST.get('limit')
    limit = int(limit) if limit and limit.isdigit() else None
    
    if not robot_name:
        return JsonResponse({'error': 'robot_name required'}, status=400)
    
    if robot_name not in ROBOT_CONFIGS:
        return JsonResponse({'error': f'No registration config for {robot_name}'}, status=400)
    
    try:
        count = register_devices_for_robot(robot_name, limit)
        return JsonResponse({'message': f'Registered {count} new devices for {robot_name}', 'count': count})
    except Exception as e:
        logger.error(f"Error registering devices for {robot_name}: {e}")
        return JsonResponse({'error': str(e)}, status=500)

def register_devices_page(request):
    robots = list(ROBOT_CONFIGS.keys())
    return render(request, 'meters/register_devices.html', {'robots': robots})

from django.utils import timezone
from datetime import datetime

@login_required
@user_passes_test(is_allowed_user)
def registered_devices_report(request):
    # Получаем параметры фильтра
    source_filter = request.GET.get('source', '')
    search = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    export = request.GET.get('export', False)

    # Базовый запрос – устройства, зарегистрированные через автоматическую регистрацию
    # Предполагаем, что при регистрации мы заполняем поле api_id значением источника (например, 'SR', 'SX', 'HK')
    devices_qs = Device.objects.select_related('model').filter(
        Q(api_id__isnull=False) & ~Q(api_id='')
    ).order_by('-created_at')

    # Фильтр по источнику
    if source_filter:
        devices_qs = devices_qs.filter(api_id=source_filter)

    # Поиск по серийному номеру
    if search:
        devices_qs = devices_qs.filter(serial_number__icontains=search)

    # Фильтр по дате регистрации
    if date_from:
        try:
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d').date()
            devices_qs = devices_qs.filter(created_at__date__gte=date_from_obj)
        except ValueError:
            pass
    if date_to:
        try:
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d').date()
            devices_qs = devices_qs.filter(created_at__date__lte=date_to_obj)
        except ValueError:
            pass

    # Экспорт в Excel
    if export == 'xlsx':
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Зарегистрированные счетчики"

        headers = ['Заводской номер', 'Тип ПУ', 'Источник', 'Дата регистрации', 'Статус']
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal='center')

        for row_idx, device in enumerate(devices_qs, 2):
            ws.cell(row=row_idx, column=1, value=device.serial_number)
            ws.cell(row=row_idx, column=2, value=device.model.model_name if device.model else '')
            ws.cell(row=row_idx, column=3, value=device.api_id or '—')
            ws.cell(row=row_idx, column=4, value=device.created_at.strftime('%Y-%m-%d %H:%M:%S') if device.created_at else '')
            ws.cell(row=row_idx, column=5, value=device.status)

        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = max_length + 2

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="registered_devices_{timezone.now().date()}.xlsx"'
        wb.save(response)
        return response

    # Пагинация
    page = int(request.GET.get('page', 1))
    per_page = 20
    total = devices_qs.count()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
    devices = devices_qs[(page-1)*per_page:page*per_page]
    page_range = range(1, total_pages + 1)

    # Список источников (роботов) для фильтра с флагом selected
    sources = [
        ('SR', 'SunRise', source_filter == 'SR'),
        ('SX', 'Sanxing_old', source_filter == 'SX'),
        ('HK', 'Hexing KUK', source_filter == 'HK'),
        # Добавьте другие источники по мере необходимости
    ]

    context = {
        'devices': devices,
        'total': total,
        'page': page,
        'total_pages': total_pages,
        'page_range': page_range,
        'sources': sources,
        'selected_source': source_filter,
        'search': search,
        'date_from': date_from,
        'date_to': date_to,
        'start_index': (page - 1) * per_page,
    }
    return render(request, 'meters/registered_devices.html', context)

@login_required
@user_passes_test(is_allowed_user)
def bulk_update_region_page(request):
    if request.method == 'POST':
        serials_text = request.POST.get('serials', '').strip()
        region_id = request.POST.get('region_id', '')
        substation_id = request.POST.get('substation_id', '')
        feeder_id = request.POST.get('feeder_id', '')
        tp_id = request.POST.get('tp_id', '')

        if not serials_text:
            messages.error(request, 'Список серийных номеров пуст.')
            return redirect('bulk_update_region_page')

        serials = [s.strip() for s in serials_text.split('\n') if s.strip()]
        if not serials:
            messages.error(request, 'Нет валидных номеров.')
            return redirect('bulk_update_region_page')

        # Подготавливаем поля для обновления
        update_fields = {}
        if region_id:
            update_fields['region_id'] = region_id
        if substation_id:
            update_fields['substation_id'] = substation_id
        if feeder_id:
            update_fields['feeder_id'] = feeder_id
        if tp_id:
            update_fields['tp_id'] = tp_id

        if not update_fields:
            messages.warning(request, 'Выберите хотя бы одно поле для обновления.')
            return redirect('bulk_update_region_page')

        updated = 0
        not_found = []
        for sn in serials:
            device = Device.objects.filter(serial_number=sn).first()
            if device:
                Device.objects.filter(id=device.id).update(**update_fields)
                updated += 1
            else:
                not_found.append(sn)

        if updated:
            messages.success(request, f'Обновлено устройств: {updated}')
        if not_found:
            messages.warning(request, f'Не найдены: {", ".join(not_found[:10])}')

        return redirect('bulk_update_region_page')

    # GET – показываем форму
    regions = Region.objects.all().order_by('name')
    substations = Substation.objects.all().order_by('name')
    feeders = Feeder.objects.all().order_by('name')
    tps = TransformerSubstation.objects.all().order_by('name')

    context = {
        'regions': regions,
        'substations': substations,
        'feeders': feeders,
        'tps': tps,
    }
    return render(request, 'meters/bulk_update_region.html', context)

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

# @login_required
# @user_passes_test(is_allowed_user)
def export_devices_csv(request):
    """Экспорт реестра приборов в CSV (потоково, без ограничений)."""
    # Берём те же фильтры, что и в devices
    qs = Device.objects.select_related('model', 'region', 'substation', 'feeder', 'tp').prefetch_related('groups')

    # Применяем фильтры (копируем из devices)
    search = request.GET.get('search', '')
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    region_filter = request.GET.get('region', '')
    substation_filter = request.GET.get('substation', '')
    feeder_filter = request.GET.get('feeder', '')
    tp_filter = request.GET.get('tp', '')
    group_filter = request.GET.get('group', '')

    if search:
        qs = qs.filter(Q(serial_number__icontains=search) | Q(model__model_name__icontains=search))
    if producer_filter:
        qs = qs.filter(model__device_type_str=producer_filter)
    if model_filter:
        qs = qs.filter(model__id=model_filter)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if region_filter:
        qs = qs.filter(region_id=region_filter)
    if substation_filter:
        qs = qs.filter(substation_id=substation_filter)
    if feeder_filter:
        qs = qs.filter(feeder_id=feeder_filter)
    if tp_filter:
        qs = qs.filter(tp_id=tp_filter)
    if group_filter:
        qs = qs.filter(groups__id=group_filter)

    # Определяем онлайн-статус для каждого устройства
    # Аннотируем has_recent_reading (как в devices)
    seven_days_ago = timezone.now() - timedelta(days=7)
    qs = qs.annotate(
        has_recent_reading=Exists(
            Reading.objects.filter(
                device=OuterRef('pk'),
                timestamp__gte=seven_days_ago
            )
        )
    )

    # Генератор строк CSV
    def generate_csv():
        # Заголовки
        yield ['Заводской номер', 'Тип ПУ', 'Статус', 'Категория', 'Район', 'Подстанция', 'Фидер', 'ТП', 'Группы', 'Ток', 'Фазность', 'Статус связи', 'Дата добавления']

        for device in qs.iterator(chunk_size=1000):
            groups = ', '.join(g.name for g in device.groups.all())
            online_status = 'Онлайн' if device.has_recent_reading else 'Оффлайн'
            yield [
                device.serial_number,
                device.model.model_name if device.model else '',
                device.status,
                device.get_category_display() or '',
                device.region.name if device.region else '',
                device.substation.name if device.substation else '',
                device.feeder.name if device.feeder else '',
                device.tp.name if device.tp else '',
                groups,
                device.nominal_current or '',
                device.model.phases if device.model else '',
                online_status,
                device.created_at.strftime('%Y-%m-%d %H:%M:%S') if device.created_at else '',
            ]

    # Создаём StreamingHttpResponse
    response = StreamingHttpResponse(
        generate_csv(),
        content_type='text/csv; charset=utf-8'
    )
    response['Content-Disposition'] = f'attachment; filename="registry_devices_{timezone.now().date()}.csv"'
    return response

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

@login_required
@user_passes_test(is_allowed_user)
def export_devices_xlsx(request):
    """Экспорт реестра приборов в Excel (только до 5000 записей, иначе предлагаем CSV)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter
    from django.db.models import Exists, OuterRef

    # Базовый queryset с фильтрами (как в devices)
    qs = Device.objects.select_related('model', 'region', 'substation', 'feeder', 'tp').prefetch_related('groups')
    seven_days_ago = timezone.now() - timedelta(days=7)
    qs = qs.annotate(
        has_recent_reading=Exists(
            Reading.objects.filter(
                device=OuterRef('pk'),
                timestamp__gte=seven_days_ago
            )
        )
    )

    # Фильтры (копируем из devices)
    search = request.GET.get('search', '')
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    region_filter = request.GET.get('region', '')
    substation_filter = request.GET.get('substation', '')
    feeder_filter = request.GET.get('feeder', '')
    tp_filter = request.GET.get('tp', '')
    group_filter = request.GET.get('group', '')

    if search:
        qs = qs.filter(Q(serial_number__icontains=search) | Q(model__model_name__icontains=search))
    if producer_filter:
        qs = qs.filter(model__device_type_str=producer_filter)
    if model_filter:
        qs = qs.filter(model__id=model_filter)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if region_filter:
        qs = qs.filter(region_id=region_filter)
    if substation_filter:
        qs = qs.filter(substation_id=substation_filter)
    if feeder_filter:
        qs = qs.filter(feeder_id=feeder_filter)
    if tp_filter:
        qs = qs.filter(tp_id=tp_filter)
    if group_filter:
        qs = qs.filter(groups__id=group_filter)

    count = qs.count()
    MAX_ROWS = 10000
    if count > MAX_ROWS:
        messages.warning(request, f'Слишком много записей ({count}). Используйте CSV-экспорт.')
        return redirect('devices')

    # Создаём книгу
    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр приборов"

    headers = [
        'Заводской номер', 'Тип ПУ', 'Статус', 'Категория', 'Район', 'Подстанция',
        'Фидер', 'ТП', 'Группы', 'Ток', 'Фазность', 'Статус связи', 'Дата добавления'
    ]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill(start_color='2d3e50', end_color='2d3e50', fill_type='solid')
        cell.alignment = Alignment(horizontal='center')

    for row_idx, device in enumerate(qs, 2):
        groups = ', '.join(g.name for g in device.groups.all())
        online_status = 'Онлайн' if device.has_recent_reading else 'Оффлайн'
        ws.cell(row=row_idx, column=1, value=device.serial_number)
        ws.cell(row=row_idx, column=2, value=device.model.model_name if device.model else '')
        ws.cell(row=row_idx, column=3, value=device.status)
        ws.cell(row=row_idx, column=4, value=device.get_category_display())
        ws.cell(row=row_idx, column=5, value=device.region.name if device.region else '')
        ws.cell(row=row_idx, column=6, value=device.substation.name if device.substation else '')
        ws.cell(row=row_idx, column=7, value=device.feeder.name if device.feeder else '')
        ws.cell(row=row_idx, column=8, value=device.tp.name if device.tp else '')
        ws.cell(row=row_idx, column=9, value=groups)
        ws.cell(row=row_idx, column=10, value=device.nominal_current or '')
        ws.cell(row=row_idx, column=11, value=device.model.phases if device.model else '')
        ws.cell(row=row_idx, column=12, value=online_status)
        ws.cell(row=row_idx, column=13, value=device.created_at.strftime('%Y-%m-%d %H:%M:%S') if device.created_at else '')

    # Автоширина
    for col in ws.columns:
        max_length = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_length + 2, 50)

    # Сохраняем
    from io import BytesIO
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="registry_devices_{timezone.now().date()}.xlsx"'
    return response

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

@require_POST
@login_required
def fetch_device_readings(request):
    serial_number = request.POST.get('serial_number')
    if not serial_number:
        return JsonResponse({'error': 'Не указан заводской номер'}, status=400)

    try:
        device = Device.objects.get(serial_number=serial_number)
    except Device.DoesNotExist:
        return JsonResponse({'error': 'Устройство не найдено'}, status=404)

    # Проверяем, есть ли уже запущенная задача для этого устройства
    existing_task = ReadingFetchTask.objects.filter(
        device=device,
        status__in=['pending', 'running']
    ).first()
    if existing_task:
        return JsonResponse({
            'task_id': existing_task.id,
            'status': existing_task.status,
            'message': 'Задача уже выполняется'
        })

    # Создаём задачу
    task = ReadingFetchTask.objects.create(
        device=device,
        serial_number=serial_number,
        status='pending'
    )

    # Запускаем в фоновом потоке
    def run_task():
        from django.core.management import call_command
        call_command('fetch_readings_task', str(task.id))

    thread = threading.Thread(target=run_task)
    thread.daemon = True
    thread.start()

    return JsonResponse({
        'task_id': task.id,
        'status': 'pending',
        'message': 'Задача запущена'
    })

@login_required
def fetch_readings_status(request):
    task_id = request.GET.get('task_id')
    if not task_id:
        return JsonResponse({'error': 'Не указан task_id'}, status=400)

    try:
        task = ReadingFetchTask.objects.get(id=task_id)
    except ReadingFetchTask.DoesNotExist:
        return JsonResponse({'error': 'Задача не найдена'}, status=404)

    return JsonResponse({
        'status': task.status,
        'records_loaded': task.records_loaded,
        'error_message': task.error_message,
        'completed_at': task.completed_at.isoformat() if task.completed_at else None,
    })

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

@login_required
@user_passes_test(is_allowed_user)
def bulk_update_category_page(request):
    if request.method == 'POST':
        serials_text = request.POST.get('serials', '').strip()
        new_category = request.POST.get('category', '')
        if not serials_text:
            messages.error(request, 'Список серийных номеров пуст.')
            return redirect('bulk_update_category_page')
        if not new_category:
            messages.error(request, 'Выберите категорию.')
            return redirect('bulk_update_category_page')
        serials = [s.strip() for s in serials_text.split('\n') if s.strip()]
        updated = 0
        not_found = []
        for sn in serials:
            device = Device.objects.filter(serial_number=sn).first()
            if device:
                device.category = new_category
                device.save()
                updated += 1
            else:
                not_found.append(sn)
        if updated:
            messages.success(request, f'Обновлено устройств: {updated}')
        if not_found:
            messages.warning(request, f'Не найдены: {", ".join(not_found[:10])}')
        return redirect('bulk_update_category_page')
    # GET – показываем форму
    categories = Device.CATEGORY_CHOICES
    return render(request, 'meters/bulk_update_category.html', {'categories': categories})


from django.http import JsonResponse
from django.views.decorators.http import require_GET

@require_GET
def search_devices(request):
    """API для поиска устройств по заводскому номеру (автодополнение)."""
    term = request.GET.get('term', '').strip()
    if not term or len(term) < 2:
        return JsonResponse([], safe=False)
    devices = Device.objects.filter(
        serial_number__icontains=term,
        status='active'
    ).order_by('serial_number')[:20]
    data = [{'id': d.id, 'text': d.serial_number} for d in devices]
    return JsonResponse(data, safe=False)


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
