import logging
from decimal import Decimal
from datetime import datetime, timedelta
from django.db.models import Q
from .models import Device, Reading, Substation, Feeder, CalculationPeriod, BalanceResult, MeterConnection


logger = logging.getLogger(__name__)
from decimal import Decimal
from .models import CoefficientHistory

def get_coefficient_for_date(device, date_obj):
    """
    Возвращает расчётный коэффициент (ТТ * ТН) для устройства на указанную дату.
    Если есть запись в CoefficientHistory, где start_date <= date_obj и (end_date is None или end_date > date_obj),
    используем её. Иначе — текущие поля tt_ratio и tn_ratio.
    """
    if not device:
        return Decimal(1)
    
    # Ищем активную запись на дату
    history = CoefficientHistory.objects.filter(
        device=device,
        start_date__lte=date_obj
    ).filter(
        Q(end_date__isnull=True) | Q(end_date__gt=date_obj)
    ).order_by('-start_date').first()
    
    if history:
        return history.tt_ratio * history.tn_ratio
    
    # Если истории нет, используем текущие поля
    if device.tt_ratio and device.tn_ratio:
        return device.tt_ratio * device.tn_ratio
    
    return Decimal(1)

def get_or_create_period(substation, year, month, period_type):
    """Создаёт или возвращает период расчёта."""
    if period_type == 'decade1':
        start_date = datetime(year, month, 1).date()
        end_date = datetime(year, month, 11).date()
    elif period_type == 'decade2':
        start_date = datetime(year, month, 11).date()
        end_date = datetime(year, month, 21).date()
    elif period_type == 'decade3':
        if month == 12:
            next_month = 1
            next_year = year + 1
        else:
            next_month = month + 1
            next_year = year
        start_date = datetime(year, month, 21).date()
        end_date = datetime(next_year, next_month, 1).date()
    else:  # month
        if month == 12:
            next_year = year + 1
            next_month = 1
        else:
            next_year = year
            next_month = month + 1
        start_date = datetime(year, month, 1).date()
        end_date = datetime(next_year, next_month, 1).date() - timedelta(days=1)

    period, created = CalculationPeriod.objects.get_or_create(
        substation=substation,
        year=year,
        month=month,
        period_type=period_type,
        defaults={'start_date': start_date, 'end_date': end_date}
    )
    return period


def get_reading_for_date(device, date_obj, direction='aplus'):
    try:
        reading = Reading.objects.filter(
            device=device,
            timestamp__date=date_obj,
            direction=direction
        ).order_by('-timestamp').first()
        return reading.reading_value if reading else None
    except Reading.DoesNotExist:
        return None


def calculate_device_consumption(device, start_date, end_date, direction='aplus'):
    """
    Вычисляет потребление для одного счётчика за период по указанному направлению.
    Возвращает: (energy_kwh, start_reading, end_reading)
    """
    start_reading = get_reading_for_date(device, start_date, direction)
    end_reading = get_reading_for_date(device, end_date, direction)
    if start_reading is None or end_reading is None:
        return None, None, None

    diff = end_reading - start_reading
    coefficient = get_coefficient_for_date(device, end_date)  # используем историю коэффициентов
    energy = diff * coefficient
    return energy, start_reading, end_reading


def calculate_balance_for_substation(substation, year, month, force_recalc=False):
    result = {}
    for period_type in ['decade1', 'decade2', 'decade3']:
        period = get_or_create_period(substation, year, month, period_type)

        if not force_recalc:
            try:
                balance = BalanceResult.objects.get(substation=substation, period=period)
                result[period_type] = {
                    'input': balance.input_energy,
                    'output': balance.output_energy,
                    'imbalance': balance.imbalance_kwh,
                    'percent': balance.imbalance_percent,
                }
                continue
            except BalanceResult.DoesNotExist:
                pass

        # Вводной фидер (feeder_type='input')
        input_feeder = substation.feeders.filter(feeder_type='input').first()
        input_energy = Decimal(0)
        if input_feeder and input_feeder.head_meter:
            # A+ поступление
            energy_aplus, _, _ = calculate_device_consumption(
                input_feeder.head_meter,
                period.start_date,
                period.end_date,
                direction='aplus'
            )
            # A- отдача (вычитаем)
            energy_aminus, _, _ = calculate_device_consumption(
                input_feeder.head_meter,
                period.start_date,
                period.end_date,
                direction='aminus'
            )
            if energy_aplus is not None:
                input_energy += energy_aplus
            if energy_aminus is not None:
                input_energy -= energy_aminus

        # Отходящие фидеры (feeder_type='output')
        output_feeders = substation.feeders.filter(feeder_type='output')
        output_energy = Decimal(0)
        for feeder in output_feeders:
            if feeder.head_meter:
                energy_aplus, _, _ = calculate_device_consumption(
                    feeder.head_meter,
                    period.start_date,
                    period.end_date,
                    direction='aplus'
                )
                energy_aminus, _, _ = calculate_device_consumption(
                    feeder.head_meter,
                    period.start_date,
                    period.end_date,
                    direction='aminus'
                )
                if energy_aplus is not None:
                    output_energy += energy_aplus
                if energy_aminus is not None:
                    output_energy -= energy_aminus

        imbalance = input_energy - output_energy
        percent = (imbalance / input_energy * 100) if input_energy else Decimal(0)

        balance, _ = BalanceResult.objects.update_or_create(
            substation=substation,
            period=period,
            defaults={
                'input_energy': input_energy,
                'output_energy': output_energy,
                'imbalance_kwh': imbalance,
                'imbalance_percent': percent,
            }
        )
        result[period_type] = {
            'input': input_energy,
            'output': output_energy,
            'imbalance': imbalance,
            'percent': percent,
        }

    # Месяц – суммируем декады
    month_period = get_or_create_period(substation, year, month, 'month')
    total_input = sum(r['input'] for r in result.values())
    total_output = sum(r['output'] for r in result.values())
    total_imbalance = total_input - total_output
    total_percent = (total_imbalance / total_input * 100) if total_input else Decimal(0)

    BalanceResult.objects.update_or_create(
        substation=substation,
        period=month_period,
        defaults={
            'input_energy': total_input,
            'output_energy': total_output,
            'imbalance_kwh': total_imbalance,
            'imbalance_percent': total_percent,
        }
    )
    result['month'] = {
        'input': total_input,
        'output': total_output,
        'imbalance': total_imbalance,
        'percent': total_percent,
    }
    return result


def recalc_all_balances(year=None, month=None):
    """Пересчитывает баланс для всех ПС за указанный месяц (или текущий)."""
    if year is None or month is None:
        now = datetime.now()
        year = now.year
        month = now.month
    substations = Substation.objects.filter(is_active=True)
    for sub in substations:
        calculate_balance_for_substation(sub, year, month, force_recalc=True)

def get_reading_for_date(device, date_obj, direction='aplus'):
    from .models import Reading
    try:
        reading = Reading.objects.filter(
            device=device,
            timestamp__date=date_obj,
            direction=direction
        ).order_by('-timestamp').first()
        return reading.reading_value if reading else None
    except Reading.DoesNotExist:
        return None

from decimal import Decimal
from .models import CoefficientHistory

def get_coefficient_for_date(device, date_obj):
    from .models import CoefficientHistory
    history = CoefficientHistory.objects.filter(
        device=device,
        start_date__lte=date_obj
    ).order_by('-start_date').first()
    if history:
        return history.tt_ratio * history.tn_ratio
    if device.tt_ratio and device.tn_ratio:
        return device.tt_ratio * device.tn_ratio
    return Decimal(1)