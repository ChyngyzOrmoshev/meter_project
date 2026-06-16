from datetime import datetime
from db_client import sync_status_col

def update_sync_status(robot_name: str, status: str, records_processed: int = 0, error: str = None):
    """
    Обновляет статус синхронизатора в MongoDB.
    
    :param robot_name: Имя робота (например, 'cEnergo', 'Sanxing', 'Sanrise', 'Hexing KUK')
    :param status: 'running', 'success', 'error'
    :param records_processed: количество обработанных записей
    :param error: текст ошибки, если status == 'error'
    """
    doc = {
        "robot_name": robot_name,
        "status": status,
        "last_update": datetime.now(),
        "records_processed": records_processed,
    }
    if error:
        doc["error"] = error
    else:
        # Убираем поле error, если его нет
        doc.pop("error", None)
    
    sync_status_col.update_one(
        {"robot_name": robot_name},
        {"$set": doc},
        upsert=True
    )