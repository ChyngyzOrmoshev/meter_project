FROM python:3.11-slim

# Устанавливаем часовой пояс (Бишкек, UTC+6)
ENV TZ=Asia/Bishkek
RUN apt-get update && apt-get install -y \
    build-essential \
    g++ \
    unixodbc-dev \
    gcc \
    freetds-bin \
    freetds-dev \
    tdsodbc \
    curl \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8501

CMD ["streamlit", "run", "web_site.py", "--server.port=8501", "--server.address=0.0.0.0"]