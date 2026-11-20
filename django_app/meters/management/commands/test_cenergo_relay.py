import os
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = 'Тест управления реле cEnergo (SOAP API)'

    def add_arguments(self, parser):
        parser.add_argument('--serial', type=str, required=True, help='Заводской номер')
        parser.add_argument('--action', choices=['status', 'on', 'off'], default='status')

    def handle(self, *args, **options):
        serial = options['serial']
        action = options['action']
        
        # ВАЖНО: судя по WSDL, адрес заканчивается на /soap/v1
        soap_url = os.getenv('CENERGO_SOAP_URL', 'http://192.168.144.95:9090/soap/v1')
        action_url = "http://energomera.ru/#/cEnergo/api/PK-Electrouchet/v1/Integration/"
        
        if action == 'status':
            self._get_status(soap_url, action_url, serial)
        else:
            self._set_status(soap_url, action_url, serial, action)

    def _get_status(self, url, action_url, serial):
        self.stdout.write(f"🔍 Проверяем статус реле для {serial}...")
        
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://energomera.ru/#/cEnergo/api/PK-Electrouchet/v1">
   <soapenv:Header/>
   <soapenv:Body>
      <tem:GetMeterState>
         <tem:serial_number>{serial}</tem:serial_number>
      </tem:GetMeterState>
   </soapenv:Body>
</soapenv:Envelope>"""

        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': f"{action_url}GetMeterState"
        }
        
        self._send_request(url, xml, headers, "GetMeterState")

    def _set_status(self, url, action_url, serial, action):
        # В cEnergo обычно 1 = Включить, 2 = Отключить (но иногда 0/1, будем тестировать)
        status_code = '1' if action == 'on' else '2'
        action_ru = 'ВКЛЮЧАЕМ' if action == 'on' else 'ОТКЛЮЧАЕМ'
        
        self.stdout.write(self.style.WARNING(f"⚡ {action_ru} реле для {serial} (передаем status={status_code})..."))
        
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://energomera.ru/#/cEnergo/api/PK-Electrouchet/v1">
   <soapenv:Header/>
   <soapenv:Body>
      <tem:SetMeterState>
         <tem:serial_number>{serial}</tem:serial_number>
         <tem:status>{status_code}</tem:status>
         <tem:sync>true</tem:sync>
         <tem:sync_timeout>30</tem:sync_timeout>
      </tem:SetMeterState>
   </soapenv:Body>
</soapenv:Envelope>"""

        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': f"{action_url}SetMeterState"
        }
        
        self._send_request(url, xml, headers, "SetMeterState")

    def _send_request(self, url, xml, headers, method_name):
        req = urllib.request.Request(url, data=xml.encode('utf-8'), method='POST')
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                res_xml = response.read().decode('utf-8')
                self.stdout.write(f"✅ Ответ получен (HTTP {response.status}):")
                self.stdout.write(res_xml)
                
                # Пытаемся вытащить сообщение об успехе/ошибке
                if '<confirm_msg>' in res_xml:
                    msg = res_xml.split('<confirm_msg>')[1].split('</confirm_msg>')[0]
                    self.stdout.write(self.style.SUCCESS(f"Результат: {msg}"))
                
                if method_name == "GetMeterState" and '<status>' in res_xml:
                    st = res_xml.split('<status>')[1].split('</status>')[0]
                    self.stdout.write(self.style.SUCCESS(f"Текущий статус реле: {st}"))

        except urllib.error.HTTPError as e:
            self.stderr.write(self.style.ERROR(f"❌ HTTP Ошибка {e.code}:"))
            self.stderr.write(e.read().decode('utf-8'))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ Ошибка сети: {e}"))