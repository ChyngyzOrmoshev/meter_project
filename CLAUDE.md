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

- **Одиночное** (`/relay-control/api/action/`): создаёт `RelayTask` на «сейчас». Прежние ожидающие задачи счётчика отменяются, а «задачи в полёте» нет (см. ниже): новая встаёт после них. Если такая же команда уже в полёте — 409. Команды view напрямую не шлёт: выполнит воркер в течение минуты.
- **Массовое** (`/relay-control/api/mass-create-tasks/`): список должников приходит из 1С (`OneCService.get_debtors`). Сервер проверяет `action` (`on`/`off`), обязательное и корректное время (минимум +30 минут; UI требует +1 час), пропускает чужие районы и дубли (такая же активная задача уже есть). Противоположные ожидающие задачи отменяются, задачи в полёте не трогаются (новая встаёт после них). Для счётчиков, найденных в 1С, но отсутствующих в АСКУЭ, создаются заявки `CompareResult` (`not_found`).
- **По списку номеров** (`/relay-control/bulk-start/`, результат в `/relay-control/bulk-status/`): для каждого номера создаётся `RelayTask` (отключение — на «сейчас + 30 минут», включение — сразу). Прямых команд нет. В интерфейсе вызова нет, доступен только запросом.
- **Повтор** (`/relay-control/api/task-retry/`): только для `error`; сбрасывает `retry_count` и `hexing_payload`.
- **Автовключение** (`check_debts_auto_on`): если `Subscriber.debt <= 0` и последняя запись `RelayCommandHistory` по счётчику — `off` со статусом `success`/`pending`, отменяет ожидающие задачи на отключение и создаёт задачу на включение. После упавшей (`error`) задачи на включение счётчик пропускается `RETRY_PAUSE_HOURS` (6 ч), чтобы не создавать задачи каждые 10 минут.

### Задачи в полёте (`relay_queue.py`)

`inflight_tasks` — задачи, команда по которым уже ушла или уходит: `processing`, а также `pending` с заполненным `hexing_payload` (первая фаза пройдена, ждём проверку итога). `supersede_tasks` отменяет только прочие `pending`. Если по счётчику есть задача в полёте, новая откладывается: минимум на 2 минуты (`BUSY_DELAY_MIN`) или на минуту после времени второй фазы (`PHASE2_GAP_MIN`); для cEnergo это около 12 минут. Используется в ручном управлении, массовом создании, загрузке по списку и автовключении.

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
- Контракт 1С (`1С_API.txt`, не в git): HTTP-сервис `POST {ONEC_SERVER_URL}/{база}/hs/ASKUE/readdebt`, тело `{"min_debt", "askue_types"}`, Basic-авторизация (`ONEC_USER`/`ONEC_PASSWORD`; логин кириллический, заголовок собирается вручную в UTF-8). Ответ: `status: ok` + `data` (`serial_number`, `account_number`, `full_name`, `debt`, `askue_type_id`) или `status: Not` (должников нет).
- Метода проверки долга по списку счётчиков у 1С нет: `check_debts` берёт `readdebt` с порогом 0.01 и ищет серийник в списке (нет в списке — долг 0, отключение отменяется). Недоступность 1С — исключение и повтор.
- `ONEC_DB_OVERRIDE` (например `proba83`): пробная база заменяет базы всех районов с 1С. Для боевого режима удалить из `cEnergo.env`: иначе серийников там нет и все отключения отменяются. После правки `cEnergo.env` нужен `docker compose up -d --no-deps --force-recreate django scheduler`: `restart` не перечитывает `env_file`.
- `Subscriber`: лицевой счёт, ФИО, долг, привязка к `Device`.
- `CompareResult`: заявки. Если 1С присылает должника, а в АСКУЭ его нет — заявка `not_found`.
- Страница `/sync-1c/` и команды `sync_1c_statuses`, `verify_1c_issues`; выгрузки 1С лежат в `1c_reports/` (в `.gitignore`).

## Окружение (`app/cEnergo.env`)

Базы: `MYSQL_*`, `DB_MSSQL_*` (cEnergo), `SANXING_*`, `SANRISE_MSSQL_*`, `HEXING_KUK_*`, `INHEMETER_MYSQL_*`, `RISESUN_PG_*`.

API и сервисы:

