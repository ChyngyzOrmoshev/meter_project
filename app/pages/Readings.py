import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from db_client import readings_col, devices_col  # Подключение к MongoDB
from pymongo.errors import BulkWriteError
from pymongo import UpdateOne
import plotly.graph_objects as go

# Настройка страницы во всю ширину экрана
st.set_page_config(page_title="Сбор показаний", page_icon="📉", layout="wide")
st.markdown(
    """
    <style>
    /* Скрываем старые английские тексты */
    div[data-testid="stSidebarNav"] ul li a span { display: none !important; }
    
    /* 1. Главная страница */
    div[data-testid="stSidebarNav"] ul li:nth-child(1) a::after { content: "🏠 Главная"; font-weight: bold; }
    
    /* 2. Справочник моделей */
    div[data-testid="stSidebarNav"] ul li:nth-child(3) a::after { content: "📋 Справочник моделей"; }
    
    /* 3. Реестр устройств */
    div[data-testid="stSidebarNav"] ul li:nth-child(2) a::after { content: "🏭 Реестр приборов учета"; }
    
    /* 4. Сбор показаний */
    div[data-testid="stSidebarNav"] ul li:nth-child(4) a::after { content: "📉 Сбор показаний"; }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("📉 Сбор показаний")
st.markdown("---")

# Достаем роль пользователя, если она нужна для фильтров или скрытия кнопок
user_role = st.session_state.get("user_role", "user")

# # ====== ГРАФИК ПОКАЗАНИЙ ======

# st.subheader("📈 График потребления")

# # Получаем список серийных номеров для выбора (только те, у которых есть показания в MongoDB)
# serial_numbers_with_readings = readings_col.distinct("serial_number")
# if serial_numbers_with_readings:
#     # Выбор счётчика
#     selected_sn = st.selectbox("Выберите прибор:", serial_numbers_with_readings)
    
#     # Выбор периода
#     col_period1, col_period2 = st.columns(2)
#     with col_period1:
#         start_date = st.date_input("Начало периода", datetime.now() - timedelta(days=30), key="graph_start")
#     with col_period2:
#         end_date = st.date_input("Конец периода", datetime.now(), key="graph_end")
    
#     if start_date <= end_date:
#         # Запрос данных из MongoDB
#         start_dt = datetime.combine(start_date, datetime.min.time())
#         end_dt = datetime.combine(end_date, datetime.max.time())
        
#         # Используем агрегацию для группировки по дням (чтобы не было дубликатов часов)
#         pipeline = [
#             {"$match": {
#                 "serial_number": selected_sn,
#                 "timestamp": {"$gte": start_dt, "$lte": end_dt}
#             }},
#             {"$sort": {"timestamp": 1}},
#             # Если данных много, можно сгруппировать по дням, чтобы избежать перегрузки
#             # Но пока оставим все точки, пусть пользователь видит максимальную детализацию
#         ]
#         data = list(readings_col.aggregate(pipeline))
        
#         if data:
#             # Преобразуем в DataFrame для удобства
#             import pandas as pd
#             df_graph = pd.DataFrame(data)
#             df_graph = df_graph.sort_values("timestamp")
            
#             # Рисуем график с plotly
#             import plotly.graph_objects as go
#             fig = go.Figure()
#             fig.add_trace(go.Scatter(
#                 x=df_graph["timestamp"],
#                 y=df_graph["reading_value"],
#                 mode="lines+markers",
#                 name="Показания",
#                 line=dict(color="royalblue", width=2),
#                 marker=dict(size=4)
#             ))
#             fig.update_layout(
#                 title=f"Показания прибора {selected_sn}",
#                 xaxis_title="Дата",
#                 yaxis_title="Значение (кВт·ч)",
#                 template="plotly_white",
#                 hovermode="x unified",
#                 height=400,
#                 margin=dict(l=0, r=0, t=40, b=0)
#             )
#             st.plotly_chart(fig, use_container_width=True)
#         else:
#             st.info("Нет данных для выбранного периода.")
#     else:
#         st.error("Дата начала должна быть раньше даты окончания.")
# else:
#     st.info("Нет приборов с показаниями в системе.")

st.subheader("Ввод текущих показаний и история")

# Превращаем в set для мгновенного поиска O(1) при валидации массового ввода
all_serial_numbers = [str(sn).strip() for sn in devices_col.distinct("serial_number")]
all_serial_numbers_set = set(all_serial_numbers)

# 🔒 Ограничение доступа: Форму добавления видят только admin и operator
if user_role in ["admin", "operator"]:
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
                        serial_options = [str(sn) for sn in all_serial_numbers]
                        r_sn = st.selectbox(
                            "Заводской номер:",
                            options=serial_options,
                            index=None,
                            placeholder="Начните вводить номер...")
                        #r_sn = st.selectbox("Заводской номер:", all_serial_numbers)
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
                        lines = [line.strip() for line in br_text.split("\n") if line.strip()]
                        if lines:
                            updates = []
                            errors = []
                            not_found = []
                            full_dt = datetime.combine(br_date, datetime.min.time())

                            for line in lines:
                                parts = line.split()
                                if len(parts) < 2:
                                    errors.append(line)
                                    continue
                                serial_number = parts[0]
                                if serial_number not in all_serial_numbers_set:
                                    not_found.append(serial_number)
                                    continue
                                try:
                                    value = float(parts[1].replace(",", "."))
                                    updates.append(
                                        UpdateOne(
                                            {"serial_number": serial_number, "timestamp": full_dt},
                                            {
                                                "$set": {
                                                    "serial_number": serial_number,
                                                    "timestamp": full_dt,
                                                    "reading_value": value,
                                                    "notes": br_notes.strip(),
                                                }
                                            },
                                            upsert=True,
                                        )
                                    )
                                except ValueError:
                                    errors.append(line)

                            if updates:
                                try:
                                    result = readings_col.bulk_write(updates, ordered=False)
                                    st.success(f"✅ Добавлено/обновлено показаний: {result.modified_count + result.upserted_count}")
                                except BulkWriteError as e:
                                    st.warning("Часть показаний не записалась из-за конфликта уникальности, но остальные сохранены.")
                            else:
                                st.error("❌ Нет корректных показаний для загрузки")

                            if not_found:
                                st.warning(f"⚠️ Не найдены в реестре ({len(not_found)} шт.)")
                                st.code("\n".join(sorted(set(not_found))))
                            if errors:
                                st.error(f"❌ Ошибочный формат строк ({len(errors)} шт.)")
                                st.code("\n".join(errors))

# --- ЖУРНАЛ ПОКАЗАНИЙ С ВЫВОДОМ ИСТОРИИ ПО ДНЯМ ВНУТРИ ПЕРИОДА ---
st.markdown("### 📊 Журнал показаний")

# Получаем точную текущую дату на сервере
today_date = datetime.now().date()

# Инициализируем дефолтный период в памяти сессии строго ТЕКУЩИМ днем
if "filter_start_date" not in st.session_state:
    st.session_state.filter_start_date = today_date
if "filter_end_date" not in st.session_state:
    st.session_state.filter_end_date = today_date

# Размещаем элементы фильтрации в один ряд с учетом нового поля поиска и кнопки "✖"
col_btn, col_s, col_e, col_search, col_clear = st.columns([0.6, 1.2, 1.2, 2.5, 0.4])

with col_btn:
    st.write("")
    st.write("")
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
    start_date = st.date_input(
        "📅 Начало периода:",
        value=st.session_state.filter_start_date,
        key="start_date_input",
    )
    st.session_state.filter_start_date = start_date

with col_e:
    end_date = st.date_input(
        "📅 Конец периода:",
        value=st.session_state.filter_end_date,
        key="end_date_input",
    )
    st.session_state.filter_end_date = end_date

# Инициализируем значение текста поиска, если его еще нет в сессии
if "search_sn_input" not in st.session_state:
    st.session_state.search_sn_input = ""

with col_search:
    # Привязываем поле ввода напрямую к ключу "search_sn_input" через параметр key
    search_sn = st.text_input(
        "🔍 Поиск по заводскому номеру:", key="search_sn_input"
    ).strip()

# ФУНКЦИЯ ОЧИСТКИ: Срабатывает строго в момент клика по кнопке "✖" до перерисовки виджета
def clear_search_callback():
    st.session_state.search_sn_input = ""

with col_clear:
    st.write("")  # Вертикальное выравнивание под одну линию с полем ввода
    st.write("")
    # Кнопка-крестик активируется только если в поле поиска есть текст
    st.button(
        "✖",
        key="btn_clear_search",
        disabled=(not st.session_state.search_sn_input),
        use_container_width=True,
        help="Очистить поиск",
        on_click=clear_search_callback,  # Используем callback для безопасной очистки состояния
    )

# === ЗАЩИТА ОТ НЕОПРЕДЕЛЁННЫХ ДАТ ===
if start_date is None:
    start_date = today_date
    st.session_state.filter_start_date = today_date
if end_date is None:
    end_date = today_date
    st.session_state.filter_end_date = today_date

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
        with st.spinner("⏳ Загрузка журнала показаний..."):
            start_idx = (st.session_state.readings_page - 1) * rows_per_page

            readings_pipeline = [
                {"$match": match_stage},
                {"$sort": {"timestamp": -1}},
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
                        },
                        "latest_hour_doc": {"$first": "$$ROOT"},
                    }
                },
                {"$replaceRoot": {"newRoot": "$latest_hour_doc"}},
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
