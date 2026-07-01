import streamlit as st
import os
from dotenv import load_dotenv
from pages.Readings import show_readings
from pages.Device_Registry import show_devices
from pages.Meter_Models import show_models
from logger_config import logger

st.set_page_config(page_title="ИнфоЭнерго", page_icon="⚡", layout="wide")

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

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_role" not in st.session_state:
    st.session_state.user_role = None
if "username" not in st.session_state:
    st.session_state.username = None

if not st.session_state.logged_in:
    st.title("⚡ Автоматизированная система коммерческого учета")
    st.subheader("Авторизация")
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

st.sidebar.markdown(f"👤 **{st.session_state.username}** (`{st.session_state.user_role}`)")

pg = st.navigation([
    st.Page(show_readings, title="Сбор показаний", icon="📊"),
    st.Page(show_devices, title="Реестр приборов", icon="🏭"),
    st.Page(show_models, title="Справочник моделей", icon="📋"),
])

if st.sidebar.button("🚪 Выйти"):
    st.session_state.logged_in = False
    st.session_state.user_role = None
    st.session_state.username = None
    st.rerun()

pg.run()