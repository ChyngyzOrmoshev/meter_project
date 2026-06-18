import os
import streamlit as st
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from db_client import sync_status_col, devices_col, readings_col, models_col
from logger_config import logger

st.set_page_config(page_title="ИнфоЭнерго", page_icon="⚡", layout="wide")

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

# Загружаем переменные окружения
load_dotenv()
required_vars = ["WEB_ADMIN_PASSWORD", "WEB_OPERATOR_PASSWORD", "WEB_USER_PASSWORD"]
for var in required_vars:
    if not os.getenv(var):
        st.error(f"Не найдена переменная: {var}")
        st.stop()

USER_CREDENTIALS = {
    "admin": {"password": os.getenv("WEB_ADMIN_PASSWORD"), "role": "admin"},
    "operator": {"password": os.getenv("WEB_OPERATOR_PASSWORD"), "role": "operator"},
    "user": {"password": os.getenv("WEB_USER_PASSWORD"), "role": "user"},
}

# Инициализация состояния
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_role" not in st.session_state:
    st.session_state.user_role = None
if "username" not in st.session_state:
    st.session_state.username = None

# Форма входа
if not st.session_state.logged_in:
    st.title("Авторизация")
    with st.form("login_form"):
        username = st.text_input("Логин:").strip().lower()
        password = st.text_input("Пароль:", type="password")
        if st.form_submit_button("Войти"):
            if username in USER_CREDENTIALS and USER_CREDENTIALS[username]["password"] == password:
                st.session_state.logged_in = True
                st.session_state.user_role = USER_CREDENTIALS[username]["role"]
                st.session_state.username = username
                logger.info(f"Пользователь {username} вошёл в систему")
                st.rerun()
            else:
                logger.warning(f"Неудачная попытка входа для пользователя {username}")
                st.error("Неверный логин или пароль")
    st.stop()

# Главный экран после входа
col1, col2 = st.columns([6, 1])
with col2:
    if st.button("Выйти"):
        logger.info(f"Пользователь {st.session_state.username} вышел из системы")
        st.session_state.logged_in = False
        st.session_state.user_role = None
        st.session_state.username = None
        st.rerun()
with col1:
    st.title("⚡ ИнфоЭнерго")
    st.markdown(f"Добро пожаловать, **{st.session_state.username}** (роль: `{st.session_state.user_role}`)")

# === СВОДНАЯ ИНФОРМАЦИЯ (ДАШБОРД) ===
st.markdown("---")
st.subheader("📊 Аналитика по приборам учета")

# Общее количество приборов
total_devices = devices_col.count_documents({})

# Активность сегодня (уникальные приборы с показаниями)
today_pipeline = [
    {
        "$match": {
            "$expr": {
                "$eq": [
                    { "$dateToString": { "format": "%Y-%m-%d", "date": "$timestamp", "timezone": "Asia/Bishkek" } },
                    datetime.now().strftime("%Y-%m-%d")
                ]
            }
        }
    },
    { "$group": { "_id": "$serial_number" } },
    { "$count": "active_today" }
]
result = list(readings_col.aggregate(today_pipeline))
active_today = result[0]["active_today"] if result else 0
active_percent = (active_today / total_devices * 100) if total_devices > 0 else 0

# Ручные показания за сегодня (исключаем все автоматические источники)
auto_notes = [
    "Авто-сбор: База cEnergo",
    "Авто-сбор: Sanxing_old",
    "Авто-сбор: SunRise",
    "Hexing KUK",
    "RiseSun"
]
today_str = datetime.now().strftime("%Y-%m-%d")

manual_pipeline = [
    {
        "$match": {
            "$expr": {
                "$eq": [
                    { "$dateToString": { "format": "%Y-%m-%d", "date": "$timestamp", "timezone": "Asia/Bishkek" } },
                    today_str
                ]
            },
            "$or": [
                { "notes": { "$exists": False } },
                { "notes": "" },
                { "notes": { "$nin": auto_notes } }
            ]
        }
    },
    { "$count": "manual_count" }
]
manual_result = list(readings_col.aggregate(manual_pipeline))
manual_today = manual_result[0]["manual_count"] if manual_result else 0

# Метрики в четыре колонки
col_m1, col_m2, col_m3, col_m4 = st.columns(4)
col_m1.metric("🏭 Всего приборов", total_devices)
col_m2.metric("📊 Передали показания сегодня", active_today)
col_m3.metric("📈 Активность за сегодня", f"{active_percent:.1f}%")
col_m4.metric("✏️ Ручных показаний сегодня", manual_today)

