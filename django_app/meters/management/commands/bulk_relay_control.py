import os
import json
import time
import re
from django.core.management.base import BaseCommand
from meters.models import Device
from meters.sunrise_service import SunRiseService

EXPORT_DIR = '/app/exports'

class Command(BaseCommand):
    help = 'Массовое управление реле SunRise'

    def add_arguments(self, parser):
        parser.add_argument('--file', type=str, required=True, help='Путь к файлу со списком номеров')
        parser.add_argument('--action', choices=['on', 'off'], required=True)

    def handle(self, *args, **options):
        file_path = options['file']
        action = options['action']
        result_path = os.path.join(EXPORT_DIR, 'bulk_relay_result.json')
        
        # Читаем серийники
        with open(file_path, 'r', encoding='utf-8') as f:
            serials = [line.strip() for line in f if line.strip()]

        total = len(serials)
        results = {
            'status': 'running',
            'total': total,
            'processed': 0,
            'success': 0,
            'errors': []
        }
        
        def save_progress():
            with open(result_path, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False)

        save_progress()

        try:
            service = SunRiseService()
            # Авторизуемся один раз для всех
            service._login()
        except Exception as e:
            results['status'] = 'error'
            results['errors'].append(f"Ошибка авторизации: {e}")
            save_progress()
            return

        for idx, serial in enumerate(serials):
            try:
                # Ищем в базе (умный поиск)
                device = Device.objects.filter(serial_number__endswith=serial).first()
                if not device:
                    results['errors'].append(f"{serial}: Не найден в базе")
                    continue

                # Отправляем команду
                service.set_relay(device.serial_number, action)
                results['success'] += 1
                
                # Небольшая пауза, чтобы не забанить API
                time.sleep(0.5)
                
            except Exception as e:
                results['errors'].append(f"{serial}: {str(e)}")
            
            results['processed'] = idx + 1
            if (idx + 1) % 5 == 0 or (idx + 1) == total:
                save_progress()

        results['status'] = 'completed'
        save_progress()