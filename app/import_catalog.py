import os
import pandas as pd
from db_client import models_col

def import_excel_catalog():
    file_path = "/app/catalog.xlsx"
    if not os.path.exists(file_path):
        print(f"File {file_path} not found!")
        return
    df = pd.read_excel(file_path)
    models_col.delete_many({})
    records = []
    for _, row in df.iterrows():
        phases_text = str(row['Фазность']).strip().lower()
        phases = 1 if "одно" in phases_text else 3
        record = {
            "catalog_code": str(row['Код']).strip(),
            "model_name": str(row['Наименование']).strip(),
            "digit_capacity": str(row['Значность']).strip(),
            "phases": phases,
            "nominal_current": str(row['Номинальный ток']).strip(),
            "nominal_voltage": str(row['Номинальное напряжение']).strip(),
            "system_type": str(row['Тип прибора учета']).strip(),
            "period": str(row['Период']).strip(),
            "device_type_id": str(row['Тип АСКУЭ']).strip(),
            "device_type_str": str(row['Для API']).strip()
        }
        records.append(record)
    if records:
        models_col.insert_many(records)
        models_col.create_index("model_name")
        print(f"SUCCESS: Imported {len(records)} meter models with splitted API codes!")

if __name__ == "__main__":
    import_excel_catalog()