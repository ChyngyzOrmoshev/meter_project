import logging
import pyodbc
import mysql.connector
import psycopg2
import os
from dotenv import load_dotenv
from django.db import transaction
from meters.models import Device, MeterModel

logger = logging.getLogger(__name__)

def transform_sunrise_serial(serial):
    """
    Преобразует серийный номер для SunRise:
    - начинается с 61 → добавляем 0861
    - начинается с 63 → добавляем 0862
    - начинается с 65 → добавляем 0863
    Остальные номера не изменяются.
    """
    if not serial:
        return serial
    serial = serial.strip()
    if serial.startswith('61'):
        return '0861' + serial
    elif serial.startswith('63'):
        return '0862' + serial
    elif serial.startswith('65'):
        return '0863' + serial
    else:
        return serial

# Утилита для безопасного декодирования
def safe_decode(value):
    if value is None:
        return ''
    if isinstance(value, str):
        # Если это строка, возможно она в кодировке LATIN1, перекодируем в UTF-8
        try:
            return value.encode('latin1').decode('utf-8', errors='replace')
        except:
            return value
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)

ROBOT_CONFIGS = {
    'SunRise': {
        'db_type': 'mssql',
        'env': {
            'server': 'SANRISE_MSSQL_SERVER',
            'user': 'SANRISE_MSSQL_USER',
            'password': 'SANRISE_MSSQL_PASSWORD',
            'db': 'SANRISE_MSSQL_DB',
            'port': '1433',
        },
        'query': """
            SELECT m.MSNO as serial, m.METERTYPEID as model_id
            FROM ACHV_METER m
            WHERE m.METERTYPEID IS NOT NULL
        """,
        'model_mapping': {
            5: 517,  # ST12-HW06
            7: 517,
            6: 518,  # ST34-HW08
            8: 518,
            9: 519,  # ST3C-HW08
            2: 520,  # ST12
            4: 520,
            1: 521,  # ST34
            3: 521,
        },
    },
    'Sanxing_old': {
        'db_type': 'mssql',
        'env': {
            'server': 'SANXING_SERVER',
            'user': 'SANXING_USER',
            'password': 'SANXING_PASSWORD',
            'db': 'SANXING_DB',
            'port': '1433',
        },
        'query': """
            SELECT m.MSNO as serial, m.METERTYPE_ID as model_id
            FROM ACHV_METER m
            WHERE m.METERTYPE_ID IS NOT NULL
        """,
        'model_mapping': {
            13: 504,  # P12S01
            14: 505,  # P34S02
            16: 505,
            15: 507,  # P34S02 CT
        },
    },
    'Hexing_KUK': {
        'db_type': 'mysql',
        'env': {
            'host': 'HEXING_KUK_MYSQL_HOST',
            'port': 'HEXING_KUK_MYSQL_PORT',
            'user': 'HEXING_KUK_MYSQL_USER',
            'password': 'HEXING_KUK_MYSQL_PASSWORD',
            'db': 'HEXING_KUK_MYSQL_DB',
        },
        'query': """
            SELECT ASSETNO as serial, METER_MODEL as model_name
            FROM a_equip_meter
            WHERE METER_MODEL IS NOT NULL
        """,
        'model_mapping': {
            'HXE110': 523,
            'HXE310': 524,
            'HXE300': 525,
        },
    },
    'CENC': {
        'db_type': 'postgresql',
        'env': {
            'host': 'CENC_PG_HOST',
            'port': 'CENC_PG_PORT',
            'user': 'CENC_PG_USER',
            'password': 'CENC_PG_PASSWORD',
            'db': 'CENC_PG_DB',
        },
        'query': """
            SELECT d."SerialNumber" as serial, d."DeviceTypeID" as type_id
            FROM "Device" d
            WHERE d."DeviceTypeID" IS NOT NULL
              AND d."SerialNumber" IS NOT NULL
              AND d."SerialNumber" != ''
        """,
        'model_mapping': {
            14: 533,   # CE207-R7
            5: 537,    # CE303-S31.746
            11: 542,   # CE307-R34.749(746)
            # CE308 и другие пока пропускаем
        },
    },
}

