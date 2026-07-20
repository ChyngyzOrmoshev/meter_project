from meters.models import Device, Reading
import pyodbc
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv('/app/cEnergo.env')
server = os.getenv("SANXING_SERVER")
user = os.getenv("SANXING_USER")
password = os.getenv("SANXING_PASSWORD")
db = os.getenv("SANXING_DB")
conn_str = f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};UID={user};PWD={password};Port=1433;TDS_Version=7.4;"

conn = pyodbc.connect(conn_str)
cursor = conn.cursor()

serials = ['00590192', '00590193', '00590194', '00590195']
devices_map = {d.serial_number: d for d in Device.objects.filter(serial_number__in=serials)}

for sn in serials:
    device = devices_map.get(sn)
    if not device:
        print(f"❌ Device {sn} not found in MySQL")
        continue
    print(f"✅ Device {sn} found, loading data...")
    query = """
        SET NOCOUNT ON;
        SELECT D.DATA_TIME, D.KWH_ABS
        FROM DATA_C_ELEC D
        INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
        WHERE RTRIM(LTRIM(M.MSNO)) = ? AND D.TARIFF_ID = 0 AND D.KWH_ABS IS NOT NULL
        ORDER BY D.DATA_TIME ASC
    """
    cursor.execute(query, (sn,))
    count = 0
    for row in cursor:
        dt = row.DATA_TIME
        if isinstance(dt, str):
            dt = datetime.strptime(dt.split('.')[0], "%Y-%m-%d %H:%M:%S")
        val = float(row.KWH_ABS) / 1000.0  # или /100, зависит от номинала
        Reading.objects.update_or_create(
            device=device,
            timestamp=dt,
            defaults={'reading_value': val, 'notes': 'Авто-сбор: Sanxing_old'}
        )
        count += 1
    print(f"   Imported {count} readings for {sn}")