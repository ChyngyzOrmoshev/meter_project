import os
import time
import pyodbc
from datetime import datetime, timedelta
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv
from sync_common import update_sync_status
from logger_config import logger

load_dotenv('/app/cEnergo.env')

SANRISE_SERVER = os.getenv("SANRISE_MSSQL_SERVER")
SANRISE_DB = os.getenv("SANRISE_MSSQL_DB")
SANRISE_USER = os.getenv("SANRISE_MSSQL_USER")
SANRISE_PASSWORD = os.getenv("SANRISE_MSSQL_PASSWORD")

mongo_client = MongoClient("mongodb://mongodb:27017/")
mongo_db = mongo_client["power_monitoring"]
devices_col = mongo_db["devices"]
readings_col = mongo_db["readings"]

# Настройки подключения с таймаутами и повторными попытками
CONNECTION_STR = (
    f"DRIVER={{FreeTDS}};"
    f"SERVER={SANRISE_SERVER};"
    f"DATABASE={SANRISE_DB};"
    f"UID={SANRISE_USER};"
    f"PWD={SANRISE_PASSWORD};"
    f"Port=1433;"
    f"TDS_Version=7.3;"
    f"Connection Timeout=30;"
    f"Login Timeout=30;"
)

MAX_RETRIES = 3
RETRY_DELAY = 10  # секунд между попытками

def connect_with_retry():
    """Подключается к MS SQL с повторными попытками при ошибке."""
    for attempt in range(MAX_RETRIES):
        try:
            conn = pyodbc.connect(CONNECTION_STR, timeout=30)
            logger.info(f"Подключение к SunRise установлено (попытка {attempt+1})")
            return conn
        except Exception as e:
            logger.warning(f"Попытка подключения {attempt+1}/{MAX_RETRIES} не удалась: {e}")
            if attempt == MAX_RETRIES - 1:
                raise  # последняя попытка — выбрасываем исключение
            time.sleep(RETRY_DELAY)
    return None

def run_sanrise_synchronization():
    logger.info(f"🔄 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Запуск синхронизации SunRise...")
    update_sync_status("Sanrise", "running")

    # Получаем все полные серийные номера из MongoDB
    devices_cursor = devices_col.find({}, {"_id": 0, "serial_number": 1})
    devices_set = {str(d["serial_number"]).strip() for d in devices_cursor}
    if not devices_set:
        logger.warning("⚠️ Реестр устройств пуст, синхронизация отменена.")
        update_sync_status("Sanrise", "idle", records_processed=0)
        return

    # Подключаемся с повторными попытками
    try:
        mssql_conn = connect_with_retry()
        if not mssql_conn:
            return
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к SunRise после {MAX_RETRIES} попыток: {e}")
        update_sync_status("Sanrise", "error", error=str(e))
        return

    try:
        cursor = mssql_conn.cursor()

        # Определяем время последней синхронизации
        last_reading = readings_col.find_one(
            {"notes": "Авто-сбор: SunRise"},
            sort=[("timestamp", -1)],
        )

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
            logger.info("📦 Первый запуск SunRise: импорт всей истории (LIMIT 100000 для ускорения)...")
            # Для первого запуска ограничим количество записей, чтобы не перегружать сервер
            query = """
                SET NOCOUNT ON;
                SELECT TOP 100000 RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_IMPORT_ABS
                FROM DATA_C_DAILY D
                INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                WHERE D.KWH_IMPORT_ABS IS NOT NULL
                ORDER BY D.DATA_TIME ASC
            """
            cursor.execute(query)

        success_count = 0
        updates = []
        total_rows = 0

        while True:
            rows = cursor.fetchmany(5000)
            if not rows:
                break

            for row in rows:
                db_sn = str(row.SerialNumber).strip()
                if not db_sn:
                    continue

                # Пытаемся найти полный серийный номер (если в реестре есть суффикс)
                full_sn = None
                if db_sn in devices_set:
                    full_sn = db_sn
                else:
                    # Поиск по суффиксу (если номер в БД короче)
                    candidates = [sn for sn in devices_set if sn.endswith(db_sn)]
                    if len(candidates) == 1:
                        full_sn = candidates[0]
                    elif len(candidates) > 1:
                        logger.warning(f"Несколько совпадений для {db_sn}: {candidates}")
                        continue
                    else:
                        # Попробуем поиск по началу (если номер в БД длиннее)
                        candidates2 = [sn for sn in devices_set if db_sn.startswith(sn)]
                        if len(candidates2) == 1:
                            full_sn = candidates2[0]
                        else:
                            continue

                if not full_sn:
                    continue

                dt_object = row.DATA_TIME
                if isinstance(dt_object, str):
                    try:
                        dt_object = datetime.strptime(dt_object.split(".")[0], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        dt_object = datetime.now()

                final_value = float(row.KWH_IMPORT_ABS)
                updates.append(
                    UpdateOne(
                        {"serial_number": full_sn, "timestamp": dt_object},
                        {
                            "$set": {
                                "serial_number": full_sn,
                                "timestamp": dt_object,
                                "reading_value": final_value,
                                "notes": "Авто-сбор: SunRise",
                            }
                        },
                        upsert=True,
                    )
                )
                total_rows += 1

                if len(updates) >= 1000:
                    result = readings_col.bulk_write(updates, ordered=False)
                    success_count += result.upserted_count + result.modified_count
                    updates = []

        if updates:
            result = readings_col.bulk_write(updates, ordered=False)
            success_count += result.upserted_count + result.modified_count

        logger.info(f"✅ Синхронизация SunRise завершена. Обработано показаний: {success_count} шт.")
        update_sync_status("Sanrise", "success", records_processed=success_count)

    except Exception as e:
        logger.error(f"❌ Ошибка во время синхронизации SunRise: {e}")
        update_sync_status("Sanrise", "error", error=str(e))
    finally:
        if mssql_conn:
            mssql_conn.close()

if __name__ == "__main__":
    logger.info("🤖 Робот синхронизации SunRise (KWH_IMPORT_ABS) запущен. Интервал 15 минут.")
    while True:
        try:
            run_sanrise_synchronization()
        except Exception as e:
            logger.error(f"❌ Необработанная ошибка: {e}")
            update_sync_status("Sanrise", "error", error=str(e))
        time.sleep(900)  # 15 минут вместо 10 для снижения нагрузки