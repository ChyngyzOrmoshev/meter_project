import os
import pyodbc
from datetime import datetime, timedelta
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

# Загружаем настройки из cEnergo.env
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else '.'
ENV_PATH = os.path.join(CURRENT_DIR, "cEnergo.env")
load_dotenv(dotenv_path=ENV_PATH)

SERVER = os.getenv("SANRISE_MSSQL_SERVER")
DB = os.getenv("SANRISE_MSSQL_DB")
USER = os.getenv("SANRISE_MSSQL_USER")
PASSWORD = os.getenv("SANRISE_MSSQL_PASSWORD")

# Наш целевой счетчик со скриншота
TEST_METER_SN = "8003200721"

# Подключаемся к центральной MongoDB
mongo_client = MongoClient("mongodb://mongodb:27017/")
mongo_db = mongo_client["power_monitoring"]
readings_col = mongo_db["readings"]

def run_accurate_single_meter_test():
    print(f"🤖 Точечный тест сбора АБСОЛЮТНЫХ показаний для прибора № {TEST_METER_SN}...")
    
    conn_str = (
        f"DRIVER={{FreeTDS}};SERVER={SERVER};DATABASE={DB};"
        f"UID={USER};PWD={PASSWORD};Port=1433;TDS_Version=7.4;"
    )
    
    # Жестко фиксируем проверочный период: последние 3 дня, как на скриншоте (июнь 2026)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=3)
    
    print(f"⏰ Период проверки: с {start_date.strftime('%Y-%m-%d')} по {end_date.strftime('%Y-%m-%d')}")
    
    try:
        with pyodbc.connect(conn_str) as mssql_conn:
            cursor = mssql_conn.cursor()

            # ИСПРАВЛЕНО: забираем колонку KWH_IMPORT_ABS и фильтруем строго по диапазону дат
            query = """
                SET NOCOUNT ON;
                SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_IMPORT_ABS
                FROM DATA_C_DAILY D
                INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
                WHERE M.MSNO = ? AND D.DATA_TIME >= ? AND D.DATA_TIME <= ? AND D.KWH_IMPORT_ABS IS NOT NULL
                ORDER BY D.DATA_TIME ASC
            """
            
            cursor.execute(query, (TEST_METER_SN, start_date.strftime('%Y-%m-%d 00:00:00'), end_date.strftime('%Y-%m-%d 23:59:59')))
            rows = cursor.fetchall()
            
            if not rows:
                print(f"⚠️ Показаний по колонке KWH_IMPORT_ABS за этот период в MS SQL не найдено!")
                return
                
            print(f"📦 Найдено архивных записей в MS SQL: {len(rows)} шт.")
            print("\n📋 Вывод результатов сопоставления с веб-интерфейсом:")
            print("-" * 80)
            
            mongo_ops = []
            for row in rows:
                db_sn = str(row.SerialNumber).strip()
                dt_object = row.DATA_TIME
                # Данные забираем «как есть» — с плавающей точкой из базы, без деления на 1000
                final_value = float(row.KWH_IMPORT_ABS)
                
                print(f"🔹 Счетчик: {db_sn} | Дата: {dt_object} | Значение: {final_value} кВт*ч")
                
                # Подготовка пакета для MongoDB
                mongo_ops.append(
                    UpdateOne(
                        {"serial_number": db_sn, "timestamp": dt_object},
                        {
                            "$set": {
                                "serial_number": db_sn,
                                "timestamp": dt_object,
                                "reading_value": final_value,
                                "notes": "Тест-сбор: SanRise (Absolute KWH)",
                            }
                        },
                        upsert=True
                    )
                )
                
            print("-" * 80)
            
            if mongo_ops:
                print("📥 Запись точечных данных в центральную MongoDB...")
                result = readings_col.bulk_write(mongo_ops, ordered=False)
                print(f"✅ УСПЕХ! В MongoDB добавлено/обновлено: {result.upserted_count + result.modified_count} записей.")
                
    except Exception as e:
        print(f"❌ Ошибка во время выполнения теста: {e}")

if __name__ == "__main__":
    run_accurate_single_meter_test()
