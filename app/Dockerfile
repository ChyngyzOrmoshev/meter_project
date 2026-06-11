FROM python:3.11-slim

WORKDIR /app

# Добавляем инструменты для сборки pyodbc и pymssql под Linux
# Добавляем инструменты для сборки и сам драйвер FreeTDS с менеджером unixodbc
RUN apt-get update && apt-get install -y \
    build-essential \
    g++ \
    unixodbc-dev \
    gcc \
    freetds-bin \
    freetds-dev \
    tdsodbc \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код проекта
COPY . .

# Открываем порт для Streamlit
EXPOSE 8501

# Указываем команду для старта сайта
CMD ["streamlit", "run", "web_site.py", "--server.port=8501", "--server.address=0.0.0.0"]

