from django.core.management.base import BaseCommand
from meters.models import Device, Subscriber
from decimal import Decimal

class Command(BaseCommand):
    help = 'Эмуляция данных из 1С для тестирования'

    def handle(self, *args, **options):
        # Наши тестовые счетчики
        test_serials = [
            '11848001', 
            '012998191109459', 
            '086263509633', 
            '201903000536'
        ]

        # Тестовые данные (Лицевой счет, ФИО, Долг, Адрес, Район)
        # Сделаем разные долги, чтобы проверить фильтры
        test_data = {
            '11848001': ('10001', 'Иванов Иван', Decimal('1500.50'), 'ул. Ленина 1', 'Кок-Арт РЭС'),
            '012998191109459': ('10002', 'Петров Петр', Decimal('500.00'), 'ул. Пушкина 2', 'Сузак ЭС'),
            '086263509633': ('10003', 'Сидоров Сидор', Decimal('0.00'), 'ул. Гагарина 3', 'Сузак ЭС'),
            '201903000536': ('10004', 'Кузнецов Алексей', Decimal('3000.00'), 'ул. Мира 4', 'Ноокен РЭС'),
        }

        for serial in test_serials:
            try:
                device = Device.objects.get(serial_number=serial)
            except Device.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"❌ Счетчик {serial} не найден в реестре (Device)! Сначала добавьте его."))
                continue

            acc, name, debt, address, region = test_data[serial]

            # Создаем или обновляем абонента
            sub, created = Subscriber.objects.update_or_create(
                device=device,
                defaults={
                    'account_number': acc,
                    'full_name': name,
                    'debt': debt,
                    'address': address,
                    'region': region,
                    'subscriber_type': 'Население'
                }
            )

            if created:
                self.stdout.write(self.style.SUCCESS(f"✅ Создан абонент для {serial}: {name} (Долг: {debt})"))
            else:
                self.stdout.write(self.style.WARNING(f"🔄 Обновлен абонент для {serial} (Долг: {debt})"))

        self.stdout.write(self.style.SUCCESS("🎉 Эмуляция 1С завершена!"))