- `MMWS_API_URL`, `MMWS_API_KEY` (RiseSun)
- `STAR_URL`, `STAR_USERNAME`, `STAR_PASSWORD`
- `SANXING_NEW_API_URL`, `SANXING_NEW_ACCOUNT`, `SANXING_NEW_PASSWORD_ENCRYPTED` (Sanxing_new 100A/5A)
- `SANXING_NEW_WEB_URL` (веб-интерфейс Sanxing_new для заголовков Origin/Referer), `CENERGO_SOAP_URL` (SOAP cEnergo); у обоих есть значение по умолчанию в коде
- `SANXING_OLD_URL`, `SANXING_OLD_USERNAME`, `SANXING_OLD_PASSWORD`
- `SUNRISE_API_URL`, `SUNRISE_API_USER`, `SUNRISE_API_PASS`
- `HEXING_HES_URL`, `HEXING_HES_USERNAME`, `HEXING_HES_PASSWORD`
- `ONEC_SERVER_URL` (без базы, например `http://192.168.144.56`), `ONEC_USER`, `ONEC_PASSWORD`, `ONEC_DB_*` (по районам), `ONEC_DB_OVERRIDE` (пробная база, необязательно)

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
│   ├── relay_queue.py                # Отмена/откладывание задач реле, задачи в полёте
│   ├── models.py                     # Device, Reading, Subscriber, RelayTask, и др.
│   ├── views.py                      # Веб-интерфейс, API (около 900 строк)
│   ├── views_relay.py                # Управление реле: страница, API, массовые задачи, монитор
│   ├── views_balance.py              # Баланс: показания по связям, сводка, отчёт, коэффициенты
│   ├── views_devices.py              # Устройства: список, карточка, загрузка, группы, массовые операции, реестры, экспорт
│   ├── views_readings.py             # Показания: просмотр, ручной ввод, импорт из Excel, отчёты, опрос счётчиков
│   ├── views_hierarchy.py            # Иерархия сети: районы, подстанции, фидеры, ТП
│   ├── views_1c.py                   # Сверка с 1С: загрузка, заявки, статистика, синхронизация и проверка статусов
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
- Записи `RelayCommandHistory` по cEnergo, созданные до двухфазной схемы, остались в `pending` и никогда не подтверждались.

### Общее
- `settings.py`: `DEBUG = True`, `ALLOWED_HOSTS = ['*']`, `USE_TZ = False` (время наивное).
- `views.py` (около 900 строк) разбит на модули: реле (`views_relay.py`), сверка с 1С (`views_1c.py`), иерархия (`views_hierarchy.py`), показания с импортом (`views_readings.py`), устройства (`views_devices.py`), баланс (`views_balance.py`); маршруты в `urls.py` ведут на них. В `views.py` остались дашборд, вход/выход, модели ПУ, статистика по производителям/районам/категориям, `restart_robot` и общие хелперы (`REVERSE_REGION_MAPPING`, `is_allowed_user`, `compare_models`, `find_device`, `get_producer_display`), которые импортируют модули. `balance_report` без маршрута оставлен намеренно. Страницы баланса считают и сохраняют `BalanceResult` при первом открытии (GET пишет в БД). В `balance_calculator.py` `get_reading_for_date` и `get_coefficient_for_date` определены дважды, действует вторая. Автотестов нет.
- Дубль индекса в `Reading`: `idx_readings_dev_ts_dir` повторяет `unique_together`.
- `Subscriber.region` — строка, `MeterConnection` ссылается на объект по числовому ID без внешнего ключа.
- Sanxing_new: пароль (`SANXING_NEW_PASSWORD_ENCRYPTED`) статичный. Если Sanxing поменяет пароль в UI, его нужно перешифровать.
- Hexing POP (`sync_hxn`): остался на Playwright. API (`/rawDataReport/downloadFile`) возвращает 0 байт. Надо разобраться с `DWRSESSIONID` или перехватить запрос через DevTools.
- 1С: `get_debtors` переведён на контракт `readdebt`, проверить в бою (пока только на заглушках; пробная база `proba83`). Уточнить у разработчиков 1С: порог `min_debt` (строго больше или не меньше), метод проверки долга по списку серийников (сейчас обход через `readdebt`), кодировку Basic-логина (UTF-8 или Windows-1251), имена баз по районам.
- UI/UX: в `add_device.html` при выборе модели ПУ редактор кода (Prettier) ломает теги `{{ m.phases }}`. Рекомендуется использовать `MeterModel.display_name`.
- Дашборд: список `auto_notes` в `views.py` (`dashboard`) должен строго совпадать с `notes`, которые пишут роботы, иначе ручные показания будут считаться неверно.
