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
