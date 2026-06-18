import os
import time
import subprocess
import csv
import io
from datetime import datetime, timedelta
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv
from sync_common import update_sync_status
from logger_config import logger

load_dotenv('/app/cEnergo.env')

HOST = os.getenv("RISESUN_PG_HOST")
PORT = os.getenv("RISESUN_PG_PORT", "5432")
DB = os.getenv("RISESUN_PG_DB")
USER = os.getenv("RISESUN_PG_USER")
PASSWORD = os.getenv("RISESUN_PG_PASSWORD")

mongo_client = MongoClient("mongodb://mongodb:27017/")
db = mongo_client["power_monitoring"]
devices_col = db["devices"]
readings_col = db["readings"]

ENCODING = "cp1251"
QUERY_TIMEOUT = 600  # 10 минут

def run_psql_query(query):
    """Выполняет SQL-запрос через psql и возвращает список строк."""
    cmd = [
        "psql",
        "-h", HOST,
        "-p", PORT,
        "-U", USER,
        "-d", DB,
        "-t", "-A", "-F", ",",
        "-c", query
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = PASSWORD
    env["PGSSLMODE"] = "disable"
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, timeout=QUERY_TIMEOUT)
        if result.returncode != 0:
            try:
                err = result.stderr.decode(ENCODING)
            except:
                err = result.stderr.decode('utf-8', errors='replace')
            logger.error(f"Ошибка psql: {err}")
            return []
        try:
            output = result.stdout.decode(ENCODING)
        except:
            output = result.stdout.decode('utf-8', errors='replace')
        output = output.strip()
        if not output:
            return []
        reader = csv.reader(io.StringIO(output))
        return list(reader)
    except subprocess.TimeoutExpired:
        logger.error(f"Запрос превысил таймаут {QUERY_TIMEOUT} секунд.")
        return []
    except Exception as e:
        logger.error(f"Ошибка выполнения psql: {e}")
        return []

def run_sync():
    logger.info("🔄 Запуск синхронизации RiseSun (только производитель RiseSun, тип 10)...")
    update_sync_status("RiseSun", "running")

    # Получаем все серийные номера из MongoDB
    devices_set = {str(d['serial_number']).strip() for d in devices_col.find({}, {'serial_number': 1})}
    if not devices_set:
        logger.warning("⚠️ Реестр устройств пуст, синхронизация отменена.")
        update_sync_status("RiseSun", "idle", records_processed=0)
        return

    # Определяем дату последней синхронизации
    last_reading = readings_col.find_one({'notes': 'RiseSun'}, sort=[('timestamp', -1)])
    if last_reading:
        last_time = last_reading['timestamp'] - timedelta(hours=1)
        last_time_str = last_time.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"Загрузка данных с {last_time_str}...")
        query = f"""
            SELECT 
                m.device_id AS serial_number,
                r.date AS timestamp,
                r.value AS reading_value
            FROM ami.readings r
            JOIN ami.meters m ON r.meter = m.id
            JOIN ami.models mo ON m.model = mo.id
            WHERE r.date >= '{last_time_str}'
              AND r.type = 10
              AND r.value IS NOT NULL
              AND m.device_id IS NOT NULL
              AND m.device_id != ''
              AND mo.producer = 5
            ORDER BY r.date ASC
        """
    else:
        logger.info("📦 Первый запуск: загрузка данных с 2026-06-01 (только RiseSun)...")
        query = """
            SELECT 
                m.device_id AS serial_number,
                r.date AS timestamp,
                r.value AS reading_value
            FROM ami.readings r
            JOIN ami.meters m ON r.meter = m.id
            JOIN ami.models mo ON m.model = mo.id
            WHERE r.date >= '2026-06-01'
              AND r.type = 10
              AND r.value IS NOT NULL
              AND m.device_id IS NOT NULL
              AND m.device_id != ''
              AND mo.producer = 5
            ORDER BY r.date ASC
        """

    rows = run_psql_query(query)
    if not rows:
        logger.info("Нет новых данных для синхронизации.")
        update_sync_status("RiseSun", "success", records_processed=0)
        return

    updates = []
    total = 0
    for row in rows:
        if len(row) < 3:
            continue
        sn = str(row[0]).strip()
        if not sn or sn not in devices_set:
            continue
        # Парсим дату
        try:
            ts = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
        except:
            try:
                ts = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S.%f")
            except:
                continue
        try:
            val = float(row[2])
        except:
            continue
        updates.append(UpdateOne(
            {'serial_number': sn, 'timestamp': ts},
            {'$set': {
                'serial_number': sn,
                'timestamp': ts,
                'reading_value': val,
                'notes': 'RiseSun'
            }},
            upsert=True
        ))
        total += 1
        if len(updates) >= 1000:
            readings_col.bulk_write(updates, ordered=False)
            updates = []
            logger.info(f"   Записано {total} показаний...")

    if updates:
        readings_col.bulk_write(updates, ordered=False)

    logger.info(f"✅ Синхронизация RiseSun завершена. Обработано показаний: {total}")
    update_sync_status("RiseSun", "success", records_processed=total)

if __name__ == "__main__":
    logger.info("🤖 Робот синхронизации RiseSun (через psql) запущен. Интервал 10 минут.")
    while True:
        try:
            run_sync()
        except Exception as e:
            logger.error(f"❌ Необработанная ошибка: {e}")
            update_sync_status("RiseSun", "error", error=str(e))
        time.sleep(600)