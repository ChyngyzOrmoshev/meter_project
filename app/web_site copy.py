import os
import streamlit as st
from dotenv import load_dotenv
from logger_config import logger

st.set_page_config(page_title="ИнфоЭнерго", page_icon="⚡", layout="wide")

# ===== ЗАГРУЗКА ПЕРЕМЕННЫХ =====
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

# ===== ИНИЦИАЛИЗАЦИЯ СОСТОЯНИЯ =====
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_role" not in st.session_state:
    st.session_state.user_role = None
if "username" not in st.session_state:
    st.session_state.username = None
if "page" not in st.session_state:
    st.session_state.page = "📊 Сбор показаний"

# ===== АВТОРИЗАЦИЯ (отображается, если не залогинены) =====
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

# ===== ВЕРХНЯЯ ПАНЕЛЬ (закреплена) =====
st.markdown(
    """
    <style>
    .top-panel {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        z-index: 999;
        background: #0e1117;
        padding: 0.5rem 2rem;
        border-bottom: 1px solid #262730;
        display: flex;
        justify-content: space-between;
        align-items: center;
        height: 60px;
    }
    .top-panel .menu {
        display: flex;
        gap: 1.5rem;
        align-items: center;
    }
    .top-panel .menu a {
        color: #fafafa;
        text-decoration: none;
        font-weight: 500;
        padding: 0.3rem 0.8rem;
        border-radius: 0.3rem;
        transition: 0.2s;
    }
    .top-panel .menu a:hover {
        background: #262730;
    }
    .top-panel .menu a.active {
        background: #ff4b4b;
        color: white;
    }
    .top-panel .user-info {
        color: #fafafa;
        display: flex;
        align-items: center;
        gap: 1rem;
    }
    .top-panel .user-info button {
        background: none;
        border: 1px solid #555;
        color: #fafafa;
        padding: 0.2rem 0.8rem;
        border-radius: 0.3rem;
        cursor: pointer;
    }
    .top-panel .user-info button:hover {
        background: #ff4b4b;
        border-color: #ff4b4b;
    }
    .main-content {
        margin-top: 70px;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# Рендерим верхнюю панель
st.markdown(
    f"""
    <div class="top-panel">
        <div class="menu">
            <a href="?page=readings" class="{'active' if st.session_state.page == '📊 Сбор показаний' else ''}">📊 Сбор показаний</a>
            <a href="?page=devices" class="{'active' if st.session_state.page == '🏭 Реестр' else ''}">🏭 Реестр</a>
            <a href="?page=models" class="{'active' if st.session_state.page == '📋 Модели' else ''}">📋 Модели</a>
            <a href="?page=dashboard" class="{'active' if st.session_state.page == '📈 Дашборд' else ''}">📈 Дашборд</a>
        </div>
        <div class="user-info">
            <span>👤 {st.session_state.username} ({st.session_state.user_role})</span>
            <button onclick="window.location.href='?page=logout'">Выйти</button>
        </div>
    </div>
    <div class="main-content">
    """,
    unsafe_allow_html=True
)

# ===== ОБРАБОТКА ПАРАМЕТРОВ URL =====
query_params = st.query_params
page_param = query_params.get("page", "readings")

if page_param == "logout":
    st.session_state.logged_in = False
    st.session_state.user_role = None
    st.session_state.username = None
    st.query_params.clear()
    st.rerun()

# Устанавливаем страницу
if page_param == "readings":
    st.session_state.page = "📊 Сбор показаний"
elif page_param == "devices":
    st.session_state.page = "🏭 Реестр"
elif page_param == "models":
    st.session_state.page = "📋 Модели"
elif page_param == "dashboard":
    st.session_state.page = "📈 Дашборд"
else:
    st.session_state.page = "📊 Сбор показаний"

# ===== ЗАГРУЗКА ВЫБРАННОЙ СТРАНИЦЫ =====
if st.session_state.page == "📊 Сбор показаний":
    import pages.Readings as readings
    readings.show()
elif st.session_state.page == "🏭 Реестр":
    import pages.Device_Registry as registry
    registry.show()
elif st.session_state.page == "📋 Модели":
    import pages.Meter_Models as models
    models.show()
elif st.session_state.page == "📈 Дашборд":
    # Вставьте сюда код дашборда или оставьте заглушку
    st.markdown("---")
    st.subheader("📊 Аналитика по приборам учета")
    st.info("📈 Дашборд загружен (здесь будет статистика).")

st.markdown("</div>", unsafe_allow_html=True)