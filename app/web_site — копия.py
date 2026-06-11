import os
import time
import streamlit as st
import pandas as pd
from datetime import datetime
from db_client import models_col, devices_col, readings_col
from pymongo import UpdateOne

# 1. Настройка внешнего вида страницы и компактных отступов
st.set_page_config(page_title="Учет Энергии", page_icon="⚡", layout="wide")

st.markdown(
    """
    <style>
        .block-container { padding-top: 1.5rem !important; padding-bottom: 0rem !important; }
        div[data-testid="stSidebarUserContent"] { padding-top: 1.5rem !important; }
        /* Стили для того, чтобы кнопки в меню были на всю ширину и без кружков */
        div[data-testid="stSidebar"] button {
            width: 100% !important;
            justify-content: flex-start !important;
            margin-bottom: 0.3rem !important;
            text-align: left !important;
        }
    </style>
""",
    unsafe_allow_html=True,
)

# 2. Инициализация переменных памяти сессии Streamlit
if "user_role" not in st.session_state:
    st.session_state.user_role = None  # Изначально гость (None)
if "current_page" not in st.session_state:
    st.session_state.current_page = "📋 Справочник моделей (Excel)"
if "last_activity_time" not in st.session_state:
    st.session_state.last_activity_time = time.time()

# База учетных записей (Логин: Пароль)
USER_CREDENTIALS = {
    "admin": {"password": "123", "role": "admin"},
    "operator": {"password": "456", "role": "operator"},
    "user": {"password": "789", "role": "user"},
}

# Порог бездействия в секундах (15 минут = 900 секунд)
INACTIVITY_TIMEOUT = 900

# Проверяем таймаут бездействия, если пользователь уже авторизован
if st.session_state.user_role is not None:
    current_time = time.time()
    elapsed_time = current_time - st.session_state.last_activity_time
    if elapsed_time > INACTIVITY_TIMEOUT:
        st.session_state.user_role = None
        st.warning("⏱️ Сессия завершена из-за длительного бездействия. Войдите заново.")
        st.rerun()
    else:
        # Обновляем время активности при любом действии на сайте
        st.session_state.last_activity_time = current_time


# Загружаем списки из базы для выпадающих меню
# Оптимизация загрузки справочников через кэш Streamlit
@st.cache_data(ttl=300)
def load_cached_dropdowns():
    # Из справочника моделей вытягиваем имя и номинальный ток
    raw_models = list(
        models_col.find({}, {"_id": 0, "model_name": 1, "nominal_current": 1})
    )

    # Формируем красивый список для выпадающего меню"
    models_display = []
    for m in raw_models:
        name = m.get("model_name", "Неизвестно")
        curr = m.get("nominal_current", "")
        display_str = f"{name} [{curr}]" if curr else name
        if display_str not in models_display:
            models_display.append(display_str)

    models_display.sort()
    serial_numbers = sorted(list(devices_col.distinct("serial_number")))
    return models_display, serial_numbers


# Получаем данные из кэша
all_models, all_serial_numbers = load_cached_dropdowns()


# --- ЛЕВОЕ БОКОВОЕ МЕНЮ (SIDEBAR) ---
st.sidebar.title("⚡ Навигация")

# Если пользователь не вошел — показываем компактный выпадающий блок авторизации
if st.session_state.user_role is None:
    with st.sidebar.expander("🔑 Войти в систему"):
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Логин:").strip().lower()
            password = st.text_input("Пароль:", type="password")
            if st.form_submit_button("Войти", use_container_width=True):
                if (
                    username in USER_CREDENTIALS
                    and USER_CREDENTIALS[username]["password"] == password
                ):
                    st.session_state.user_role = USER_CREDENTIALS[username]["role"]
                    st.session_state.last_activity_time = time.time()
                    st.rerun()
                else:
                    st.error("❌ Неверный логин или пароль!")
    temp_role = "user"
else:
    # Если вошел — пишем его роль и показываем кнопку выхода
    st.sidebar.write(f"Вы вошли как: **{st.session_state.user_role.upper()}**")
    if st.sidebar.button("🚪 Выйти из системы", type="secondary"):
        st.session_state.user_role = None
        st.rerun()
    temp_role = st.session_state.user_role

st.sidebar.markdown("---")

