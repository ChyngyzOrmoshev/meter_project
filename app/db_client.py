from pymongo import MongoClient, ASCENDING, DESCENDING

client = MongoClient("mongodb://mongodb:27017/")
db = client["power_monitoring"]

models_col = db["meter_models"]
devices_col = db["devices"]
readings_col = db["readings"]

# Индексы для устройств
devices_col.create_index([("serial_number", ASCENDING)], unique=True)

# Индексы для показаний
# Основной уникальный индекс (серийный номер + время) для предотвращения дублей
readings_col.create_index(
    [("serial_number", ASCENDING), ("timestamp", ASCENDING)],
    unique=True
)

# Индекс для быстрой сортировки и фильтрации по времени
readings_col.create_index([("timestamp", DESCENDING)])

# Индекс для фильтрации по notes и времени (для дашборда и аналитики)
readings_col.create_index([("notes", ASCENDING), ("timestamp", DESCENDING)])

# Индекс для поиска по серийному номеру (если ищем конкретный)
readings_col.create_index([("serial_number", ASCENDING)])

# Индекс для агрегаций по дате (если часто группируем по дням)
# Можно добавить частичный индекс, но пока оставим полноценный

# Коллекция статусов синхронизаторов
sync_status_col = db["sync_status"]
sync_status_col.create_index("robot_name", unique=True)
sync_status_col.create_index("last_update", expireAfterSeconds=604800)  # храним записи неделю