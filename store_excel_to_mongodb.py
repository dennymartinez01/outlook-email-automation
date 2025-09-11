import pandas as pd
from pymongo import MongoClient, ASCENDING
from urllib.parse import quote_plus

# --- MongoDB connection ---
# client = MongoClient("mongodb://localhost:27017/")  # change if needed
# db = client["mydatabase"]       # database name
# collection = db["mycollection"] # collection name

username = quote_plus("mongoadmin")
password = quote_plus("P@ssw0rd!!")
uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"

client = MongoClient(uri, tls=True)
db = client["email_invoice_db"]
collection = db["vendorcollection"]

# --- STEP 1: Deduplicate existing records ---
print("🔍 Checking for duplicates...")
pipeline = [
    {
        "$group": {
            "_id": {"Account Code": "$Account Code", "Company Code": "$Company Code"},
            "ids": {"$push": "$_id"},
            "count": {"$sum": 1}
        }
    },
    {"$match": {"count": {"$gt": 1}}}
]

duplicates = collection.aggregate(pipeline)
deleted_count = 0

for group in duplicates:
    ids = group["ids"]
    ids_to_delete = ids[1:]  # keep the first, delete the rest
    if ids_to_delete:
        result = collection.delete_many({"_id": {"$in": ids_to_delete}})
        deleted_count += result.deleted_count

print(f"🗑️ Removed {deleted_count} duplicate documents.")

# --- STEP 2: Create unique compound index ---
print("📌 Creating unique index on (Account Code, Company Code)...")
collection.create_index(
    [("Account Code", ASCENDING), ("Company Code", ASCENDING)],
    unique=True,
    name="account_company_unique"
)
print("✅ Unique index created successfully.")

# --- STEP 3: Read Excel file ---
file_path = "downloads/vendor.xlsx"  # replace with your Excel file path
df = pd.read_excel(file_path)

# --- STEP 4: Insert/Update into MongoDB ---
data = df.to_dict(orient="records")
for record in data:
    filter_query = {
        "Account Code": record.get("Account Code"),
        "Company Code": record.get("Company Code")
    }
    collection.update_one(filter_query, {"$set": record}, upsert=True)

print(f"📥 Processed {len(data)} records into MongoDB (duplicates prevented).")