# Кнопки навигации (Активная кнопка подсвечивается синим цветом 'primary')
if st.sidebar.button(
    "📋 Справочник моделей (Excel)",
    type=(
        "primary"
        if st.session_state.current_page == "📋 Справочник моделей (Excel)"
        else "secondary"
    ),
):
    st.session_state.current_page = "📋 Справочник моделей (Excel)"
    st.rerun()

if st.sidebar.button(
    "🏭 Реестр устройств (Devices)",
    type=(
        "primary"
        if st.session_state.current_page == "🏭 Реестр устройств (Devices)"
        else "secondary"
    ),
):
    st.session_state.current_page = "🏭 Реестр устройств (Devices)"
    st.rerun()

if st.sidebar.button(
    "📉 Сбор показаний (Readings)",
    type=(
        "primary"
        if st.session_state.current_page == "📉 Сбор показаний (Readings)"
        else "secondary"
    ),
):
    st.session_state.current_page = "📉 Сбор показаний (Readings)"
    st.rerun()

page = st.session_state.current_page

# === ОГРАНИЧЕНИЕ: Если зашли как гость, выводим подсказку сверху ===
# if st.session_state.user_role is None:
#    st.info(
#        "ℹ️ Вы находитесь в режиме просмотра. Для активации ввода данных разверните блок «Войти в систему» в левом меню."
#    )

# ==============================================================================
# === РАЗДЕЛ 1: СПРАВОЧНИК МОДЕЛЕЙ ===
# ==============================================================================
if page == "📋 Справочник моделей (Excel)":
    st.subheader("Справочник моделей счетчиков")

    models = list(models_col.find({}, {"_id": 0}))
    if models:
        df_models_view = pd.DataFrame(models).rename(
            columns={
                "catalog_code": "Код",
                "model_name": "Наименование",
                "digit_capacity": "Значность",
                "phases": "Фазность",
                "nominal_current": "Номинальный ток",
                "nominal_voltage": "Номинальное напряжение",
                "system_type": "Тип прибора учета",
                "period": "Период",
                "device_type_id": "Код АСКУЭ (ID)",
                "device_type_str": "Идентификатор API",
            }
        )
        st.dataframe(df_models_view, use_container_width=True, hide_index=True)
    else:
        st.info("Справочник моделей в базе пуст. Запустите импорт из Excel.")

