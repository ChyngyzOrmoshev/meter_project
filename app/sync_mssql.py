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

MSSQL_SERVER = os.getenv("DB_MSSQL_SERVER")
MSSQL_USER = os.getenv("DB_MSSQL_USER")
MSSQL_PASSWORD = os.getenv("DB_MSSQL_PASSWORD")
MSSQL_DB = os.getenv("DB_MSSQL_NAME")

mongo_client = MongoClient("mongodb://mongodb:27017/")
mongo_db = mongo_client["power_monitoring"]
devices_col = mongo_db["devices"]
readings_col = mongo_db["readings"]

def run_synchronization():
    logger.info(f"🔄 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Запуск синхронизации с cEnergo...")
    update_sync_status("cEnergo", "running")

    registered_sns_set = {str(sn).strip() for sn in devices_col.distinct("serial_number")}
    if not registered_sns_set:
        logger.warning("⚠️ Синхронизация отменена: Реестр устройств (Devices) в MongoDB пуст!")
        update_sync_status("cEnergo", "idle", records_processed=0)
        return

    conn_str = f"DRIVER={{FreeTDS}};SERVER={MSSQL_SERVER};DATABASE={MSSQL_DB};UID={MSSQL_USER};PWD={MSSQL_PASSWORD};Port=1433;TDS_Version=7.4;"

    try:
        with pyodbc.connect(conn_str) as mssql_conn:
            cursor = mssql_conn.cursor()

            last_reading = readings_col.find_one(
                {"notes": "Авто-сбор: База cEnergo (MS SQL)"}, sort=[("timestamp", -1)]
            )

            if last_reading:
                last_sync_time = last_reading["timestamp"] - timedelta(minutes=1)
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.SerialNumber)) as SerialNumber, V.DT, V.Val
                    FROM [Values] V
                    INNER JOIN Meters M ON V.MeterId = M.MeterId
                    WHERE V.DT >= ? AND V.PropertyId = 12
                    ORDER BY V.DT ASC
                """
                cursor.execute(query, (last_sync_time,))
            else:
                logger.info("📦 Первый запуск: выкачиваем историю активной энергии А+ (PropertyId=12)...")
                query = """
                    SET NOCOUNT ON;
                    SELECT RTRIM(LTRIM(M.SerialNumber)) as SerialNumber, V.DT, V.Val
                    FROM [Values] V
                    INNER JOIN Meters M ON V.MeterId = M.MeterId
                    WHERE V.PropertyId = 12
                    ORDER BY V.DT ASC
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
                        dt_object = row.DT
                        if isinstance(dt_object, str):
                            try:
                                clean_dt_str = dt_object.split(".")[0]
                                dt_object = datetime.strptime(clean_dt_str, "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                dt_object = datetime.now()
                        mongo_ops.append(
                            UpdateOne(
                                {"serial_number": db_sn, "timestamp": dt_object},
                                {
                                    "$set": {
                                        "serial_number": db_sn,
                                        "timestamp": dt_object,
                                        "reading_value": float(row.Val),
                                        "notes": "Авто-сбор: База cEnergo",
                                    }
                                },
                                upsert=True,
                            )
                        )

                if mongo_ops:
                    result = readings_col.bulk_write(mongo_ops, ordered=False)
                    success_count += result.upserted_count + result.modified_count

            logger.info(f"✅ Синхронизация завершена. Обработано/Добавлено показаний А+: {success_count} шт.")
            update_sync_status("cEnergo", "success", records_processed=success_count)

    except Exception as e:
        logger.error(f"❌ Ошибка во время синхронизации: {e}")
        update_sync_status("cEnergo", "error", error=str(e))

if __name__ == "__main__":
    logger.info("🤖 Робот автоматического сбора cEnergo запущен (Фильтр: Активная энергия А+)")
    logger.info("Синхронизация происходит в фоне каждые 10 минут.")
    logger.info("-" * 75)

    while True:
        try:
            run_synchronization()
        except Exception as e:
            logger.error(f"❌ Необработанная ошибка: {e}. Перезапуск через 10 минут...")
        time.sleep(600)