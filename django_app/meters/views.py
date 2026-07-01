from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.db.models import Count, Q, OuterRef, Exists, Subquery, Max, Value, IntegerField
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.core.cache import cache
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.db import transaction
from datetime import datetime, timedelta, date
import csv
import io
import openpyxl
from io import BytesIO
import pandas as pd
import logging
import threading

from .models import Device, Reading, SyncStatus, MeterModel

logger = logging.getLogger(__name__)

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
        "8": "Others",
    }
    return mapping.get(code, code or "Неизвестно")

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

        active_device_ids = Reading.objects.filter(
            device__status='active',
            timestamp__gte=start_dt,
            timestamp__lte=end_dt
        ).values_list('device_id', flat=True).distinct()
        active_today = active_device_ids.count()
        active_percent = round((active_today / total_devices * 100), 1) if total_devices else 0

        auto_notes = [
            "Авто-сбор: База cEnergo",
            "Авто-сбор: Sanxing_old",
            "Авто-сбор: SunRise",
            "Hexing KUK",
            "RiseSun"
        ]
        manual_today = Reading.objects.filter(
            device__status='active',
            timestamp__gte=start_dt,
            timestamp__lte=end_dt
        ).exclude(notes__in=auto_notes).count()

        devices = Device.objects.filter(status='active').select_related('model').only(
            'id', 'model__device_type_str', 'model__device_type_id'
        )
        active_set = set(active_device_ids)
        producer_stats_dict = {}
        for device in devices:
            model = device.model
            if model:
                code = model.device_type_str or ""
                type_id = model.device_type_id or ""
                display_name = get_producer_display(code, type_id)
            else:
                display_name = "Неизвестно"
            if display_name not in producer_stats_dict:
                producer_stats_dict[display_name] = {'total': 0, 'active': 0}
            producer_stats_dict[display_name]['total'] += 1
            if device.id in active_set:
                producer_stats_dict[display_name]['active'] += 1

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

