"""Сверка с 1С: загрузка выгрузки, заявки, статистика, синхронизация статусов и проверка заявок.

Вынесено из views.py. Маршруты подключены в urls.py через `views_1c`.
"""
import os
import re
import json
import subprocess
import threading
from datetime import datetime

import pandas as pd
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.management import call_command
from django.core.paginator import Paginator
from django.db.models import Q, Count
from django.http import JsonResponse, HttpResponse, FileResponse, HttpResponseForbidden
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

from .models import CompareResult, Device, Region, Reading, SyncStatus
from .views import is_allowed_user, compare_models


# ===== СВЕРКА С 1С =====
def find_header_row(df, keywords=None):
    """Определяет строку с заголовками по наличию ключевых слов."""
    if keywords is None:
        keywords = ['заводской', 'зав', '№', 'счетчик', 'Тип ПУ', 'тип', 'лицевой']
    for idx, row in df.iterrows():
        # Преобразуем все ячейки строки в строки и объединяем
        row_text = ' '.join([str(cell).lower() for cell in row if pd.notna(cell)])
        if any(kw in row_text for kw in keywords):
            return idx
    return 0  # если не нашли – используем первую строку


# @login_required
def compare_1c(request):
    """
    Загрузка Excel-файла из 1С.
    - Обязательный выбор района из выпадающего списка.
    - Все проблемные заявки относятся к выбранному району.
    - В БД сохраняются только проблемные записи (не OK и не partial).
    """
    if request.GET.get('clear') == '1':
        request.session.pop('compare_results', None)
        request.session.pop('compare_total', None)
        messages.info(request, 'Данные сессии очищены.')
        return redirect('compare_1c')

    if request.method == 'POST' and request.FILES.get('excel_file'):
        file = request.FILES['excel_file']
        
        # --- Получаем выбранный район из формы ---
        region_id = request.POST.get('region_id')
        if not region_id:
            messages.error(request, 'Пожалуйста, выберите район для загрузки.')
            return redirect('compare_1c')
        
        try:
            default_region = Region.objects.get(id=region_id)
        except Region.DoesNotExist:
            messages.error(request, 'Выбранный район не найден.')
            return redirect('compare_1c')
        
        messages.info(request, f'Загрузка для района: "{default_region.name}"')

        # --- ЧТЕНИЕ ФАЙЛА (через calamine) ---
        try:
            file.seek(0)
            df_raw = pd.read_excel(file, engine='calamine', header=None, dtype=str)
        except Exception as e:
            messages.error(request, f'Ошибка чтения файла: {e}')
            return redirect('compare_1c')

        if df_raw.empty:
            messages.error(request, 'Файл пуст.')
            return redirect('compare_1c')

        header_row = find_header_row(df_raw)
        
        try:
            file.seek(0) # Обязательно сбрасываем указатель перед вторым чтением!
            df = pd.read_excel(file, engine='calamine', header=header_row, dtype=str)
        except Exception as e:
            messages.error(request, f'Ошибка чтения данных файла: {e}')
            return redirect('compare_1c')

        # Определяем колонки
        serial_col = None
        model_col = None
        account_col = None
        subscriber_col = None
        for col in df.columns:
            col_lower = str(col).lower()
            # Ищем заводской номер
            if 'заводской' in col_lower or 'зав' in col_lower or '№' in col_lower:
                serial_col = col
            # Ищем тип ПУ
            if 'счетчик' in col_lower or 'тип пу' in col_lower or 'тип' in col_lower:
                model_col = col
            # Ищем лицевой счет
            if 'лицевой' in col_lower or 'лиц' in col_lower:
                account_col = col
            # Ищем абонента
            if 'наименование' in col_lower or 'абонент' in col_lower:
                subscriber_col = col

        if serial_col is None:
            messages.error(request, 'Не найдена колонка с заводскими номерами.')
            return redirect('compare_1c')

        # --- Индексы устройств ---
        devices = Device.objects.filter(status__in=['active', 'temporary_off']).select_related('model', 'region')
        devices_by_norm = {}

        def norm_serial(s):
            if not s:
                return ''
            digits = re.sub(r'\D', '', str(s))
            return digits.lstrip('0') or '0'

        for d in devices:
            norm = norm_serial(d.serial_number)
            devices_by_norm[norm] = d

        today = timezone.now().date()
        created_count = 0
        updated_count = 0
        skipped_ok = 0
        skipped_partial = 0

        for idx, row in df.iterrows():
            serial_val = row[serial_col]
            if pd.isna(serial_val) or str(serial_val).strip() == '':
                continue

            # Очистка номера от .0
            serial_1c_raw = str(serial_val).strip()
            serial_1c_raw = re.sub(r'\.0+$', '', serial_1c_raw)
            serial_1c = serial_1c_raw

            model_1c = str(row[model_col]).strip() if model_col and not pd.isna(row[model_col]) else ''
            account_1c = str(row[account_col]).strip() if account_col and not pd.isna(row[account_col]) else ''
            subscriber_1c = str(row[subscriber_col]).strip() if subscriber_col and not pd.isna(row[subscriber_col]) else ''

            # Поиск устройства
            device = None
            norm = norm_serial(serial_1c)
            device = devices_by_norm.get(norm)

            # --- Определяем тип заявки ---
            found = device is not None
            model_db = ''
            model_match_type = 'none'
            is_online = False
            last_reading_value = None
            last_reading_date = None
            action_type = 'not_found'
            action_text = ''

            if device:
                model_db = device.model.model_name if device.model else ''
                model_match_type = compare_models(model_1c, model_db)

                last_reading = Reading.objects.filter(device=device).order_by('-timestamp').first()
                if last_reading:
                    last_reading_value = float(last_reading.reading_value)
                    last_reading_date = last_reading.timestamp
                    days = (timezone.now() - last_reading.timestamp).days
                    is_online = days < 7

                # --- ПРОВЕРКА НА ВРЕМЕННО ОТКЛЮЧЕННЫЙ СЧЕТЧИК (первая) ---
                if device.status == 'temporary_off':
                    action_type = 'temporary_off'
                    action_text = 'Счётчик временно отключен...'
                else:
                    # --- Остальная логика для активных устройств ---
                    if model_match_type == 'exact' and is_online:
                        skipped_ok += 1
                        continue
                    elif model_match_type == 'partial':
                        skipped_partial += 1
                        continue
                    elif model_match_type in ('partial', 'none'):
                        action_type = 'fix_model'
                        action_text = f'Проверьте и исправьте Тип ПУ в 1С на «{model_db}».'
                    elif not is_online:
                        action_type = 'offline'
                        action_text = 'Счётчик оффлайн. Проверьте связь по инструкции.'
                    else:
                        skipped_ok += 1
                        continue
            else:
                # Не найден
                action_type = 'not_found'
                action_text = f'Не найден в базе. Проверить фактический ПУ'

            # --- Сохраняем только проблемные заявки с выбранным районом ---
            existing_issue = CompareResult.objects.filter(serial_1c=serial_1c).order_by('-check_date').first()

            if existing_issue:
                # Если заявка закрыта – создаём новую
                if existing_issue.status in ['fixed', 'auto_fixed', 'ignored']:
                    CompareResult.objects.create(
                        serial_1c=serial_1c,
                        model_1c=model_1c,
                        account_1c=account_1c,
                        subscriber_1c=subscriber_1c,
                        device=device,
                        model_db=model_db,
                        model_match_type=model_match_type,
                        is_online=is_online,
                        last_reading_date=last_reading_date,
                        last_reading_value=last_reading_value,
                        region=default_region,
                        action_type=action_type,
                        action_text=action_text,
                        status='new',
                        check_date=today,
                    )
                    created_count += 1
                else:
                    # Заявка активна – обновляем её
                    existing_issue.model_1c = model_1c
                    existing_issue.account_1c = account_1c
                    existing_issue.subscriber_1c = subscriber_1c
                    existing_issue.device = device
                    existing_issue.model_db = model_db
                    existing_issue.model_match_type = model_match_type
                    existing_issue.is_online = is_online
                    existing_issue.last_reading_date = last_reading_date
                    existing_issue.last_reading_value = last_reading_value
                    existing_issue.region = default_region
                    existing_issue.action_type = action_type
                    existing_issue.action_text = action_text
                    existing_issue.check_date = today
                    existing_issue.save()
                    updated_count += 1
            else:
                # Нет заявки – создаём новую
                CompareResult.objects.create(
                    serial_1c=serial_1c,
                    model_1c=model_1c,
                    account_1c=account_1c,
                    subscriber_1c=subscriber_1c,
                    device=device,
                    model_db=model_db,
                    model_match_type=model_match_type,
                    is_online=is_online,
                    last_reading_date=last_reading_date,
                    last_reading_value=last_reading_value,
                    region=default_region,
                    action_type=action_type,
                    action_text=action_text,
                    status='new',
                    check_date=today,
                )
                created_count += 1

        messages.success(
            request,
            f'Загрузка завершена.\n'
            f'📌 Район: {default_region.name}\n'
            f'✅ Создано новых заявок: {created_count}\n'
            f'🔄 Обновлено заявок: {updated_count}\n'
            f'⏭️ Пропущено (OK): {skipped_ok}\n'
            f'⏭️ Пропущено (частичное совпадение): {skipped_partial}'
        )
        return redirect('compare_1c_issues')

    # GET – форма загрузки
    regions = Region.objects.all().order_by('name')
    return render(request, 'meters/compare_1c.html', {'regions': regions})


