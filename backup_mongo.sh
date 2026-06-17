#!/bin/bash

# Путь к папке с бэкапами (внутри контейнера)
BACKUP_DIR="/backups"
# Имя файла с датой
BACKUP_NAME="backup_$(date +%Y%m%d_%H%M%S).gz"
# Путь внутри контейнера
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"

# Выполняем бэкап внутри контейнера meters_db
docker exec meters_db mongodump --archive="$BACKUP_PATH" --gzip --db=power_monitoring

# Проверяем успешность
if [ $? -eq 0 ]; then
    echo "✅ Бэкап создан: $BACKUP_NAME"
    # Удаляем бэкапы старше 7 дней
    docker exec meters_db find "$BACKUP_DIR" -name "*.gz" -mtime +7 -delete
else
    echo "❌ Ошибка создания бэкапа"
    exit 1
fi