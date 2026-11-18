from django.db.models import Q
from .models import Device


def find_device(serial_number, model=None):
    """
    Ищет устройство по серийному номеру.
    Сначала точное совпадение.
    Если не найдено — ищет по суффиксу (последние N символов).
    """
    if not serial_number:
        return None
    sn = str(serial_number).strip()

    # 1. Точное совпадение
    qs = Device.objects.filter(status='active', serial_number=sn)
    if model:
        qs = qs.filter(model=model)
    if qs.exists():
        return qs.first()

    # 2. Поиск по суффиксу (если номер в источнике короче)
    candidates = Device.objects.filter(status='active', serial_number__endswith=sn)
    if model:
        candidates = candidates.filter(model=model)
    if candidates.count() == 1:
        return candidates.first()

    return None


def is_sunrise_device(device):
    """Проверяет, относится ли устройство к SunRise."""
    if not device:
        return False
    # По askue_id или api_id
    if device.askue_id == '26':
        return True
    if device.api_id and device.api_id.upper().startswith('SR'):
        return True
    # По префиксам серийного номера
    prefixes = ['0861', '0862', '0863']
    for p in prefixes:
        if device.serial_number.startswith(p):
            return True
    return False

def is_risesun_device(device):
    """Проверяет, относится ли устройство к RiseSun."""
    if not device:
        return False
    if device.askue_id == '24':
        return True
    if device.api_id and device.api_id.upper().startswith('RS'):
        return True
    return False

def get_robot_devices(robot_name, status='active'):
    """
    Возвращает QuerySet устройств, которые должны обрабатываться указанным роботом.
    Используются поля askue_id и api_id для идентификации производителя/типа,
    а для SunRise — дополнительно префиксы серийного номера.
    """
    qs = Device.objects.filter(status=status).select_related('model')

    # SunRise: askue_id=26, api_id начинается с SR, ИЛИ префиксы 0861/0862/0863
    if robot_name == 'SunRise':
        condition = (
            Q(askue_id='26')
            | Q(api_id__istartswith='SR')
            | Q(serial_number__startswith='0861')
            | Q(serial_number__startswith='0862')
            | Q(serial_number__startswith='0863')
        )
        return qs.filter(condition)

    # Маппинг: robot_name -> условия фильтрации
    filters = {
        'RiseSun': Q(askue_id='24') | Q(api_id__istartswith='RS'),
        'cEnergo': Q(askue_id='23') | Q(api_id__istartswith='EM'),
        'Hexing_KUK': Q(askue_id='25') | Q(api_id__istartswith='UK'),
        'Hexing_POP': Q(askue_id='5') | Q(api_id__istartswith='HX'),
        'Sanxing_old': Q(askue_id='22') | Q(api_id__istartswith='SX'),
        'Sanxing_new_100A': Q(askue_id='18') | Q(api_id__istartswith='SX'),
        'Sanxing_new_5A': Q(askue_id='18') | Q(api_id__istartswith='SX'),
        'Star': Q(askue_id='14') | Q(api_id__istartswith='ST'),
        'Inhemeter': Q(askue_id='6') | Q(api_id__istartswith='IM'),
    }

    condition = filters.get(robot_name)
    if condition:
        return qs.filter(condition)
    return Device.objects.none()