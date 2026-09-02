from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Q
from meters.models import CompareResult, Reading
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Автоматически закрывает заявки оффлайн, если устройство снова онлайн'

    def handle(self, *args, **options):
        self.stdout.write('🔄 Начинаем проверку оффлайн-заявок...')
        
        # Берём заявки со статусом 'new' или 'in_progress' и action_type='offline'
        issues = CompareResult.objects.filter(
            Q(status='new') | Q(status='in_progress'),
            action_type='offline',
            device__isnull=False  # только те, у которых есть устройство
        ).select_related('device')

        updated_count = 0
        for issue in issues:
            device = issue.device
            # Проверяем последнее показание
            last_reading = Reading.objects.filter(device=device).order_by('-timestamp').first()
            if last_reading:
                days_since = (timezone.now() - last_reading.timestamp).days
                if days_since < 7:  # устройство онлайн (было показание за последние 7 дней)
                    # Закрываем заявку
                    issue.status = 'auto_fixed'
                    issue.fixed_at = timezone.now()
                    issue.fixed_by = None  # системное действие
                    issue.comment = 'Автоматически закрыто: устройство восстановило связь.'
                    issue.save()
                    updated_count += 1
                    self.stdout.write(f'✅ Закрыта заявка #{issue.id} для {issue.serial_1c}')
                else:
                    self.stdout.write(f'⏳ Устройство {issue.serial_1c} всё ещё оффлайн')
            else:
                self.stdout.write(f'⚠️ Нет показаний для {issue.serial_1c}')

        self.stdout.write(self.style.SUCCESS(f'✅ Обновлено заявок: {updated_count}'))