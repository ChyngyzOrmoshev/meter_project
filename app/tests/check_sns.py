import os
import pyodbc
from dotenv import load_dotenv

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(CURRENT_DIR, 'cEnergo.env'))

conn_str = f"DRIVER={{SQL Server}};SERVER={os.getenv('DB_MSSQL_SERVER')};DATABASE={os.getenv('DB_MSSQL_NAME')};UID={os.getenv('DB_MSSQL_USER')};PWD={os.getenv('DB_MSSQL_PASSWORD')};"

try:
    conn = pyodbc.connect(conn_str)
    cursor = conn.cursor()
    
    print("🔍 Выгружаем первые 10 реальных заводских номеров из базы cEnergo:")
    print("-" * 50)
    
    # Берем первые 10 приборов из оригинальной таблицы Энергомеры
    cursor.execute("SELECT TOP 10 MeterId, SerialNumber FROM Meters")
    rows = cursor.fetchall()
    
    for row in rows:
        # Показываем номер и его длину, чтобы увидеть скрытые пробелы
        sn = row.SerialNumber
        print(f"  • ID: {row.MeterId:<5} | Номер в базе: '{sn}' | Длина: {len(str(sn))} симв.")
        
    print("-" * 50)
    
    # Также проверим, есть ли вообще свежие показания в таблице Values
    cursor.execute("SELECT COUNT(*) FROM [Values]")
    total_values = cursor.fetchone()[0]
    print(f"📊 Всего записей с показаниями в таблице [Values]: {total_values} шт.")
    
    conn.close()
except Exception as e:
    print(f"❌ Ошибка: {e}")
