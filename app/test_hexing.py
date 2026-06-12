import os
import mysql.connector
from dotenv import load_dotenv

# Загружаем настройки из cEnergo.env
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else '.'
ENV_PATH = os.path.join(CURRENT_DIR, "cEnergo.env")
load_dotenv(dotenv_path=ENV_PATH)

HOST = os.getenv("HEXING_MYSQL_HOST")
PORT = os.getenv("HEXING_MYSQL_PORT")
DB = os.getenv("HEXING_MYSQL_DB")
USER = os.getenv("HEXING_MYSQL_USER")
PASSWORD = os.getenv("HEXING_MYSQL_PASSWORD")

def explore_hexing_tables():
    print("🔐 Попытка подключения к MySQL Hexing (192.168.144.64)...")
    try:
        # Устанавливаем соединение с MySQL
        conn = mysql.connector.connect(
            host=HOST,
            port=int(PORT),
            user=USER,
            password=PASSWORD,
            database=DB,
            connect_timeout=10
        )
        cursor = conn.cursor()
        print("✅ УСПЕХ! Подключились к базе данных minimdm.")

        # Ищем таблицы, названия которых содержат слова meter или device
        print("\n🔍 Ищем таблицы сопоставления оборудования...")
        
        cursor.execute("SHOW TABLES LIKE '%meter%'")
        print(f"📦 Таблицы [METER]: {cursor.fetchall()}")
        
        cursor.execute("SHOW TABLES LIKE '%device%'")
        print(f"📦 Таблицы [DEVICE]: {cursor.fetchall()}")

        cursor.close()
        conn.close()
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        print("💡 Если пишет No module named 'mysql', мы установим его за секунду.")

if __name__ == "__main__":
    explore_hexing_tables()
