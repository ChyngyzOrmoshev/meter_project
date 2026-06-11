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
@st.cache_data(ttl=300)  # Кэшируем списки на 5 минут, чтобы не перегружать MongoDB
def load_cached_dropdowns():
    models = sorted(list(models_col.distinct("model_name")))
    serial_numbers = sorted(list(devices_col.distinct("serial_number")))
    return models, serial_numbers


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
                        model = st.selectbox("Модель счетчика:", all_models)
                    with col3:
                        status = st.selectbox(
                            "Статус прибора:", ["active", "repair", "inactive"]
                        )
                    if st.form_submit_button("Сохранить"):
                        if sn:
                            devices_col.update_one(
                                {"serial_number": sn},
                                {
                                    "$set": {
                                        "serial_number": sn,
                                        "model_name": model,
                                        "status": status,
                                    }
                                },
                                upsert=True,
                            )
                            # Сбрасываем кэш выпадающих списков, так как появился новый прибор
                            st.cache_data.clear()
                            st.success(f"Счетчик {sn} добавлен!")
                            st.rerun()
                        else:
                            st.error("Введите заводской номер!")

            elif add_type == "Группой (Массовый ввод)":
                with st.form("bulk_device_form", clear_on_submit=True):
                    b_model = st.selectbox("Модель для всей группы:", all_models)
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
                        if sn_list:
                            # Оптимизация: Собираем операции в пакет
                            operations = [
                                UpdateOne(
                                    {"serial_number": s_num},
                                    {
                                        "$set": {
                                            "serial_number": s_num,
                                            "model_name": b_model,
                                            "status": b_status,
                                        }
                                    },
                                    upsert=True,
                                )
                                for s_num in sn_list
                            ]
                            # Отправляем в MongoDB одним быстрым запросом
                            devices_col.bulk_write(operations)
                            # Сбрасываем кэш списков
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
        m_filter = st.selectbox(
            "Фильтр по модели:",
            ["Все"] + all_models,
        )

    # Оптимизация: Формируем запрос фильтрации строго на стороне MongoDB
    query = {}
    if s_search:
        query["serial_number"] = {"$regex": s_search, "$options": "i"}
    if s_filter != "Все":
        query["status"] = s_filter
    if m_filter != "Все":
        query["model_name"] = m_filter

    # Запрашиваем из базы только те документы, которые подходят под фильтры
    devices = list(devices_col.find(query, {"_id": 0}))

    if devices:
        df_devices = pd.DataFrame(devices)

        # Легковесный запрос метаданных моделей (только нужные поля)
        df_m_meta = pd.DataFrame(
            list(
                models_col.find(
                    {},
                    {
                        "_id": 0,
                        "model_name": 1,
                        "device_type_id": 1,
                        "device_type_str": 1,
                        "phases": 1,
                    },
                )
            )
        )

        final_df = (
            pd.merge(df_devices, df_m_meta, on="model_name", how="left")
            if not df_m_meta.empty
            else df_devices
        )

        final_df = final_df.rename(
            columns={
                "serial_number": "Заводской номер",
                "model_name": "Модель",
                "status": "Статус прибора",
                "device_type_id": "Код АСКУЭ (ID)",
                "device_type_str": "Идентификатор API",
                "phases": "Фазность",
            }
        )

        st.dataframe(final_df, use_container_width=True, hide_index=True)
    else:
        st.info("Приборы с указанными параметрами не найдены.")

