from flask import Flask, render_template, request, jsonify, send_file
import threading
import time
import monitor_azure_cosmosdb_cloud_version
from pymongo import MongoClient
from urllib.parse import quote_plus
import csv
import io
import json

app = Flask(__name__)
invoice_thread_started = False

# ------------------ BACKGROUND INVOICE CHECK ------------------ #
def run_invoice_checker_loop():
    while True:
        try:
            print("🔁 Running invoice check...")
            token = monitor_azure_cosmosdb_cloud_version.get_graph_token()
            if token:
                monitor_azure_cosmosdb_cloud_version.check_emails(token)
            else:
                print("❌ Failed to get token")
        except Exception as e:
            print(f"❌ Error during invoice check: {e}")
        time.sleep(180)  # Every 3 minutes

def start_background_thread():
    global invoice_thread_started
    if not invoice_thread_started:
        thread = threading.Thread(target=run_invoice_checker_loop, daemon=True)
        thread.start()
        invoice_thread_started = True
        print("✅ Background thread started")

start_background_thread()

# ------------------ COSMOS DB MONGO CONNECTION ------------------ #
def get_mongo_collection():
    username = quote_plus("mongoadmin")
    password = quote_plus("P@ssw0rd!!")  # Use env var in real deployment
    host = "email-monitor-cluster-mongodb.mongo.cosmos.azure.com"
    port = 10255
    # uri = f"mongodb://{username}:{password}@{host}:{port}/?ssl=true&authMechanism=SCRAM-SHA-256&retrywrites=false"
    uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"
    client = MongoClient(uri, tls=True)
    db = client["email_invoice_db"]
    return db["invoicecollection"]

# ------------------ WEB ROUTES ------------------ #
@app.route("/")
def home():
    return "📬 Invoice monitoring is running every 3 minutes. Visit /invoices to view stored data."

@app.route("/invoices")
def view_data():
    collection = get_mongo_collection()
    documents = list(collection.find())
    for doc in documents:
        doc["_id"] = str(doc["_id"])
    return render_template("index.html", documents=documents)

@app.route("/invoice/<doc_id>")
def view_invoice_detail(doc_id):
    collection = get_mongo_collection()
    doc = collection.find_one({"_id": doc_id})
    if doc:
        doc["_id"] = str(doc["_id"])
        return render_template("details.html", doc=doc)
    return "❌ Invoice not found", 404

@app.route("/export")
def export_csv():
    collection = get_mongo_collection()
    docs = list(collection.find())
    if not docs:
        return "No data to export."

    keys = sorted(set().union(*(doc.keys() for doc in docs)))
    si = io.StringIO()
    cw = csv.DictWriter(si, fieldnames=keys)
    cw.writeheader()
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        cw.writerow(doc)

    output = io.BytesIO()
    output.write(si.getvalue().encode())
    output.seek(0)
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name="invoices.csv")

# ------------------ MAIN ------------------ #
if __name__ == "__main__":
    app.run(debug=True)
