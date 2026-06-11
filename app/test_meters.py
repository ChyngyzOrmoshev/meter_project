from pymongo import MongoClient
from datetime import datetime

db = MongoClient("mongodb://localhost:27017/")["power_monitoring"]

print("🔄 Корректировка времени прошлых ручных записей в 00:00:00...")

# Находим все записи, у которых НЕТ примечания про авто-сбор (значит они введены вручную)
manual_readings = db.readings.find({"notes": {"$not": {"$regex": "Авто-сбор"}}})

updated_count = 0
for doc in manual_readings:
    current_ts = doc["timestamp"]
    if current_ts:
        # Сбрасываем время до начала суток
        new_ts = datetime.combine(current_ts.date(), datetime.min.time())

        # Обновляем документ в базе
        db.readings.update_one({"_id": doc["_id"]}, {"$set": {"timestamp": new_ts}})
        updated_count += 1

print(f"✅ Успешно скорректировано документов: {updated_count} шт.")