# ==============================================================================
# === РАЗДЕЛ 3: СБОР ПОКАЗАНИЙ (ИСПРАВЛЕН ФОРМАТ ДАТЫ) ===
# ==============================================================================
elif page == "📉 Сбор показаний (Readings)":
    st.subheader("Ввод текущих показаний и история")

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
                            full_dt = datetime.combine(r_date, datetime.now().time())
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
                                success_cnt = 0
                                full_dt = datetime.combine(
                                    br_date, datetime.now().time()
                                )
                                for line in lines:
                                    parts = line.split()
                                    if len(parts) >= 2 and parts in all_serial_numbers:
                                        try:
                                            readings_col.insert_one(
                                                {
                                                    "serial_number": parts,
                                                    "timestamp": full_dt,
                                                    "reading_value": float(
                                                        parts.replace(",", ".")
                                                    ),
                                                    "notes": br_notes.strip(),
                                                }
                                            )
                                            success_cnt += 1
                                        except ValueError:
                                            pass
                                st.success(
                                    f"Успешно внесено показаний: {success_cnt} шт."
                                )
                                st.rerun()

    # --- ЖУРНАЛ ПОКАЗАНИЙ С СОВМЕЩЕННЫМ ФИЛЬТРОМ ---
    st.markdown("### 📊 Журнал показаний")

    # 1. Сначала пользователь выбирает дату ведомости
    selected_filter_date = st.date_input(
        "📅 Выберите дату для просмотра ведомости за день:", datetime.now().date()
    )

    # 2. Поле текстового поиска по заводскому номеру
    search_sn = st.text_input(
        "🔍 Быстрый поиск по заводскому номеру внутри выбранного дня:", ""
    ).strip()

    # Рассчитываем строгие временные границы выбранных суток
    filter_start = datetime.combine(selected_filter_date, datetime.min.time())
    filter_end = datetime.combine(selected_filter_date, datetime.max.time())

    # Формируем базовый запрос к MongoDB: фильтр по дате обязателен всегда!
    mongo_query = {"timestamp": {"$gte": filter_start, "$lte": filter_end}}

    # Если введён заводской номер — добавляем его в этот же запрос как второе условие
    if search_sn:
        mongo_query["serial_number"] = {"$regex": search_sn, "$options": "i"}

    # Сверхбыстрый совмещенный запрос в MongoDB
    readings = list(readings_col.find(mongo_query, {"_id": 0}))

    if readings:
        df_readings = pd.DataFrame(readings)
        dev_meta = list(
            devices_col.find({}, {"_id": 0, "serial_number": 1, "model_name": 1})
        )
        df_readings = (
            pd.merge(
                df_readings, pd.DataFrame(dev_meta), on="serial_number", how="left"
            )
            if dev_meta
            else df_readings
        )

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
            df_readings_display = df_readings_display.sort_values(
                by="Дата и время", ascending=False
            )

        # --- НАСТРОЙКА СТРОКИ НАВИГАЦИИ (ПАГИНАЦИЯ) ---
        total_rows = len(df_readings_display)
        rows_per_page = 10
        total_pages = max(1, (total_rows + rows_per_page - 1) // rows_per_page)

        if "readings_page" not in st.session_state:
            st.session_state.readings_page = 1

        if st.session_state.readings_page > total_pages:
            st.session_state.readings_page = 1

        start_idx = (st.session_state.readings_page - 1) * rows_per_page
        end_idx = start_idx + rows_per_page
        paginated_df = df_readings_display.iloc[start_idx:end_idx]

        # Выводим таблицу результатов
        st.dataframe(paginated_df, use_container_width=True, hide_index=True)

        # Вывод кнопок пагинации внизу
        st.markdown("---")
        col_nav, col_stats = st.columns(2)

        with col_nav:
            btn_start, btn_prev, btn_next, btn_end = st.columns(4)
            with btn_start:
                if st.button(
                    "⏮️",
                    help="В самое начало",
                    disabled=(st.session_state.readings_page == 1),
                    use_container_width=True,
                ):
                    st.session_state.readings_page = 1
                    st.rerun()
            with btn_prev:
                if st.button(
                    "◀️",
                    help="Назад",
                    disabled=(st.session_state.readings_page == 1),
                    use_container_width=True,
                ):
                    st.session_state.readings_page -= 1
                    st.rerun()
            with btn_next:
                if st.button(
                    "▶️",
                    help="Вперед",
                    disabled=(st.session_state.readings_page == total_pages),
                    use_container_width=True,
                ):
                    st.session_state.readings_page += 1
                    st.rerun()
            with btn_end:
                if st.button(
                    "⏭️",
                    help="В самый конец",
                    disabled=(st.session_state.readings_page == total_pages),
                    use_container_width=True,
                ):
                    st.session_state.readings_page = total_pages
                    st.rerun()

        with col_stats:
            st.markdown(
                f"<p style='text-align: center; padding-top: 0.5rem; font-size: 14px; margin: 0; white-space: nowrap;'> "
                f"Страница <b>{st.session_state.readings_page}</b> из <b>{total_pages}</b> &nbsp;|&nbsp; "
                f"Найдено записей за день: <b>{total_rows}</b>"
                f"</p>",
                unsafe_allow_html=True,
            )
    else:
        st.info(
            f"📆 За выбранную дату ({selected_filter_date}) показаний с такими параметрами в базе не найдено."
        )