# ==============================================================================
# === РАЗДЕЛ 2: РЕЕСТР УСТРОЙСТВ (DEVICES) ===
# ==============================================================================
elif page == "🏭 Реестр устройств (Devices)":
    st.subheader("Реестр зарегистрированных приборов учета")

    # 🔒 Ограничение доступа: Форму добавления видят только admin и operator
    if temp_role in ["admin", "operator"] and st.session_state.user_role is not None:
        with st.expander("➕ Добавить новые приборы в базу данных"):
            add_type = st.radio(
                "Способ ввода:",
                ["По одному", "Группой (Массовый ввод)"],
                horizontal=True,
            )

            if add_type == "По одному":
                with st.form("single_device_form", clear_on_submit=True):
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        sn = st.text_input("Заводской номер:").strip()
                    with col2:
                        # Сюда прилетит красивое имя, например: "P34S02 [5(80)]"
                        selected_model_str = st.selectbox(
                            "Модель счетчика (и ток):", all_models
                        )
                    with col3:
                        status = st.selectbox(
                            "Статус прибора:", ["active", "repair", "inactive"]
                        )
                    if st.form_submit_button("Сохранить"):
                        if sn and selected_model_str:
                            # Распаковываем "P34S02 [5(80)]" обратно на имя и ток
                            model_name = selected_model_str.split(" [")[0]
                            nominal_current = (
                                selected_model_str.split(" [")[1].replace("]", "")
                                if " [" in selected_model_str
                                else ""
                            )

                            devices_col.update_one(
                                {"serial_number": sn},
                                {
                                    "$set": {
                                        "serial_number": sn,
                                        "model_name": model_name,
                                        "nominal_current": nominal_current,
                                        "status": status,
                                    }
                                },
                                upsert=True,
                            )
                            st.cache_data.clear()  # Сброс кэша dropdown
                            st.success(f"Счетчик {sn} добавлен!")
                            st.rerun()
                        else:
                            st.error("Заполните заводской номер!")

            elif add_type == "Группой (Массовый ввод)":
                from pymongo import UpdateOne

                with st.form("bulk_device_form", clear_on_submit=True):
                    b_selected_model_str = st.selectbox(
                        "Модель (и ток) для всей группы:", all_models
                    )
                    b_status = st.selectbox(
                        "Статус для всей группы:", ["active", "repair", "inactive"]
                    )
                    b_sns = st.text_area(
                        "Вставьте список заводских номеров (каждый с новой строки):"
                    )
                    if st.form_submit_button("Зарегистрировать группу"):
                        sn_list = [
                            line.strip() for line in b_sns.split("\n") if line.strip()
                        ]
                        if sn_list and b_selected_model_str:
                            # Распаковка параметров для группы
                            b_model_name = b_selected_model_str.split(" [")[0]
                            b_nominal_current = (
                                b_selected_model_str.split(" [")[1].replace("]", "")
                                if " [" in b_selected_model_str
                                else ""
                            )

                            operations = [
                                UpdateOne(
                                    {"serial_number": s_num},
                                    {
                                        "$set": {
                                            "serial_number": s_num,
                                            "model_name": b_model_name,
                                            "nominal_current": b_nominal_current,
                                            "status": b_status,
                                        }
                                    },
                                    upsert=True,
                                )
                                for s_num in sn_list
                            ]
                            devices_col.bulk_write(operations)
                            st.cache_data.clear()
                            st.success(f"Успешно добавлено счетчиков: {len(sn_list)}")
                            st.rerun()

    # --- Интерфейс фильтрации данных ---
    f1, f2, f3 = st.columns(3)
    with f1:
        s_search = st.text_input("🔍 Поиск по заводскому номеру:").strip()
    with f2:
        s_filter = st.selectbox(
            "Фильтр по статусу:", ["Все", "active", "repair", "inactive"]
        )
    with f3:
        # Для фильтра используем чистые имена моделей из базы
        pure_models = sorted(list(models_col.distinct("model_name")))
        m_filter = st.selectbox("Фильтр по модели:", ["Все"] + pure_models)

    # Запрос фильтрации к MongoDB
    query = {}
    if s_search:
        query["serial_number"] = {"$regex": s_search, "$options": "i"}
    if s_filter != "Все":
        query["status"] = s_filter
    if m_filter != "Все":
        query["model_name"] = m_filter

    devices = list(devices_col.find(query, {"_id": 0}))

    if devices:
        df_devices = pd.DataFrame(devices)

        # Если в старых записях устройств нет поля nominal_current, создаем его пустым для корректного merge
        if "nominal_current" not in df_devices.columns:
            df_devices["nominal_current"] = ""
        df_devices["nominal_current"] = df_devices["nominal_current"].fillna("")

        # Выгружаем метаданные из справочника моделей
        df_m_meta = pd.DataFrame(
            list(
                models_col.find(
                    {},
                    {
                        "_id": 0,
                        "model_name": 1,
                        "nominal_current": 1,
                        "device_type_id": 1,
                        "device_type_str": 1,
                        "phases": 1,
                    },
                )
            )
        )

        if not df_m_meta.empty:
            df_m_meta["nominal_current"] = df_m_meta["nominal_current"].fillna("")
            # ИСПРАВЛЕНИЕ: Связываем таблицы ОДНОВРЕМЕННО по двум полям (имя модели + её ток)
            # Это полностью исключает дублирование строк при выводе
            final_df = pd.merge(
                df_devices, df_m_meta, on=["model_name", "nominal_current"], how="left"
            )
        else:
            final_df = df_devices

        final_df = final_df.rename(
            columns={
                "serial_number": "Заводской номер",
                "model_name": "Модель",
                "nominal_current": "Номинальный ток",
                "status": "Статус прибора",
                "device_type_id": "Код АСКУЭ (ID)",
                "device_type_str": "Идентификатор API",
                "phases": "Фазность",
            }
        )

        # Выстраиваем красивый порядок отображения колонок
        display_cols = [
            "Заводской номер",
            "Модель",
            "Номинальный ток",
            "Статус прибора",
            "Фазность",
            "Код АСКУЭ (ID)",
            "Идентификатор API",
        ]
        final_df = final_df[[c for c in display_cols if c in final_df.columns]]

        st.dataframe(final_df, use_container_width=True, hide_index=True)
    else:
        st.info("Приборы с указанными параметрами не найдены.")


