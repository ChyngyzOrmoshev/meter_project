import logging
import os
from logging.handlers import RotatingFileHandler

# Создаём папку для логов, если её нет
LOG_DIR = "/app/logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Настройка форматирования
formatter = logging.Formatter(
    fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Консольный обработчик (для вывода в stdout, чтобы видеть в docker logs)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.INFO)

# Файловый обработчик с ротацией (максимум 10 файлов по 5 МБ)
file_handler = RotatingFileHandler(
    filename=os.path.join(LOG_DIR, "infoenergo.log"),
    maxBytes=5 * 1024 * 1024,  # 5 МБ
    backupCount=10,
    encoding="utf-8"
)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.DEBUG)  # в файл пишем всё, включая DEBUG

# Корневой логгер
logger = logging.getLogger("infoenergo")
logger.setLevel(logging.DEBUG)       # минимальный уровень для захвата
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Чтобы избежать дублирования, если модуль импортируется несколько раз
logger.propagate = False

# Тестовое сообщение для проверки
logger.info("✅ Система логирования инициализирована. Логи сохраняются в /app/logs/infoenergo.log")