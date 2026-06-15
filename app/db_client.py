from pymongo import MongoClient, ASCENDING, DESCENDING

client = MongoClient("mongodb://mongodb:27017/")
db = client["power_monitoring"]

models_col = db["meter_models"]
devices_col = db["devices"]
readings_col = db["readings"]

devices_col.create_index(
    [("serial_number", ASCENDING)],
    unique=True
)

readings_col.create_index(
    [
        ("serial_number", ASCENDING),
        ("timestamp", ASCENDING)
    ],
    unique=True
)

readings_col.create_index(
    [
        ("serial_number", ASCENDING),
        ("timestamp", DESCENDING)
    ]
)