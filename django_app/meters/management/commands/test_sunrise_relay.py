import os
import io
import gzip
import base64
import hashlib
import json
import urllib.request
import urllib.parse
from django.core.management.base import BaseCommand
from dotenv import load_dotenv

def compress_xml(xml_str):
    """Сжимает XML в GZIP и кодирует в Base64"""
    b = xml_str.encode('utf-8')
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="w") as f:
        f.write(b)
    return base64.b64encode(out.getvalue()).decode('utf-8')

def decompress_xml(b64_str):
    """Раскодирует Base64 и распаковывает GZIP"""
    if not b64_str:
        return ""
    try:
        b = base64.b64decode(b64_str)
        out = io.BytesIO(b)
        with gzip.GzipFile(fileobj=out, mode="r") as f:
            return f.read().decode('utf-8')
    except Exception as e:
        return f"ERROR_DECOMPRESS: {e}"

class Command(BaseCommand):
    help = 'Тест управления реле SunRise'

    def add_arguments(self, parser):
        parser.add_argument('--serial', type=str, required=True, help='Заводской номер счетчика')
        parser.add_argument('--action', choices=['status', 'on', 'off'], default='status', help='Действие: status (проверить), on (включить), off (выключить)')

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        
        base_url = os.getenv('SUNRISE_API_URL', '').rstrip('/')
        user = os.getenv('SUNRISE_API_USER')
        password = os.getenv('SUNRISE_API_PASS')
        serial = options['serial']
        action = options['action']

        if not base_url or not user or not password:
            self.stderr.write(self.style.ERROR("Не заданы SUNRISE_API_URL / USER / PASS в cEnergo.env"))
            return

        # 1. Авторизация (MD5 + POST Form Data)
        self.stdout.write("🔐 Авторизация в SUNRISE MDC...")
        md5_pass = hashlib.md5(password.encode('utf-8')).hexdigest()
        login_data = urllib.parse.urlencode({
            'userAccount': user,
            'userPassword': md5_pass
        }).encode('utf-8')

        req = urllib.request.Request(f"{base_url}/api/login", data=login_data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                login_res = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Ошибка сети при логине: {e}"))
            return

        if login_res.get('errorCode') != 0:
            self.stderr.write(self.style.ERROR(f"Ошибка логина: {login_res.get('errorMsg')}"))
            return
            
        token = login_res['data']['token']
        self.stdout.write(self.style.SUCCESS(f"✅ Токен получен: {token[:8]}..."))

        # 2. Выполнение действия
        if action == 'status':
            self._get_status(base_url, token, serial)
        else:
            self._control_relay(base_url, token, serial, action)

    def _get_status(self, base_url, token, serial):
        self.stdout.write(f"🔍 Проверяем статус реле для {serial}...")
        
        xml = f"""<RequestMessage xmlns:m="http://iec.ch/TC57/2011/schema/message">
<Header>
<Verb>create</Verb>
<Noun>MeterReadings</Noun>
<Revision>2.0</Revision>
<Timestamp>2026-10-29T12:00:00.0Z</Timestamp>
<Source>SUNHOPE</Source>
<MessageID>EF68F453-A562-414D-8AA8-7BB90C20E725</MessageID>
<CorrelationID>EF68F453-A562-414D-8AA8-7BB90C20E725</CorrelationID>
</Header>
<Payload>
<m:MeterReadings>
<m:ReadingType><m:mRID>3.15.91.26</m:mRID><m:name>Reading</m:name></m:ReadingType>
<m:MeterReading><m:Meter><m:mRID>{serial}</m:mRID></m:Meter></m:MeterReading>
</m:MeterReadings>
</Payload>
</RequestMessage>"""

        body = urllib.parse.urlencode({
            'nativeData': '1',
            'xml': compress_xml(xml)
        }).encode('utf-8')

        req = urllib.request.Request(f"{base_url}/api/meter/relayStatus?token={token}", data=body, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                
            if res.get('errorCode') != 0:
                self.stderr.write(self.style.ERROR(f"Ошибка API: {res.get('errorMsg')}"))
                return
                
            xml_res = decompress_xml(res.get('data'))
            if 'ERROR_DECOMPRESS' in xml_res:
                self.stderr.write(self.style.ERROR(xml_res))
                return
                
            self.stdout.write("📄 Ответ сервера (XML):")
            self.stdout.write(xml_res)
            
            # Простой парсинг значения (ищем тег value после ReadingType)
            import re
            match = re.search(r'<m:value>(.*?)</m:value>', xml_res)
            if match:
                val = match.group(1)
                status_text = "ВКЛЮЧЕНО (1)" if val == '1' else "ОТКЛЮЧЕНО (0)"
                self.stdout.write(self.style.SUCCESS(f"🔌 Статус реле: {status_text}"))
                
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Ошибка сети: {e}"))

    def _control_relay(self, base_url, token, serial, action):
        # 3.15.91.18 — Connect, 3.15.91.42 — Disconnect
        cim_code = '3.15.91.18' if action == 'on' else '3.15.91.42'
        action_ru = 'ВКЛЮЧАЕМ' if action == 'on' else 'ОТКЛЮЧАЕМ'
        
        self.stdout.write(self.style.WARNING(f"⚡ {action_ru} реле для {serial}..."))
        
        xml = f"""<RequestMessage xmlns:m="http://iec.ch/TC57/2011/schema/message">
<Header>
<Verb>create</Verb>
<Noun>EndDeviceControls</Noun>
<Revision>2.0</Revision>
<Timestamp>2026-10-29T12:00:00.0Z</Timestamp>
<Source>SUNHOPE</Source>
<MessageID>C4B79701-6C98-417A-93F0-D2C672642942</MessageID>
<CorrelationID>C4B79701-6C98-417A-93F0-D2C672642942</CorrelationID>
</Header>
<Payload>
<m:EndDeviceControls>
<m:EndDeviceControl>
<m:EndDevices><m:mRID>{serial}</m:mRID></m:EndDevices>
<m:EndDeviceControlType ref="{cim_code}" />
</m:EndDeviceControl>
</m:EndDeviceControls>
</Payload>
</RequestMessage>"""

        body = urllib.parse.urlencode({
            'xml': compress_xml(xml)
        }).encode('utf-8')

        req = urllib.request.Request(f"{base_url}/api/meter/relayControl?token={token}", data=body, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                
            if res.get('errorCode') != 0:
                self.stderr.write(self.style.ERROR(f"Ошибка API: {res.get('errorMsg')}"))
                return
                
            xml_res = decompress_xml(res.get('data'))
            self.stdout.write(self.style.SUCCESS("✅ Команда успешно отправлена!"))
            self.stdout.write("📄 Ответ сервера (XML):")
            self.stdout.write(xml_res)
                
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Ошибка сети: {e}"))