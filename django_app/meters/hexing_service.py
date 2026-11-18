import os
import gzip
import json
import time
import uuid
import base64
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
import re
import io
import mysql.connector
from datetime import datetime, timezone
from dotenv import load_dotenv


# CIM mRID (IEC 61968)
MRID_RELAY_ON    = '3.15.91.18'
MRID_RELAY_OFF   = '3.15.91.42'


class HexingService:
    def __init__(self):
        load_dotenv('/app/cEnergo.env')
        # API REST
        self.base_url = os.getenv('HEXING_HES_URL', 'http://192.168.144.64:8091/HES').rstrip('/')
        self.username = os.getenv('HEXING_HES_USERNAME')
        self.password = os.getenv('HEXING_HES_PASSWORD')
        self.timeout  = int(os.getenv('HEXING_HES_TIMEOUT', 30))
        
        if not self.username or not self.password:
            raise ValueError("Критическая ошибка: Не заданы HEXING_HES_USERNAME или HEXING_HES_PASSWORD в cEnergo.env")
            
        self._session_id = None
        
        # База данных Hexing
        self.db_host = os.getenv("HEXING_KUK_MYSQL_HOST")
        self.db_port = int(os.getenv("HEXING_KUK_MYSQL_PORT", 3306))
        self.db_user = os.getenv("HEXING_KUK_MYSQL_USER")
        self.db_pass = os.getenv("HEXING_KUK_MYSQL_PASSWORD")
        self.db_name = os.getenv("HEXING_KUK_MYSQL_DB")

    # ================== ЧТЕНИЕ СТАТУСА ИЗ БД ==================
    def get_relay_status_from_db(self, serial):
        try:
            conn = mysql.connector.connect(
                host=self.db_host, port=self.db_port,
                user=self.db_user, password=self.db_pass,
                database=self.db_name, connect_timeout=5
            )
            cursor = conn.cursor(dictionary=True)
            
            # Ищем и в METER_NO, и в ASSETNO (на всякий случай)
            cursor.execute("SELECT METER_ID FROM a_equip_meter WHERE METER_NO = %s OR ASSETNO = %s", (str(serial), str(serial)))
            row = cursor.fetchone()
            if not row:
                cursor.close()
                conn.close()
                return 'unknown'
            
            meter_id = row['METER_ID']
            
            # Ищем последний статус
            cursor.execute("""
                SELECT RELAY_STATUS 
                FROM hes_log_relay_status_detail 
                WHERE METER_ID = %s 
                ORDER BY TV DESC 
                LIMIT 1
            """, (meter_id,))
            
            status_row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if status_row:
                # 0 = Включено, 1 = Отключено
                return 'on' if str(status_row['RELAY_STATUS']) == '0' else 'off'
            
            return 'unknown'
            
        except Exception as e:
            print(f"Ошибка чтения из БД Hexing: {e}")
            return 'unknown'

    # ================== УПРАВЛЕНИЕ РЕЛЕ (REST API) ==================
    def _login(self):
        md5_pass = hashlib.md5(self.password.encode('utf-8')).hexdigest()
        url = f'{self.base_url}/login?username={urllib.parse.quote(self.username)}&password={md5_pass}'
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        sid = data.get('sessionID')
        if not sid:
            raise RuntimeError(f'Hexing login failed: {data}')
        self._session_id = sid
        return sid

    def _compress_xml(self, xml_str):
        xml_clean = re.sub(r'>\s+<', '><', xml_str.strip())
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode='w', mtime=0) as f:
            f.write(xml_clean.encode('utf-8'))
        return base64.b64encode(buf.getvalue()).decode('ascii')

    def _decompress_xml(self, b64_str):
        if not b64_str:
            return ''
        try:
            raw = base64.b64decode(b64_str)
            with gzip.GzipFile(fileobj=io.BytesIO(raw), mode='r') as f:
                return f.read().decode('utf-8', errors='replace')
        except Exception:
            return b64_str

    def _request(self, endpoint, xml_b64, extra_params=None):
        if not self._session_id:
            self._login()
        url = f'{self.base_url}/{endpoint}?token={urllib.parse.quote(self._session_id)}&xml={urllib.parse.quote(xml_b64)}'
        if extra_params:
            url += '&' + urllib.parse.urlencode(extra_params)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'Hexing HTTP {e.code}: {body[:200]}')

    def _parse_result(self, response):
        err_code = str(response.get('errorCode'))
        err_msg  = response.get('errorMsg') or ''
        if err_code == '2001':
            return {'status': 'pending', 'error_code': err_code, 'message': 'ACK: команда принята'}
        if err_code == '2003':
            return {'status': 'error', 'error_code': err_code, 'message': 'Таймаут: счётчик не ответил'}
        if err_code != '0':
            return {'status': 'error', 'error_code': err_code, 'message': err_msg or f'Ошибка HES {err_code}'}

        xml_raw = response.get('xml')
        if xml_raw:
            try:
                xml_dec = self._decompress_xml(xml_raw)
                root = ET.fromstring(xml_dec)
                reply_result = root.find('.//{*}Result')
                if reply_result is not None and reply_result.text == 'FAILED':
                    err_c = root.find('.//{*}Error/{*}code') or root.find('.//{*}ErrorType/{*}code')
                    err_d = root.find('.//{*}Error/{*}details') or root.find('.//{*}ErrorType/{*}details')
                    return {'status': 'error', 'error_code': 'HES_FAILED',
                            'message': f"{err_c.text if err_c is not None else ''} - {err_d.text if err_d is not None else ''}"}
            except Exception as e:
                return {'status': 'success', 'error_code': '0', 'message': f'Успех, но XML не распарсен: {e}'}
        return {'status': 'success', 'error_code': '0', 'message': 'OK'}

    def set_relay(self, serial, action):
        if action not in ('on', 'off'):
            raise ValueError(f'action must be on/off, got {action}')

        correlation_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.0Z')
        command_ref = MRID_RELAY_ON if action == 'on' else MRID_RELAY_OFF

        xml_str = f"""<?xml version="1.0" encoding="UTF-8"?>
<RequestMessage xmlns:m="http://iec.ch/TC57/2011/schema/message">
    <Header>
        <Verb>create</Verb>
        <Noun>EndDeviceControls</Noun>
        <Revision>2.0</Revision>
        <Timestamp>{timestamp}</Timestamp>
        <Source>FDM</Source>
        <AckRequired>false</AckRequired>
        <AsyncReplyFlag>true</AsyncReplyFlag>
        <MessageID>{correlation_id}</MessageID>
        <CorrelationID>{correlation_id}</CorrelationID>
    </Header>
    <Payload>
        <m:EndDeviceControls>
            <m:EndDeviceControl>
                <m:EndDevices><m:mRID>{serial}</m:mRID></m:EndDevices>
                <m:EndDeviceAction><m:command></m:command></m:EndDeviceAction>
                <m:EndDeviceControlType ref="{command_ref}" />
            </m:EndDeviceControl>
        </m:EndDeviceControls>
    </Payload>
</RequestMessage>"""

        xml_b64 = self._compress_xml(xml_str)
        response = self._request('switch', xml_b64)
        parsed = self._parse_result(response)
        parsed['correlation_id'] = correlation_id
        parsed['xml_b64'] = xml_b64
        return parsed

    def check_pending(self, correlation_id, xml_b64, action):
        response = self._request('switch', xml_b64)
        parsed = self._parse_result(response)
        parsed['correlation_id'] = correlation_id
        parsed['xml_b64'] = xml_b64
        return parsed