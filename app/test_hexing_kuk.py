
import mysql.connector, os
from dotenv import load_dotenv
from datetime import datetime, timedelta
load_dotenv('/app/cEnergo.env')
conn = mysql.connector.connect(
    host=os.getenv('HEXING_KUK_MYSQL_HOST'),
    port=int(os.getenv('HEXING_KUK_MYSQL_PORT')),
    user=os.getenv('HEXING_KUK_MYSQL_USER'),
    password=os.getenv('HEXING_KUK_MYSQL_PASSWORD'),
    database=os.getenv('HEXING_KUK_MYSQL_DB')
)
cursor = conn.cursor()
three_days_ago = datetime.now() - timedelta(days=3)
query = '''
    SELECT 
        m.METER_NO AS serial_number,
        t.TV AS timestamp,
        t.CA AS reading_value
    FROM biz_pub_data_t_energy_d t
    JOIN a_data_catalogue c ON t.DATA_ID = c.DATA_ID
    JOIN a_equip_meter m ON c.METER_ID = m.METER_ID
    WHERE t.TV >= %s
      AND t.CA IS NOT NULL
    ORDER BY t.TV DESC
    LIMIT 10
'''
cursor.execute(query, (three_days_ago,))
rows = cursor.fetchall()
print('Последние 10 показаний комбинированной энергии (CA) за последние 3 дня:')
if rows:
    for row in rows:
        print(row)
else:
    print('Нет данных за последние 3 дня. Проверяем за всё время...')
    cursor.execute('''
        SELECT 
            m.METER_NO AS serial_number,
            t.TV AS timestamp,
            t.CA AS reading_value
        FROM biz_pub_data_t_energy_d t
        JOIN a_data_catalogue c ON t.DATA_ID = c.DATA_ID
        JOIN a_equip_meter m ON c.METER_ID = m.METER_ID
        WHERE t.CA IS NOT NULL
        ORDER BY t.TV DESC
        LIMIT 10
    ''')
    rows = cursor.fetchall()
    if rows:
        print('Последние 10 показаний комбинированной энергии (CA) за всё время:')
        for row in rows:
            print(row)
    else:
        print('Нет данных с CA вообще!')
