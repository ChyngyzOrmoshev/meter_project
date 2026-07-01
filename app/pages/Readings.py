import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from db_client import readings_col, devices_col, models_col
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

def show():
    st.title("📉 Сбор показаний")
    st.markdown("---")

    user_role = st.session_state.get("user_role", "user")

    # ===== ПОИСК СЧЁТЧИКА =====
    st.subheader("🔍 Поиск прибора учета")
    with st.form(key="search_form"):
        search_sn = st.text_input("Введите заводской номер:", placeholder="Например, 084600001431")
        search_submitted = st.form_submit_button("Найти")

    selected_sn = None
    device_info = None
    model_info = None

    if search_submitted and search_sn:
        device_info = devices_col.find_one({"serial_number": search_sn.strip()})
        if device_info:
            selected_sn = search_sn.strip()
            model_info = models_col.find_one({"model_name": device_info.get("model_name")})
        else:
            st.warning(f"Прибор с номером '{search_sn}' не найден в реестре.")

    if selected_sn and device_info:
        if st.button("🔁 Сброс поиска"):
            st.rerun()

        st.markdown("---")
        st.subheader(f"📋 Информация о приборе {selected_sn}")

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Модель", device_info.get("model_name", "—"))
        col2.metric("Статус", device_info.get("status", "—"))
        col3.metric("Номинальный ток", device_info.get("nominal_current", "—"))
        col4.metric("Фазность", model_info.get("phases", "—") if model_info else "—")

        with st.expander("🔧 Подробные параметры"):
            if device_info:
                st.json({
                    "Серийный номер": device_info.get("serial_number"),
                    "Модель": device_info.get("model_name"),
                    "Ток": device_info.get("nominal_current"),
                    "Статус": device_info.get("status"),
                    "Фазность": model_info.get("phases") if model_info else None,
                    "Код АСКУЭ": model_info.get("device_type_id") if model_info else None,
                    "Идентификатор API": model_info.get("device_type_str") if model_info else None,
                    "Дата добавления": device_info.get("created_at").strftime("%Y-%m-%d %H:%M:%S") if device_info.get("created_at") else None
                })
            else:
                st.warning("Данные о приборе не найдены.")

        tab1, tab2, tab3 = st.tabs(["📊 Показания", "📈 График", "📋 Параметры"])

        with tab1:
            st.subheader("Журнал показаний")
            col_date1, col_date2 = st.columns(2)
            with col_date1:
                start_date = st.date_input("Начало периода", datetime.now() - timedelta(days=7), key="start_tab1")
            with col_date2:
                end_date = st.date_input("Конец периода", datetime.now(), key="end_tab1")

            if start_date and end_date and start_date <= end_date:
                start_dt = datetime.combine(start_date, datetime.min.time())
                end_dt = datetime.combine(end_date, datetime.max.time())

                readings_cursor = readings_col.find(
                    {"serial_number": selected_sn, "timestamp": {"$gte": start_dt, "$lte": end_dt}},
                    {"_id": 0, "timestamp": 1, "reading_value": 1, "notes": 1}
                ).sort("timestamp", -1)

                df_readings = pd.DataFrame(list(readings_cursor))
                if not df_readings.empty:
                    df_readings["timestamp"] = pd.to_datetime(df_readings["timestamp"]).dt.strftime("%Y-%m-%d %H:%M:%S")
                    df_readings = df_readings.rename(columns={
                        "timestamp": "Дата и время",
                        "reading_value": "Показания (кВт·ч)",
                        "notes": "Примечание"
                    })
                    st.dataframe(df_readings, use_container_width=True, hide_index=True)
                    st.caption(f"Всего записей: {len(df_readings)}")
                else:
                    st.info("Нет показаний за выбранный период.")
            else:
                st.warning("Некорректный период.")

        with tab2:
            st.subheader("График показаний")
            col_date3, col_date4 = st.columns(2)
            with col_date3:
                graph_start = st.date_input("Начало", datetime.now() - timedelta(days=30), key="start_graph")
            with col_date4:
                graph_end = st.date_input("Конец", datetime.now(), key="end_graph")

            if graph_start and graph_end and graph_start <= graph_end:
                start_g = datetime.combine(graph_start, datetime.min.time())
                end_g = datetime.combine(graph_end, datetime.max.time())

                readings_cursor = readings_col.find(
                    {"serial_number": selected_sn, "timestamp": {"$gte": start_g, "$lte": end_g}},
                    {"_id": 0, "timestamp": 1, "reading_value": 1}
                ).sort("timestamp", 1)

                df_graph = pd.DataFrame(list(readings_cursor))
                if not df_graph.empty:
                    import plotly.graph_objects as go
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=df_graph["timestamp"],
                        y=df_graph["reading_value"],
                        mode="lines+markers",
                        name=selected_sn,
                        line=dict(color="royalblue", width=2)
                    ))
                    fig.update_layout(
                        title=f"Показания прибора {selected_sn}",
                        xaxis_title="Дата",
                        yaxis_title="Значение (кВт·ч)",
                        template="plotly_white",
                        hovermode="x unified",
                        height=400
                    )
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Нет данных для построения графика за выбранный период.")
            else:
                st.warning("Некорректный период.")

        with tab3:
            st.subheader("Технические параметры")
            if model_info:
                st.json({
                    "Наименование модели": model_info.get("model_name"),
                    "Значность": model_info.get("digit_capacity"),
                    "Фазность": model_info.get("phases"),
                    "Номинальный ток": model_info.get("nominal_current"),
                    "Номинальное напряжение": model_info.get("nominal_voltage"),
                    "Тип прибора": model_info.get("system_type"),
                    "Период": model_info.get("period"),
                    "Тип АСКУЭ": model_info.get("device_type_id"),
                    "Идентификатор API": model_info.get("device_type_str")
                })
            else:
                st.warning("Модель не найдена в справочнике.")

    else:
        if search_submitted and search_sn:
            pass
        else:
            st.info("👆 Введите заводской номер и нажмите 'Найти' для просмотра данных прибора.")

    # ===== БЛОК РУЧНОГО ВВОДА ПОКАЗАНИЙ =====
    st.markdown("---")
    if user_role in ["admin", "operator"]:
        with st.expander("✏️ Внести новые показания вручную"):
            all_serial_numbers = sorted([str(sn).strip() for sn in devices_col.distinct("serial_number")])
            if not all_serial_numbers:
                st.warning("Нет зарегистрированных приборов.")
            else:
                with st.form("manual_reading_form"):
                    col_sn, col_val, col_date = st.columns(3)
                    with col_sn:
                        manual_sn = st.selectbox("Заводской номер", all_serial_numbers)
                    with col_val:
                        manual_val = st.number_input("Показание", min_value=0.0, step=0.1)
                    with col_date:
                        manual_date = st.date_input("Дата", datetime.now())
                    manual_notes = st.text_input("Примечание")
                    if st.form_submit_button("Сохранить"):
                        full_dt = datetime.combine(manual_date, datetime.min.time())
                        try:
                            readings_col.insert_one({
                                "serial_number": manual_sn,
                                "timestamp": full_dt,
                                "reading_value": manual_val,
                                "notes": manual_notes.strip()
                            })
                            st.success("Показание сохранено!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Ошибка: {e}")

if __name__ == "__main__":
    show()