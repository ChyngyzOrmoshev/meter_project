# meter_project — АСКУЭ «ИнфоЭнерго»

Django-приложение для автоматизированного сбора, хранения и анализа показаний приборов учёта электроэнергии, а также удалённого управления реле.

## Важно при работе с проектом

- **База боевая.** Реальные команды **отключения** на счётчики при отладке не отправлять. Безопасно: чтение, команды `on` на заведомо включённый счётчик, запросы состояния реле. Тестовый абонент: счётчик `11848001` (Sanxing_old, «Иванов Иван»). Тестовые Hexing-счётчики: `61002510` (на связи, реле включено), `61007588` (не на связи).
- Секреты только в `app/cEnergo.env` (в `.gitignore`). Пароли в коде и в `docker-compose.yml` запрещены.
- Код смонтирован в контейнеры томом `./django_app:/app`: для применения правок Python достаточно `docker compose restart django scheduler`. Пересборка (`--build`) нужна только при смене `requirements.txt` или `Dockerfile.django`.
- Каталог `django_app/meters/migrations/` и `__pycache__` создаются от root (Docker). Миграции создавать в контейнере: `docker exec meters_django python manage.py makemigrations`.

## Стек

- Python 3.11, Django 5.2 (версии пакетов закреплены в `django_app/requirements.txt`)
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
| `meters_mysql` | БД MySQL 8. Лимит памяти 7G (InnoDB buffer pool 5G). Порт 3306 открыт только на `127.0.0.1` |
| `meters_django` | Веб (gunicorn + whitenoise), порт 8000 |
| `meters_scheduler` | Планировщик APScheduler со всеми роботами в одном процессе |

