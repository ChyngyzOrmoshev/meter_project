import os
import requests
from django.core.management.base import BaseCommand
from dotenv import load_dotenv
import xml.etree.ElementTree as ET
from datetime import datetime

class Command(BaseCommand):
    help = 'Тест потокового скачивания XML из Star Power (с точными параметрами)'

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        
        username = os.getenv('STAR_USERNAME')
        password = os.getenv('STAR_PASSWORD')
        raw_url = os.getenv('STAR_URL', 'http://192.168.20.246:59101/hes/login')
        
        self.root_url = raw_url[:-6] if raw_url.endswith('/login') else raw_url.rstrip('/')
        self.stdout.write(f"🌐 Корневой URL: {self.root_url}")

        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        })

        # 1. Логин
        self.stdout.write("\n1️⃣ Авторизация...")
        session.get(f"{self.root_url}/login", timeout=10)
        resp_post = session.post(
            f"{self.root_url}/dologin", 
            data={'username': username, 'password': password}, 
            timeout=10,
            allow_redirects=True 
        )
        if 'login' in resp_post.url.lower():
            self.stderr.write(self.style.ERROR("❌ Логин не удался."))
            return
        self.stdout.write(self.style.SUCCESS("✅ Логин успешен!"))

        # 2. Скачивание
        self.stdout.write("\n2️⃣ Запрос на скачивание XML...")
        
        # Берем сегодняшнюю дату (как в вашем URL)
        target_date = datetime.now().strftime('%Y-%m-%d')
        
        export_url = f"{self.root_url}/hDatDayController/exportXmlFile2"
        
        # ТОЧНЫЕ ПАРАМЕТРЫ ИЗ ВАШЕГО БРАУЗЕРА
        params = {
            'meterNo': '',
            'deptId': '',
            'deptNo': '',
            'regNos': '11010000',  # ВАЖНО: regNos (с S)
            'start': f"{target_date} 00:00:00",
            'end': f"{target_date} 23:59:59",
            'transformerId': '',
            'gridNo': '',          # ВАЖНО: добавили
            'regionId': '',
            'regionNo': '',
            'groupId': '',
            'bLast': '1',          # ВАЖНО: bLast (с большой L)
            'custNo': '',
            'dataType': '1'
        }

        # Добавляем Referer, так как Java серверы часто его проверяют
        export_headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f"{self.root_url}/hDatDayController/rowDataList"
        }

        try:
            resp_dl = session.get(export_url, params=params, headers=export_headers, timeout=180, stream=True)
            
            self.stdout.write(f"   Статус: {resp_dl.status_code}")

            if resp_dl.status_code == 200:
                self.stdout.write("\n3️⃣ Потоковый парсинг (Total kWh / paramId=1)...")
                
                total_readings = 0
                filtered_readings = 0
                sample = []

                for event, elem in ET.iterparse(resp_dl.raw, events=('end',)):
                    if elem.tag == 'mreadings':
                        total_readings += 1
                        param_id_elem = elem.find('paramId')
                        
                        # Фильтруем только Total kWh (paramId=1)
                        if param_id_elem is not None and param_id_elem.text == '1':
                            filtered_readings += 1
                            if len(sample) < 10:
                                meter_no = elem.find('meterNo').text if elem.find('meterNo') is not None else 'N/A'
                                value = elem.find('value').text if elem.find('value') is not None else 'N/A'
                                timestamp = elem.find('timestamp').text if elem.find('timestamp') is not None else 'N/A'
                                sample.append((meter_no, value, timestamp))
                        
                        elem.clear()

                self.stdout.write(f"   Всего записей в файле: {total_readings:,}")
                self.stdout.write(self.style.SUCCESS(f"   Отфильтровано (Total kWh): {filtered_readings:,}"))
                
                self.stdout.write("\n   Примеры (Total kWh):")
                for m, v, t in sample:
                    self.stdout.write(f"     • Счетчик: {m}, Значение: {v}, Дата: {t}")

            else:
                self.stderr.write(self.style.ERROR("   ❌ Ошибка скачивания."))
                self.stdout.write(resp_dl.text[:500])

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ Ошибка сети: {e}"))