# ===== УСТРОЙСТВА (Реестр) =====
@login_required
@user_passes_test(is_allowed_user)
def devices(request):
    search = request.GET.get('search', '')
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    page = int(request.GET.get('page', 1))
    per_page = 20

    qs = Device.objects.select_related('model')

    if search:
        qs = qs.filter(serial_number__icontains=search)

    if producer_filter:
        qs = qs.filter(model__device_type_str=producer_filter)

    if model_filter:
        qs = qs.filter(model__id=model_filter)

    if status_filter:
        qs = qs.filter(status=status_filter)

    total = qs.count()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
    devices_list = qs.order_by('serial_number')[(page-1)*per_page:page*per_page]
    page_range = range(1, total_pages + 1)

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

    statuses = ['active', 'repair', 'inactive']
    statuses_with_selected = [(s, s == status_filter) for s in statuses]

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
    }
    return render(request, 'meters/devices.html', context)

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
                    Device.objects.create(
                        serial_number=serial,
                        model=model,
                        status=status,
                        nominal_current=nominal_current,
                        phase=phase,
                        askue_id=askue_id,
                        api_id=api_id,
                    )
                    messages.success(request, f'Прибор {serial} добавлен.')
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
                messages.success(request, f'Добавлено: {added}. Пропущено: {len(skipped)}.')
                if skipped:
                    messages.warning(request, f'Пропущенные: {", ".join(skipped[:5])}')
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
                    Device.objects.create(
                        serial_number=sn,
                        model=model,
                        status=status,
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
    serial = request.GET.get('serial', '')
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
        device = Device.objects.filter(serial_number=serial).first()
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
            messages.success(request, f"Модель {model.model_name} добавлена.")
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
            messages.success(request, f"Модель {model.model_name} обновлена.")
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
        messages.success(request, f"Модель удалена.")
        return redirect('models')
    return render(request, 'meters/confirm_delete.html', {'object': model, 'type': 'модель'})

def producer_stats_table(request):
    period = request.GET.get('period', 'today')
    cache_key = f'producer_stats_table_{period}'
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

        devices = Device.objects.filter(status='active').select_related('model').only(
            'id', 'model__device_type_str', 'model__device_type_id'
        )
        active_device_ids = set(
            Reading.objects.filter(
                device__status='active',
                timestamp__gte=start_dt,
                timestamp__lte=end_dt
            ).values_list('device_id', flat=True).distinct()
        )

        producer_stats_dict = {}
        for device in devices:
            model = device.model
            if model:
                code = model.device_type_str or ""
                type_id = model.device_type_id or ""
                display_name = get_producer_display(code, type_id)
            else:
                display_name = "Неизвестно"
            if display_name not in producer_stats_dict:
                producer_stats_dict[display_name] = {'total': 0, 'active': 0}
            producer_stats_dict[display_name]['total'] += 1
            if device.id in active_device_ids:
                producer_stats_dict[display_name]['active'] += 1

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
    # Получаем параметры
    period = request.GET.get('period', 'today')
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    export = request.GET.get('export', False)

    # Определяем дату начала периода
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

    # Базовый запрос устройств
    devices_qs = Device.objects.select_related('model')

    # Аннотация: дата последнего показания
    last_reading_subquery = Reading.objects.filter(
        device=OuterRef('pk')
    ).order_by('-timestamp').values('timestamp')[:1]
    devices_qs = devices_qs.annotate(
        last_reading_date=Subquery(last_reading_subquery)
    )

    # Аннотация: есть ли показания за последние 7 дней (для статуса связи)
    seven_days_ago = timezone.now() - timedelta(days=7)
    devices_qs = devices_qs.annotate(
        has_recent_reading=Exists(
            Reading.objects.filter(
                device=OuterRef('pk'),
                timestamp__gte=seven_days_ago
            )
        )
    )

    # Применяем фильтры
    if status_filter:
        devices_qs = devices_qs.filter(status=status_filter)

    if model_filter:
        devices_qs = devices_qs.filter(model__id=model_filter)

    if producer_filter:
        devices_qs = devices_qs.filter(model__device_type_str=producer_filter)

    # Список устройств без показаний за период
    from django.db.models import Count, Value, IntegerField
    from django.db.models.functions import Coalesce

    readings_subquery = Reading.objects.filter(
        device_id=OuterRef('id'),
        timestamp__date__gte=start_date,
        timestamp__date__lte=today
    ).values('device_id').annotate(cnt=Count('id')).values('cnt')

    devices_qs = devices_qs.annotate(
        readings_count=Coalesce(readings_subquery, Value(0))
    ).filter(readings_count=0)

    # ===== ЭКСПОРТ В CSV =====
    if export == '1':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="missing_readings_{period}_{today}.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Заводской номер', 'Модель', 'Статус реестра',
            'Статус связи', 'Производитель', 'Ток', 'Фазность', 'Последнее показание'
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
                device.last_reading_date.strftime('%Y-%m-%d %H:%M:%S') if device.last_reading_date else '',
            ])
        return response

    # ===== ЭКСПОРТ В EXCEL =====
    if export == 'xlsx':
        import openpyxl
        from openpyxl.styles import Font, Alignment
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Счетчики без показаний"

        headers = [
            'Заводской номер', 'Модель', 'Статус реестра',
            'Статус связи', 'Производитель', 'Ток', 'Фазность', 'Последнее показание'
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
            ws.cell(row=row_idx, column=8, value=device.last_reading_date.strftime('%Y-%m-%d %H:%M:%S') if device.last_reading_date else '')

        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = max_length + 2

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="missing_readings_{period}_{today}.xlsx"'
        wb.save(response)
        return response

    # ===== ПАГИНАЦИЯ =====
    page = int(request.GET.get('page', 1))
    per_page = 50
    total = devices_qs.count()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
    devices_list = devices_qs.order_by('serial_number')[(page-1)*per_page:page*per_page]
    page_range = range(1, total_pages + 1)

    # Добавляем producer_display для отображения в таблице
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

    statuses_with_selected = [(s, s == status_filter) for s in ['active', 'repair', 'inactive']]

    context = {
        'devices': devices_list,
        'total': total,
        'page': page,
        'total_pages': total_pages,
        'page_range': page_range,
        'period': period,
        'producer_filter': producer_filter,
        'model_filter': model_filter,
        'status_filter': status_filter,
        'start_index': (page - 1) * per_page,
        'periods': periods,
        'producers_with_selected': producers_with_selected,
        'models_with_selected': models_with_selected,
        'statuses_with_selected': statuses_with_selected,
        'start_date': start_date,
    }
    return render(request, 'meters/missing_readings.html', context)

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

            obj, created = Reading.objects.update_or_create(
                device=device,
                timestamp=dt,
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