# @login_required
# @user_passes_test(is_allowed_user)
def compare_1c_export(request):
    """Экспорт результатов сверки в Excel."""
    results = request.session.get('compare_results', [])
    if not results:
        messages.error(request, 'Нет данных для экспорта.')
        return redirect('compare_1c')

    wb = Workbook()
    ws = wb.active
    ws.title = "Сверка с 1С"

    headers = [
        '№', 'Заводской номер (1С)', 'Найден в базе', 'Тип ПУ (1С)', 'Тип ПУ (база)',
        'Совпадение модели', 'Последнее показание', 'Дата показания', 'Статус связи',
        'Район', 'Действие'
    ]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill(start_color='2d3e50', end_color='2d3e50', fill_type='solid')
        cell.alignment = Alignment(horizontal='center')

    for row_idx, item in enumerate(results, 2):
        ws.cell(row=row_idx, column=1, value=row_idx-1)
        ws.cell(row=row_idx, column=2, value=item['serial_1c'])
        ws.cell(row=row_idx, column=3, value='Да' if item['found'] else 'Нет')
        ws.cell(row=row_idx, column=4, value=item['model_1c'])
        ws.cell(row=row_idx, column=5, value=item['model_db'])
        # Совпадение модели
        match_display = {
            'exact': 'Полное',
            'partial': 'Частичное',
            'none': 'Не совпадает'
        }.get(item.get('model_match_type'), '—')
        ws.cell(row=row_idx, column=6, value=match_display)
        ws.cell(row=row_idx, column=7, value=item['last_reading_value'])
        ws.cell(row=row_idx, column=8, value=item['last_reading_date'] or '')
        ws.cell(row=row_idx, column=9, value='Онлайн' if item['is_online'] else 'Оффлайн')
        ws.cell(row=row_idx, column=10, value=item['region'])
        ws.cell(row=row_idx, column=11, value=item.get('action_text', ''))

    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_length + 2, 40)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="compare_1c_{timezone.now().date()}.xlsx"'
    wb.save(response)
    return response


