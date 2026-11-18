import os
import json
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from dotenv import load_dotenv

class Command(BaseCommand):
    help = 'Тест пакетного управления реле RiseSun (MMWS API)'

    def add_arguments(self, parser):
        parser.add_argument('--serials', type=str, required=True, help='Список номеров через запятую')
        parser.add_argument('--action', choices=['on', 'off'], required=True, help='on (включить) или off (отключить)')
        parser.add_argument('--timeout', type=int, default=30, help='Максимальное время ожидания в минутах')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        
        base_url = os.getenv('MMWS_API_URL', '').rstrip('/')
        api_key = os.getenv('MMWS_API_KEY')
        
        if not base_url or not api_key:
            self.stderr.write(self.style.ERROR("Не заданы MMWS_API_URL / API_KEY"))
            return

        serials = [s.strip() for s in options['serials'].split(',') if s.strip()]
        action = options['action']
        timeout_minutes = options['timeout']
        
        endpoint = 'disconnect-batch' if action == 'off' else 'reconnect-batch'
        reason = 'test_disconnect' if action == 'off' else 'test_reconnect'
        
        self.stdout.write(f"⚡ Отправляем пакет ({len(serials)} шт.) на {action}...")
        
        payload = {"reason": reason, "meters": serials}
        idempotency_key = str(uuid.uuid4())
        
        url = f"{base_url}/{endpoint}"
        data = json.dumps(payload).encode('utf-8')
        
        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Authorization', f'Bearer {api_key}')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Idempotency-Key', idempotency_key)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                self.stdout.write(self.style.SUCCESS("✅ Пакет принят сервером!"))
                batch_id = res.get('batch_id')
                self.stdout.write(f"Batch ID: {batch_id}")
                
                if batch_id:
                    self._check_status(base_url, api_key, batch_id, timeout_minutes)
                    
        except urllib.error.HTTPError as e:
            self.stderr.write(self.style.ERROR(f"❌ HTTP Ошибка {e.code}: {e.read().decode('utf-8')}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ Ошибка сети: {e}"))

    def _check_status(self, base_url, api_key, batch_id, timeout_minutes):
        self.stdout.write(f"\n⏳ Ждем ответа от счетчиков (макс. {timeout_minutes} мин). Опрос каждые 10 секунд...")
        url = f"{base_url}/disconnect-batch/{batch_id}"
        
        start_time = time.time()
        max_seconds = timeout_minutes * 60
        
        while True:
            if (time.time() - start_time) > max_seconds:
                self.stdout.write(self.style.ERROR(f"\n⏰ Истекло время ожидания ({timeout_minutes} мин). Счетчик так и не вышел на связь."))
                break
                
            time.sleep(10)
            
            req = urllib.request.Request(url, method='GET')
            req.add_header('Authorization', f'Bearer {api_key}')
            
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    res = json.loads(resp.read().decode('utf-8'))
                    status = res.get('status')
                    
                    # Считаем прошедшее время для красоты
                    elapsed = int(time.time() - start_time)
                    self.stdout.write(f"⏱️ Статус: {status} (прошло {elapsed} сек.)")
                    
                    if status in ['completed', 'completed_with_errors', 'failed']:
                        self.stdout.write("\n📊 Сырой ответ от сервера:")
                        self.stdout.write(json.dumps(res, indent=2, ensure_ascii=False))
                        
                        self.stdout.write("\nИтог по счетчикам:")
                        for item in res.get('items', []):
                            serial = item.get('meter_serial')
                            item_status = item.get('status')
                            if item_status == 'done':
                                self.stdout.write(self.style.SUCCESS(f"  ✅ {serial}: Успешно"))
                            else:
                                self.stderr.write(self.style.ERROR(f"  ❌ {serial}: {item_status}"))
                        break
            except Exception as e:
                self.stderr.write(f"Ошибка при опросе статуса: {e}")
                break