import os
import hashlib
import requests
import base64
import zlib
from dotenv import load_dotenv

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(CURRENT_DIR, "cEnergo.env"))

API_BASE_URL = os.getenv("SUNRISE_API_URL")
USER = os.getenv("SUNRISE_USER")
PASSWORD = os.getenv("SUNRISE_PASSWORD")


def get_token():
    md5_password = hashlib.md5(PASSWORD.encode("utf-8")).hexdigest()
    login_url = f"{API_BASE_URL}/api/login"
    params = {"userAccount": USER, "userPassword": md5_password}
    try:
        r = requests.post(login_url, params=params, timeout=5)
        res = r.json()
        if res.get("errorCode") == 0:
            return res["data"]["token"]
    except Exception:
        return None
    return None


def run_url_diagnostic_tests():
    token = get_token()
    if not token:
        print("❌ Не удалось получить Token авторизации!")
        return

    billing_url = f"{API_BASE_URL}/api/meter/billing"
    test_meters = ["084600000025", "8002201668"]

    # Каноническая XML структура по спецификации
    xml_parts = []
    xml_parts.append('<RequestMessage xmlns:m="http://iec.ch">')
    xml_parts.append(
        "<Header><Verb>create</Verb><Noun>MeterReadSchedule</Noun><Revision>2.0</Revision>"
    )
    xml_parts.append(
        "<Timestamp>2025-02-17T00:00:00.0Z</Timestamp><Source>SUNHOPE</Source><AckRequired>false</AckRequired>"
    )
    xml_parts.append(
        "<AsyncReplyFlag>false</AsyncReplyFlag><MessageID>F48A47C8-BB14-4823-8789-E7E4E242FE11</MessageID>"
    )
    xml_parts.append(
        "<CorrelationID>F48A47C8-BB14-4823-8789-E7E4E242FE11</CorrelationID></Header>"
    )
    xml_parts.append(
        "<Payload><m:MeterReadSchedule><m:ReadingType><m:name>DailyBilling</m:name></m:ReadingType>"
    )
    xml_parts.append(
        "<m:TimeSchedule><m:scheduleInterval><m:end>2025-02-17T00:00:00.0Z</m:end>"
    )
    xml_parts.append(
        "<m:start>2025-02-15T00:00:00.0Z</m:start></m:scheduleInterval></m:TimeSchedule>"
    )
    for meter_sn in test_meters:
        xml_parts.append(f"<m:Meter><m:mRID>{meter_sn}</m:mRID></m:Meter>")
    xml_parts.append("</m:MeterReadSchedule></Payload></RequestMessage>")
    flat_xml_request = "".join(xml_parts)

    # Тест 1: Сжатие ZLIB + Base64 внутри URL params
    print("\n🔄 Тест 1: Все параметры в URL (Сжатие ZLIB Deflate + Base64)...")
    zlib_bytes = zlib.compress(flat_xml_request.encode("utf-8"))
    xml_v1 = base64.b64encode(zlib_bytes).decode("utf-8")

    params_v1 = {
        "token": token,
        "bLast": "1",
        "tariff": "0",
        "item": "1",
        "nativeData": "1",
        "xml": xml_v1,  # Передаем упакованную строку в URL
    }
    try:
        # Отправляем БЕЗ data=, строго через params=
        r = requests.post(billing_url, params=params_v1, timeout=10)
        print(f"   Ответ сервера: {r.text}")
    except Exception as e:
        print(f"   Сбой: {e}")

    # Тест 2: Чистый Base64 (без сжатия) внутри URL params
    print("\n🔄 Тест 2: Все параметры в URL (Чистый Base64 без сжатия)...")
    xml_v2 = base64.b64encode(flat_xml_request.encode("utf-8")).decode("utf-8")

    params_v2 = {
        "token": token,
        "bLast": "1",
        "tariff": "0",
        "item": "1",
        "nativeData": "1",
        "xml": xml_v2,
    }
    try:
        r = requests.post(billing_url, params=params_v2, timeout=10)
        print(f"   Ответ сервера: {r.text}")
    except Exception as e:
        print(f"   Сбой: {e}")


if __name__ == "__main__":
    run_url_diagnostic_tests()
