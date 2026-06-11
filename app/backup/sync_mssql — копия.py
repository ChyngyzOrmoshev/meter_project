import os
import time
import pyodbc
from datetime import datetime
from pymongo import MongoClient
from dotenv import load_dotenv

# Загружаем настройки из cEnergo.env
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(CURRENT_DIR, "cEnergo.env")
load_dotenv(dotenv_path=ENV_PATH)

MSSQL_SERVER = os.getenv("DB_MSSQL_SERVER")
MSSQL_USER = os.getenv("DB_MSSQL_USER")
MSSQL_PASSWORD = os.getenv("DB_MSSQL_PASSWORD")
MSSQL_DB = os.getenv("DB_MSSQL_NAME")

# Подключение к локальной MongoDB в Docker
mongo_client = MongoClient("mongodb://localhost:27017/")
mongo_db = mongo_client["power_monitoring"]
devices_col = mongo_db["devices"]
readings_col = mongo_db["readings"]


def run_synchronization():
    print(
        f"🔄 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Запуск синхронизации с cEnergo..."
    )

    # Получаем список 15-значных номеров, зарегистрированных на сайте
    registered_sns = [str(sn).strip() for sn in devices_col.distinct("serial_number")]
    if not registered_sns:
        print("⚠️ Синхронизация отменена: Реестр устройств (Devices) в MongoDB пуст!")
        return

    conn_str = f"DRIVER={{SQL Server}};SERVER={MSSQL_SERVER};DATABASE={MSSQL_DB};UID={MSSQL_USER};PWD={MSSQL_PASSWORD};"

    try:
        mssql_conn = pyodbc.connect(conn_str)
        cursor = mssql_conn.cursor()

        # Узнаем время самого последнего замера в нашей MongoDB, чтобы не дублировать старые данные
        last_reading = readings_col.find_one(
            {"notes": "Авто-сбор: База cEnergo (MS SQL)"}, sort=[("timestamp", -1)]
        )

        if last_reading:
            # Если данные уже есть — берем объект даты напрямую из MongoDB без перевода в текст
            last_sync_time = last_reading["timestamp"]
            query = """
                SELECT RTRIM(LTRIM(M.SerialNumber)) as SerialNumber, V.DT, V.Val
                FROM [Values] V
                INNER JOIN Meters M ON V.MeterId = M.MeterId
                WHERE V.DT > ? AND V.PropertyId = 12
                ORDER BY V.DT ASC
            """
            cursor.execute(query, (last_sync_time,))
        else:
            print(
                "📦 Первый запуск: выкачиваем чистую историю активной энергии А+ (PropertyId=12)..."
            )
            query = """
                SELECT RTRIM(LTRIM(M.SerialNumber)) as SerialNumber, V.DT, V.Val
                FROM [Values] V
                INNER JOIN Meters M ON V.MeterId = M.MeterId
                WHERE V.PropertyId = 12
                ORDER BY V.DT ASC
            """
            cursor.execute(query)

        success_count = 0

        while True:
            rows = cursor.fetchmany(5000)  # Берем пачками по 5000 штук
            if not rows:
                break

            mongo_docs = []
            for row in rows:
                db_sn = str(row.SerialNumber).strip()

                # Проверяем, есть ли счетчик в нашем реестре на сайте
                if db_sn in registered_sns:
                    mongo_docs.append(
                        {
                            "serial_number": db_sn,
                            "timestamp": row.DT,
                            "reading_value": float(row.Val),
                            "notes": "Авто-сбор: База cEnergo (MS SQL)",
                        }
                    )
                    success_count += 1

            if mongo_docs:
                readings_col.insert_many(mongo_docs)

        print(
            f"✅ Синхронизация завершена. Добавлено чистых показаний А+: {success_count} шт."
        )
        mssql_conn.close()

    except Exception as e:
        print(f"❌ Ошибка во время синхронизации: {e}")


if __name__ == "__main__":
    print(
        "🤖 Робот автоматического сбора cEnergo запущен (Фильтр: Активная энергия А+)"
    )
    print("Синхронизация происходит в фоне каждые 10 минут. Окно нельзя закрывать.")
    print("-" * 75)

    while True:
        run_synchronization()
        time.sleep(600)  # Обход каждые 10 минут
