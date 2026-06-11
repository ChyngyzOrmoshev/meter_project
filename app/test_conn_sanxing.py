import os
import pyodbc
from pymongo import MongoClient
from dotenv import load_dotenv

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(CURRENT_DIR, "cEnergo.env")
load_dotenv(dotenv_path=ENV_PATH)

try:
    mongo_client = MongoClient("mongodb://localhost:27017/")
    mongo_db = mongo_client["power_monitoring"]
    devices_cursor = mongo_db.devices.find(
        {}, {"_id": 0, "serial_number": 1, "nominal_current": 1}
    )
    devices_map = {
        str(d["serial_number"]).strip(): str(d.get("nominal_current", "")).strip()
        for d in devices_cursor
    }
except Exception:
    devices_map = {}

if not devices_map:
    devices_map = {"35004441": "5(10)", "11849223": "5(100)"}

conn_str = f"DRIVER={{SQL Server}};SERVER={os.getenv('SANXING_SERVER')};DATABASE={os.getenv('SANXING_DB')};UID={os.getenv('SANXING_USER')};PWD={os.getenv('SANXING_PASSWORD')};"

print("\n📡 Повторный точечный тест с исправленной логикой распознавания...")
print("-" * 95)

try:
    with pyodbc.connect(conn_str) as conn:
        cursor = conn.cursor()
        query = """
            SET NOCOUNT ON;
            WITH LatestReadings AS (
                SELECT 
                    RTRIM(LTRIM(M.MSNO)) as SerialNumber, 
                    D.DATA_TIME, 
                    D.KWH_ABS,
                    ROW_NUMBER() OVER (PARTITION BY M.MSNO ORDER BY D.DATA_TIME DESC) as rn
                FROM DATA_C_ELEC D
                INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                WHERE M.MSNO IN ('35004441', '11849223') AND D.TARIFF_ID = 0 AND D.KWH_ABS IS NOT NULL
            )
            SELECT SerialNumber, DATA_TIME, KWH_ABS FROM LatestReadings WHERE rn = 1;
        """
        cursor.execute(query)
        rows = cursor.fetchall()

        print(
            f"{'Номер счетчика':<15} | {'Ток (база)':<12} | {'Дата и время в БД':<20} | {'Сырое в БД':<15} | {'Итог (кВт*ч)':<15}"
        )
        print("-" * 95)

        for row in rows:
            sn = str(row.SerialNumber).strip()
            raw_value = float(row.KWH_ABS)
            current_type = devices_map.get(sn, "")

            # НОВАЯ ТОЧНАЯ ЛОГИКА: ищем "100" в строке тока прибора прямого включения
            if "100" in current_type:
                final_value = raw_value / 100.0
                calc_note = "/ 100.0"
            else:
                final_value = raw_value / 1000.0
                calc_note = "/ 1000.0"

            print(
                f"{sn:<15} | {current_type:<12} | {str(row.DATA_TIME):<20} | {raw_value:<15.3f} | {final_value:<15.3f} ({calc_note})"
            )
        print("-" * 95)

except Exception as e:
    print(f"❌ Ошибка: {e}")
