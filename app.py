import os
import threading
import time
import datetime
import io
import csv
from bson.objectid import ObjectId
from flask import Flask, render_template, request, send_file, send_from_directory, jsonify, Response
from pymongo import MongoClient
from urllib.parse import quote_plus
import monitor_azure_cosmosdb_cloud_version

app = Flask(__name__)
invoice_thread_started = False

# ------------------ BACKGROUND THREAD ------------------ #
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

# ------------------ MONGO CONNECTION ------------------ #
def get_mongo_collection():
    username = quote_plus("mongoadmin")
    password = quote_plus("P@ssw0rd!!")  # 🔒 best to use env var in production
    uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"
    client = MongoClient(uri, tls=True)
    db = client["email_invoice_db"]
    return db["invoicecollection"]

# ------------------ ROUTES ------------------ #
@app.route("/")
def home():
    return "📬 Invoice monitoring is running every 3 minutes. Visit /v2/invoices to view stored data."

@app.route("/api/invoice/<doc_id>")
def invoice_detail_json(doc_id):
    collection = get_mongo_collection()
    try:
        if ObjectId.is_valid(doc_id):
            doc = collection.find_one({"_id": ObjectId(doc_id)})
        else:
            doc = collection.find_one({"_id": doc_id})
    except Exception as e:
        print("❌ Error fetching invoice:", e)
        return jsonify({"error": "Error fetching invoice"}), 500

    if not doc:
        return jsonify({"error": "Invoice not found"}), 404

    doc["_id"] = str(doc["_id"])
    return jsonify(doc)

@app.route("/v2/invoices")
def invoices_v2():
    username = quote_plus("mongoadmin")
    password = quote_plus("P@ssw0rd!!")
    uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"
    client = MongoClient(uri, tls=True)
    db = client["email_invoice_db"]
    collection = db["invoicecollection"]

    # --- Search ---
    search_query = request.args.get("vendor", "").strip()
    mongo_query = {}
    if search_query:
        regex = {"$regex": search_query, "$options": "i"}
        mongo_query["$or"] = [
            {"Invoice.InvoiceNumber": regex},
            {"Invoice.Supplier.Name": regex},
            {"Invoice.Customer.Name": regex},
        ]

    # --- Pagination ---
    page = int(request.args.get("page", 1))
    per_page = 10
    total_docs = collection.count_documents(mongo_query)
    total_pages = max(1, (total_docs + per_page - 1) // per_page)

    cursor = (
        collection.find(mongo_query)
        .sort("Invoice.InvoiceDate", -1)
        .skip((page - 1) * per_page)
        .limit(per_page)
    )

    invoices = []
    for inv in cursor:
        line_items = inv.get("LineItems", [])
        category = "Stock" if line_items and len(line_items) > 0 else "Non-stock"

        invoices.append({
            "_id": str(inv.get("_id")),
            "invoice_no": inv.get("Invoice", {}).get("InvoiceNumber", ""),
            "date": inv.get("Invoice", {}).get("InvoiceDate", ""),
            "vendor": inv.get("Invoice", {}).get("Supplier", {}).get("Name", ""),
            "customer": inv.get("Invoice", {}).get("Customer", {}).get("Name", ""),
            "total": inv.get("Invoice", {}).get("InvoiceTotal", 0),
            "category": category,
            "status": "Exported",
            "pdf_file": inv.get("pdf_file", "")
        })

    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)

    return render_template(
        "roxor_template_3.html",
        invoices=invoices,
        vendor=search_query,
        page=page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page
    )

@app.route("/pdf/<path:filename>")
def serve_pdf(filename):
    try:
        if filename.startswith("http"):
            import requests
            r = requests.get(filename)
            return Response(
                r.content,
                mimetype="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{filename}"'}
            )

        from azure.storage.blob import BlobServiceClient
        AZURE_STORAGE_CONNECTION_STRING = (
            "DefaultEndpointsProtocol=https;AccountName=roxorinvoicefiles;"
            "AccountKey=...;EndpointSuffix=core.windows.net"
        )
        blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container = blob_service.get_container_client("invoices")
        blob_client = container.get_blob_client(filename)
        pdf_bytes = blob_client.download_blob().readall()

        return Response(pdf_bytes, mimetype="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{filename}"'})
    except Exception as e:
        print(f"❌ Error serving PDF {filename}: {e}")
        return f"Error serving PDF: {e}", 500

@app.route("/purge", methods=["POST"])
def purge_invoices():
    try:
        collection = get_mongo_collection()
        all_invoices = list(collection.find({}, {"pdf_file": 1}))
        collection.delete_many({})
        print("✅ All invoices deleted from MongoDB")

        from azure.storage.blob import BlobServiceClient
        AZURE_STORAGE_CONNECTION_STRING = "DefaultEndpointsProtocol=https;AccountName=roxorinvoicefiles;AccountKey=...;EndpointSuffix=core.windows.net"
        blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container = blob_service.get_container_client("invoices")

        for inv in all_invoices:
            pdf_ref = inv.get("pdf_file")
            if not pdf_ref:
                continue
            try:
                blob_name = pdf_ref.split("/")[-1] if pdf_ref.startswith("http") else pdf_ref
                container.delete_blob(blob_name)
                print(f"🗑️ Deleted from Blob: {blob_name}")
            except Exception as e:
                print(f"⚠️ Could not delete blob {pdf_ref}: {e}")

        return jsonify({"success": True, "message": "All invoices and PDFs purged successfully."})
    except Exception as e:
        print(f"❌ Purge failed: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

# ------------------ MAIN ------------------ #
if __name__ == "__main__":
    app.run(debug=True)
