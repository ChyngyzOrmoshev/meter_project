from django.core.management.base import BaseCommand
from datetime import timedelta

from django.utils import timezone
from meters.models import Subscriber, RelayTask, RelayCommandHistory
from meters.relay_queue import supersede_tasks

# После упавшей задачи на включение автоматически не пробуем снова это время,
# иначе каждые 10 минут создавалась бы новая задача (история остаётся 'off')
RETRY_PAUSE_HOURS = 6

class Command(BaseCommand):
    help = 'Автоматическая постановка задач на включение при погашении долга'

    def handle(self, *args, **options):
        self.stdout.write("🔍 Проверка должников, которые оплатили долг...")
        
        # Ищем всех, у кого долг <= 0
        paid_subs = Subscriber.objects.filter(debt__lte=0).select_related('device')
        self.stdout.write(f"Найдено абонентов с долгом <= 0: {paid_subs.count()}")
        
        tasks_created = 0

        for sub in paid_subs:
            device = sub.device
            self.stdout.write(f"-> Проверяем {sub.full_name} (ЛС: {sub.account_number}, Счетчик: {device.serial_number}), долг: {sub.debt}")
            
            # 1. Проверяем, нет ли уже активной задачи
            existing_task = RelayTask.objects.filter(
                device=device, 
                action='on', 
                status__in=['pending', 'processing']
            ).exists()
            
            if existing_task:
                self.stdout.write("   [Пропуск] Уже есть активная задача на включение в очереди.")
                continue 
                
            # 2. Недавно упавшая задача на включение — пауза, чтобы не долбить счётчик каждые 10 минут
            failed_recently = RelayTask.objects.filter(
                device=device, action='on', status='error',
                updated_at__gte=timezone.now() - timedelta(hours=RETRY_PAUSE_HOURS)
            ).exists()
            if failed_recently:
                self.stdout.write(f"   [Пропуск] Задача на включение завершилась ошибкой менее {RETRY_PAUSE_HOURS} ч назад.")
                continue

            # 3. Проверяем историю
            last_history = RelayCommandHistory.objects.filter(device=device).order_by('-created_at').first()
            
            if not last_history:
                self.stdout.write("   [Пропуск] Для этого счетчика нет истории действий.")
                continue

            self.stdout.write(f"   [Инфо] Последняя история: {last_history.action} - {last_history.status}")
            
            # 4. Логика проверки
            if last_history.action == 'off' and last_history.status in ['success', 'pending']:
                
                # Отменяем ожидающие задачи на ОТКЛЮЧЕНИЕ; выполняющуюся не трогаем,
                # включение пойдёт после неё
                task_time, _busy = supersede_tasks(
                    device, timezone.now(), 'Отменено: абонент оплатил долг до выполнения', actions=('off',)
                )

                RelayTask.objects.create(
                    device=device,
                    action='on',
                    scheduled_at=task_time,
                    status='pending',
                    debt_snapshot=sub.debt
                )
                tasks_created += 1
                self.stdout.write(self.style.SUCCESS(f"   [УСПЕХ] Создана задача на ВКЛЮЧЕНИЕ, старые задачи на отключение отменены!"))
            else:
                self.stdout.write("   [Пропуск] По истории последнее действие не 'off' или оно завершилось ошибкой.")

        self.stdout.write(self.style.SUCCESS(f"🎉 Создано задач на включение: {tasks_created}"))