# === АНАЛИТИКА ПО ПРОИЗВОДИТЕЛЯМ ===
st.markdown("---")
st.subheader("📊 Активность по производителям")
with st.spinner("Загрузка данных по типам..."):
    devices_df = pd.DataFrame(list(devices_col.find({}, {"_id": 0, "serial_number": 1, "model_name": 1})))
    models_df = pd.DataFrame(list(models_col.find({}, {"_id": 0, "model_name": 1, "device_type_str": 1, "device_type_id": 1})))
    models_df["device_type_str"] = models_df["device_type_str"].fillna("Неизвестно")
    models_df["device_type_id"] = models_df["device_type_id"].fillna("").astype(str)
    merged_df = devices_df.merge(models_df, on="model_name", how="left")
    merged_df["device_type_str"] = merged_df["device_type_str"].fillna("Неизвестно")
    merged_df["device_type_id"] = merged_df["device_type_id"].fillna("")

    producer_mapping = {"ST": "Star", "HX": "Hexing", "SR": "SunRise", "UK": "Hexing KUK", "RS": "RiseSun", "EM": "Energomera", "8": "Other"}
    def get_producer(row):
        type_str = row.get("device_type_str", "").strip()
        type_id = row.get("device_type_id", "").strip()
        if type_str == "SX":
            return "Sanxing_new" if type_id == "18" else "Sanxing_old" if type_id == "22" else "Sanxing (unknown)"
        return producer_mapping.get(type_str, type_str)
    merged_df["Производитель"] = merged_df.apply(get_producer, axis=1)

    type_stats = merged_df.groupby("Производитель").size().reset_index(name="total_devices")
    active_sns_pipeline = [{"$match": {"$expr": {"$eq": [{ "$dateToString": { "format": "%Y-%m-%d", "date": "$timestamp", "timezone": "Asia/Bishkek" } }, datetime.now().strftime("%Y-%m-%d")]}}}, {"$group": {"_id": "$serial_number"}}]
    active_sns = [doc["_id"] for doc in readings_col.aggregate(active_sns_pipeline)]
    active_sns_set = set(active_sns)
    merged_df["active_today"] = merged_df["serial_number"].apply(lambda x: x in active_sns_set)
    active_stats = merged_df.groupby("Производитель")["active_today"].sum().reset_index(name="active_devices")
    final_stats = type_stats.merge(active_stats, on="Производитель", how="left")
    final_stats["active_devices"] = final_stats["active_devices"].fillna(0).astype(int)
    final_stats["activity_percent"] = (final_stats["active_devices"] / final_stats["total_devices"] * 100).round(1)
    final_stats = final_stats.sort_values("activity_percent", ascending=False)
    st.dataframe(final_stats.rename(columns={"Производитель": "Производитель", "total_devices": "Всего приборов", "active_devices": "Активных сегодня", "activity_percent": "Активность %"}), use_container_width=True, hide_index=True)

# === СТАТУСЫ СИНХРОНИЗАТОРОВ ===
st.markdown("---")
st.subheader("🤖 Статус синхронизаторов")
statuses = list(sync_status_col.find({}, {"_id": 0}).sort("last_update", -1))
if statuses:
    df_status = pd.DataFrame(statuses)
    from zoneinfo import ZoneInfo
    local_tz = ZoneInfo('Asia/Bishkek')
    df_status["last_update"] = pd.to_datetime(df_status["last_update"]).dt.tz_localize('UTC').dt.tz_convert(local_tz).dt.strftime("%Y-%m-%d %H:%M:%S")
    df_status = df_status.rename(columns={"robot_name": "Робот", "status": "Статус", "last_update": "Последнее обновление", "records_processed": "Записей обработано", "error": "Ошибка"})
    if "Ошибка" not in df_status.columns: df_status["Ошибка"] = ""
    if "Записей обработано" not in df_status.columns: df_status["Записей обработано"] = 0
    else: df_status["Записей обработано"] = df_status["Записей обработано"].fillna(0).astype(int)
    def color_status(val):
        return "🟢" if val == "success" else "🟡" if val == "running" else "🔴" if val == "error" else "⚪"
    df_status["Статус"] = df_status["Статус"].apply(color_status) + " " + df_status["Статус"]
    st.dataframe(df_status[["Робот", "Статус", "Последнее обновление", "Записей обработано", "Ошибка"]], use_container_width=True, hide_index=True)
else:
    st.info("Статусы синхронизаторов пока не получены. Дождитесь первого запуска.")

st.markdown("---")
st.caption("Роботы сбора данных работают в фоне 24/7")