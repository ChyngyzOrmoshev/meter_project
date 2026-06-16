import os
import time
import pyodbc
from datetime import datetime, timedelta
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

# В Docker переменные уже в системе, но оставим совместимость для локального запуска
# ИСПРАВЛЕНО: Файл лежит в той же папке app, убираем переход на уровень вверх
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(CURRENT_DIR, "cEnergo.env") 
load_dotenv(dotenv_path=ENV_PATH)


MSSQL_SERVER = os.getenv("DB_MSSQL_SERVER")
MSSQL_USER = os.getenv("DB_MSSQL_USER")
MSSQL_PASSWORD = os.getenv("DB_MSSQL_PASSWORD")
MSSQL_DB = os.getenv("DB_MSSQL_NAME")

# ИСПРАВЛЕНО: Меняем localhost на имя сервиса в сети Docker
mongo_client = MongoClient("mongodb://mongodb:27017/")
mongo_db = mongo_client["power_monitoring"]
devices_col = mongo_db["devices"]
readings_col = mongo_db["readings"]


def run_synchronization():
    print(
        f"🔄 [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Запуск синхронизации с cEnergo..."
    )

    # Переводим список в ХЭШ-СЕТ (set) для мгновенного поиска O(1)
    registered_sns_set = {
        str(sn).strip() for sn in devices_col.distinct("serial_number")
    }
    if not registered_sns_set:
        print("⚠️ Синхронизация отменена: Реестр устройств (Devices) в MongoDB пуст!")
        return

    # ИСПРАВЛЕНО: Вместо Windows-названия драйвера используем стандартный Linux-драйвер FreeTDS
    conn_str = f"DRIVER={{FreeTDS}};SERVER={MSSQL_SERVER};DATABASE={MSSQL_DB};UID={MSSQL_USER};PWD={MSSQL_PASSWORD};Port=1433;TDS_Version=7.4;"


    try:
        with pyodbc.connect(conn_str) as mssql_conn:
            cursor = mssql_conn.cursor()

            # Узнаем время самого последнего замера в нашей MongoDB
            last_reading = readings_col.find_one(
                {"notes": "Авто-сбор: База cEnergo (MS SQL)"}, sort=[("timestamp", -1)]
            )

            # ВАЖНО: Добавляем SET NOCOUNT ON; для подавления технических сообщений MS SQL
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
                print(
                    "📦 Первый запуск: выкачиваем историю активной энергии А+ (PropertyId=12)..."
                )
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
                # Теперь fetchmany отработает стабильно, так как NOCOUNT убрал лишние сообщения сервера
                rows = cursor.fetchmany(5000)
                if not rows:
                    break

                mongo_ops = []
                for row in rows:
                    db_sn = str(row.SerialNumber).strip()

                    if db_sn in registered_sns_set:
                        # УНИВЕРСАЛЬНАЯ И БЕЗОПАСНАЯ КОНВЕРТАЦИЯ ДАТЫ СТЫК В СТЫК
                        dt_object = row.DT
                        if isinstance(dt_object, str):
                            try:
                                # Если пришла строка, отсекаем наносекунды и парсим
                                clean_dt_str = dt_object.split(".")[0]
                                dt_object = datetime.strptime(
                                    clean_dt_str, "%Y-%m-%d %H:%M:%S"
                                )
                            except Exception:
                                dt_object = (
                                    datetime.now()
                                )  # Резервный вариант, если формат совсем сломан

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

            print(
                f"✅ Синхронизация завершена. Обработано/Добавлено показаний А+: {success_count} шт."
            )

    except Exception as e:
        print(f"❌ Ошибка во время синхронизации: {e}")


if __name__ == "__main__":
    print("🤖 Робот автоматического сбора cEnergo запущен (Фильтр: Активная энергия А+)")
    print("Синхронизация происходит в фоне каждые 10 минут.")
    print("-" * 75)

    while True:
        try:
            run_synchronization()
        except Exception as e:
            print(f"❌ Необработанная ошибка: {e}. Перезапуск через 10 минут...")
        time.sleep(600)
