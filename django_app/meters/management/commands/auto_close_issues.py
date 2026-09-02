from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Q
from meters.models import CompareResult, Device, Reading
from datetime import timedelta
import re

class Command(BaseCommand):
    help = 'Автоматически закрывает заявки оффлайн и not_found, если условия выполнены'

    def handle(self, *args, **options):
        self.stdout.write('🔄 Начинаем автозакрытие заявок...')
        updated_count = 0

        # 1. Оффлайн заявки
        updated_count += self._close_offline_issues()

        # 2. Not found заявки
        updated_count += self._close_not_found_issues()

        self.stdout.write(self.style.SUCCESS(f'✅ Всего обновлено заявок: {updated_count}'))

    def _close_offline_issues(self):
        updated = 0
        issues = CompareResult.objects.filter(
            Q(status='new') | Q(status='in_progress'),
            action_type='offline',  # только оффлайн
            device__isnull=False
        ).select_related('device')
        for issue in issues:
            device = issue.device
            # --- ПРОПУСКАЕМ, если устройство временно отключено ---
            if device.status == 'temporary_off':
                self.stdout.write(f'⏭️ Пропущена заявка #{issue.id} для {issue.serial_1c} (устройство временно отключено)')
                continue
            # --- Остальная логика ---
            last_reading = Reading.objects.filter(device=device).order_by('-timestamp').first()
            if last_reading:
                days_since = (timezone.now() - last_reading.timestamp).days
                if days_since < 7:
                    issue.status = 'auto_fixed'
                    issue.fixed_at = timezone.now()
                    issue.fixed_by = None
                    issue.comment = 'Автоматически закрыто: устройство восстановило связь.'
                    issue.save()
                    updated += 1
                    self.stdout.write(f'✅ Закрыта оффлайн-заявка #{issue.id} для {issue.serial_1c}')
        return updated

    def _close_not_found_issues(self):
        """Закрывает заявки not_found, если устройство появилось в базе."""
        updated = 0
        issues = CompareResult.objects.filter(
            Q(status='new') | Q(status='in_progress'),
            action_type='not_found',
            device__isnull=True
        )

        if not issues.exists():
            return 0

        # Индексы активных устройств
        devices = Device.objects.filter(status='active')
        devices_by_norm = {}
        # devices_by_suffix = {}

        def norm_serial(s):
            return re.sub(r'\D', '', str(s)).lstrip('0') or '0'

        for d in devices:
            norm = norm_serial(d.serial_number)
            devices_by_norm[norm] = d
            # for i in range(8, min(len(norm), 16)):
            #     suffix = norm[-i:]
            #     if suffix not in devices_by_suffix:
            #         devices_by_suffix[suffix] = d

        for issue in issues:
            serial_1c = issue.serial_1c
            norm = norm_serial(serial_1c)
            device = devices_by_norm.get(norm)
            # if not device and len(norm) >= 8:
            #     suffix = norm[-8:]
            #     device = devices_by_suffix.get(suffix)
            # if not device:
            #     device = devices_by_suffix.get(norm)

            if device:
                # Нашли устройство – обновляем заявку и закрываем
                issue.device = device
                issue.model_db = device.model.model_name if device.model else ''
                issue.region = device.region
                issue.status = 'auto_fixed'
                issue.fixed_at = timezone.now()
                issue.fixed_by = None
                issue.comment = 'Автоматически закрыто: устройство найдено в базе.'
                issue.save()
                updated += 1
                self.stdout.write(f'✅ Закрыта not_found-заявка #{issue.id} для {issue.serial_1c}')

        return updated