import pyodbc

MSSQL_SERVER = "192.168.144.30"
MSSQL_USER = "sa"
MSSQL_PASSWORD = "ASYjrek53367"  # Впишите пароль sa
MSSQL_DB = "emera_new"

def scan_target_tables():
    conn_str = f"DRIVER={{SQL Server}};SERVER={MSSQL_SERVER};DATABASE={MSSQL_DB};UID={MSSQL_USER};PWD={MSSQL_PASSWORD};"
    
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        print("✅ Подключено! Сканируем целевые таблицы для сбора данных...\n")

        # Список трех главных таблиц, из которых мы будем строить общую базу
        target_tables = ["Meters", "Values", "ValueInstants"]

        for t_name in target_tables:
            print(f"📊 Поля таблицы [{t_name}]:")
            cursor.execute(f"""
                SELECT COLUMN_NAME, DATA_TYPE 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_NAME = '{t_name}'
            """)
            for col in cursor.fetchall():
                print(f"    - {col[0]} ({col[1]})")
            print("-" * 50)

        conn.close()
    except Exception as e:
        print(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    scan_target_tables()
