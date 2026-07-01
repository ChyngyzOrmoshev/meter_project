from django.core.management.base import BaseCommand
from django.db import transaction
from pymongo import MongoClient
from meters.models import MeterModel, Device, Reading, SyncStatus
from datetime import datetime

class Command(BaseCommand):
    help = 'Migrate data from MongoDB to MySQL'

    def handle(self, *args, **kwargs):
        mongo_client = MongoClient("mongodb://mongodb:27017/")
        mongo_db = mongo_client["power_monitoring"]

        self.stdout.write("🗂️ Начинаем миграцию...")

        # 1. Модели
        self.stdout.write("📋 Миграция моделей...")
        models_col = mongo_db["meter_models"]
        for doc in models_col.find():
            MeterModel.objects.update_or_create(
                model_name=doc.get("model_name"),
                defaults={
                    "catalog_code": doc.get("catalog_code", ""),
                    "digit_capacity": doc.get("digit_capacity", ""),
                    "phases": doc.get("phases", 1),
                    "nominal_current": doc.get("nominal_current", ""),
                    "nominal_voltage": doc.get("nominal_voltage", ""),
                    "system_type": doc.get("system_type", ""),
                    "period": doc.get("period", ""),
                    "device_type_id": doc.get("device_type_id", ""),
                    "device_type_str": doc.get("device_type_str", ""),
                }
            )
        self.stdout.write(f"✅ Модели: {MeterModel.objects.count()}")

        # 2. Устройства
        self.stdout.write("🏭 Миграция устройств...")
        devices_col = mongo_db["devices"]
        for doc in devices_col.find():
            model_name = doc.get("model_name")
            model = None
            if model_name:
                model = MeterModel.objects.filter(model_name=model_name).first()
            Device.objects.update_or_create(
                serial_number=doc["serial_number"],
                defaults={
                    "model": model,
                    "nominal_current": doc.get("nominal_current", ""),
                    "status": doc.get("status", "active"),
                    "phase": doc.get("phase"),
                    "askue_id": doc.get("askue_id", ""),
                    "api_id": doc.get("api_id", ""),
                    "created_at": doc.get("created_at", datetime.now()),
                }
            )
        self.stdout.write(f"✅ Устройства: {Device.objects.count()}")

        # 3. Показания (пакетно)
        self.stdout.write("📊 Миграция показаний...")
        readings_col = mongo_db["readings"]
        total = readings_col.count_documents({})
        batch_size = 5000
        processed = 0
        batch = []

        for doc in readings_col.find().sort("timestamp", 1):
            serial = doc.get("serial_number")
            device = Device.objects.filter(serial_number=serial).first()
            if not device:
                continue
            batch.append(Reading(
                device=device,
                timestamp=doc["timestamp"],
                reading_value=doc["reading_value"],
                notes=doc.get("notes", ""),
            ))
            if len(batch) >= batch_size:
                Reading.objects.bulk_create(batch, ignore_conflicts=True)
                processed += len(batch)
                self.stdout.write(f"   Загружено {processed} из {total}")
                batch = []

        if batch:
            Reading.objects.bulk_create(batch, ignore_conflicts=True)
            processed += len(batch)
            self.stdout.write(f"   Загружено {processed} из {total}")

        self.stdout.write(f"✅ Показания: {Reading.objects.count()}")

        # 4. Статусы
        self.stdout.write("🤖 Миграция статусов...")
        status_col = mongo_db["sync_status"]
        for doc in status_col.find():
            SyncStatus.objects.update_or_create(
                robot_name=doc["robot_name"],
                defaults={
                    "status": doc.get("status", "unknown"),
                    "last_update": doc["last_update"],
                    "records_processed": doc.get("records_processed", 0),
                    "error": doc.get("error"),
                }
            )
        self.stdout.write(f"✅ Статусы: {SyncStatus.objects.count()}")

        self.stdout.write("🎉 Миграция завершена успешно!")
