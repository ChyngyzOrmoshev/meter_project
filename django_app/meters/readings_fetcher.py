import logging
import pyodbc
import mysql.connector
import psycopg2
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from django.conf import settings

logger = logging.getLogger(__name__)

# Загружаем переменные окружения (чтобы получить доступ к настройкам источников)
load_dotenv('/app/cEnergo.env')


def get_reading_fetcher(robot_name):
    """
    Возвращает функцию-загрузчик для указанного робота.
    Функция должна принимать (serial_number, start_date, end_date) и возвращать список словарей {timestamp, value}.
    """
    fetchers = {
        'cEnergo': fetch_mssql,
        'Sanxing_old': fetch_mssql_sanxing,
        'SunRise': fetch_mssql_sunrise,
        'Hexing_KUK': fetch_mysql_hexing,
        'RiseSun': fetch_postgresql_risesun,
        # 'Star' и другие можно добавить позже
    }
    return fetchers.get(robot_name)


def fetch_mssql(serial_number, start_date, end_date):
    """Загрузка данных из cEnergo MSSQL."""
    server = os.getenv("DB_MSSQL_SERVER")
    user = os.getenv("DB_MSSQL_USER")
    password = os.getenv("DB_MSSQL_PASSWORD")
    db = os.getenv("DB_MSSQL_NAME")
    conn_str = f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};UID={user};PWD={password};Port=1433;TDS_Version=7.4;"
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        query = """
            SET NOCOUNT ON;
            SELECT RTRIM(LTRIM(M.SerialNumber)) as SerialNumber, V.DT, V.Val
            FROM [Values] V
            INNER JOIN Meters M ON V.MeterId = M.MeterId
            WHERE M.SerialNumber = ? AND V.DT >= ? AND V.DT <= ? AND V.PropertyId = 12
            ORDER BY V.DT ASC
        """
        cursor.execute(query, (serial_number, start_date, end_date))
        rows = cursor.fetchall()
        results = []
        for row in rows:
            results.append({
                'timestamp': row.DT,
                'value': row.Val
            })
        return results
    except Exception as e:
        logger.error(f"MSSQL fetch error for {serial_number}: {e}")
        return None


def fetch_mssql_sanxing(serial_number, start_date, end_date):
    """Загрузка данных из Sanxing MSSQL."""
    server = os.getenv("SANXING_SERVER")
    user = os.getenv("SANXING_USER")
    password = os.getenv("SANXING_PASSWORD")
    db = os.getenv("SANXING_DB")
    conn_str = f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};UID={user};PWD={password};Port=1433;TDS_Version=7.4;"
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        query = """
            SET NOCOUNT ON;
            SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_ABS
            FROM DATA_C_ELEC D
            INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
            WHERE M.MSNO = ? AND D.DATA_TIME >= ? AND D.DATA_TIME <= ? AND D.TARIFF_ID = 0
            ORDER BY D.DATA_TIME ASC
        """
        cursor.execute(query, (serial_number, start_date, end_date))
        rows = cursor.fetchall()
        results = []
        for row in rows:
            # Может понадобиться деление на 1000/100 – как в sync_sanxing.py
            results.append({
                'timestamp': row.DATA_TIME,
                'value': row.KWH_ABS
            })
        return results
    except Exception as e:
        logger.error(f"Sanxing fetch error for {serial_number}: {e}")
        return None


def fetch_mssql_sunrise(serial_number, start_date, end_date):
    """Загрузка данных из SunRise MSSQL."""
    server = os.getenv("SANRISE_MSSQL_SERVER")
    user = os.getenv("SANRISE_MSSQL_USER")
    password = os.getenv("SANRISE_MSSQL_PASSWORD")
    db = os.getenv("SANRISE_MSSQL_DB")
    conn_str = f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};UID={user};PWD={password};Port=1433;TDS_Version=7.4;"
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        query = """
            SET NOCOUNT ON;
            SELECT RTRIM(LTRIM(M.MSNO)) as SerialNumber, D.DATA_TIME, D.KWH_IMPORT_ABS
            FROM DATA_C_DAILY D
            INNER JOIN ACHV_METER M ON D.METER_ID = M.ID
            WHERE M.MSNO = ? AND D.DATA_TIME >= ? AND D.DATA_TIME <= ?
            ORDER BY D.DATA_TIME ASC
        """
        cursor.execute(query, (serial_number, start_date, end_date))
        rows = cursor.fetchall()
        results = []
        for row in rows:
            results.append({
                'timestamp': row.DATA_TIME,
                'value': row.KWH_IMPORT_ABS
            })
        return results
    except Exception as e:
        logger.error(f"SunRise fetch error for {serial_number}: {e}")
        return None


def fetch_mysql_hexing(serial_number, start_date, end_date):
    """Загрузка данных из Hexing KUK MySQL."""
    host = os.getenv("HEXING_KUK_MYSQL_HOST")
    port = int(os.getenv("HEXING_KUK_MYSQL_PORT", 3306))
    user = os.getenv("HEXING_KUK_MYSQL_USER")
    password = os.getenv("HEXING_KUK_MYSQL_PASSWORD")
    db = os.getenv("HEXING_KUK_MYSQL_DB")
    try:
        conn = mysql.connector.connect(
            host=host, port=port, user=user, password=password, database=db
        )
        cursor = conn.cursor()
        query = """
            SELECT 
                m.METER_NO AS serial_number,
                t.TV AS timestamp,
                t.CA AS reading_value
            FROM biz_pub_data_t_energy_d t
            JOIN a_data_catalogue c ON t.DATA_ID = c.DATA_ID
            JOIN a_equip_meter m ON c.METER_ID = m.METER_ID
            WHERE m.METER_NO = %s AND t.TV >= %s AND t.TV <= %s AND t.CA IS NOT NULL
            ORDER BY t.TV ASC
        """
        cursor.execute(query, (serial_number, start_date, end_date))
        rows = cursor.fetchall()
        results = []
        for row in rows:
            results.append({
                'timestamp': row[1],
                'value': row[2]
            })
        return results
    except Exception as e:
        logger.error(f"Hexing MySQL fetch error for {serial_number}: {e}")
        return None


def fetch_postgresql_risesun(serial_number, start_date, end_date):
    """Загрузка данных из RiseSun PostgreSQL."""
    host = os.getenv("RISESUN_PG_HOST")
    port = os.getenv("RISESUN_PG_PORT", "5432")
    db = os.getenv("RISESUN_PG_DB")
    user = os.getenv("RISESUN_PG_USER")
    password = os.getenv("RISESUN_PG_PASSWORD")
    conn_str = f"dbname={db} user={user} password={password} host={host} port={port}"
    try:
        conn = psycopg2.connect(conn_str)
        cursor = conn.cursor()
        query = """
            SELECT 
                m.device_id AS serial_number,
                r.date AS timestamp,
                r.value AS reading_value
            FROM ami.readings r
            JOIN ami.meters m ON r.meter = m.id
            WHERE m.device_id = %s AND r.date >= %s AND r.date <= %s AND r.type = 10
            ORDER BY r.date ASC
        """
        cursor.execute(query, (serial_number, start_date, end_date))
        rows = cursor.fetchall()
        results = []
        for row in rows:
            results.append({
                'timestamp': row[1],
                'value': row[2]
            })
        return results
    except Exception as e:
        logger.error(f"RiseSun PostgreSQL fetch error for {serial_number}: {e}")
        return None