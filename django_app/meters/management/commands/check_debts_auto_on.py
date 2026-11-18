from django.core.management.base import BaseCommand
from django.utils import timezone
from meters.models import Subscriber, RelayTask, RelayCommandHistory

class Command(BaseCommand):
    help = 'Автоматическая постановка задач на включение при погашении долга'

    def handle(self, *args, **options):
        self.stdout.write("🔍 Проверка должников, которые оплатили долг...")
        
        # Ищем всех, у кого долг <= 0
        paid_subs = Subscriber.objects.filter(debt__lte=0)
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
                
            # 2. Проверяем историю
            last_history = RelayCommandHistory.objects.filter(device=device).order_by('-created_at').first()
            
            if not last_history:
                self.stdout.write("   [Пропуск] Для этого счетчика нет истории действий.")
                continue

            self.stdout.write(f"   [Инфо] Последняя история: {last_history.action} - {last_history.status}")
            
            # 3. Логика проверки
            if last_history.action == 'off' and last_history.status in ['success', 'pending']:
                
                # НОВОЕ: Отменяем все старые задачи на ОТКЛЮЧЕНИЕ, которые еще висят в очереди
                RelayTask.objects.filter(
                    device=device, 
                    action='off', 
                    status__in=['pending', 'processing']
                ).update(
                    status='cancelled', 
                    error_message='Отменено: абонент оплатил долг до выполнения'
                )

                RelayTask.objects.create(
                    device=device,
                    action='on',
                    scheduled_at=timezone.now(),
                    status='pending',
                    debt_snapshot=sub.debt
                )
                tasks_created += 1
                self.stdout.write(self.style.SUCCESS(f"   [УСПЕХ] Создана задача на ВКЛЮЧЕНИЕ, старые задачи на отключение отменены!"))
            else:
                self.stdout.write("   [Пропуск] По истории последнее действие не 'off' или оно завершилось ошибкой.")

        self.stdout.write(self.style.SUCCESS(f"🎉 Создано задач на включение: {tasks_created}"))