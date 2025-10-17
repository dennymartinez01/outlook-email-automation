import os
import threading
import time
import logging
import atexit
from bson.objectid import ObjectId
from flask import Flask, render_template, request, jsonify, send_file, Response
from pymongo import MongoClient
from urllib.parse import unquote
from dotenv import load_dotenv
import requests
import monitor_azure_cosmosdb_cloud_version

# ---------------------------------------------------------------------
# Load .env file and environment variables
# ---------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------
# Flask app setup
# ---------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("invoice-monitor")

# ---------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------
_stop_worker = False

def run_invoice_checker_loop():
    global _stop_worker
    while not _stop_worker:
        try:
            logger.info("🔁 Running invoice check...")
            token = monitor_azure_cosmosdb_cloud_version.get_graph_token()
            if token:
                monitor_azure_cosmosdb_cloud_version.check_emails(token)
            else:
                logger.warning("❌ Failed to get Graph token.")
        except Exception as e:
            logger.exception(f"❌ Error during invoice check: {e}")
        time.sleep(180)  # every 3 minutes

def start_background_thread():
    thread = threading.Thread(target=run_invoice_checker_loop, daemon=True)
    thread.start()
    logger.info("✅ Background invoice monitor thread started.")

def stop_background_thread():
    global _stop_worker
    _stop_worker = True
    logger.info("🛑 Background worker stopping...")

# Start worker when app launches
start_background_thread()
atexit.register(stop_background_thread)

# ---------------------------------------------------------------------
# MongoDB / Cosmos DB connection
# ---------------------------------------------------------------------
def get_mongo_collection():
    """
    Connect to the database defined in .env (MONGO_URI).
    Example: MONGO_URI=MongoClient("mongodb://localhost:27017/")
    """
    mongo_uri = os.getenv("MONGO_URI")
    # if mongo_uri.startswith("MongoClient"):
    #     # Strip MongoClient(...) wrapper if it exists
    #     mongo_uri = mongo_uri.replace('MongoClient("', '').replace('")', '')
    client = MongoClient(mongo_uri)
    db = client["email_invoice_db"]
    return db["invoicecollection"]

# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def home():
    return "📬 Invoice monitor running. Visit /v2/invoices to view data."

@app.route("/api/invoice/<doc_id>")
def invoice_detail_json(doc_id):
    collection = get_mongo_collection()
    try:
        # handle both string and ObjectId _id
        if ObjectId.is_valid(doc_id):
            doc = collection.find_one({"_id": ObjectId(doc_id)})
        else:
            doc = collection.find_one({"_id": doc_id})
    except Exception as e:
        logger.exception("❌ Error fetching invoice")
        return jsonify({"error": "Error fetching invoice"}), 500

    if not doc:
        return jsonify({"error": "Invoice not found"}), 404

    inv_data = doc.get("invoice_data", {})

    # ✅ build structure matching your Jinja JavaScript expectations
    invoice = {
        "InvoiceNumber": inv_data.get("InvoiceId", ""),
        "InvoiceDate": inv_data.get("InvoiceDate", ""),
        "DueDate": inv_data.get("DueDate", ""),
        "Supplier": {"Name": inv_data.get("VendorName", "").replace("\n", " ")},
        "Customer": {"Name": inv_data.get("CustomerName", "").replace("\n", " ")},
        "Currency": inv_data.get("InvoiceTotal", {}).get("code", ""),
        "SubTotal": inv_data.get("SubTotal", {}).get("amount", ""),
        "TaxAmount": inv_data.get("TotalTax", {}).get("amount", ""),
        "InvoiceTotal": inv_data.get("InvoiceTotal", {}).get("amount", ""),
        "AmountDue": inv_data.get("AmountDue", {}).get("amount", ""),
    }

    # ✅ normalize line items
    line_items = []
    for item in inv_data.get("Items", []):
        line_items.append({
            "Description": item.get("Description", ""),
            "Net": item.get("Amount", ""),
            "VAT": next((tax.get("Amount") for tax in inv_data.get("TaxDetails", []) if tax.get("Amount")), ""),
            "Total": item.get("Amount", "")
        })

    return jsonify({
        "Invoice": invoice,
        "LineItems": line_items,
        "pdf_file": doc.get("pdf_file", ""),
        "status": doc.get("status", ""),
        "_id": str(doc.get("_id")),
    })


