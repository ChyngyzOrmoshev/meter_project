import os
import time
import mysql.connector
from datetime import datetime, timedelta
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv
from sync_common import update_sync_status
from logger_config import logger

load_dotenv('/app/cEnergo.env')

HEXING_HOST = os.getenv('HEXING_KUK_MYSQL_HOST')
HEXING_PORT = int(os.getenv('HEXING_KUK_MYSQL_PORT', 3306))
HEXING_DB = os.getenv('HEXING_KUK_MYSQL_DB')
HEXING_USER = os.getenv('HEXING_KUK_MYSQL_USER')
HEXING_PASS = os.getenv('HEXING_KUK_MYSQL_PASSWORD')

mongo_client = MongoClient('mongodb://mongodb:27017/')
db = mongo_client['power_monitoring']
devices_col = db['devices']
readings_col = db['readings']

def run_sync():
    logger.info(f'🔄 [{datetime.now()}] Запуск синхронизации Hexing KUK (комбинированная энергия CA)...')
    update_sync_status("Hexing KUK", "running")

    # Получаем устройства из реестра
    devices_set = set(str(d['serial_number']).strip() for d in devices_col.find({}, {'serial_number': 1}))
    if not devices_set:
        logger.warning('⚠️ Реестр устройств пуст, синхронизация отменена.')
        update_sync_status("Hexing KUK", "idle", records_processed=0)
        return

    # Проверяем, сколько записей уже есть
    existing_count = readings_col.count_documents({'notes': 'Hexing KUK'})
    logger.info(f"📊 Существующих записей Hexing KUK: {existing_count}")

    # Подключаемся к MySQL
    try:
        conn = mysql.connector.connect(
            host=HEXING_HOST,
            port=HEXING_PORT,
            user=HEXING_USER,
            password=HEXING_PASS,
            database=HEXING_DB,
            connect_timeout=10
        )
    except Exception as e:
        logger.error(f'❌ Ошибка подключения к MySQL: {e}')
        update_sync_status("Hexing KUK", "error", error=str(e))
        return

    cursor = conn.cursor()

    # Ищем последнюю запись
    last_reading = readings_col.find_one({'notes': 'Hexing KUK'}, sort=[('timestamp', -1)])
    if last_reading:
        last_time = last_reading['timestamp'] - timedelta(hours=1)
        last_time_str = last_time.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"🕒 Последняя синхронизация: {last_time_str}")
        query = """
            SELECT 
                m.METER_NO AS serial_number,
                t.TV AS timestamp,
                t.CA AS reading_value
            FROM biz_pub_data_t_energy_d t
            JOIN a_data_catalogue c ON t.DATA_ID = c.DATA_ID
            JOIN a_equip_meter m ON c.METER_ID = m.METER_ID
            WHERE t.TV >= %s
              AND t.CA IS NOT NULL
            ORDER BY t.TV ASC
        """
        cursor.execute(query, (last_time,))
    else:
        logger.info('📦 Первый запуск Hexing: импорт всей истории комбинированной энергии...')
        query = """
            SELECT 
                m.METER_NO AS serial_number,
                t.TV AS timestamp,
                t.CA AS reading_value
            FROM biz_pub_data_t_energy_d t
            JOIN a_data_catalogue c ON t.DATA_ID = c.DATA_ID
            JOIN a_equip_meter m ON c.METER_ID = m.METER_ID
            WHERE t.CA IS NOT NULL
            ORDER BY t.TV ASC
        """
        cursor.execute(query)

    updates = []
    total = 0
    for (serial_number, timestamp, reading_value) in cursor:
        sn = str(serial_number).strip()
        if sn not in devices_set:
            continue
        if reading_value is None:
            continue
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
            except:
                continue
        updates.append(UpdateOne(
            {'serial_number': sn, 'timestamp': timestamp},
            {'$set': {
                'serial_number': sn,
                'timestamp': timestamp,
                'reading_value': float(reading_value),
                'notes': 'Hexing KUK'
            }},
            upsert=True
        ))
        total += 1
        if len(updates) >= 1000:
            try:
                result = readings_col.bulk_write(updates, ordered=False)
                logger.info(f"   💾 Сохранено пакет: upserted={result.upserted_count}, modified={result.modified_count}")
            except Exception as e:
                logger.error(f"   ❌ Ошибка bulk_write: {e}")
            updates = []

    if updates:
        try:
            result = readings_col.bulk_write(updates, ordered=False)
            logger.info(f"   💾 Сохранено пакет: upserted={result.upserted_count}, modified={result.modified_count}")
        except Exception as e:
            logger.error(f"   ❌ Ошибка bulk_write: {e}")

    cursor.close()
    conn.close()
    logger.info(f'✅ Синхронизация Hexing KUK завершена. Обработано показаний: {total}')
    # Проверяем итоговое количество
    final_count = readings_col.count_documents({'notes': 'Hexing KUK'})
    logger.info(f"📊 Итоговое количество записей Hexing KUK: {final_count}")
    update_sync_status("Hexing KUK", "success", records_processed=total)

if __name__ == '__main__':
    logger.info('🤖 Робот синхронизации Hexing KUK (комбинированная энергия CA) запущен. Интервал 10 минут.')
    while True:
        try:
            run_sync()
        except Exception as e:
            logger.error(f'❌ Необработанная ошибка: {e}')
            update_sync_status("Hexing KUK", "error", error=str(e))
        time.sleep(600)