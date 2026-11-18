# Архитектура проекта Meter_Project (АСКУЭ)

## 1. Общая архитектура
Проект развернут в 3 контейнерах (Docker Compose):
1. `meters_mysql` — MySQL 8.
2. `meters_django` — Веб-интерфейс (Gunicorn).
3. `meters_scheduler` — Планировщик (APScheduler) и все роботы.

## 2. Ключевые модели (Core Models)
*   `Device`: Реестр ПУ. Поля `askue_id`, `api_id` определяют принадлежность к системе.
*   `Reading`: Показания. Уникальность `(device, timestamp, direction)`.
*   `SyncStatus`: Статусы роботов для дашборда.
*   `CompareResult`: Заявки при сверке с 1С.
*   `RelayCommandHistory`: Аудит команд реле (кто, когда, какой счетчик отключал).

## 3. Роботы синхронизации (Сбор данных)
Запускаются через `scheduler.py` (быстрая цепочка раз в 30 мин).
*   **БД (Быстрые):** `sync_mssql` (cEnergo), `sync_sanrise` (SunRise), `sync_sanxing` (Sanxing_old), `sync_hexing` (Hexing KUK), `sync_inhemeter` (Inhemeter).
*   **API:** `sync_risesun_api` (RiseSun).
*   **Playwright:** `sync_star`, `sync_hxn`, `sync_website`, `sync_website_period1` (запускаются в 03:00, 03:30, 04:00, 04:30).

## 4. Сверка с 1С
*   Источник: Excel-файлы в `1c_reports/YYYY-MM-DD/`.
*   Парсер: `calamine` (обязательно, `openpyxl` ломается на файлах 1С).
*   Логика: Автообновление района/категории. Отключение "мертвых" счетчиков (>30 дней без связи).
*   Специфика: Словарь маппинга районов (`REGION_MAPPING`).

## 5. Управление реле (Remote Control)
Реализовано на странице `relay_control.html` (доступно админам). Все сервисы лежат в `meters/*_service.py`.
1.  **SunRise:** SOAP API. Синхронный (мгновенный ответ).
2.  **RiseSun (MMWS):** REST API (Batch). Асинхронный (отправляем `POST`, получаем `batch_id`, опрашиваем статус).
3.  **cEnergo (Энергомера):** SOAP API. Синхронный с таймаутом (`sync_timeout`).
4.  **Sanxing:** Playwright + DWR. Логин через браузер с обходом капчи (`ddddocr`). Отправка через `dwrAction.doAjax`.
5.  **Hexing KUK (В РАЗРАБОТКЕ):** 
    *   *Отказались от DWR (MiniMDM, порт 8080).* Он ломает сессии и требует сложных манипуляций с UI.
    *   *Нашли официальный REST API (HES, порт 8091).* Документация: `HX Interface SpecificationV to customer 20230222.pdf`.
    *   **Логин:** `GET http://192.168.144.64:8091/HES/login?username=billing&password=md5(billing)` -> возвращает `sessionID`.
    *   **Запросы:** XML сжимается GZIP + Base64 и передается в URL (или POST). 
    *   **Чтение статуса:** `.../HES/reading?token=x&xml=x`. XML содержит `mRID=3.15.91.26`.
    *   **Вкл/Выкл:** `.../HES/switch?token=x&xml=x`. XML содержит `mRID`, `command` (пустой) и `EndDeviceControlType ref="3.15.91.18"` (Вкл) или `"3.15.91.42"` (Выкл).
    *   **Асинхронность (ГЛАВНАЯ ФИШКА):** Если `errorCode == 2001` (ACK), сервер принял команду. Нужно подождать >2 минут и отправить **тот же самый XML** с тем же `CorrelationID`. Сервер вернет финальный результат (0 - успех, 2003 - таймаут).

## 6. Важные правила (Best Practices)
1.  **Нормализация серийников:** Всегда использовать `norm_serial()` (удаление нецифровых символов и ведущих нулей). Поиск по последним 8 цифрам.
2.  **Потоки при Playwright:** Любые обращения к БД во время Playwright-сессий должны быть изолированы в `ThreadPoolExecutor`.
3.  **Безопасность:** Пароли хранятся в `app/cEnergo.env`. Никакого хардкода в репозитории.

## 7. Текущая задача для нового чата
**Hexing KUK (HES API).**
Необходимо дописать `test_hexing_hes.py` и перенести логику в `hexing_service.py`.
*   Скрипт уже успешно логинится и получает `2001 ACK`.
*   Нужно реализовать: отправку первого запроса -> ожидание 125 секунд -> повторную отправку того же XML с тем же `CorrelationID` -> парсинг ответа (распаковка GZIP + Base64 -> поиск `<m:value>`).
*   После теста — добавить Hexing в `relay_api_action` (в `views.py`), используя этот REST API вместо Playwright.