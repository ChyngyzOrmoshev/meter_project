import streamlit as st
import pandas as pd
from db_client import models_col

def show():
    st.title("📋 Справочник моделей приборов учета")
    st.markdown("---")

    try:
        models_data = list(models_col.find({}, {"_id": 0}))
        if models_data:
            df_models = pd.DataFrame(models_data)
            df_models.columns = [
                "Код", "Наименование", "Значность", "Фазность",
                "Ном. ток", "Ном. напряжение", "Тип прибора",
                "Период", "Тип АСКУЭ", "Для API"
            ]
            search_query = st.text_input("🔍 Быстрый поиск по наименованию модели:", "")
            if search_query:
                df_models = df_models[df_models["Наименование"].str.contains(search_query, case=False, na=False)]
            st.dataframe(df_models, use_container_width=True)
            st.caption(f"Всего в базе данных доступно моделей: {len(df_models)}")
        else:
            st.warning("Справочник моделей пуст. Запустите скрипт импорта или внесите данные.")
    except Exception as e:
        st.error(f"Ошибка загрузки справочника моделей: {e}")

if __name__ == "__main__":
    show()