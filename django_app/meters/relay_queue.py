from datetime import timedelta

from django.utils import timezone

from .models import RelayTask

# Если противоположная задача уже выполняется воркером, новую откладываем на этот срок
BUSY_DELAY_MIN = 2


def supersede_tasks(device, scheduled_at, reason, actions=('on', 'off')):
    """
    Подготовка очереди к новой задаче по счётчику.

    Отменяются только ожидающие (pending) задачи указанных действий.
    Задачи в processing не трогаем: воркер их уже выполняет, команда всё равно уйдёт,
    а отмена только исказила бы статус и могла поменять порядок команд (off после on).

    Возвращает (scheduled_at, busy): если по счётчику есть выполняющаяся задача,
    время новой сдвигается минимум на BUSY_DELAY_MIN минут вперёд, чтобы она шла после неё.
    """
    RelayTask.objects.filter(
        device=device, action__in=actions, status='pending'
    ).update(status='cancelled', error_message=reason)

    busy = RelayTask.objects.filter(
        device=device, action__in=actions, status='processing'
    ).exists()
    if busy:
        scheduled_at = max(scheduled_at, timezone.now() + timedelta(minutes=BUSY_DELAY_MIN))
    return scheduled_at, busy
