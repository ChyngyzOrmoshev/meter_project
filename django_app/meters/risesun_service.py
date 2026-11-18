import os
import json
import uuid
import urllib.request
import urllib.error
from dotenv import load_dotenv

class RiseSunService:
    def __init__(self):
        load_dotenv('/app/cEnergo.env')
        self.base_url = os.getenv('MMWS_API_URL', '').rstrip('/')
        self.api_key = os.getenv('MMWS_API_KEY')

    def set_relay(self, serial, action):
        """Отправляет пакетную команду в RiseSun (не ждет выполнения)"""
        if not self.base_url or not self.api_key:
            raise Exception("Не заданы MMWS_API_URL или API_KEY")

        endpoint = 'disconnect-batch' if action == 'off' else 'reconnect-batch'
        reason = 'manual_disconnect' if action == 'off' else 'manual_reconnect'
        
        payload = {"reason": reason, "meters": [serial]}
        idempotency_key = str(uuid.uuid4())
        
        url = f"{self.base_url}/{endpoint}"
        data = json.dumps(payload).encode('utf-8')
        
        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Authorization', f'Bearer {self.api_key}')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Idempotency-Key', idempotency_key)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                # Возвращаем batch_id, чтобы можно было показать его пользователю
                return res.get('batch_id')
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            raise Exception(f"Ошибка RiseSun API {e.code}: {error_body}")
        except Exception as e:
            raise Exception(f"Ошибка сети RiseSun: {e}")

    def get_batch_status(self, batch_id):
        """Возвращает статус пакета по его ID"""
        url = f"{self.base_url}/disconnect-batch/{batch_id}"
        req = urllib.request.Request(url, method='GET')
        req.add_header('Authorization', f'Bearer {self.api_key}')
        
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            raise Exception(f"Ошибка получения статуса: {e}")