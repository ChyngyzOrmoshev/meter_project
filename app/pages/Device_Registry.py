import streamlit as st
import pandas as pd
from datetime import datetime
from db_client import devices_col, models_col
from pymongo.errors import DuplicateKeyError

st.set_page_config(page_title="Реестр устройств", page_icon="🏭", layout="wide")
st.markdown(
    """
    <style>
    div[data-testid="stSidebarNav"] ul li a span { display: none !important; }
    div[data-testid="stSidebarNav"] ul li:nth-child(1) a::after { content: "🏠 Главная"; font-weight: bold; }
    div[data-testid="stSidebarNav"] ul li:nth-child(3) a::after { content: "📋 Справочник моделей"; }
    div[data-testid="stSidebarNav"] ul li:nth-child(2) a::after { content: "🏭 Реестр приборов учета"; }
    div[data-testid="stSidebarNav"] ul li:nth-child(4) a::after { content: "📉 Сбор показаний"; }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("🏭 Реестр приборов учета")
st.markdown("---")

user_role = st.session_state.get("user_role", "user")

# ====== РАЗДЕЛ ДОБАВЛЕНИЯ УСТРОЙСТВ (без изменений) ======
st.subheader("Реестр зарегистрированных приборов учета")

if user_role in ["admin", "operator"]:
    with st.expander("➕ Добавить новые приборы в базу данных"):
        add_type = st.radio(
            "Способ ввода:",
            ["По одному", "Группой (Массовый ввод)"],
            horizontal=True,
        )

        all_models = []
        try:
            models_cursor = models_col.find({}, {"model_name": 1, "nominal_current": 1, "_id": 0})
            for m in models_cursor:
                name = str(m.get("model_name", "")).strip()
                current = str(m.get("nominal_current", "")).strip()
                if name:
                    full_model_string = f"{name} [{current}]" if current else name
                    if full_model_string not in all_models:
                        all_models.append(full_model_string)
            all_models = sorted(all_models)
        except Exception as e:
            st.error(f"Ошибка загрузки моделей для формы: {e}")

        if not all_models:
            all_models = ["Справочник пуст (заполните модели)"]

        if add_type == "По одному":
            with st.form("single_device_form", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    sn = st.text_input("Заводской номер:", disabled=False).strip()
                with col2:
                    selected_model_str = st.selectbox("Модель счетчика (и ток):", all_models, disabled=False)
                submit_btn = st.form_submit_button("💾 Зарегистрировать")
                if submit_btn:
                    if not sn:
                        st.error("❌ Заводской номер прибора не может быть пустым!")
                    else:
                        try:
                            if " [" in selected_model_str:
                                pure_model_name = selected_model_str.split(" [")[0].strip()
                            else:
                                pure_model_name = selected_model_str.strip()
                            model_info = models_col.find_one({"model_name": pure_model_name})
                            if model_info:
                                phase_val = model_info.get("phase")
                                askue_id_val = model_info.get("askue_id")
                                api_id_val = model_info.get("api_identifier")
                                nominal_current = model_info.get("nominal_current")
                            else:
                                phase_val = 1
                                askue_id_val = None
                                api_id_val = None
                                nominal_current = "5(80)"
                            device_document = {
                                "serial_number": sn,
                                "model_name": pure_model_name,
                                "nominal_current": nominal_current,
                                "status": "active",
                                "phase": phase_val,
                                "askue_id": askue_id_val,
                                "api_id": api_id_val,
                                "created_at": datetime.now()
                            }
                            try:
                                devices_col.insert_one(device_document)
                                st.success(f"🎉 Прибор № {sn} успешно добавлен в систему!")
                                st.rerun()
                            except DuplicateKeyError:
                                st.warning(f"⚠️ Прибор № {sn} уже зарегистрирован в реестре!")
                            except Exception as e:
                                st.error(f"Ошибка добавления: {e}")
                        except Exception as e:
                            st.error(f"❌ Ошибка при сохранении прибора: {e}")

        elif add_type == "Группой (Массовый ввод)":
            from pymongo.errors import BulkWriteError
            with st.form("bulk_device_form", clear_on_submit=True):
                b_selected_model_str = st.selectbox("Модель (и ток) для всей группы:", all_models)
                b_status = st.selectbox("Статус для всей группы:", ["active", "repair", "inactive"])
                b_sns = st.text_area("Вставьте список заводских номеров (каждый с новой строки):")
                submit = st.form_submit_button("Зарегистрировать группу")
                if submit:
                    sn_list = [s.strip() for s in b_sns.split("\n") if s.strip()]
                    if not sn_list:
                        st.error("Список пуст")
                        st.stop()
                    unique_sn_list = list(dict.fromkeys(sn_list))
                    b_model_name = b_selected_model_str.split(" [")[0]
                    b_nominal_current = (
                        b_selected_model_str.split(" [")[1].replace("]", "")
                        if " [" in b_selected_model_str
                        else ""
                    )
                    existing = set(devices_col.distinct("serial_number", {"serial_number": {"$in": unique_sn_list}}))
                    new_sns = [s for s in unique_sn_list if s not in existing]
                    duplicates = [s for s in unique_sn_list if s in existing]
                    if new_sns:
                        docs = [{
                            "serial_number": sn,
                            "model_name": b_model_name,
                            "nominal_current": b_nominal_current,
                            "status": b_status,
                            "created_at": datetime.now()
                        } for sn in new_sns]
                        result = devices_col.insert_many(docs)
                        st.success(f"✅ Добавлено новых приборов: {len(result.inserted_ids)}")
                    if duplicates:
                        st.warning(f"⚠️ Уже существуют ({len(duplicates)}):")
                        st.code("\n".join(duplicates))
                    st.rerun()

# ====== ИНТЕРФЕЙС ФИЛЬТРАЦИИ ======
f1, f2, f3 = st.columns(3)
with f1:
    s_search = st.text_input("🔍 Поиск по заводскому номеру:").strip()
with f2:
    s_filter = st.selectbox("Фильтр по статусу:", ["Все", "active", "repair", "inactive"])
with f3:
    pure_models = sorted(list(models_col.distinct("model_name")))
    m_filter = st.selectbox("Фильтр по модели:", ["Все"] + pure_models)

# Формируем фильтр
query = {}
if s_search:
    query["serial_number"] = {"$regex": s_search, "$options": "i"}
if s_filter != "Все":
    query["status"] = s_filter
if m_filter != "Все":
    query["model_name"] = m_filter

# ====== ПАГИНАЦИЯ ======
# Параметры пагинации
PAGE_SIZE = 20  # Количество записей на странице
if "devices_page" not in st.session_state:
    st.session_state.devices_page = 1

# Получаем общее количество записей по фильтру
total_count = devices_col.count_documents(query)
total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)

# Корректируем текущую страницу, если она выходит за границы
if st.session_state.devices_page > total_pages:
    st.session_state.devices_page = total_pages
if st.session_state.devices_page < 1:
    st.session_state.devices_page = 1

# Вычисляем смещение для пропуска
skip = (st.session_state.devices_page - 1) * PAGE_SIZE

# Получаем записи для текущей страницы (сортировка по серийному номеру)
devices_cursor = devices_col.find(query, {"_id": 0}).sort("serial_number", 1).skip(skip).limit(PAGE_SIZE)
devices = list(devices_cursor)

# ====== ВЫВОД ТАБЛИЦЫ ======
if devices:
    df_devices = pd.DataFrame(devices)
    if "nominal_current" not in df_devices.columns:
        df_devices["nominal_current"] = ""
    df_devices["nominal_current"] = df_devices["nominal_current"].fillna("")

    # Подгружаем метаданные моделей
    df_m_meta = pd.DataFrame(list(
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
    ))
    if not df_m_meta.empty:
        df_m_meta["nominal_current"] = df_m_meta["nominal_current"].fillna("")
        final_df = pd.merge(df_devices, df_m_meta, on=["model_name", "nominal_current"], how="left")
    else:
        final_df = df_devices

    final_df = final_df.rename(columns={
        "serial_number": "Заводской номер",
        "model_name": "Модель",
        "nominal_current": "Номинальный ток",
        "status": "Статус прибора",
        "device_type_id": "Код АСКУЭ (ID)",
        "device_type_str": "Идентификатор API",
        "phases": "Фазность",
    })

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

    # ====== НАВИГАЦИЯ ПО СТРАНИЦАМ ======
    st.markdown("---")
    col_nav1, col_nav2, col_nav3 = st.columns([1, 3, 1])
    with col_nav2:
        # Номера страниц с кнопками
        col_btn1, col_btn2, col_btn3, col_btn4 = st.columns(4)
        with col_btn1:
            if st.button("⏮️ Первая", use_container_width=True):
                st.session_state.devices_page = 1
                st.rerun()
        with col_btn2:
            if st.button("◀️ Назад", use_container_width=True):
                if st.session_state.devices_page > 1:
                    st.session_state.devices_page -= 1
                    st.rerun()
        with col_btn3:
            if st.button("Вперед ▶️", use_container_width=True):
                if st.session_state.devices_page < total_pages:
                    st.session_state.devices_page += 1
                    st.rerun()
        with col_btn4:
            if st.button("Последняя ⏭️", use_container_width=True):
                st.session_state.devices_page = total_pages
                st.rerun()

        # Индикатор текущей страницы
        st.markdown(
            f"<p style='text-align: center; margin-top: 0.5rem;'>Страница <b>{st.session_state.devices_page}</b> из <b>{total_pages}</b> (всего записей: {total_count})</p>",
            unsafe_allow_html=True
        )
else:
    st.info("Приборы с указанными параметрами не найдены.")

# ====== РАЗДЕЛ УДАЛЕНИЯ (без изменений) ======
st.markdown("---")
if "delete_step2" not in st.session_state:
    st.session_state.delete_step2 = False
if "delete_target_sn" not in st.session_state:
    st.session_state.delete_target_sn = ""

if user_role == "admin":
    with st.expander("🗑️ Удалить прибор учета из базы данных"):
        st.warning("⚠️ Внимание: Удаление прибора сотрет его карту из реестра. Данные показаний в аналитике останутся.")
        col_del1, col_del2 = st.columns(2)
        with col_del1:
            search_sn_to_delete = st.text_input("Введите или вставьте заводской номер для удаления:", key="sn_delete_input").strip()
        with col_del2:
            st.markdown("<br>", unsafe_allow_html=True)
            initiate_delete = st.button("❌ Удалить прибор", use_container_width=True)
        if initiate_delete:
            if not search_sn_to_delete:
                st.error("❌ Пожалуйста, введите заводской номер прибора!")
                st.session_state.delete_step2 = False
            else:
                device_to_check = devices_col.find_one({"serial_number": search_sn_to_delete})
                if not device_to_check:
                    st.error(f"❌ Прибор с заводским номером № {search_sn_to_delete} не найден в реестре!")
                    st.session_state.delete_step2 = False
                else:
                    st.session_state.delete_target_sn = search_sn_to_delete
                    st.session_state.delete_step2 = True

        if st.session_state.delete_step2:
            st.markdown("---")
            st.error(f"❓ **Вы действительно уверены, что хотите безвозвратно удалить прибор № {st.session_state.delete_target_sn}?**")
            c_btn1, c_btn2, c_btn3 = st.columns(3)
            with c_btn1:
                final_confirm = st.button("🔥 Да, удалить", type="primary", use_container_width=True)
            with c_btn2:
                cancel_delete = st.button("🚫 Отмена", use_container_width=True)
            if final_confirm:
                try:
                    result = devices_col.delete_one({"serial_number": st.session_state.delete_target_sn})
                    if result.deleted_count > 0:
                        st.success(f"🎉 ОТВЕТ СИСТЕМЫ: Прибор № {st.session_state.delete_target_sn} успешно и окончательно удален из базы данных!")
                        st.session_state.delete_step2 = False
                        st.session_state.delete_target_sn = ""
                        st.rerun()
                    else:
                        st.error("Ошибка при выполнении операции удаления в MongoDB.")
                except Exception as e:
                    st.error(f"❌ Критическая ошибка: {e}")
            if cancel_delete:
                st.session_state.delete_step2 = False
                st.session_state.delete_target_sn = ""
                st.info("Операция удаления успешно отменена пользователем.")
                st.rerun()