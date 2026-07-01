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