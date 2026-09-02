from .models import Device

def find_device(serial_number, model=None):
    """
    Ищет устройство по серийному номеру.
    Сначала точное совпадение.
    Если не найдено, ищет по суффиксу (последние N символов).
    Если найдено несколько, возвращает первое (или None, если неоднозначно).
    """
    if not serial_number:
        return None
    sn = str(serial_number).strip()

    # 1. Точное совпадение
    qs = Device.objects.filter(status='active', serial_number=serial_number)
    if model:
        qs = qs.filter(model=model)
    if qs.exists():
        return qs.first()

    # 2. Поиск по суффиксу (если номер в источнике короче)
    # Например, "65501307" входит в "086365501307"
    candidates = Device.objects.filter(status='active', serial_number__endswith=serial_number)
    if model:
        candidates = candidates.filter(model=model)
    if candidates.count() == 1:
        return candidates.first()
    # elif candidates.count() > 1:
    #     # Если несколько, попробуем выбрать по длине (более длинный? или первый)
    #     # В вашем случае обычно один, поэтому вернём первый
    #     return candidates.first()

    # 3. Если ничего не найдено, вернуть None
    return None

from django.db.models import Q

def get_robot_devices(robot_name, status='active'):
    """
    Возвращает QuerySet устройств, которые должны обрабатываться указанным роботом.
    Используются поля askue_id и api_id для идентификации производителя/типа.
    """
    qs = Device.objects.filter(status=status).select_related('model')
    
    # Маппинг: robot_name -> условия фильтрации
    filters = {
        'RiseSun': Q(askue_id='24') | Q(api_id__istartswith='RS'),
        'SunRise': Q(askue_id='26') | Q(api_id__istartswith='SR'),
        'cEnergo': Q(askue_id='23') | Q(api_id__istartswith='EM'),
        'Hexing_KUK': Q(askue_id='25') | Q(api_id__istartswith='UK'),
        'Hexing_POP': Q(askue_id='5') | Q(api_id__istartswith='HX'),
        'Sanxing_old': Q(askue_id='22') | Q(api_id__istartswith='SX'),
        'Sanxing_new_100A': Q(askue_id='18') | Q(api_id__istartswith='SX'),
        'Sanxing_new_5A': Q(askue_id='18') | Q(api_id__istartswith='SX'),
        'Star': Q(askue_id='14') | Q(api_id__istartswith='ST'),
    }
    
    condition = filters.get(robot_name)
    if condition:
        return qs.filter(condition)
    else:
        return Device.objects.none()