import os
import time
import pyodbc
from datetime import datetime, timedelta
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv
from sync_common import update_sync_status
from logger_config import logger

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(CURRENT_DIR, "cEnergo.env")
load_dotenv(dotenv_path=ENV_PATH)

SANXING_SERVER = os.getenv("SANXING_SERVER")
SANXING_DB = os.getenv("SANXING_DB")
SANXING_USER = os.getenv("SANXING_USER")
SANXING_PASSWORD = os.getenv("SANXING_PASSWORD")

mongo_client = MongoClient("mongodb://mongodb:27017/")
mongo_db = mongo_client["power_monitoring"]
devices_col = mongo_db["devices"]
readings_col = mongo_db["readings"]

def run_sanxing_synchronization():
    logger.info(f"🔄 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Запуск умной синхронизации Sanxing_old...")
    update_sync_status("Sanxing", "running")

    devices_cursor = devices_col.find(
        {}, {"_id": 0, "serial_number": 1, "nominal_current": 1}
    )
    devices_map = {
        str(d["serial_number"]).strip(): str(d.get("nominal_current", "")).strip()
        for d in devices_cursor
    }

    if not devices_map:
        logger.warning("⚠️ Синхронизация отменена: Реестр устройств (Devices) в MongoDB пуст!")
        update_sync_status("Sanxing", "idle", records_processed=0)
        return

    conn_str = f"DRIVER={{FreeTDS}};SERVER={SANXING_SERVER};DATABASE={SANXING_DB};UID={SANXING_USER};PWD={SANXING_PASSWORD};Port=1433;TDS_Version=7.4;"

    try:
        with pyodbc.connect(conn_str) as mssql_conn:
            cursor = mssql_conn.cursor()

            last_reading = readings_col.find_one(
                {"notes": "Авто-сбор: Sanxing_old"},
                sort=[("timestamp", -1)],
            )

            if last_reading:
                last_sync_time = last_reading["timestamp"] - timedelta(hours=2)
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_ABS
                    FROM DATA_C_ELEC D
                    INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                    WHERE D.DATA_TIME >= ? AND D.TARIFF_ID = 0 AND D.KWH_ABS IS NOT NULL
                    ORDER BY D.DATA_TIME ASC
                """
                cursor.execute(query, (last_sync_time,))
            else:
                logger.info("📦 Первый запуск Sanxing: импорт оперативной истории общего потребления (KWH_ABS)...")
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_ABS
                    FROM DATA_C_ELEC D
                    INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                    WHERE D.TARIFF_ID = 0 AND D.KWH_ABS IS NOT NULL
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
                    if db_sn in devices_map:
                        dt_object = row.DATA_TIME
                        if isinstance(dt_object, str):
                            try:
                                dt_object = datetime.strptime(dt_object.split(".")[0], "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                dt_object = datetime.now()

                        current_type = devices_map[db_sn]
                        raw_value = float(row.KWH_ABS)
                        if "100" in current_type:
                            final_value = raw_value / 100.0
                        else:
                            final_value = raw_value / 1000.0

                        mongo_ops.append(
                            UpdateOne(
                                {"serial_number": db_sn, "timestamp": dt_object},
                                {
                                    "$set": {
                                        "serial_number": db_sn,
                                        "timestamp": dt_object,
                                        "reading_value": final_value,
                                        "notes": "Авто-сбор: Sanxing_old",
                                    }
                                },
                                upsert=True,
                            )
                        )

                if mongo_ops:
                    result = readings_col.bulk_write(mongo_ops, ordered=False)
                    success_count += result.upserted_count + result.modified_count

            logger.info(f"✅ Синхронизация завершена. Добавлено/Обновлено суточных показаний: {success_count} шт.")
            update_sync_status("Sanxing", "success", records_processed=success_count)

    except Exception as e:
        logger.error(f"❌ Ошибка во время синхронизации Sanxing: {e}")
        update_sync_status("Sanxing", "error", error=str(e))

if __name__ == "__main__":
    logger.info("🤖 Робот умного автоматического сбора Sanxing_old (192.168.144.71) запущен.")
    logger.info("Автоматическое распознавание коэффициентов для 5(10)А и 5(100)А счетчиков.")
    logger.info("-" * 75)

    while True:
        try:
            run_sanxing_synchronization()
        except Exception as e:
            logger.error(f"❌ Необработанная ошибка: {e}. Перезапуск через 10 минут...")
        time.sleep(600)