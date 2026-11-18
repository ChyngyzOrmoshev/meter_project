import os
import requests
from dotenv import load_dotenv
from meters.models import MeterModel, Subscriber
from django.db.models import Q

class OneCService:
    def __init__(self):
        load_dotenv('/app/cEnergo.env')
        self.server_url = os.getenv('ONEC_SERVER_URL', '').rstrip('/')
        
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

    def get_debtors(self, region_name, min_debt):
        """
        Получение должников. Если есть база 1С - идем в 1С, если нет - ищем в локальной БД.
        """
        db_name = self.region_db_map.get(region_name)
        
        # Если 1С нет (например, ЖПЭС), берем из нашей базы
        if not db_name or not self.server_url:
            qs = Subscriber.objects.filter(debt__gte=min_debt)
            if region_name:
                qs = qs.filter(Q(region__icontains=region_name) | Q(device__region__name__icontains=region_name))
            return list(qs.values('device__serial_number', 'account_number', 'full_name', 'debt'))

        # Запрос в 1С
        url = f"{self.server_url}/{db_name}/get_debtors"
        payload = {
            "min_debt": float(min_debt),
            "askue_types": self._get_managed_askue_types()
        }
        
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data.get('data', [])
        except Exception as e:
            raise Exception(f"Ошибка связи с 1С ({region_name}): {e}")

    def check_debts(self, region_name, meters_list):
        """
        Проверка актуального долга для конкретных счетчиков (используется в воркере)
        Возвращает словарь: { 'serial_number': debt, ... }
        """
        db_name = self.region_db_map.get(region_name)
        
        # Если 1С нет, возвращаем статус "неизвестно" (или берем из нашей БД)
        if not db_name or not self.server_url:
            result = {}
            for m in meters_list:
                sub = Subscriber.objects.filter(device__serial_number=m['serial_number']).first()
                result[m['serial_number']] = float(sub.debt) if sub else 0.0
            return result

        # Запрос в 1С
        url = f"{self.server_url}/{db_name}/check_debts"
        payload = {"meters": meters_list}
        
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json().get('data', [])
            
            # Преобразуем в удобный словарь {serial: debt}
            return {item['serial_number']: float(item['debt']) for item in data}
        except Exception as e:
            # Если 1С упала, мы выбрасываем ошибку, чтобы задача ушла в ретрай (не отключать наугад)
            raise Exception(f"Ошибка проверки долга в 1С ({region_name}): {e}")