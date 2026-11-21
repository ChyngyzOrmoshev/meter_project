"""Показания: просмотр, ручной ввод, импорт из Excel, отчёты, опрос счётчиков.

Вынесено из views.py. Маршруты подключены в urls.py через `views_readings`.
"""

import csv
import logging
import threading
from datetime import datetime, timedelta

import pandas as pd
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import OuterRef, Subquery, Exists
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Device, DeviceGroup, MeterConnection, MeterModel, Reading, ReadingFetchTask, Region
from .views import is_allowed_user, find_device, get_producer_display, producer_model_ids

logger = logging.getLogger(__name__)


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


def read_import_excel(file):
    """
    Читает Excel для импорта показаний. Все ячейки читаются как текст: так данные
    безопасно кладутся в JSON-сессию (даты Excel не превращаются в Timestamp) и серийные
    номера не становятся дробными ('61002510.0') из-за пустых ячеек в колонке.
    Пустые ячейки — None.
    """
    df = pd.read_excel(file, engine='openpyxl', dtype=str)
    return df.astype(object).where(df.notna(), None)


# ===== ГРУППОВОЙ ВВОД И ИМПОРТ EXCEL (С ФОНОМ) =====
def run_import_background(session_key):
    from django.contrib.sessions.backends.db import SessionStore
    try:
        session = SessionStore(session_key=session_key)
        if not session.exists(session_key):
            logger.warning("Сессия импорта не найдена")
            return
        import_data = session.get('import_data', [])
        params = session.get('import_params', {})
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
                sn = str(row.get(col_serial) or '').strip()
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

        session['import_result'] = {
            'success': success,
            'updated': updated,
            'errors': errors[:10],
            'skipped': skipped[:10],
            'skipped_empty': skipped_empty[:10],
            'error_count': len(errors),
            'skipped_count': len(skipped),
            'empty_count': len(skipped_empty),
        }
        session.pop('import_data', None)
        session.pop('import_params', None)
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
            df = read_import_excel(file)
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
        # Поток читает параметры из БД: сохраняем сессию до запуска, а после не даём
        # middleware перезаписать её устаревшей копией (иначе потеряется результат импорта)
        request.session.save()
        request.session.modified = False
        thread = threading.Thread(target=run_import_background, args=(request.session.session_key,))
        thread.daemon = True
        thread.start()
        messages.info(request, 'Импорт запущен в фоновом режиме. Обновите страницу через некоторое время для просмотра результатов.')
        return redirect('bulk_readings')

    return render(request, 'meters/bulk_readings.html')


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
    producer_name = request.GET.get('producer_name', '')
    if producer_filter:
        devices_qs = devices_qs.filter(model__device_type_str=producer_filter)
    if producer_name:
        devices_qs = devices_qs.filter(model_id__in=producer_model_ids(producer_name))
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


# импорт показания Exsel
@login_required
@user_passes_test(is_allowed_user)
def import_readings_excel(request):
    import logging
    logger = logging.getLogger(__name__)

    if request.method == 'POST' and request.FILES.get('excel_file'):
        file = request.FILES['excel_file']
        try:
            df = read_import_excel(file)
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
            sn = str(row.get(col_serial) or '').strip()
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
    producer_name = request.GET.get('producer_name', '')
    if producer_filter:
        devices_qs = devices_qs.filter(model__device_type_str=producer_filter)
    if producer_name:
        devices_qs = devices_qs.filter(model_id__in=producer_model_ids(producer_name))
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
