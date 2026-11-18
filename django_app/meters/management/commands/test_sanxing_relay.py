from django.core.management.base import BaseCommand
from meters.sanxing_service import SanxingService

class Command(BaseCommand):
    help = 'Тест управления реле Sanxing_old с авто-логином'

    def add_arguments(self, parser):
        parser.add_argument('--serial', type=str, required=True, help='Заводской номер')
        parser.add_argument('--action', choices=['on', 'off'], required=True)

    def handle(self, *args, **options):
        serial = options['serial']
        action = options['action']
        
        action_ru = 'ОТКЛЮЧАЕМ' if action == 'off' else 'ВКЛЮЧАЕМ'
        self.stdout.write(self.style.WARNING(f"⚡ {action_ru} реле для {serial}..."))
        
        try:
            service = SanxingService()
            result = service.set_relay(serial, action)
            self.stdout.write(self.style.SUCCESS(f"✅ Успех! Ответ сервера: {result}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ Ошибка: {e}"))