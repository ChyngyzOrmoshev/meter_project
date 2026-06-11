from pymongo import MongoClient

client = MongoClient("mongodb://mongodb:27017/")
db = client["power_monitoring"]

models_col = db["meter_models"]
devices_col = db["devices"]
readings_col = db["readings"]
