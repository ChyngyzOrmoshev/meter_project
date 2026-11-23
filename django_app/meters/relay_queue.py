from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import RelayTask

# Если по счётчику уже выполняется задача, новую откладываем минимум на этот срок
BUSY_DELAY_MIN = 2
# Запас после времени второй фазы выполняющейся задачи
PHASE2_GAP_MIN = 1


def inflight_tasks(device, actions=('on', 'off')):
    """
    Задачи, команда по которым уже ушла или уходит прямо сейчас:
    - processing: воркер выполняет их в эту минуту;
    - pending с заполненным hexing_payload: первая фаза пройдена (команда отправлена),
      задача ждёт проверки итога (Hexing, SunRise, RiseSun, cEnergo).
    Отменять такие задачи нельзя: команда всё равно сработает (у cEnergo она ещё держится
    на сервере до 10 минут), а отмена скроет результат и нарушит порядок команд.
    """
    return RelayTask.objects.filter(device=device, action__in=actions).filter(
        Q(status='processing')
        | (Q(status='pending') & ~Q(hexing_payload__isnull=True) & ~Q(hexing_payload=''))
    )


def supersede_tasks(device, scheduled_at, reason, actions=('on', 'off')):
    """
    Подготовка очереди к новой задаче по счётчику.

    Отменяются только ожидающие задачи указанных действий, которые ещё не отправили команду.
    Задачи в полёте (см. inflight_tasks) не трогаем.

    Возвращает (scheduled_at, busy): если по счётчику есть задача в полёте, время новой
    сдвигается так, чтобы она пошла после неё: для processing — через BUSY_DELAY_MIN минут,
    для ожидающей проверки — после времени её второй фазы.
    """
    inflight = list(inflight_tasks(device, actions))
    inflight_ids = [t.id for t in inflight]

    RelayTask.objects.filter(
        device=device, action__in=actions, status='pending'
    ).exclude(id__in=inflight_ids).update(status='cancelled', error_message=reason)

    if not inflight:
        return scheduled_at, False

    now = timezone.now()
    earliest = now + timedelta(minutes=BUSY_DELAY_MIN)
    for t in inflight:
        if t.status == 'pending':
            earliest = max(earliest, t.scheduled_at + timedelta(minutes=PHASE2_GAP_MIN))
    return max(scheduled_at, earliest), True


def last_off_snapshot(device, before=None):
    """
    Лицевой счёт и ФИО из последней задачи на отключение счётчика, где они сохранены
    (для задач на включение, когда 1С их не вернула). Возвращает (account, name) или (None, None).
    """
    from .models import RelayTask
    qs = RelayTask.objects.filter(device=device, action='off').exclude(account_snapshot__isnull=True) \
        .exclude(account_snapshot='')
    if before is not None:
        qs = qs.filter(id__lt=before)
    t = qs.order_by('-id').first()
    return (t.account_snapshot, t.name_snapshot) if t else (None, None)


# Пометка успешной задачи включения: команда принята, счётчик ждёт нажатия кнопки на нём
READY_NOTE = 'Команда принята: счётчик ждёт нажатия кнопки на нём (готов к включению)'
# Через сколько часов без нажатия кнопки воркер сам перепроверяет состояние счётчика
READY_EXPIRE_HOURS = 6

# Счётчики 5А (через трансформатор тока) физически не отключаются
CT_METER_TEXT = 'счётчик 5А (через трансформатор тока): физически отключить нельзя'
CT_MAX_CURRENT_A = 10  # максимальный ток в скобках не выше этого — счётчик трансформаторного включения


def is_ct_meter(device):
    """
    Счётчик трансформаторного включения («5А»): максимальный ток в обозначении модели не выше 10 А
    (5(10), 5(7.5), 1/5(10), 0.25-5(10)) или в названии модели «CT». Прямого включения
    (5(80), 5(100), 10(80)) подлежат отключению. Модель не задана — считаем обычным.
    """
    import re
    model = getattr(device, 'model', None)
    if not model:
        return False
    if re.search(r'\bCT\b', (model.model_name or '').upper()):
        return True
    m = re.search(r'\(([\d.,]+)\)\s*$', (model.nominal_current or '').strip())
    if not m:
        return False
    try:
        return float(m.group(1).replace(',', '.')) <= CT_MAX_CURRENT_A
    except ValueError:
        return False