def get_db_connection(robot_name):
    config = ROBOT_CONFIGS.get(robot_name)
    if not config:
        raise ValueError(f"No config for robot {robot_name}")

    load_dotenv('/app/cEnergo.env')
    env = config['env']

    if config['db_type'] == 'mssql':
        server = os.getenv(env['server'])
        user = os.getenv(env['user'])
        password = os.getenv(env['password'])
        db = os.getenv(env['db'])
        port = env.get('port', '1433')
        conn_str = f"DRIVER={{FreeTDS}};SERVER={server};DATABASE={db};UID={user};PWD={password};Port={port};TDS_Version=7.4;"
        return pyodbc.connect(conn_str)
    elif config['db_type'] == 'mysql':
        host = os.getenv(env['host'])
        port = int(os.getenv(env.get('port', '3306'), 3306))
        user = os.getenv(env['user'])
        password = os.getenv(env['password'])
        db = os.getenv(env['db'])
        return mysql.connector.connect(host=host, port=port, user=user, password=password, database=db)
    elif config['db_type'] == 'postgresql':
        host = os.getenv(env['host'])
        port = os.getenv(env.get('port', '5432'))
        user = os.getenv(env['user'])
        password = os.getenv(env['password'])
        db = os.getenv(env['db'])
        # Формируем строку подключения с явным указанием кодировки LATIN1
        conn_str = f"dbname={db} user={user} password={password} host={host} port={port} client_encoding=LATIN1"
        conn = psycopg2.connect(conn_str)
        return conn
    else:
        raise ValueError(f"Unsupported db_type: {config['db_type']}")

def get_new_devices_from_source(robot_name, limit=None):
    config = ROBOT_CONFIGS.get(robot_name)
    if not config:
        logger.error(f"No registration config for robot {robot_name}")
        return []

    conn = None
    cursor = None
    try:
        conn = get_db_connection(robot_name)
        cursor = conn.cursor()
        query = config['query']
        if limit:
            # if config['db_type'] == 'mssql':
            #     # Для MSSQL используем TOP
            #     if 'SELECT' in query.upper():
            #         query = query.replace('SELECT', f'SELECT TOP {limit} ', 1)
            if config['db_type'] == 'mysql':
                query += f" LIMIT {limit}"
            elif config['db_type'] == 'postgresql':
                query += f" LIMIT {limit}"
        cursor.execute(query)
        if config['db_type'] == 'postgresql':
            cursor.execute("SET client_encoding TO 'LATIN1';")
        rows = cursor.fetchall()

        mapping = config['model_mapping']
        result = []
        for row in rows:
            # Безопасно извлекаем серийный номер (может быть str, bytes, None)
            serial_raw = row[0]
            logger.debug(f"Row: {row}")
            serial = safe_decode(serial_raw).strip()
            if not serial:
                continue

            if robot_name == 'SunRise':
                serial = transform_sunrise_serial(serial)

            # Определяем catalog_code по второму полю
            if 'type_id' in config['query'] or 'model_id' in config['query']:
                raw_id = row[1]
                if raw_id is None:
                    continue
                # Если raw_id – строка (даже в Latin1), преобразуем в int
                if isinstance(raw_id, str):
                    try:
                        model_id = int(raw_id.encode('latin1').decode('utf-8', errors='ignore'))
                    except:
                        continue
                elif isinstance(raw_id, bytes):
                    try:
                        model_id = int(raw_id.decode('utf-8', errors='ignore'))
                    except:
                        continue
                else:
                    model_id = int(raw_id) if raw_id is not None else None
                if model_id is None:
                    continue
                catalog_code = mapping.get(model_id)
            else:
                raw_name = row[1]
                model_name = safe_decode(raw_name)
                catalog_code = mapping.get(model_name)

            if catalog_code is None:
                logger.debug(f"Unknown model for serial {serial}, skipping")
                continue
            result.append((serial, catalog_code))
        return result
    except Exception as e:
        logger.error(f"Error fetching devices from {robot_name}: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def register_devices_for_robot(robot_name, limit=None):
    new_devices = get_new_devices_from_source(robot_name, limit)
    if not new_devices:
        logger.info(f"No new devices found for {robot_name}")
        return 0

    registered = 0
    with transaction.atomic():
        for serial, catalog_code in new_devices:
            if Device.objects.filter(serial_number=serial).exists():
                logger.debug(f"Device {serial} already exists, skipping")
                continue
            try:
                model = MeterModel.objects.get(catalog_code=catalog_code)
            except MeterModel.DoesNotExist:
                logger.warning(f"Model with catalog_code {catalog_code} not found, skipping serial {serial}")
                continue
            jpes_region, _ = Region.objects.get_or_create(name='ЖПЭС')
            Device.objects.create(
                serial_number=serial,
                model=model,
                status='active',
                region=jpes_region,
                nominal_current=model.nominal_current,
                phase=model.phases,
                askue_id=model.device_type_id,
                api_id=model.device_type_str,
            )
            registered += 1
            logger.info(f"Registered new device {serial} with model {model.model_name}")

    logger.info(f"Registered {registered} devices for {robot_name}")
    return registered