# @login_required
# @user_passes_test(is_allowed_user)
def compare_1c_issues(request):
    """Страница со списком заявок на исправление (карточки)."""
    # Фильтры
    status_filter = request.GET.get('status', 'new')  # по умолчанию новые
    region_filter = request.GET.get('region', '')
    action_filter = request.GET.get('action', '')
    upload_region = request.GET.get('upload_region', '')
    verified_filter = request.GET.get('verified', '')  # '' - все, 'yes', 'no'

    # Базовый QuerySet – только записи, требующие действия (исключаем ok и partial)
    qs = CompareResult.objects.exclude(
        action_type__in=['ok']
    ).exclude(
        model_match_type='partial'
    ).select_related('device', 'region', 'fixed_by')

    if status_filter:
        qs = qs.filter(status=status_filter)
    if region_filter:
        qs = qs.filter(region_id=region_filter)
    if action_filter:
        qs = qs.filter(action_type=action_filter)
    if verified_filter == 'yes':
        qs = qs.filter(verified=True)
    elif verified_filter == 'no':
        qs = qs.filter(verified=False)

    # Сортировка: сначала новые, потом по дате создания
    qs = qs.order_by('-updated_at')

    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Подготовка фильтров для выпадающих списков
    status_choices = [
        ('new', 'Новые', status_filter == 'new'),
        ('in_progress', 'в процессе', status_filter == 'in_progress'),
        ('fixed', 'Исправленные', status_filter == 'fixed'),
        ('auto_fixed', 'Автозакрыто', status_filter == 'auto_fixed'),
        ('ignored', 'Пропущенные', status_filter == 'ignored'),
        ('', 'Все', status_filter == ''),
    ]

    action_choices = [
        ('', 'Все', action_filter == ''),
        ('fix_model', 'Исправить Тип ПУ', action_filter == 'fix_model'),
        ('fix_serial', 'Исправить номер', action_filter == 'fix_serial'),
        ('not_found', 'Не найден', action_filter == 'not_found'),
        ('offline', 'Проверить связь', action_filter == 'offline'),
        ('temporary_off', 'Временно отключен', action_filter == 'temporary_off'),
    ]

    verified_choices = [
        ('', 'Все', verified_filter == ''),
        ('yes', 'Проверенные', verified_filter == 'yes'),
        ('no', 'Не проверенные', verified_filter == 'no'),
    ]

    can_verify = request.user.is_staff or request.user.groups.filter(name='operator').exists()

    regions = Region.objects.all().order_by('name')
    region_choices = [(r.id, r.name, str(r.id) == region_filter) for r in regions]
    region_choices.insert(0, ('', 'Все', region_filter == ''))

    verify_report = None
    report_path = '/app/exports/verify_1c_result.json'
    if os.path.exists(report_path):
        try:
            with open(report_path, 'r', encoding='utf-8') as f:
                verify_report = json.load(f)
        except Exception:
            pass
    
    context = {
        'page_obj': page_obj,
        'total_count': qs.count(),
        'status_choices': status_choices,
        'action_choices': action_choices,
        'region_choices': region_choices,
        'selected_status': status_filter,
        'selected_action': action_filter,
        'selected_region': region_filter,
        'verified_choices': verified_choices,
        'selected_verified': verified_filter,
        'can_verify': can_verify,
        'verify_report': verify_report,
    }
    return render(request, 'meters/compare_1c_issues.html', context)