`docker.sock` в контейнеры не монтируется. Пароли MySQL (`MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`) берутся из `app/cEnergo.env` через `env_file`.

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
```

UI: http://192.168.144.145:8000

## Роботы синхронизации

### Быстрая цепочка — каждые 30 минут (строго последовательно)

| Команда | Источник | Протокол | Примечание |
|---|---|---|---|
| `sync_risesun_api` | MMWS API | REST (HTTP) | — |
| `sync_inhemeter` | Inhemeter MySQL | MySQL | — |
| `sync_mssql` | cEnergo | MS SQL | — |
| `sync_hexing` | Hexing KUK | MySQL | — |
| `sync_sanrise` | SunRise | MS SQL | — |
| `sync_sanxing` | Sanxing_old | MS SQL | — |

### Роботы по расписанию (каждый в своём слоте)

| Команда | Время | Источник | Примечание |
|---|---|---|---|
| `sync_star` | 03:00 / 15:00 | Star | HTTP API, XML за 1–2 сек |
| `sync_hxn` | 03:30 / 15:30 | Hexing POP / Mini-MDM | API сломан, работает через браузер (Playwright) |
| `sync_website` | 04:00 / 16:00 | Sanxing_new_100A | HTTP API (генерация ZIP ~50 сек) |
| `sync_website_period1` | 04:30 / 16:30 | Sanxing_new_5A | HTTP API (модель S34U18 CT) |

### Обслуживание и реле (задания планировщика)

- `auto_close_issues` — каждый час в :10.
- `check_debts_auto_on` — каждые 10 минут: автоматическая постановка задач на включение для оплативших абонентов.
- `process_relay_tasks` — каждую минуту: воркер очереди реле (см. ниже).

## ⚡ Управление реле (Remote Control)

Страница `/relay-control/`: вкладки «Реле» (одиночное управление), «Задачи» (массовые), «Мониторинг».

### Доступ и ограничения

- Доступ: `is_staff` или группы `operator` / `Инженер АСКУЭ` (`can_manage_relay`). Кнопка «Повтор» задачи — только `is_staff`.
- Группы `ASKUE:<район>` (название района ровно как в `Region.name`, например `ASKUE:03 Сузак`) ограничивают инженера своим районом. Проверка `relay_region_denied` стоит в статусе, действии, массовом создании задач и загрузке по списку: чужой счётчик или счётчик без района для ограниченного пользователя закрыт (403). Админы не ограничены.
- Поиск счётчика (`find_relay_device`): точное совпадение, иначе однозначное совпадение по окончанию номера (без ведущих нулей, не короче 5 символов). Пустой или неоднозначный номер счётчик не выбирает.

### Создание задач

- **Одиночное** (`/relay-control/api/action/`): создаёт `RelayTask` на «сейчас», отменяет прежние `pending`/`processing` задачи счётчика. Команды view напрямую не шлёт: выполнит воркер в течение минуты.
- **Массовое** (`/relay-control/api/mass-create-tasks/`): список должников приходит из 1С (`OneCService.get_debtors`). Сервер проверяет `action` (`on`/`off`), обязательное и корректное время (минимум +30 минут; UI требует +1 час), пропускает чужие районы и дубли (такая же активная задача уже есть). Противоположные активные задачи отменяются. Для счётчиков, найденных в 1С, но отсутствующих в АСКУЭ, создаются заявки `CompareResult` (`not_found`).
- **По списку номеров** (`/relay-control/bulk-start/`, результат в `/relay-control/bulk-status/`): для каждого номера создаётся `RelayTask` (отключение — на «сейчас + 30 минут», включение — сразу). Прямых команд нет. В интерфейсе вызова нет, доступен только запросом.
- **Повтор** (`/relay-control/api/task-retry/`): только для `error`; сбрасывает `retry_count` и `hexing_payload`.
- **Автовключение** (`check_debts_auto_on`): если `Subscriber.debt <= 0` и последняя запись `RelayCommandHistory` по счётчику — `off` со статусом `success`/`pending`, отменяет задачи на отключение и создаёт задачу на включение.

### Воркер очереди (`process_relay_tasks.py`)

- Каждую минуту берёт до 50 задач `pending` с наступившим `scheduled_at` (`select_for_update(skip_locked)`), переводит в `processing`, выполняет в 5 потоках.
- **Проверка долга перед отключением:** запрос в 1С (`OneCService.check_debts`); для района без базы 1С или без `ONEC_SERVER_URL` — локальный `Subscriber`. Долг ≤ 0 — задача `cancelled`. Если 1С недоступна — отключать нельзя, задача идёт в повтор.
- **Повторы:** при ошибке 1-я повторная попытка через 5 минут, 2-я через 10, 3-я через 20; после этого `error`.
- **Зависшие задачи:** `processing` старше 30 минут (`STUCK_TIMEOUT_MIN`) возвращаются в `pending`.
- В `RelayCommandHistory` пишется `user = task.created_by`. Ошибки в историю не пишутся (только `success`/`pending`), они видны в `RelayTask.error_message`.

### Системы счётчиков и двухфазная схема

Поток воркера не держится «спящим» в ожидании. Для систем с асинхронным итогом после отправки команды задача возвращается в очередь (`pending`, новое `scheduled_at`), а состояние хранится в JSON-поле `RelayTask.hexing_payload` (название историческое, поле общее для всех систем; `provider` внутри JSON). Вторая фаза проверяет итог. Если не подтвердилось — ошибка и обычные повторы с повторной отправкой команды (для отключения долг проверяется заново). Долг на второй фазе не перепроверяется.

| Система (askue_id / api_id) | Отправка | Проверка итога |
|---|---|---|
| Hexing KUK (25 / UK) | REST `/switch`, ACK `2001` | Через 125 сек (`HEXING_RESULT_DELAY_SEC`) повторный `/switch` тем же XML (`check_pending`). Успешный сценарий проверен в бою (команда `on` на счётчик 61002510, задача завершилась за ~4 минуты); путь ошибки не проверялся |
| SunRise (26 / SR) | REST, CIM XML (GZIP+Base64) | Раз в минуту читается реальное состояние реле (`nativeData=1`), до 5 минут (`SUNRISE_WAIT_MIN`) |
| RiseSun (24 / RS) | MMWS REST, пакет `disconnect-batch` / `reconnect-batch`, один `Idempotency-Key` на все сетевые повторы | Раз в минуту `GET /disconnect-batch/{id}` (единый статус для обеих операций), итог по `items` (`done`/`failed`, коды ошибок по APIDOC), до 15 минут (`RISESUN_WAIT_MIN`). Проверено только на заглушках |
| cEnergo (23 / EM) | SOAP `SetMeterState`, `sync_timeout` 60 сек | При `TimeoutExpired` (счётчик спит) cEnergo сама повторяет команду каждые 5 минут в течение 10 минут (настройка сервера cEnergo, `CENERGO_WINDOW_MIN`), затем «Сбой отправки команды управления реле» (`SetMeterStateFailed`). Через 11 минут воркер читает `GetMeterState` (1 — включён, 2 — отключён) |
| Sanxing_old (22 / SX) | HTTP, `/realtimereading/index/switch`, логин с капчей через ddddocr | Синхронно: `errorCode == 0` — выполнено, `Off line(code:-2)` — счётчик офлайн (повтор). Чтения состояния реле нет |

Только cEnergo держит команду на своём сервере; остальные системы команды не удерживают, поэтому после ошибки старая команда позже не выполнится.

Код ошибок cEnergo расшифровывается в `cenergo_service.ERROR_TEXTS`, RiseSun — в `risesun_service.ERROR_TEXTS`.

## 🔗 Интеграция с 1С

- `OneCService` (`onec_service.py`): `get_debtors(region, min_debt)` и `check_debts(region, meters)`. База 1С выбирается по району (`ONEC_DB_*`); для районов без базы (ЖПЭС) и без `ONEC_SERVER_URL` данные берутся из локальной таблицы `Subscriber`.
- `Subscriber`: лицевой счёт, ФИО, долг, привязка к `Device`.
- `CompareResult`: заявки. Если 1С присылает должника, а в АСКУЭ его нет — заявка `not_found`.
- Страница `/sync-1c/` и команды `sync_1c_statuses`, `verify_1c_issues`; выгрузки 1С лежат в `1c_reports/` (в `.gitignore`).

## Окружение (`app/cEnergo.env`)

Базы: `MYSQL_*`, `DB_MSSQL_*` (cEnergo), `SANXING_*`, `SANRISE_MSSQL_*`, `HEXING_KUK_*`, `INHEMETER_MYSQL_*`, `RISESUN_PG_*`.

API и сервисы:

- `MMWS_API_URL`, `MMWS_API_KEY` (RiseSun)
- `STAR_URL`, `STAR_USERNAME`, `STAR_PASSWORD`
- `SANXING_NEW_API_URL`, `SANXING_NEW_ACCOUNT`, `SANXING_NEW_PASSWORD_ENCRYPTED` (Sanxing_new 100A/5A)
- `SANXING_OLD_URL`, `SANXING_OLD_USERNAME`, `SANXING_OLD_PASSWORD`
- `SUNRISE_API_URL`, `SUNRISE_API_USER`, `SUNRISE_API_PASS`
- `HEXING_HES_URL`, `HEXING_HES_USERNAME`, `HEXING_HES_PASSWORD`
- `ONEC_SERVER_URL`, `ONEC_DB_*` (по районам)

## Структура проекта

```text
django_app/
├── config/                           # settings, urls
├── meters/
│   ├── management/commands/
│   │   ├── process_relay_tasks.py    # Воркер очереди реле
│   │   ├── check_debts_auto_on.py    # Автовключение
│   │   ├── run_scheduler.py          # Запуск планировщика
│   │   ├── sync_*.py                 # Роботы синхронизации
│   │   ├── sync_1c_statuses.py, verify_1c_issues.py   # Интеграция с 1С
│   │   └── test_*.py                 # Отладочные скрипты (не автотесты)
│   ├── hexing_service.py             # Hexing KUK REST + чтение статуса из БД Hexing
│   ├── cenergo_service.py            # cEnergo SOAP
│   ├── sunrise_service.py            # SunRise REST
│   ├── risesun_service.py            # RiseSun (MMWS) REST
│   ├── sanxing_service.py            # Sanxing_old HTTP + капча (ddddocr)
│   ├── onec_service.py               # Клиент 1С
│   ├── models.py                     # Device, Reading, Subscriber, RelayTask, и др.
│   ├── views.py                      # Веб-интерфейс, API (около 5300 строк)
│   ├── scheduler.py                  # Планировщик
│   └── balance_calculator.py         # Логика баланса
└── ...
```

## Известные проблемы и планы (TODO)

### Реле
- **Hexing:** успешный сценарий проверен на счётчике 61002510 (`on` на включённый счётчик; физического переключения не наблюдали). Вторая фаза при повторном `pending` считает задачу успешной; лучше подтверждать по `get_relay_status_from_db` (проверить на отключённом счётчике). Путь ошибки не проверялся: для него годится 61007588 (в БД Hexing нет статусов реле, похоже, не на связи).
- **RiseSun:** схема написана по черновому регламенту (APIDOC «проект для согласования»), проверить на реальном MMWS одной командой `on`. `DISCONNECT_NOT_SUPPORTED` повторяется впустую три раза.
- **SunRise:** `MessageID` и дата в XML-заголовке зашиты (`2026-10-29`), команды при этом проходят.
- **Sanxing_old:** параллельные логины (по одному на задачу, до 5 потоков под одной учётной записью) на практике не проверялись. Успех входа определяется слабо (любой ответ 200 без признаков ошибки). Сбой по таймауту: неизвестно, дошла ли команда (повтор безвреден).
- **Автовключение зацикливается:** если задача на включение упала (`error`), последняя запись истории остаётся `off`, и каждые 10 минут создаётся новая задача.
- Новая команда из ручного управления отменяет в том числе `processing`-задачу, которую воркер уже выполняет: статус перезапишется, команда всё равно уйдёт.
- Записи `RelayCommandHistory` по cEnergo, созданные до двухфазной схемы, остались в `pending` и никогда не подтверждались.

### Общее
- `settings.py`: `DEBUG = True`, `ALLOWED_HOSTS = ['*']`, `USE_TZ = False` (время наивное).
- `views.py` (около 5300 строк) стоит разбить на модули (реле, сверка с 1С, иерархия, баланс, устройства). Автотестов нет.
- Дубль индекса в `Reading`: `idx_readings_dev_ts_dir` повторяет `unique_together`.
- `Subscriber.region` — строка, `MeterConnection` ссылается на объект по числовому ID без внешнего ключа.
- Sanxing_new: пароль (`SANXING_NEW_PASSWORD_ENCRYPTED`) статичный. Если Sanxing поменяет пароль в UI, его нужно перешифровать.
- Hexing POP (`sync_hxn`): остался на Playwright. API (`/rawDataReport/downloadFile`) возвращает 0 байт. Надо разобраться с `DWRSESSIONID` или перехватить запрос через DevTools.
- 1С: уточнить у разработчиков 1С итоговые эндпоинты Pull-модели (`get_debtors`, `check_debts`).
- UI/UX: в `add_device.html` при выборе модели ПУ редактор кода (Prettier) ломает теги `{{ m.phases }}`. Рекомендуется использовать `MeterModel.display_name`.
- Дашборд: список `auto_notes` в `views.py` (`dashboard`) должен строго совпадать с `notes`, которые пишут роботы, иначе ручные показания будут считаться неверно.