@app.route("/v2/invoices")
def invoices_v2():
    collection = get_mongo_collection()

    # 🔍 Search query from user
    search_query = request.args.get("vendor", "").strip().lower()

    # Fetch all invoices (you can later add MongoDB regex filtering)
    docs = list(collection.find().sort("processed_at", -1))

    invoices = []
    for doc in docs:
        data = doc.get("invoice_data", {})
        total_data = data.get("InvoiceTotal", {}) or {}

        # Optional: text filter
        vendor_name = (data.get("VendorName") or "").lower()
        invoice_no = str(data.get("InvoiceId", "")).lower()
        customer_name = (data.get("CustomerName") or "").lower()

        if search_query and search_query not in vendor_name and search_query not in invoice_no and search_query not in customer_name:
            continue

        invoices.append({
            "_id": str(doc.get("_id")),
            "invoice_no": data.get("InvoiceId", "N/A"),
            "date": data.get("InvoiceDate", "N/A"),
            "vendor": data.get("VendorName", "N/A").replace("\n", " "),
            "customer": data.get("CustomerName", "N/A").replace("\n", " "),
            "total": f"{total_data.get('symbol', '')}{total_data.get('amount', '')}",
            "status": doc.get("status", "N/A"),
            "pdf_file": doc.get("pdf_file", "")
        })

    # Pagination logic
    page = int(request.args.get("page", 1))
    per_page = 10
    total_pages = (len(invoices) + per_page - 1) // per_page or 1
    start = (page - 1) * per_page
    end = start + per_page

    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)

    return render_template(
        "roxor_template_3.html",
        invoices=invoices[start:end],
        vendor=search_query,
        page=page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page
    )


@app.route("/pdf/<path:filename>")
def serve_pdf(filename):
    try:
        decoded_filename = unquote(filename)
        if decoded_filename.startswith("http"):
            resp = requests.get(decoded_filename, stream=True)
            if resp.status_code == 200:
                return Response(resp.iter_content(chunk_size=8192), content_type="application/pdf")
            return f"Error fetching PDF from {decoded_filename}", 400

        local_path = os.path.join("invoices", decoded_filename)
        if os.path.exists(local_path):
            return send_file(local_path, mimetype="application/pdf")

        return f"PDF not found: {decoded_filename}", 404
    except Exception as e:
        logger.exception("Error serving PDF")
        return f"Error serving PDF: {str(e)}", 500

@app.route("/purge", methods=["POST"])
def purge_invoices():
    try:
        collection = get_mongo_collection()
        all_invoices = list(collection.find({}, {"pdf_file": 1}))
        collection.delete_many({})
        logger.info("✅ All invoices deleted from MongoDB")

        from azure.storage.blob import BlobServiceClient
        AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container = blob_service.get_container_client("invoices")

        for inv in all_invoices:
            pdf_ref = inv.get("pdf_file")
            if not pdf_ref:
                continue
            try:
                blob_name = pdf_ref.split("/")[-1] if pdf_ref.startswith("http") else pdf_ref
                container.delete_blob(blob_name)
                logger.info(f"🗑️ Deleted from Blob: {blob_name}")
            except Exception as e:
                logger.warning(f"⚠️ Could not delete blob {pdf_ref}: {e}")

        return jsonify({"success": True, "message": "All invoices and PDFs purged successfully."})
    except Exception as e:
        logger.exception("❌ Purge failed")
        return jsonify({"success": False, "message": str(e)}), 500

# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"🚀 Starting Flask server on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
