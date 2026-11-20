"""Баланс: показания по связям, сводка, отчёт по всем, коэффициенты устройства.

Вынесено из views.py. Маршруты подключены в urls.py через `views_balance`.
`balance_report` намеренно оставлен без маршрута (отключённая функция).
"""
import json
import logging
from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from .balance_calculator import (
    get_reading_for_date, get_coefficient_for_date, get_or_create_period,
    calculate_balance_for_substation,
)
from .models import Device, Region, Substation, CoefficientHistory, BalanceResult
from .views import is_allowed_user

logger = logging.getLogger(__name__)


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
