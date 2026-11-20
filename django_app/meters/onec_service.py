import os
import base64
import requests
from dotenv import load_dotenv
from meters.models import MeterModel, Subscriber
from django.db.models import Q

class OneCService:
    def __init__(self):
        load_dotenv('/app/cEnergo.env')
        self.server_url = os.getenv('ONEC_SERVER_URL', '').rstrip('/')
        # HTTP-сервис 1С: {сервер}/{база}/hs/askue/{readdebt|checkdebt}, авторизация Basic
        self.auth = (os.getenv('ONEC_USER', ''), os.getenv('ONEC_PASSWORD', ''))
        # Пробная база для проверки обмена: если задана, ею заменяются базы всех районов,
        # у которых в 1С есть база (районы без базы, ЖПЭС, остаются на локальных данных)
        self.db_override = os.getenv('ONEC_DB_OVERRIDE', '').strip()
        
        # Маппинг районов на базы 1С
        self.region_db_map = {
            '02 Кок-Арт': os.getenv('ONEC_DB_KOK_ART'),
            '03 Сузак': os.getenv('ONEC_DB_SUZAK'),
            '04 Базар-Коргон': os.getenv('ONEC_DB_BAZAR_KORGON'),
            '05 Ноокен': os.getenv('ONEC_DB_NOOKEN'),
            '06 Майлуу-Суу': os.getenv('ONEC_DB_MAILUU_SUU'),
            '07 Таш-Комур': os.getenv('ONEC_DB_TASH_KOMUR'),
            '08 Аксы': os.getenv('ONEC_DB_AKSY'),
            '09 Ала-Бука': os.getenv('ONEC_DB_ALA_BUKA'),
            '10 Чаткал': os.getenv('ONEC_DB_CHATKAL'),
            '11 Кара-Куль': os.getenv('ONEC_DB_KARA_KUL'),
            '12 Токтогул': os.getenv('ONEC_DB_TOKTOGUL'),
            '01 Манас': os.getenv('ONEC_DB_MANAS'),
            # ЖПЭС отсутствует, будет брать из локальной БД
        }

    def _get_managed_askue_types(self):
        """Возвращает доступные нам типы АСКУЭ (22, 23, 24, 25, 26)"""
        models = MeterModel.objects.filter(device_type_id__in=['22', '23', '24', '25', '26']).distinct()
        return list(models.values_list('device_type_id', flat=True).distinct())

    def _db_for(self, region_name):
        """База 1С района или None, если для района (или всей интеграции) 1С не используется."""
        db_name = self.region_db_map.get(region_name)
        if not db_name or not self.server_url:
            return None
        return self.db_override or db_name

    def _call(self, db_name, region_name, method, payload):
        """POST в HTTP-сервис 1С. Возвращает data ([] при статусе Not), иначе исключение."""
        if not all(self.auth):
            raise Exception("Не заданы ONEC_USER / ONEC_PASSWORD в cEnergo.env")

        url = f"{self.server_url}/{db_name}/hs/askue/{method}"
        try:
            # Basic вручную в UTF-8: requests кодирует auth в latin-1, а логин 1С может быть кириллицей
            token = base64.b64encode(f"{self.auth[0]}:{self.auth[1]}".encode('utf-8')).decode('ascii')
            response = requests.post(
                url, json=payload, headers={'Authorization': f'Basic {token}'}, timeout=30
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            raise Exception(f"Ошибка связи с 1С ({region_name}): {e}")

        status = data.get('status')
        if status == 'ok':
            return data.get('data') or []
        if status == 'Not':
            # Штатный ответ: ничего не найдено (для readdebt - должников выше порога нет)
            return []
        raise Exception(f"1С ({region_name}) вернула статус {status!r}: {data.get('Message', '')}")

    def _read_debt(self, db_name, region_name, min_debt):
        """Метод 1С readdebt: все абоненты с долгом не меньше min_debt по нашим типам АСКУЭ."""
        payload = {
            "min_debt": float(min_debt),
            "askue_types": [str(t) for t in self._get_managed_askue_types()],
        }
        return self._call(db_name, region_name, 'readdebt', payload)

    def get_debtors(self, region_name, min_debt):
        """
        Получение должников. Если есть база 1С - идем в 1С, если нет - ищем в локальной БД.
        """
        db_name = self._db_for(region_name)

        # Если 1С нет (например, ЖПЭС), берем из нашей базы
        if not db_name:
            qs = Subscriber.objects.filter(debt__gte=min_debt)
            if region_name:
                qs = qs.filter(Q(region__icontains=region_name) | Q(device__region__name__icontains=region_name))
            return list(qs.values('device__serial_number', 'account_number', 'full_name', 'debt'))

        return self._read_debt(db_name, region_name, min_debt)

    def check_debts(self, region_name, meters_list):
        """
        Проверка актуального долга для конкретных счетчиков (используется в воркере).
        meters_list: [{'serial_number', 'askue_type_id', 'askue_type_str'}, ...]
        Возвращает словарь: { 'serial_number': debt, ... }

        Метод 1С checkdebt. Счётчика нет в ответе - долг 0 (отключение отменяется:
        «не знаем» безопаснее, чем отключить). Если 1С недоступна - исключение,
        отключать наугад нельзя.
        """
        db_name = self._db_for(region_name)

        # Если 1С нет, берем из нашей БД
        if not db_name:
            result = {}
            for m in meters_list:
                sub = Subscriber.objects.filter(device__serial_number=m['serial_number']).first()
                result[m['serial_number']] = float(sub.debt) if sub else 0.0
            return result

        payload = {"meters": [{
            "serial_number": str(m['serial_number']),
            "askue_type_id": str(m.get('askue_type_id') or ''),
            "askue_type_str": str(m.get('askue_type_str') or ''),
        } for m in meters_list]}

        try:
            rows = self._call(db_name, region_name, 'checkdebt', payload)
        except Exception as e:
            raise Exception(f"Ошибка проверки долга в 1С ({region_name}): {e}")

        def norm(serial):
            return str(serial).strip().lstrip('0')

        debts = {norm(r.get('serial_number')): float(r.get('debt') or 0) for r in rows}
        return {m['serial_number']: debts.get(norm(m['serial_number']), 0.0) for m in meters_list}
