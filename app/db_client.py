from pymongo import MongoClient, ASCENDING, DESCENDING

client = MongoClient("mongodb://mongodb:27017/")
db = client["power_monitoring"]

models_col = db["meter_models"]
devices_col = db["devices"]
readings_col = db["readings"]

# Уникальный индекс на серийный номер (устройства)
devices_col.create_index([("serial_number", ASCENDING)], unique=True)

# Уникальный составной индекс (серийник + временная метка) – гарантирует отсутствие дубликатов показаний
readings_col.create_index(
    [("serial_number", ASCENDING), ("timestamp", ASCENDING)], unique=True
)

# Дополнительный индекс для быстрой сортировки по времени (используется в агрегациях)
readings_col.create_index([("timestamp", DESCENDING)])
