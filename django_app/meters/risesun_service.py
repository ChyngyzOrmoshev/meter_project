import os
import json
import time
import uuid
import urllib.request
import urllib.error
from dotenv import load_dotenv

# Регламент MMWS (APIDOC): статусы пакета и счётчика, коды ошибок бизнес-уровня
BATCH_TERMINAL = ('completed', 'completed_with_errors', 'failed')

ERROR_TEXTS = {
    'METER_UNREACHABLE': 'Счётчик недоступен на момент выполнения операции',
    'METER_AUTH_FAILED': 'Ошибка аутентификации на уровне протокола счётчика',
    'METER_NOT_FOUND': 'Счётчик отсутствует в справочнике MMWS',
    'DISCONNECT_NOT_SUPPORTED': 'Модель счётчика не поддерживает удалённое отключение',
    'BATCH_TOO_LARGE': 'Размер пакета превышает лимит',
    'DUPLICATE_IDEMPOTENCY_KEY': 'Повторное использование Idempotency-Key с иным содержимым',
}


class RiseSunService:
    MAX_ATTEMPTS = 3  # сетевые повторы одной отправки (регламент п. 7 допускает до 5)

    def __init__(self):
        load_dotenv('/app/cEnergo.env')
        self.base_url = os.getenv('MMWS_API_URL', '').rstrip('/')
        self.api_key = os.getenv('MMWS_API_KEY')

    @staticmethod
    def _api_error(body):
        """Разбор тела ошибки MMWS: {"error": {"code", "message", "request_id"}}"""
        try:
            err = json.loads(body).get('error') or {}
            code = err.get('code')
            if code:
                text = ERROR_TEXTS.get(code)
                head = f"{text} ({code})" if text else code
                tail = f": {err['message']}" if err.get('message') else ''
                rid = f" [request_id {err['request_id']}]" if err.get('request_id') else ''
                return f"{head}{tail}{rid}"
        except Exception:
            pass
        return (body or '')[:200]

    def set_relay(self, serial, action):
        """
        Отправляет пакетную команду в RiseSun (не ждёт выполнения) и возвращает batch_id.
        Один Idempotency-Key на все сетевые повторы этой отправки (регламент п. 2.3, 7):
        если первый запрос дошёл, а ответ потерялся, дубль пакета не создаётся.
        Повторы — на 429/5xx и сетевые сбои с нарастающей задержкой.
        """
        if not self.base_url or not self.api_key:
            raise Exception("Не заданы MMWS_API_URL или API_KEY")

        endpoint = 'disconnect-batch' if action == 'off' else 'reconnect-batch'
        reason = 'manual_disconnect' if action == 'off' else 'manual_reconnect'

        url = f"{self.base_url}/{endpoint}"
        data = json.dumps({"reason": reason, "meters": [serial]}).encode('utf-8')
        idempotency_key = str(uuid.uuid4())

        last_error = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Authorization', f'Bearer {self.api_key}')
            req.add_header('Content-Type', 'application/json')
            req.add_header('Idempotency-Key', idempotency_key)

            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    res = json.loads(resp.read().decode('utf-8'))
                    batch_id = res.get('batch_id')
                    if not batch_id:
                        raise Exception("RiseSun не вернул batch_id")
                    return batch_id
            except urllib.error.HTTPError as e:
                body = e.read().decode('utf-8', errors='replace')
                if e.code != 429 and e.code < 500:
                    # 400/401/403 и т.п. — повторять бессмысленно
                    raise Exception(f"Ошибка RiseSun API {e.code}: {self._api_error(body)}")
                last_error = f"Ошибка RiseSun API {e.code}: {self._api_error(body)}"
                try:
                    delay = int(e.headers.get('Retry-After')) if e.code == 429 else 2 ** attempt
                except (TypeError, ValueError):
                    delay = 2 ** attempt
            except urllib.error.URLError as e:
                last_error = f"Ошибка сети RiseSun: {e}"
                delay = 2 * attempt
            except OSError as e:  # таймаут сокета и т.п.
                last_error = f"Ошибка сети RiseSun: {e}"
                delay = 2 * attempt

            if attempt < self.MAX_ATTEMPTS:
                time.sleep(min(delay, 30))

        raise Exception(last_error)

    def get_batch_status(self, batch_id):
        """
        Статус пакета. Метод единый для отключения и подключения
        (GET /disconnect-batch/{batch_id}, регламент п. 4.5).
        """
        url = f"{self.base_url}/disconnect-batch/{batch_id}"
        req = urllib.request.Request(url, method='GET')
        req.add_header('Authorization', f'Bearer {self.api_key}')

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            if e.code == 404:
                raise Exception(f"Пакет {batch_id} не найден в RiseSun")
            raise Exception(f"Ошибка получения статуса {e.code}: {self._api_error(body)}")
        except Exception as e:
            raise Exception(f"Ошибка получения статуса: {e}")

    def get_item_result(self, batch_id, serial):
        """
        Итог команды по конкретному счётчику пакета:
        {'state': 'done'} | {'state': 'failed', 'error': текст} | {'state': 'pending'}
        Регламент п. 7: пакет с частичным отказом — штатная ситуация, смотрим items.
        """
        data = self.get_batch_status(batch_id)
        batch_status = data.get('status')

        item = next(
            (i for i in (data.get('items') or []) if str(i.get('meter_serial')) == str(serial)),
            None
        )
        if item:
            status = item.get('status')
            if status == 'done':
                return {'state': 'done'}
            if status == 'failed':
                code = item.get('error_code')
                text = ERROR_TEXTS.get(code)
                return {'state': 'failed', 'error': f"{text} ({code})" if text else (code or 'ошибка выполнения')}

        if batch_status in BATCH_TERMINAL:
            return {'state': 'failed', 'error': f"пакет завершён ({batch_status}) без результата по счётчику"}
        return {'state': 'pending'}
