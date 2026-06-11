import streamlit as st
import pandas as pd
from db_client import models_col  # Импортируем коллекцию моделей счетчиков

# Настройка страницы (опционально, делает отображение во всю ширину)
st.set_page_config(page_title="Справочник моделей", page_icon="📋", layout="wide")
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

st.title("📋 Справочник моделей приборов учета")
st.markdown("---")

# Код вывода справочника (переносим из web_site.py строки ~160-183)
try:
    models_data = list(models_col.find({}, {"_id": 0}))
    if models_data:
        df_models = pd.DataFrame(models_data)
        
        # Переименуем колонки для красивого вывода в интерфейсе
        df_models.columns = [
            "Код", "Наименование", "Значность", "Фазность", 
            "Ном. ток", "Ном. напряжение", "Тип прибора", 
            "Период", "Тип АСКУЭ", "Для API"
        ]
        
        # Поиск по моделям
        search_query = st.text_input("🔍 Быстрый поиск по наименованию модели:", "")
        if search_query:
            df_models = df_models[df_models["Наименование"].str.contains(search_query, case=False, na=False)]
            
        st.dataframe(df_models, use_container_width=True)
        st.caption(f"Всего в базе данных доступно моделей: {len(df_models)}")
    else:
        st.warning("Справочник моделей пуст. Запустите скрипт импорта или внесите данные.")
except Exception as e:
    st.error(f"Ошибка загрузки справочника моделей: {e}")