# @login_required
# @user_passes_test(is_allowed_user)
def compare_1c_issue_update(request, issue_id):
    issue = get_object_or_404(CompareResult, id=issue_id)
    if request.method == 'POST':
        new_status = request.POST.get('status')
        comment = request.POST.get('comment', '').strip()
    
        if new_status in dict(CompareResult.STATUS_CHOICES):
            issue.status = new_status
            
            # Устанавливаем время обработки для всех статусов, кроме 'new'
            if new_status != 'new':
                issue.fixed_at = timezone.now()
                if request.user.is_authenticated:
                    issue.fixed_by = request.user
                else:
                    issue.fixed_by = None
            else:
                # Если случайно выбрали 'new' (хотя мы его скрыли), сбрасываем время
                issue.fixed_at = None
                issue.fixed_by = None

            if comment:
                issue.comment = comment
            issue.save()
            messages.success(request, f'Заявка обновлена: {issue.get_status_display()}')
        else:
            messages.error(request, 'Некорректный статус')
        return redirect('compare_1c_issues')

    status_choices_with_selected = [
        (value, label, issue.status == value) for value, label in CompareResult.STATUS_CHOICES if value not in ('new', 'auto_fixed')
    ]
    context = {
        'issue': issue,
        'status_choices': status_choices_with_selected,
    }
    return render(request, 'meters/compare_1c_issue_update.html', context)


