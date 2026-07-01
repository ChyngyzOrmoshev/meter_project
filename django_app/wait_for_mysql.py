import time
import os
import sys
import MySQLdb

host = os.getenv('MYSQL_HOST', 'mysql')
port = int(os.getenv('MYSQL_PORT', 3306))
user = os.getenv('MYSQL_USER', 'infoenergo')
password = os.getenv('MYSQL_PASSWORD', 'infopass')
db = os.getenv('MYSQL_DATABASE', 'infoenergo')

for i in range(30):
    try:
        conn = MySQLdb.connect(host=host, port=port, user=user, password=password, db=db)
        conn.close()
        print("✅ MySQL is ready")
        sys.exit(0)
    except Exception as e:
        print(f"⏳ Waiting for MySQL... ({i+1}/30) - {e}")
        time.sleep(2)
print("❌ MySQL not ready after 60 seconds")
sys.exit(1)
