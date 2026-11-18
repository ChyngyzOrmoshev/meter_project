import os
import io
import gzip
import base64
import hashlib
import json
import urllib.request
import urllib.parse
import re
from dotenv import load_dotenv

class SunRiseService:
    def __init__(self):
        load_dotenv('/app/cEnergo.env')
        self.base_url = os.getenv('SUNRISE_API_URL', '').rstrip('/')
        self.user = os.getenv('SUNRISE_API_USER')
        self.password = os.getenv('SUNRISE_API_PASS')
        self.token = None
        
        if not self.base_url or not self.user or not self.password:
            raise ValueError("Критическая ошибка: Не заданы SUNRISE_API_URL, SUNRISE_API_USER или SUNRISE_API_PASS")

    def _compress(self, xml_str):
        b = xml_str.encode('utf-8')
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=out, mode="w") as f:
            f.write(b)
        return base64.b64encode(out.getvalue()).decode('utf-8')

    def _decompress(self, b64_str):
        if not b64_str: return ""
        try:
            b = base64.b64decode(b64_str)
            out = io.BytesIO(b)
            with gzip.GzipFile(fileobj=out, mode="r") as f:
                return f.read().decode('utf-8')
        except Exception:
            return ""

    def _login(self):
        md5_pass = hashlib.md5(self.password.encode('utf-8')).hexdigest()
        data = urllib.parse.urlencode({'userAccount': self.user, 'userPassword': md5_pass}).encode('utf-8')
        req = urllib.request.Request(f"{self.base_url}/api/login", data=data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            if res.get('errorCode') != 0:
                raise Exception(f"Ошибка авторизации: {res.get('errorMsg')}")
            self.token = res['data']['token']

    def _post_request(self, endpoint, xml_str, extra_data=None):
        if not self.token:
            self._login()
            
        body = {'xml': self._compress(xml_str)}
        if extra_data:
            body.update(extra_data)
            
        body_encoded = urllib.parse.urlencode(body).encode('utf-8')
        req = urllib.request.Request(f"{self.base_url}/api{endpoint}?token={self.token}", data=body_encoded, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            if res.get('errorCode') != 0:
                raise Exception(f"Ошибка API: {res.get('errorMsg')}")
            return self._decompress(res.get('data'))

    def _format_serial_for_api(self, serial):
        """Преобразует наш формат БД (0862...) в формат SunRise (63...)"""
        serial = str(serial).strip()
        # Просто отрезаем наши добавленные префиксы 0861/0862/0863
        if serial.startswith('0861') or serial.startswith('0862') or serial.startswith('0863'):
            return serial[4:]
        return serial
    
    def get_relay_status(self, serial):
        api_serial = self._format_serial_for_api(serial)
        
        xml = f"""<RequestMessage xmlns:m="http://iec.ch/TC57/2011/schema/message"><Header><Verb>create</Verb><Noun>MeterReadings</Noun><Revision>2.0</Revision><Timestamp>2026-10-29T12:00:00.0Z</Timestamp><Source>SUNHOPE</Source><MessageID>EF68F453</MessageID><CorrelationID>EF68F453</CorrelationID></Header><Payload><m:MeterReadings><m:ReadingType><m:mRID>3.15.91.26</m:mRID><m:name>Reading</m:name></m:ReadingType><m:MeterReading><m:Meter><m:mRID>{api_serial}</m:mRID></m:Meter></m:MeterReading></m:MeterReadings></Payload></RequestMessage>"""

        # Всегда запрашиваем статус в реальном времени (nativeData = 1)
        try:
            res_xml = self._post_request('/meter/relayStatus', xml, {'nativeData': '1'})
            match = re.search(r'<[a-zA-Z0-9:]*value>(.*?)</[a-zA-Z0-9:]*value>', res_xml)
            if match:
                return match.group(1).strip() == '1'
            
            # Если value нет, ищем ошибку
            error_match = re.search(r'<code>(.*?)</code>', res_xml)
            if error_match and error_match.group(1) != '0.0':
                code = error_match.group(1).strip()
                if code == '900031' or code == '900036' or code == '900038':
                    raise Exception("Счетчик офлайн (не отвечает на запрос)")
                if code == '4.3':
                    raise Exception("Счетчик не отвечает (Таймаут сервера SunRise)")
                raise Exception(f"Ошибка SunRise {code}")
                
        except Exception as e:
            raise Exception(f"Счетчик офлайн или не отвечает: {str(e)}")

        raise Exception("Не удалось получить статус реле.")

    def set_relay(self, serial, state):
        api_serial = self._format_serial_for_api(serial)

        cim_code = '3.15.91.18' if state == 'on' else '3.15.91.42'
        xml = f"""<RequestMessage xmlns:m="http://iec.ch/TC57/2011/schema/message">
<Header>
<Verb>create</Verb>
<Noun>EndDeviceControls</Noun>
<Revision>2.0</Revision>
<Timestamp>2026-10-29T12:00:00.0Z</Timestamp>
<Source>SUNHOPE</Source>
<MessageID>C4B79701</MessageID>
<CorrelationID>C4B79701</CorrelationID>
</Header>
<Payload>
<m:EndDeviceControls>
<m:EndDeviceControl>
<m:EndDevices><m:mRID>{api_serial}</m:mRID></m:EndDevices>
<m:EndDeviceControlType ref="{cim_code}" />
</m:EndDeviceControl>
</m:EndDeviceControls>
</Payload>
</RequestMessage>"""
        self._post_request('/meter/relayControl', xml)
        return True