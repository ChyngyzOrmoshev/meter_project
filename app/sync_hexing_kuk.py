import os
import time
import mysql.connector
from datetime import datetime, timedelta
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

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
    print(f'🔄 [{datetime.now()}] Запуск синхронизации Hexing KUK (комбинированная энергия CA)...')

    # Получаем все серийные номера из MongoDB
    devices_set = set(str(d['serial_number']).strip() for d in devices_col.find({}, {'serial_number': 1}))
    if not devices_set:
        print('⚠️ Реестр устройств пуст, синхронизация отменена.')
        return

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
        print(f'❌ Ошибка подключения к MySQL: {e}')
        return

    cursor = conn.cursor()

    # Определяем время последней синхронизации
    last_reading = readings_col.find_one({'notes': 'Hexing KUK'}, sort=[('timestamp', -1)])
    if last_reading:
        last_time = last_reading['timestamp'] - timedelta(hours=1)
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
        print('📦 Первый запуск Hexing: импорт всей истории комбинированной энергии...')
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
                'notes': 'Авто-сбор: База Hexing KUK'
            }},
            upsert=True
        ))
        total += 1
        if len(updates) >= 1000:
            readings_col.bulk_write(updates, ordered=False)
            updates = []
            print(f'   Записано {total} показаний...')

    if updates:
        readings_col.bulk_write(updates, ordered=False)

    cursor.close()
    conn.close()
    print(f'✅ Синхронизация Hexing KUK завершена. Обработано показаний: {total}')

if __name__ == '__main__':
    print('🤖 Робот синхронизации Hexing KUK запущен. Интервал 10 минут.')
    while True:
        try:
            run_sync()
        except Exception as e:
            print(f'❌ Необработанная ошибка: {e}')
        time.sleep(600)