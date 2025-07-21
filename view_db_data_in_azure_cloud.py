from pymongo import MongoClient
from urllib.parse import quote_plus
import json

# Encode username & password
username = quote_plus("mongoadmin")
password = quote_plus("P@ssw0rd!!")  # IMPORTANT: URL encode special characters
# host = "email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com"
# port = 10255

uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"

# Cosmos DB requires standard Mongo URI with ssl and authMechanism
# uri = f"mongodb://{username}:{password}@{host}:{port}/?ssl=true&authMechanism=SCRAM-SHA-256&retrywrites=false"


client = MongoClient(uri, tls=True)
db = client["email_invoice_db"]
collection = db["invoicecollection"]

# Find all documents
for doc in collection.find():
    print(json.dumps(doc, indent=4, default=str))  # default=str handles datetime objects
