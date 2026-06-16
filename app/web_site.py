import os
import streamlit as st
from dotenv import load_dotenv
from db_client import sync_status_col
from logger_config import logger

st.set_page_config(page_title="ИнфоЭнерго", page_icon="⚡", layout="wide")

# Кастомное меню (CSS)
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

# Инициализация состояния (только для текущей сессии)
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
    # === Блок статуса синхронизаторов ===
    st.markdown("---")
    st.subheader("🤖 Статус синхронизаторов")
    statuses = list(sync_status_col.find({}, {"_id": 0}).sort("last_update", -1))
    if statuses:
        import pandas as pd
        df_status = pd.DataFrame(statuses)
        df_status["last_update"] = pd.to_datetime(df_status["last_update"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        df_status = df_status.rename(columns={
            "robot_name": "Робот",
            "status": "Статус",
            "last_update": "Последнее обновление",
            "records_processed": "Записей обработано",
            "error": "Ошибка"
        })
        if "Ошибка" not in df_status.columns:
            df_status["Ошибка"] = ""
        if "Записей обработано" not in df_status.columns:
            df_status["Записей обработано"] = 0
        def color_status(val):
            if val == "success":
                return "🟢"
            elif val == "running":
                return "🟡"
            elif val == "error":
                return "🔴"
            else:
                return "⚪"
        df_status["Статус"] = df_status["Статус"].apply(color_status) + " " + df_status["Статус"]
        st.dataframe(df_status[["Робот", "Статус", "Последнее обновление", "Записей обработано", "Ошибка"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("Статусы синхронизаторов пока не получены. Дождитесь первого запуска.")

st.markdown("---")
st.info("Выберите раздел в боковом меню.")
c1, c2, c3 = st.columns(3)
c1.markdown("### 📋 Справочник моделей")
c2.markdown("### 🏭 Реестр приборов учета")
c3.markdown("### 📉 Сбор показаний")
st.caption("Роботы сбора данных работают в фоне 24/7")