# ==============================================================================
# === РАЗДЕЛ 3: СБОР ПОКАЗАНИЙ (ФОРМЫ ВВОДА С ФИКСАЦИЕЙ ВРЕМЕНИ 00:00:00) ===
# ==============================================================================
elif page == "📉 Сбор показаний (Readings)":
    st.subheader("Ввод текущих показаний и история")

    # Превращаем в set для мгновенного поиска O(1) при валидации массового ввода
    all_serial_numbers_set = set(all_serial_numbers)

    # 🔒 Ограничение доступа: Форму добавления видят только admin и operator
    if temp_role in ["admin", "operator"] and st.session_state.user_role is not None:
        if not all_serial_numbers:
            st.warning(
                "⚠️ Зарегистрируйте счетчики в Реестре устройств перед вводом показаний!"
            )
        else:
            with st.expander("📝 Внести новые показания киловатт-часов"):
                r_type = st.radio(
                    "Способ ввода данных:",
                    ["По одному", "Группой (Массовый ввод)"],
                    horizontal=True,
                )

                if r_type == "По одному":
                    with st.form("single_reading_form", clear_on_submit=True):
                        c1, c2, c3 = st.columns(3)
                        with c1:
                            r_sn = st.selectbox("Заводской номер:", all_serial_numbers)
                        with c2:
                            r_val = st.number_input(
                                "Показания (кВт*ч):", min_value=0.0, step=0.1
                            )
                        with c3:
                            r_date = st.date_input("Дата снятия:", datetime.now())
                        r_notes = st.text_input("Примечание:")

                        if st.form_submit_button("Сохранить показание"):
                            # ИСПРАВЛЕНО: Вместо текущего времени жестко пишем 00:00:00
                            full_dt = datetime.combine(r_date, datetime.min.time())
                            readings_col.insert_one(
                                {
                                    "serial_number": r_sn,
                                    "timestamp": full_dt,
                                    "reading_value": float(r_val),
                                    "notes": r_notes.strip(),
                                }
                            )
                            st.success("Показание сохранено!")
                            st.rerun()

                elif r_type == "Группой (Массовый ввод)":
                    from pymongo import InsertOne

                    st.info(
                        "💡 Формат: **Заводской_номер [пробел] Показания** (каждая пара с новой строки)."
                    )
                    with st.form("bulk_reading_form", clear_on_submit=True):
                        br_date = st.date_input(
                            "Дата снятия для группы:", datetime.now()
                        )
                        br_text = st.text_area("Данные:")
                        br_notes = st.text_input("Примечание для группы:")

                        if st.form_submit_button("Сохранить группу показаний"):
                            lines = [
                                line.strip()
                                for line in br_text.split("\n")
                                if line.strip()
                            ]

                            if lines:
                                inserts = []
                                # ИСПРАВЛЕНО: Для всей группы принудительно задаем 00:00:00
                                full_dt = datetime.combine(br_date, datetime.min.time())

                                for line in lines:
                                    parts = line.split()
                                    if (
                                        len(parts) >= 2
                                        and parts[0] in all_serial_numbers_set
                                    ):
                                        try:
                                            val_str = parts[1].replace(",", ".")
                                            inserts.append(
                                                InsertOne(
                                                    {
                                                        "serial_number": parts[0],
                                                        "timestamp": full_dt,
                                                        "reading_value": float(val_str),
                                                        "notes": br_notes.strip(),
                                                    }
                                                )
                                            )
                                        except (ValueError, IndexError):
                                            pass

                                if inserts:
                                    result = readings_col.bulk_write(inserts)
                                    st.success(
                                        f"Успешно внесено показаний: {result.inserted_count} шт."
                                    )
                                    st.rerun()
                                else:
                                    st.error(
                                        "Не найдено корректных строк для добавления."
                                    )

    # --- ЖУРНАЛ ПОКАЗАНИЙ С ВЫВОДОМ ИСТОРИИ ПО ДНЯМ ВНУТРИ ПЕРИОДА ---
    st.markdown("### 📊 Журнал показаний")

    # Получаем точную текущую дату на сервере
    today_date = datetime.now().date()

    # Инициализируем дефолтный период в памяти сессии строго ТЕКУЩИМ днем
    if "filter_start_date" not in st.session_state:
        st.session_state.filter_start_date = today_date
    if "filter_end_date" not in st.session_state:
        st.session_state.filter_end_date = today_date

    # Размещаем элементы фильтрации в один ряд
    col_btn, col_s, col_e, col_search = st.columns([0.6, 1.2, 1.2, 3])

    with col_btn:
        st.write("")
        st.write("")
        # ПЕРЕИМЕНОВАНО: Кнопка теперь называется "🔄 Сброс" и мгновенно возвращает календари на сегодня
        if st.button(
            "🔄 Сброс",
            key="btn_today_reset",
            use_container_width=True,
            help="Сбросить период на текущий день",
        ):
            st.session_state.filter_start_date = today_date
            st.session_state.filter_end_date = today_date
            st.rerun()

    with col_s:
        # ИСПРАВЛЕНО: Календарь при первом открытии фокусируется строго на текущем дне
        start_date = st.date_input(
            "📅 Начало периода:",
            value=st.session_state.filter_start_date,
            key="start_date_input",
        )
        st.session_state.filter_start_date = start_date

    with col_e:
        # ИСПРАВЛЕНО: Второй календарь также по умолчанию открывается на текущем дне
        end_date = st.date_input(
            "📅 Конец периода:",
            value=st.session_state.filter_end_date,
            key="end_date_input",
        )
        st.session_state.filter_end_date = end_date

    with col_search:
        search_sn = st.text_input(
            "🔍 Быстрый поиск по заводскому номеру внутри периода:", ""
        ).strip()

    # Валидация ограничения периода (максимум 7 дней)
    delta_days = (end_date - start_date).days

    if delta_days < 0:
        st.error("❌ Дата начала не может быть позже даты окончания!")
    elif delta_days > 7:
        st.error(
            f"⚠️ Выбран слишком большой период ({delta_days} дн.). Максимальный интервал просмотра — 7 дней!"
        )
    else:
        # Сброс страницы пагинации при любом изменении фильтров
        filter_key = f"range_history_filter_{start_date}_{end_date}_{search_sn}"
        if (
            "last_filter_key" not in st.session_state
            or st.session_state.last_filter_key != filter_key
        ):
            st.session_state.readings_page = 1
            st.session_state.last_filter_key = filter_key

        # Границы времени периода
        filter_start = datetime.combine(start_date, datetime.min.time())
        filter_end = datetime.combine(end_date, datetime.max.time())

        match_stage = {"timestamp": {"$gte": filter_start, "$lte": filter_end}}
        if search_sn:
            match_stage["serial_number"] = {"$regex": search_sn, "$options": "i"}

        # ИСПРАВЛЕННАЯ АГРЕГАЦИЯ 1: Считаем общее количество СУТОЧНЫХ записей за период
        # Группируем по связке (номер + день), чтобы узнать точное число строк в будущей таблице
        count_pipeline = [
            {"$match": match_stage},
            {
                "$group": {
                    "_id": {
                        "serial_number": "$serial_number",
                        "day": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$timestamp",
                            }
                        },
                    }
                }
            },
            {"$count": "total_records"},
        ]
        count_result = list(readings_col.aggregate(count_pipeline))
        # ИСПРАВЛЕНО: Берем первый элемент списка, так как агрегационный ответ возвращается в списке
        total_rows = count_result[0]["total_records"] if count_result else 0

        rows_per_page = 10
        total_pages = max(1, (total_rows + rows_per_page - 1) // rows_per_page)

        # Контроль границ текущей страницы
        if "readings_page" not in st.session_state:
            st.session_state.readings_page = 1
        if st.session_state.readings_page > total_pages:
            st.session_state.readings_page = total_pages

        if total_rows > 0:
            start_idx = (st.session_state.readings_page - 1) * rows_per_page

            # ИСПРАВЛЕННАЯ АГРЕГАЦИЯ 2: Посуточный конвейер (Часы схлопываем внутри дня, но дни разделяем)
            readings_pipeline = [
                {"$match": match_stage},
                {"$sort": {"timestamp": -1}},  # Свежие часы всегда первыми
                {
                    "$group": {
                        # Группируем одновременно по номеру счетчика и по календарному дню
                        "_id": {
                            "serial_number": "$serial_number",
                            "day": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": "$timestamp",
                                }
                            },
                        },
                        "latest_hour_doc": {
                            "$first": "$$ROOT"
                        },  # Берем самый свежий час внутри ЭТОГО конкретного дня
                    }
                },
                {"$replaceRoot": {"newRoot": "$latest_hour_doc"}},
                # Сортируем ведомость: сначала по убыванию даты (свежие дни сверху), а внутри дня — по номеру счетчика
                {"$sort": {"timestamp": -1, "serial_number": 1}},
                {"$skip": start_idx},
                {"$limit": rows_per_page},
            ]

            readings = list(readings_col.aggregate(readings_pipeline))
            df_readings = pd.DataFrame(readings)

            # Точечная подгрузка моделей только для выводимых 10 строк
            unique_sns_on_page = df_readings["serial_number"].unique().tolist()
            dev_meta = list(
                devices_col.find(
                    {"serial_number": {"$in": unique_sns_on_page}},
                    {"_id": 0, "serial_number": 1, "model_name": 1},
                )
            )

            sn_to_model = {
                item["serial_number"]: item["model_name"] for item in dev_meta
            }
            df_readings["model_name"] = (
                df_readings["serial_number"].map(sn_to_model).fillna("Неизвестно")
            )

            # Переименование и красивое форматирование таблицы
            df_readings_display = df_readings.rename(
                columns={
                    "serial_number": "Заводской номер",
                    "model_name": "Модель",
                    "timestamp": "Дата и время",
                    "reading_value": "Показания (кВт*ч)",
                    "notes": "Примечание",
                }
            )

            cols = [
                "Заводской номер",
                "Модель",
                "Дата и время",
                "Показания (кВт*ч)",
                "Примечание",
            ]
            df_readings_display = df_readings_display[
                [c for c in cols if c in df_readings_display.columns]
            ]

            if "Дата и время" in df_readings_display.columns:
                df_readings_display["Дата и время"] = pd.to_datetime(
                    df_readings_display["Дата и время"], errors="coerce"
                )
                df_readings_display["Дата и время"] = df_readings_display[
                    "Дата и время"
                ].dt.strftime("%Y-%m-%d %H:%M:%S")

            st.dataframe(
                df_readings_display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Показания (кВт*ч)": st.column_config.NumberColumn(
                        "Показания (кВт*ч)",
                        format="%.3f",
                    )
                },
            )

            # --- НАСТРОЙКА СТРОКИ НАВИГАЦИИ (ПАГИНАЦИЯ) ---
            st.markdown("---")
            col_nav, col_stats = st.columns(2)

            with col_nav:
                btn_start, btn_prev, btn_next, btn_end = st.columns(4)
                with btn_start:
                    if st.button("⏮️", key="nav_start", use_container_width=True):
                        st.session_state.readings_page = 1
                        st.rerun()
                with btn_prev:
                    if st.button("◀️", key="nav_prev", use_container_width=True):
                        if st.session_state.readings_page > 1:
                            st.session_state.readings_page -= 1
                            st.rerun()
                with btn_next:
                    if st.button("▶️", key="nav_next", use_container_width=True):
                        if st.session_state.readings_page < total_pages:
                            st.session_state.readings_page += 1
                            st.rerun()
                with btn_end:
                    if st.button("⏭️", key="nav_end", use_container_width=True):
                        st.session_state.readings_page = total_pages
                        st.rerun()

            with col_stats:
                st.markdown(
                    f"<p style='text-align: center; padding-top: 0.5rem; font-size: 14px; margin: 0; white-space: nowrap;'> "
                    f"Страница <b>{st.session_state.readings_page}</b> из <b>{total_pages}</b> &nbsp;|&nbsp; "
                    f"Найдено записей ведомости за период: <b>{total_rows}</b>"
                    f"</p>",
                    unsafe_allow_html=True,
                )
        else:
            st.info(
                f"📆 За выбранный период с {start_date} по {end_date} показаний в базе не найдено."
            )
