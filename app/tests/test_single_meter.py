import os
import pyodbc
from dotenv import load_dotenv

# Загружаем настройки подключения из конфигурационного файла
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(CURRENT_DIR, 'cEnergo.env'))

MSSQL_SERVER = os.getenv("DB_MSSQL_SERVER")
MSSQL_USER = os.getenv("DB_MSSQL_USER")
MSSQL_PASSWORD = os.getenv("DB_MSSQL_PASSWORD")
MSSQL_DB = os.getenv("DB_MSSQL_NAME")

# Целевой заводской номер счетчика для точечной проверки
TARGET_SN = "013000196475556"

def test_fetch_readings():
    print(f"🕵️‍♂️ Начинаем точечный поиск показаний для счетчика: '{TARGET_SN}'")
    print(f"🔌 Подключение к {MSSQL_SERVER}...")
    
    conn_str = (
        f"DRIVER={{SQL Server}};"
        f"SERVER={MSSQL_SERVER};"
        f"DATABASE={MSSQL_DB};"
        f"UID={MSSQL_USER};"
        f"PWD={MSSQL_PASSWORD};"
        f"Connection Timeout=15;"
    )
    
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.conn.cursor() if hasattr(conn, 'conn') else conn.cursor()
        print("✅ Подключено успешно. Ищем прибор в таблице Meters...")

        # 1. Проверяем наличие прибора в таблице Meters и узнаем его внутренний MeterId
        cursor.execute("""
            SELECT MeterId, SerialNumber, Designation 
            FROM Meters 
            WHERE LTRIM(RTRIM(SerialNumber)) = ? 
               OR LTRIM(RTRIM(SerialNumber)) LIKE ?
        """, (TARGET_SN, f"%{TARGET_SN}%"))
        
        meter_row = cursor.fetchone()
        
        if not meter_row:
            print(f"❌ ОШИБКА: Счетчик с номером '{TARGET_SN}' не найден в таблице Meters сервера cEnergo!")
            conn.close()
            return
            
        meter_id = meter_row.MeterId
        db_sn = meter_row.SerialNumber
        print(f"🎯 Найдено совпадение! Внутренний ID прибора (MeterId): {meter_id}")
        print(f"   Полное имя в оригинальной БД: '{db_sn}'")
        print("-" * 75)

        # 2. Выгружаем показания вместе со служебными идентификаторами параметров
        print(f"📊 Выгружаем первые 10 записей показаний из таблицы [Values] для MeterId {meter_id}...")
        cursor.execute("""
            SELECT TOP 10 DT, Val, PropertyId, TariffId 
            FROM [Values] 
            WHERE MeterId = ? 
            ORDER BY DT DESC
        """, (meter_id,))
        
        rows = cursor.fetchall()
            
        if rows:
            print(f"🎉 УСПЕХ! Найдено записей: {len(rows)} шт. Выводим на экран:")
            print(f"{'Дата и время замера':<25} | {'Показания':<15} | {'PropertyId':<12} | {'TariffId'}")
            print("-" * 75)
            for r in rows:
                print(f"  {str(r.DT):<23} | {r.Val:<15} | {r.PropertyId:<12} | {r.TariffId}")
        else:
            print("❌ Ошибка: В таблице [Values] нет записей для этого прибора.")
            
        conn.close()
        
    except Exception as e:
        print(f"❌ Системная ошибка выполнения теста: {e}")

if __name__ == "__main__":
    test_fetch_readings()
