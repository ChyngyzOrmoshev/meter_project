from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from meters.models import Device, RelayCommandHistory, RelayTask, Subscriber
from meters.onec_service import OneCService
from meters.relay_queue import last_off_snapshot, supersede_tasks

# После упавшей задачи на включение автоматически не пробуем снова это время,
# иначе каждые 10 минут создавалась бы новая задача (история остаётся 'off')
RETRY_PAUSE_HOURS = 6

# Счётчики, отключённые давнее этого срока, автоматически не проверяются
# (иначе каждые 10 минут опрашивались бы все когда-либо отключённые)
OFF_WINDOW_DAYS = 14

# Размер пачки счётчиков в одном запросе checkdebt
CHECK_BATCH = 100


class Command(BaseCommand):
    help = ('Автоматическая постановка задач на включение при погашении долга: '
            'отключённые за последние 14 дней счётчики проверяются в 1С (checkdebt)')

    def off_candidates(self):
        """
        Счётчики, у которых последняя команда on/off — успешное (или ожидающее подтверждения)
        отключение не старше OFF_WINDOW_DAYS.
        """
        cutoff = timezone.now() - timedelta(days=OFF_WINDOW_DAYS)
        rows = RelayCommandHistory.objects.filter(
            action__in=['on', 'off'], created_at__gte=cutoff
        ).order_by('created_at').values_list('device_id', 'action', 'status')
        last = {}
        for device_id, action, status in rows:
            last[device_id] = (action, status)
        return [d for d, (a, st) in last.items() if a == 'off' and st in ('success', 'pending')]

    def skip_reason(self, device):
        if RelayTask.objects.filter(device=device, action='on', status__in=['pending', 'processing']).exists():
            return 'уже есть активная задача на включение'
        if RelayTask.objects.filter(
            device=device, action='on', status='error',
            updated_at__gte=timezone.now() - timedelta(hours=RETRY_PAUSE_HOURS)
        ).exists():
            return f'задача на включение завершилась ошибкой менее {RETRY_PAUSE_HOURS} ч назад'
        return None

    def create_on_task(self, device, debt):
        task_time, _busy = supersede_tasks(
            device, timezone.now(), 'Отменено: абонент оплатил долг до выполнения', actions=('off',)
        )
        # checkdebt лицевой счёт и ФИО не возвращает: берём из задачи на отключение, иначе из Subscriber
        account, name = last_off_snapshot(device)
        sub = getattr(device, 'subscriber', None)
        RelayTask.objects.create(
            device=device, action='on', scheduled_at=task_time, status='pending', debt_snapshot=debt,
            account_snapshot=account or (sub.account_number if sub else None),
            name_snapshot=name or (sub.full_name if sub else None),
        )

    def handle(self, *args, **options):
        ids = self.off_candidates()
        self.stdout.write(f"🔍 Отключённых за {OFF_WINDOW_DAYS} дн. счётчиков: {len(ids)}")

        devices = Device.objects.filter(id__in=ids).select_related('region', 'subscriber')
        by_region = {}
        for device in devices:
            reason = self.skip_reason(device)
            if reason:
                self.stdout.write(f"   [Пропуск] {device.serial_number}: {reason}")
                continue
            by_region.setdefault(device.region.name if device.region else '', []).append(device)

        onec = OneCService()
        tasks_created = 0

        for region_name, devs in by_region.items():
            if onec.has_1c(region_name):
                paid = self.paid_in_1c(onec, region_name, devs)
            else:
                paid = self.paid_locally(devs)
            for device, debt in paid:
                self.create_on_task(device, debt)
                tasks_created += 1
                self.stdout.write(self.style.SUCCESS(
                    f"   [УСПЕХ] {device.serial_number}: долг {debt}, создана задача на ВКЛЮЧЕНИЕ"))

        self.stdout.write(self.style.SUCCESS(f"🎉 Создано задач на включение: {tasks_created}"))

    def paid_in_1c(self, onec, region_name, devs):
        """Оплатившие по ответу 1С. Счётчик, которого нет в ответе, не включается. Ошибка 1С — район пропускается."""
        paid = []
        for i in range(0, len(devs), CHECK_BATCH):
            chunk = devs[i:i + CHECK_BATCH]
            payload = [{
                'serial_number': d.serial_number,
                'askue_type_id': d.askue_id or '',
                'askue_type_str': d.api_id or '',
            } for d in chunk]
            try:
                debts = onec.known_debts(region_name, payload)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"   [1С недоступна] {region_name or 'без района'}: {e}"))
                return paid
            for d in chunk:
                debt = debts.get(d.serial_number)
                if debt is None:
                    self.stdout.write(f"   [Пропуск] {d.serial_number}: 1С не вернула счётчик")
                elif debt <= 0:
                    paid.append((d, debt))
        return paid

    def paid_locally(self, devs):
        """Район без базы 1С: только по локальной записи Subscriber с долгом <= 0."""
        paid = []
        for d in devs:
            sub = getattr(d, 'subscriber', None)
            if sub is not None and sub.debt <= 0:
                paid.append((d, sub.debt))
        return paid
