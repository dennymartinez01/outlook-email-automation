from pymongo import MongoClient
from urllib.parse import quote_plus

username = quote_plus("mongoadmin")
password = quote_plus("P@ssw0rd!!")  # IMPORTANT: URL encode special characters

uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"

client = MongoClient(uri, tls=True)

try:
    dbs = client.list_database_names()
    print("✅ Connected! Databases:", dbs)
except Exception as e:
    print("❌ Connection failed:", e)