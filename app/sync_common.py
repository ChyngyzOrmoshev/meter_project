from datetime import datetime, timezone
from db_client import sync_status_col
from logger_config import logger

def update_sync_status(robot_name: str, status: str, records_processed: int = 0, error: str = None):
    """
    Обновляет статус синхронизатора в MongoDB и логирует действие.
    При успешном статусе поле error удаляется из документа.
    """
    doc = {
        "robot_name": robot_name,
        "status": status,
        "last_update": datetime.now(timezone.utc),
        "records_processed": records_processed,
    }
    update_operation = {"$set": doc}
    if error:
        doc["error"] = error
        update_operation["$set"] = doc
        logger.error(f"{robot_name}: {status} - {error}")
    else:
        # Удаляем поле error, если оно было
        update_operation["$unset"] = {"error": ""}
        if status == "success":
            logger.info(f"{robot_name}: успешно, обработано {records_processed} записей")
        elif status == "running":
            logger.info(f"{robot_name}: запущен")
        elif status == "idle":
            logger.info(f"{robot_name}: бездействует (реестр пуст)")

    try:
        sync_status_col.update_one(
            {"robot_name": robot_name},
            update_operation,
            upsert=True
        )
    except Exception as e:
        logger.error(f"Ошибка обновления статуса {robot_name}: {e}")