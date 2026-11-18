import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

class CEnergoService:
    def __init__(self):
        self.soap_url = "http://192.168.144.95:9090/soap/v1"
        self.namespace = "http://energomera.ru/#/cEnergo/api/PK-Electrouchet/v1"

    def _send_request(self, xml_body, action_name):
        action_url = f"{self.namespace}/Integration/{action_name}"
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': action_url
        }
        
        req = urllib.request.Request(self.soap_url, data=xml_body.encode('utf-8'), method='POST')
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            # Увеличиваем таймаут до 40 секунд, так как sync_timeout = 30
            with urllib.request.urlopen(req, timeout=65) as response:
                res_xml = response.read().decode('utf-8')
                return self._parse_response(res_xml)
        except Exception as e:
            raise Exception(f"Ошибка сети cEnergo: {e}")

    def _parse_response(self, xml_str):
        try:
            root = ET.fromstring(xml_str)
            # Ищем теги без учета пространства имен (добавлено {*})
            confirm_msg = root.find('.//{*}confirm_msg')
            status = root.find('.//{*}status')
            
            msg = confirm_msg.text if confirm_msg is not None else "UnknownError"
            st = status.text if status is not None else None
            
            return {'msg': msg, 'status': st}
        except Exception as e:
            raise Exception(f"Не удалось распарсить ответ от cEnergo: {e}")

    def get_status(self, serial):
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="{self.namespace}">
   <soapenv:Body>
      <tem:GetMeterState>
         <tem:serial_number>{serial}</tem:serial_number>
      </tem:GetMeterState>
   </soapenv:Body>
</soapenv:Envelope>"""
        
        res = self._send_request(xml, "GetMeterState")
        
        if res['msg'] == 'Success':
            if res['status'] == '1':
                return 'on'
            elif res['status'] == '2':
                return 'off'
            else:
                return 'unknown'
        elif res['msg'] == 'UnknownMeterState':
            return 'unknown' # Сервер не знает состояние
        elif res['msg'] == 'MeterNotFound':
            raise Exception("Счетчик не найден в cEnergo")
        else:
            raise Exception(f"Ошибка cEnergo: {res['msg']}")

    def set_status(self, serial, action):
        status_code = '1' if action == 'on' else '2'
        
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="{self.namespace}">
   <soapenv:Body>
      <tem:SetMeterState>
         <tem:serial_number>{serial}</tem:serial_number>
         <tem:status>{status_code}</tem:status>
         <tem:sync>true</tem:sync>
         <tem:sync_timeout>60</tem:sync_timeout>
      </tem:SetMeterState>
   </soapenv:Body>
</soapenv:Envelope>"""
        
        res = self._send_request(xml, "SetMeterState")
        
        if res['msg'] == 'Success':
            return 'success'
        elif res['msg'] == 'TimeoutExpired':
            return 'queued' # Команда принята, но счетчик спит
        elif res['msg'] == 'MeterNotFound':
            raise Exception("Счетчик не найден в cEnergo")
        else:
            raise Exception(f"Ошибка cEnergo: {res['msg']}")