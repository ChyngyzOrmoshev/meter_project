import os
import streamlit as st
from dotenv import load_dotenv

# Настройка главной страницы
st.set_page_config(
    page_title="ИнфоЭнерго", 
    page_icon="⚡", 
    layout="wide"
)
# Переименовываем отображение главного файла в боковом меню
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


# Загружаем переменные окружения (.env подтягивается Docker-ом автоматически)
load_dotenv()
required_vars = [
    "WEB_ADMIN_PASSWORD",
    "WEB_OPERATOR_PASSWORD",
    "WEB_USER_PASSWORD"
]

for var in required_vars:
    if not os.getenv(var):
        st.error(f"Не найдена переменная окружения: {var}")
        st.stop()

# Безопасная структура учетных записей из переменных окружения
USER_CREDENTIALS = {
    "admin": {
        "password": os.getenv("WEB_ADMIN_PASSWORD"),
        "role": "admin"
    },
    "operator": {
        "password": os.getenv("WEB_OPERATOR_PASSWORD"),
        "role": "operator"
    },
    "user": {
        "password": os.getenv("WEB_USER_PASSWORD"),
        "role": "user"
    }
}

# Инициализация состояния сессии для авторизации
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_role" not in st.session_state:
    st.session_state.user_role = None
if "username" not in st.session_state:
    st.session_state.username = None

# === ЭКРАН 1: ФОРМА АВТОРИЗАЦИИ ===
if not st.session_state.logged_in:
    st.title("⚡ Автоматизированная система коммерческого учета электроэнергии")
    st.subheader("Авторизация в системе")
    
    with st.form("login_form"):
        username = st.text_input("👤 Логин:").strip().lower()
        password = st.text_input("🔑 Пароль:", type="password")
        submit_button = st.form_submit_button("Войти в систему")
        
        if submit_button:
            if username in USER_CREDENTIALS and USER_CREDENTIALS[username]["password"] == password:
                st.session_state.logged_in = True
                st.session_state.user_role = USER_CREDENTIALS[username]["role"]
                st.session_state.username = username
                st.success("Успешный вход!")
                st.rerun()
            else:
                st.error("❌ Неверный логин или пароль. Попробуйте снова.")

# === ЭКРАН 2: ПРИВЕТСТВЕННЫЙ ЭКРАН (ПОСЛЕ ВХОДА) ===
else:
    # Кнопка выхода в верхнем углу
    col1, col2 = st.columns([6, 1])
    with col2:
        if st.button("🚪 Выйти из системы"):
            st.session_state.logged_in = False
            st.session_state.user_role = None
            st.session_state.username = None
            st.rerun()
            
    with col1:
        st.title("⚡ ИнфоЭнерго")
        st.markdown(f"Добро пожаловать, **{st.session_state.username}**! Ваша роль в системе: `{st.session_state.user_role}`.")
    
    st.markdown("---")
    
    st.info("👈 Все инструменты управления теперь доступны в боковом меню слева. Перейдите в нужный раздел:")
    
    # Красивые информационные карточки-навигаторы для оператора
    c1, c2, c3 = st.columns(3)
    
    with c1:
        st.markdown("### 📋 Справочник моделей")
    #    st.write("Просмотр технических характеристик, фазности, номинальных токов и API-кодов поддерживаемых счетчиков.")
        
    with c2:
        st.markdown("### 🏭 Реестр устройств")
    #    st.write("Управление базой данных установленных приборов учета. Добавление новых точек, привязка к подстанциям и массовый импорт.")
        
    with c3:
        st.markdown("### 📉 Сбор показаний")
    #    st.write("Аналитический хаб системы. Графики энергопотребления, выгрузка истории замеров и ручное внесение контрольных показаний.")
        
    st.markdown("---")
    st.caption("🤖 Роботы фонового сбора данных cEnergo и Sanxing работают на сервере в штатном режиме 24/7.")
