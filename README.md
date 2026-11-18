# meter_project — АСКУЭ «ИнфоЭнерго»

Django-приложение для автоматизированного сбора, хранения и анализа показаний приборов учёта электроэнергии, а также удалённого управления реле.

## Стек

- Python 3.11, Django 5.x
- MySQL 8 (`mysqlclient`)
- Внешние источники: MSSQL (FreeTDS), PostgreSQL, MySQL
- APScheduler (единый планировщик)
- Playwright + Chromium (остался только для Hexing POP)
- pandas, openpyxl, python-calamine (для 1С)
- Docker Compose

## Архитектура

Три контейнера:

| Контейнер | Роль |
|---|---|
| `meters_mysql` | БД MySQL 8 |
| `meters_django` | Веб (gunicorn + whitenoise) |
| `meters_scheduler` | Планировщик APScheduler со всеми роботами в одном процессе |

## Запуск

```bash
# 1. Заполнить секреты
cp app/cEnergo.env.example app/cEnergo.env
$EDITOR app/cEnergo.env

# 2. Поднять
docker compose up -d --build

# 3. Миграции
docker compose exec django python manage.py migrate

# 4. Создать суперпользователя
docker compose exec django python manage.py createsuperuser
UI: http://192.168.144.145:8000

🤖 Роботы синхронизации
Быстрая цепочка — каждые 30 минут (строго последовательно)
Команда	Источник	Протокол	Примечание
sync_risesun_api	MMWS API	REST (HTTP)	—
sync_inhemeter	Inhemeter MySQL	MySQL	—
sync_mssql	cEnergo	MS SQL	—
sync_hexing	Hexing KUK	MySQL	—
sync_sanrise	SunRise	MS SQL	—
sync_sanxing	Sanxing_old	MS SQL	—
sync_website	Sanxing_new_100A	HTTP API	Переведен с Playwright (генерация ZIP ~50 сек)
sync_website_period1	Sanxing_new_5A	HTTP API	Переведен с Playwright (модель S34U18 CT)
Playwright-роботы (расписание)
sync_hxn — 03:30 / 15:30 (Hexing POP / Mini-MDM). API сломан, работает через браузер.

sync_star — 03:00 / 15:00. Переведен на HTTP API (вызывается через планировщик, скачивает XML за 1-2 секунды).

Обслуживание
auto_close_issues — каждый час в :10.

check_debts_auto_on — проверка должников для автовключения (раз в час).

⚡ Управление реле (Remote Control)
Доступно администраторам и инженерам АСКУЭ на странице /relay-control/.

Логика работы
Создание задачи: Инженер выбирает абонентов (по району/долгу) и ставит задачу на Включение или Отключение (с датой и временем).

Валидация: Если инженер пытается отключить тех, у кого долг 0, система не даст это сделать.

Очередь (RelayTask): Задача попадает в БД. Если создается новая задача (например, на включение), старые противоположные задачи отменяются.

Воркер (process_relay_tasks.py): Раз в минуту берет пачку задач (до 50 шт.) и выполняет их в 5 потоков.

Проверка долга перед отключением: Воркер запрашивает 1С (в будущем) или локальную БД Subscriber. Если долг оплачен — задача отменяется (cancelled).

Ретраи: При ошибке (счетчик офлайн) задача уходит в ожидание: 1-я попытка через 5 минут, 2-я через 10, 3-я через 20. Если все 3 провалились — статус error.

Поддерживаемые системы:
Hexing KUK (REST API, порт 8091): Мгновенный /switch. Чтение статуса напрямую из БД Hexing (hes_log_relay_status_detail).

cEnergo (SOAP API): Синхронный запрос с таймаутом 30 сек. Если счетчик офлайн, cEnergo сама ставит в очередь на 4 часа.

Sanxing_old (Playwright + DWR): Использует авторизацию с капчей через ddddocr.

SunRise (REST API): CIM XML (GZIP+Base64).

RiseSun (MMWS REST API): Пакетная отправка (batch).

🔗 Интеграция с 1С
На данный момент интеграция находится на стадии проектирования и подготовки БД.

Модель Subscriber: Хранит лицевой счет, ФИО, долг. Привязывается к Device.

Модель CompareResult: Используется как «Заявки». Если 1С присылает должника, а в нашей базе его нет — создается заявка not_found.

Планируемая Pull-модель:

При поиске должников инженер нажимает «Применить», запрос уходит в 1С (с указанием массива askue_types), 1С возвращает список.

Воркер реле перед отключением делает запрос в 1С (check_debts) для проверки актуальности долга.

Окружение (.env)
Все секреты хранятся в app/cEnergo.env. Пароли в коде запрещены.

Ключевые переменные:

Базы: MYSQL_*, DB_MSSQL_* (cEnergo), SANXING_*, SANRISE_MSSQL_*, HEXING_KUK_*, RISESUN_PG_*.

API:

MMWS_API_* (RiseSun)

STAR_URL, STAR_USERNAME, STAR_PASSWORD

SANXING_NEW_API_URL, SANXING_NEW_ACCOUNT, SANXING_NEW_PASSWORD_ENCRYPTED (для Sanxing_new 100A/5A)

SUNRISE_API_* (API реле SunRise)

Структура проекта
text
django_app/
├── config/
├── meters/
│   ├── management/commands/
│   │   ├── process_relay_tasks.py    # Воркер очереди реле
│   │   ├── check_debts_auto_on.py    # Автовключение
│   │   ├── sync_website.py           # Sanxing 100A (HTTP)
│   │   ├── sync_website_period1.py   # Sanxing 5A (HTTP)
│   │   ├── sync_star.py              # Star (HTTP)
│   │   ├── sync_hxn.py               # Hexing POP (Playwright)
│   │   └── ... (остальные роботы)
│   ├── services (в корне meters/)
│   │   ├── hexing_service.py         # Hexing KUK REST
│   │   ├── cenergo_service.py        # cEnergo SOAP
│   │   ├── sunrise_service.py        # SunRise REST
│   │   └── sanxing_service.py        # Sanxing DWR/Playwright
│   ├── models.py                     # Device, Reading, Subscriber, RelayTask, etc.
│   ├── views.py                      # Веб-интерфейс, API
│   ├── scheduler.py                  # Планировщик
│   └── balance_calculator.py         # Логика баланса
└── ...
Известные проблемы и планы (TODO)
Sanxing_new: Пароль (SANXING_NEW_PASSWORD_ENCRYPTED) зашит в env, но имеет статичный вид. Если 1С/разработчики Sanxing поменяют пароль в UI, его нужно будет перешифровать.

Hexing POP (sync_hxn): Остался на Playwright. API (/rawDataReport/downloadFile) возвращает 0 байт или пустой файл. Надо будет разобраться с DWRSESSIONID или перехватить запрос через DevTools.

1С: Ждем от разработчиков 1С точных эндпоинтов для Pull-модели (запрос должников и проверка долга).

UI/UX: В add_device.html при выборе модели ПУ (выпадающий список) редактор кода (Prettier) ломает теги {{ m.phases }}. Рекомендуется вынести красивое название в MeterModel.display_name.

Группы инженеров: Для ограничения видимости по районам созданы группы ASKUE:Название_района. Инженер видит только свой район в фильтрах.

Дашборд: Список auto_notes в views.py (dashboard) должен строго совпадать с notes, которые пишут роботы, иначе ручные показания будут считаться неверно.