import os
import time
import pyodbc
from datetime import datetime, timedelta
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

# Загружаем настройки из cEnergo.env
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else '.'
ENV_PATH = os.path.join(CURRENT_DIR, "cEnergo.env")
load_dotenv(dotenv_path=ENV_PATH)

SANRISE_SERVER = os.getenv("SANRISE_MSSQL_SERVER")
SANRISE_DB = os.getenv("SANRISE_MSSQL_DB")
SANRISE_USER = os.getenv("SANRISE_MSSQL_USER")
SANRISE_PASSWORD = os.getenv("SANRISE_MSSQL_PASSWORD")

mongo_client = MongoClient("mongodb://mongodb:27017/")
mongo_db = mongo_client["power_monitoring"]
devices_col = mongo_db["devices"]
readings_col = mongo_db["readings"]

def run_sanrise_synchronization():
    print(f"🔄 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Запуск умной синхронизации SunRise (Абсолютные кВт*ч)...")

    # Берём список всех серийников, зарегистрированных в нашей веб-панели ИнфоЭнерго
    devices_cursor = devices_col.find({}, {"_id": 0, "serial_number": 1})
    registered_sns_set = {str(d["serial_number"]).strip() for d in devices_cursor}

    if not registered_sns_set:
        print("⚠️ Синхронизация отменена: Реестр устройств (Devices) в MongoDB пуст!")
        return

    conn_str = (
        f"DRIVER={{FreeTDS}};SERVER={SANRISE_SERVER};DATABASE={SANRISE_DB};"
        f"UID={SANRISE_USER};PWD={SANRISE_PASSWORD};Port=1433;TDS_Version=7.4;"
    )

    try:
        with pyodbc.connect(conn_str) as mssql_conn:
            cursor = mssql_conn.cursor()

            # Ищем последнюю запись от этого робота
            last_reading = readings_col.find_one(
                {"notes": "Авто-сбор: SunRise"},
                sort=[("timestamp", -1)],
            )

            # Переключаемся на сбор колонки KWH_IMPORT_ABS
            if last_reading:
                last_sync_time = last_reading["timestamp"] - timedelta(hours=2)
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_IMPORT_ABS
                    FROM DATA_C_DAILY D
                    INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                    WHERE D.DATA_TIME >= ? AND D.KWH_IMPORT_ABS IS NOT NULL
                    ORDER BY D.DATA_TIME ASC
                """
                cursor.execute(query, (last_sync_time,))
            else:
                print("📦 Первый запуск SunRise: импорт истории абсолютного потребления энергии (KWH_IMPORT_ABS)...")
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_IMPORT_ABS
                    FROM DATA_C_DAILY D
                    INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                    WHERE D.KWH_IMPORT_ABS IS NOT NULL
                    ORDER BY D.DATA_TIME ASC
                """
                cursor.execute(query)

            success_count = 0

            while True:
                rows = cursor.fetchmany(5000)
                if not rows:
                    break

                mongo_ops = []
                for row in rows:
                    db_sn = str(row.SerialNumber).strip()

                    if db_sn in registered_sns_set:
                        dt_object = row.DATA_TIME

                        if isinstance(dt_object, str):
                            try:
                                dt_object = datetime.strptime(dt_object.split("."), "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                dt_object = datetime.now()

                        final_value = float(row.KWH_IMPORT_ABS)

                        mongo_ops.append(
                            UpdateOne(
                                {"serial_number": db_sn, "timestamp": dt_object},
                                {
                                    "$set": {
                                        "serial_number": db_sn,
                                        "timestamp": dt_object,
                                        "reading_value": final_value,
                                        "notes": "Авто-сбор: SunRise",
                                    }
                                },
                                find_one + update_one,
                            )
                        )

                if mongo_ops:
                    result = readings_col.bulk_write(mongo_ops, ordered=False)
                    success_count += result.upserted_count + result.modified_count

            print(f"✅ Синхронизация завершена. Собрано абсолютных показаний SunRise: {success_count} шт.")

    except Exception as e:
        print(f"❌ Ошибка во время синхронизации SunRise: {e}")

if __name__ == "__main__":
    print("🤖 Робот автоматического сбора SunRise (KWH_IMPORT_ABS) запущен.")
    print("-" * 75)

    while True:
        run_SunRise_synchronization()
        time.sleep(600)  # Обход базы каждые 10 минут