# @login_required
# @user_passes_test(is_allowed_user)
def compare_1c_issues_export(request):
    """Экспорт заявок в CSV."""
    import csv
    from django.http import StreamingHttpResponse

    qs = CompareResult.objects.exclude(
        action_type__in=['ok']
    ).exclude(
        model_match_type='partial'
    ).select_related('device', 'region')

    def generate():
        yield ['ID', 'Заводской номер (1С)', 'Тип ПУ (1С)', 'Тип ПУ (база)', 'Совпадение модели',
               'Район', 'Статус связи', 'Действие', 'Статус заявки', 'Комментарий', 'Дата создания']
        for item in qs.iterator():
            yield [
                item.id,
                item.serial_1c,
                item.model_1c,
                item.model_db,
                item.get_model_match_type_display(),
                item.region.name if item.region else '',
                'Онлайн' if item.is_online else 'Оффлайн',
                item.action_text,
                item.get_status_display(),
                item.comment,
                item.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            ]

    response = StreamingHttpResponse(generate(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="compare_issues_{timezone.now().date()}.csv"'
    return response


def compare_1c_stats(request):
    region_filter = request.GET.get('region', '')
    
    qs = CompareResult.objects.exclude(
        Q(action_type='ok') | Q(model_match_type='partial')
    )
    
    if region_filter:
        qs = qs.filter(region_id=region_filter)
    
    # Сначала получаем все статусы
    total_stats = qs.aggregate(
        total=Count('id'),
        new=Count('id', filter=Q(status='new')),
        in_progress=Count('id', filter=Q(status='in_progress')),
        fixed=Count('id', filter=Q(status='fixed')),
        auto_fixed=Count('id', filter=Q(status='auto_fixed')),
        ignored=Count('id', filter=Q(status='ignored')),
    )
    
    # Оффлайн – считаем отдельно: это заявки с action_type='offline' И status != 'auto_fixed' (исключаем уже автозакрытые)
    # Также исключаем fixed и ignored, которые не являются оффлайн
    offline_count = qs.filter(
        action_type='offline',
        status__in=['new', 'in_progress']  # только активные оффлайн
    ).count()
    
    # Закрытые = исправлено + автозакрыто (фиксированные вручную или автоматически)
    closed_total = total_stats['fixed'] + total_stats['auto_fixed']
    
    # Процент закрытых от общего числа
    if total_stats['total'] > 0:
        closed_percent = round((closed_total / total_stats['total']) * 100, 1)
    else:
        closed_percent = 0
    
    # Статистика по районам
    region_stats = qs.values('region__id', 'region__name').annotate(
        total=Count('id'),
        new=Count('id', filter=Q(status='new')),
        in_progress=Count('id', filter=Q(status='in_progress')),
        fixed=Count('id', filter=Q(status='fixed')),
        auto_fixed=Count('id', filter=Q(status='auto_fixed')),
        ignored=Count('id', filter=Q(status='ignored')),
        offline=Count('id', filter=Q(action_type='offline', status__in=['new', 'in_progress'])),
    ).order_by('region__name')
    
    # Данные для диаграмм (если они используются)
    pie_data = {
        'labels': ['Новые', 'в процессе', 'Исправлено', 'Автозакрыто', 'Пропущено'],
        'values': [
            total_stats['new'],
            total_stats['in_progress'],
            total_stats['fixed'],
            total_stats['auto_fixed'],
            total_stats['ignored'],
        ],
        'colors': ['#ffc107', '#0d6efd', '#198754', '#17a2b8', '#6c757d'],
    }
    
    bar_data = {
        'labels': [s['region__name'] or 'Без района' for s in region_stats],
        'total': [s['total'] for s in region_stats],
        'new': [s['new'] for s in region_stats],
        'offline': [s['offline'] for s in region_stats],
    }
    
    regions = Region.objects.all().order_by('name')
    region_choices = [(r.id, r.name, str(r.id) == region_filter) for r in regions]
    region_choices.insert(0, ('', 'Все районы', region_filter == ''))

    context = {
        'total_stats': {
            'total': total_stats['total'],
            'new': total_stats['new'],
            'in_progress': total_stats['in_progress'],
            'fixed': total_stats['fixed'],
            'auto_fixed': total_stats['auto_fixed'],
            'ignored': total_stats['ignored'],
            'offline': offline_count,
            'closed': closed_total,
            'closed_percent': closed_percent,
        },
        'region_stats': region_stats,
        'pie_data_json': json.dumps(pie_data),
        'bar_data_json': json.dumps(bar_data),
        'region_choices': region_choices,
        'selected_region': region_filter,
        'has_results': region_stats.exists(),
    }
    return render(request, 'meters/compare_1c_stats.html', context)


@login_required
def check_offline_issues(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Требуются права администратора'}, status=403)
    try:
        call_command('auto_close_issues')
        return JsonResponse({'message': 'Автозакрытие выполнено успешно'}, status=200)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def compare_1c_issues_export_excel(request):
    """Экспорт заявок в Excel."""
    qs = CompareResult.objects.exclude(
        action_type__in=['ok']
    ).exclude(
        model_match_type='partial'
    ).select_related('device', 'region')

    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки на исправление"

    headers = [
        'ID', 'Заводской номер (1С)', 'Тип ПУ (1С)', 'Тип ПУ (база)',
        'Совпадение модели', 'Район', 'Статус связи', 'Действие',
        'Статус заявки', 'Комментарий', 'Дата создания', 'Дата обработки'
    ]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill(start_color='2d3e50', end_color='2d3e50', fill_type='solid')
        cell.alignment = Alignment(horizontal='center')

    for row_idx, item in enumerate(qs, 2):
        ws.cell(row=row_idx, column=1, value=item.id)
        ws.cell(row=row_idx, column=2, value=item.serial_1c)
        ws.cell(row=row_idx, column=3, value=item.model_1c)
        ws.cell(row=row_idx, column=4, value=item.model_db)
        ws.cell(row=row_idx, column=5, value=item.get_model_match_type_display())
        ws.cell(row=row_idx, column=6, value=item.region.name if item.region else '')
        ws.cell(row=row_idx, column=7, value='Онлайн' if item.is_online else 'Оффлайн')
        ws.cell(row=row_idx, column=8, value=item.action_text)
        ws.cell(row=row_idx, column=9, value=item.get_status_display())
        ws.cell(row=row_idx, column=10, value=item.comment)
        ws.cell(row=row_idx, column=11, value=item.created_at.strftime('%Y-%m-%d %H:%M:%S'))
        ws.cell(row=row_idx, column=12, value=item.fixed_at.strftime('%Y-%m-%d %H:%M:%S') if item.fixed_at else '')

    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_length + 2, 40)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="issues_{timezone.now().date()}.xlsx"'
    wb.save(response)
    return response


@login_required
def compare_1c_issue_verify(request, issue_id):
    """Переключает отметку 'Проверено' для заявки (только для staff/operator)."""
    if not request.user.is_staff and not request.user.groups.filter(name='operator').exists():
        return JsonResponse({'error': 'Permission denied'}, status=403)
    
    issue = get_object_or_404(CompareResult, id=issue_id)
    if request.method == 'POST':
        issue.verified = not issue.verified
        issue.save()
        return JsonResponse({'verified': issue.verified, 'id': issue.id})
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@login_required
@user_passes_test(is_allowed_user)
def sync_1c_manage(request):
    """Страница управления синхронизацией с 1С"""
    exports_dir = '/app/exports'
    result_path = os.path.join(exports_dir, 'sync_1c_result.json')
    
    new_meters_exists = os.path.exists(os.path.join(exports_dir, 'new_meters.csv'))
    missing_exists = os.path.exists(os.path.join(exports_dir, 'missing_in_1c.csv'))
    updated_exists = os.path.exists(os.path.join(exports_dir, 'updated_meters.csv'))
    
    new_mtime = datetime.fromtimestamp(os.path.getmtime(os.path.join(exports_dir, 'new_meters.csv'))).strftime('%d.%m.%Y %H:%M') if new_meters_exists else None
    missing_mtime = datetime.fromtimestamp(os.path.getmtime(os.path.join(exports_dir, 'missing_in_1c.csv'))).strftime('%d.%m.%Y %H:%M') if missing_exists else None
    updated_mtime = datetime.fromtimestamp(os.path.getmtime(os.path.join(exports_dir, 'updated_meters.csv'))).strftime('%d.%m.%Y %H:%M') if updated_exists else None

    # Читаем последние результаты из JSON, если они есть
    last_result = None
    last_run_time = None
    if os.path.exists(result_path):
        try:
            with open(result_path, 'r', encoding='utf-8') as f:
                last_result = json.load(f)
            # Время последнего запуска берем по дате модификации файла результата
            last_run_time = datetime.fromtimestamp(os.path.getmtime(result_path)).strftime('%d.%m.%Y %H:%M')
        except Exception:
            pass

    context = {
        'new_meters_exists': new_meters_exists,
        'new_mtime': new_mtime,
        'missing_exists': missing_exists,
        'missing_mtime': missing_mtime,
        'sync_status': SyncStatus.objects.filter(robot_name='Sync1C').first(),
        'last_result': last_result,       # <--- Передаем результаты
        'last_run_time': last_run_time,   # <--- Передаем время
        'updated_exists': updated_exists,
        'updated_mtime': updated_mtime,
    }
    return render(request, 'meters/sync_1c.html', context)


@login_required
@user_passes_test(is_allowed_user)
def run_sync_1c(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    apply_changes = request.POST.get('apply') == 'true'
    
    def run_task():
        cmd = ['python', 'manage.py', 'sync_1c_statuses']
        if apply_changes:
            cmd.append('--apply')
        
        log_path = '/app/exports/sync_1c.log'
        result_path = '/app/exports/sync_1c_result.json'
        
        # Удаляем старый результат, чтобы сайт не прочитал его раньше времени
        if os.path.exists(result_path):
            os.remove(result_path)
            
        # ОЧИЩАЕМ ЛОГ ПЕРЕД НОВЫМ ЗАПУСКОМ
        with open(log_path, 'w', encoding='utf-8') as f:
            pass
            
        try:
            with open(log_path, 'a', encoding='utf-8') as log_file:
                log_file.write(f"Запуск: {timezone.now()} | Apply: {apply_changes}\n")
                log_file.flush()
                process = subprocess.Popen(cmd, cwd='/app', stdout=log_file, stderr=subprocess.STDOUT)
                process.wait()
                log_file.write(f"Завершено с кодом: {process.returncode}\n")
        except Exception as e:
            pass

    thread = threading.Thread(target=run_task)
    thread.daemon = True
    thread.start()

    return JsonResponse({'message': 'Синхронизация запущена'})


@login_required
@user_passes_test(is_allowed_user)
def download_1c_report(request, filename):
    """Скачивание CSV отчета"""
    exports_dir = '/app/exports'
    # Защита от path traversal
    if '..' in filename or filename.startswith('/'):
        return HttpResponseForbidden()
        
    file_path = os.path.join(exports_dir, filename)
    if os.path.exists(file_path):
        return FileResponse(open(file_path, 'rb'), as_attachment=True, filename=filename)
    else:
        messages.error(request, 'Файл не найден. Сначала запустите синхронизацию.')
        return redirect('sync_1c_manage')


@login_required
@user_passes_test(is_allowed_user)
def check_sync_1c_status(request):
    result_path = '/app/exports/sync_1c_result.json'
    log_path = '/app/exports/sync_1c.log'
    
    if os.path.exists(result_path):
        try:
            with open(result_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return JsonResponse({'status': 'completed', 'data': data})
        except json.JSONDecodeError:
            # Файл еще дописывается, подождем
            return JsonResponse({'status': 'running'})
            
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8') as f:
            log_content = f.read()
            # Ищем именно код возврата 1
            if "Завершено с кодом: 1" in log_content:
                return JsonResponse({'status': 'error', 'message': 'Робот завершился с ошибкой. Проверьте файл sync_1c.log.'})
                
    return JsonResponse({'status': 'running'})


@login_required
@user_passes_test(is_allowed_user)
def verify_1c_issues_ajax(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    def run_task():
        from django.core.management import call_command
        try:
            call_command('verify_1c_issues')
        except Exception as e:
            print(f"Error: {e}")

    thread = threading.Thread(target=run_task)
    thread.daemon = True
    thread.start()
    return JsonResponse({'message': 'Запущена автоматическая проверка. Обновите страницу через 10-20 секунд.'})


@login_required
@user_passes_test(is_allowed_user)
def start_verify_1c_issues(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    result_path = '/app/exports/verify_1c_result.json'
    if os.path.exists(result_path):
        os.remove(result_path)

    def run_task():
        import subprocess
        try:
            subprocess.Popen(['python', 'manage.py', 'verify_1c_issues'], cwd='/app')
        except Exception:
            pass

    threading.Thread(target=run_task).start()
    return JsonResponse({'message': 'Запущено'})


@login_required
@user_passes_test(is_allowed_user)
def check_verify_1c_status(request):
    result_path = '/app/exports/verify_1c_result.json'
    
    if os.path.exists(result_path):
        try:
            with open(result_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return JsonResponse(data)
        except json.JSONDecodeError:
            return JsonResponse({'status': 'running'})
            
    return JsonResponse({'status': 'running'})
