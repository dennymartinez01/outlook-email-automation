# app.py
import os
import io
import zipfile
import base64
import time
import logging
import threading
import atexit
import re
import pandas as pd

from urllib.parse import unquote

from flask import Flask, render_template, request, jsonify, send_file, Response
from pymongo import MongoClient
from bson.objectid import ObjectId
import requests
from dotenv import load_dotenv

# Load environment
load_dotenv()

# Services (expected to exist as in your repo)
from services.azure_graph_service import get_graph_token, fetch_new_messages
from services.azure_blob_service import upload_pdf_blob
from services.azure_ai_service import analyze_and_store

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("invoice-monitor")

# Flask app
app = Flask(__name__)

# Environment variables (required)
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI missing in environment. Exiting.")
    raise SystemExit("MONGO_URI not configured")

# Target inbox env names (per your confirmation)
EMAIL_NONSTOCK = os.getenv("TARGET_EMAIL_NONSTOCK") or os.getenv("EMAIL_NONSTOCK")
EMAIL_STOCK = os.getenv("TARGET_EMAIL_STOCK") or os.getenv("EMAIL_STOCK")
EMAIL_CUSTOMER = os.getenv("TARGET_EMAIL_CUSTOMER") or os.getenv("EMAIL_CUSTOMER")

TARGET_EMAILS = {
    "nonstock": EMAIL_NONSTOCK,
    "stock": EMAIL_STOCK,
    "customer": EMAIL_CUSTOMER
}

# Background worker stop flag
_stop_worker = False

def get_mongo_collection():
    client = MongoClient(MONGO_URI)
    db = client.get_database("email_invoice_db")
    return db.get_collection("invoicecollection")

def get_collection_for_export(collection_name):
    client = MongoClient(MONGO_URI)
    db = client.get_database("email_invoice_db")
    return db.get_collection(collection_name)

# ... (other functions unchanged: process_message_attachments, run loop, etc.)
# (keep your original helper functions and background thread code)

# -------------------------
# Flask routes
# -------------------------

@app.route("/")
def home():
    return "📬 Invoice monitor running — visit /v2/invoices"

@app.route("/api/invoice/<doc_id>")
def invoice_detail_json(doc_id):
    coll = get_mongo_collection()
    # try ObjectId then string id
    doc = None
    try:
        if ObjectId.is_valid(doc_id):
            doc = coll.find_one({"_id": ObjectId(doc_id)})
    except Exception:
        doc = None
    if not doc:
        doc = coll.find_one({"_id": doc_id})

    if not doc:
        return jsonify({"error": "Invoice not found"}), 404

    inv_data = doc.get("invoice_data", {}) or {}

    # NOTE: Ensure keys match what your frontend expects
    invoice = {
        "InvoiceNumber": inv_data.get("InvoiceId", ""),
        "InvoiceDate": inv_data.get("InvoiceDate", ""),
        "DueDate": inv_data.get("DueDate", ""),
        "Supplier": {"Name": (inv_data.get("VendorName") or "").replace("\n", " ")},
        "Customer": {"Name": (inv_data.get("CustomerName") or "").replace("\n", " ")},
        # keep using code here; UI will map to symbol
        "Currency": (inv_data.get("InvoiceTotal") or {}).get("code", "") or (inv_data.get("SubTotal") or {}).get("code", ""),
        "SubTotal": (inv_data.get("SubTotal") or {}).get("amount", ""),
        "TaxAmount": (inv_data.get("TotalTax") or {}).get("amount", ""),
        "InvoiceTotal": (inv_data.get("InvoiceTotal") or {}).get("amount", ""),
        "AmountDue": (inv_data.get("AmountDue") or {}).get("amount", ""),
    }

    # normalize line items
    line_items = []
    for item in inv_data.get("Items", []):
        line_items.append({
            "Description": item.get("Description", ""),
            "Net": item.get("Amount", "") or item.get("Net", ""),
            "VAT": next((td.get("Amount") for td in inv_data.get("TaxDetails", []) if td.get("Amount")), ""),
            "Total": item.get("Amount", "")
        })

    return jsonify({
        "Invoice": invoice,
        "LineItems": line_items,
        "pdf_file": doc.get("pdf_file", ""),
        "status": doc.get("status", ""),
        "_id": str(doc.get("_id"))
    })

@app.route("/v2/invoices")
def invoices_v2():
    coll = get_mongo_collection()

    search_query = request.args.get("vendor", "").strip()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 10))
    category = request.args.get("category", "nonstock").lower()

    # Build query: for nonstock include documents without category (back-compat)
    if category == "nonstock":
        query = {"$or": [{"category": "nonstock"}, {"category": {"$exists": False}}]}
    else:
        query = {"category": category}

    if search_query:
        regex = {"$regex": search_query, "$options": "i"}
        query["$or"] = [
            {"invoice_data.InvoiceId": regex},
            {"invoice_data.VendorName": regex},
            {"invoice_data.CustomerName": regex}
        ]

    total = coll.count_documents(query)
    total_pages = (total + per_page - 1) // per_page or 1
    skip = (page - 1) * per_page

    docs = list(coll.find(query).sort("processed_at", -1).skip(skip).limit(per_page))

    invoices = []
    for doc in docs:
        inv = doc.get("invoice_data", {}) or {}

        subtotal_data = inv.get("SubTotal") or {}
        subtotal_amount = subtotal_data.get("amount", "")
        subtotal_symbol = (
                subtotal_data.get("symbol")
                or inv.get("Currency")
                or "£"
        )

        invoices.append({
            "_id": str(doc.get("_id")),
            "invoice_no": inv.get("InvoiceId", "") or inv.get("InvoiceNumber", "") or "",
            "date": inv.get("InvoiceDate", ""),
            "vendor": (inv.get("VendorName") or "").replace("\n", " "),
            "customer": (inv.get("CustomerName") or "").replace("\n", " "),
            "total": f"{subtotal_symbol}{subtotal_amount}",
            "status": doc.get("status", ""),
            "pdf_file": doc.get("pdf_file", ""),
            # NEW: exported flag (default False)
            "exported": bool(doc.get("exported", False))
        })

    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)

    return render_template(
        "roxor_template_3.html",
        invoices=invoices,
        vendor=search_query,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page,
        active_category=category
    )

@app.route("/pdf/<path:filename>")
def serve_pdf(filename):
    try:
        decoded = unquote(filename)
        if decoded.startswith("http://") or decoded.startswith("https://"):
            r = requests.get(decoded, stream=True, timeout=30)
            if r.status_code == 200:
                return Response(r.iter_content(chunk_size=8192), content_type="application/pdf")
            else:
                return (f"Failed to fetch remote PDF ({r.status_code})", 404)
        local_path = os.path.join("invoices", decoded)
        if os.path.exists(local_path):
            return send_file(local_path, mimetype="application/pdf")
        return (f"PDF not found: {decoded}", 404)
    except Exception as e:
        logger.exception("Error serving PDF")
        return (f"Error serving PDF: {e}", 500)

@app.route("/export_batch", methods=["POST"])
def export_batch():
    try:
        data = request.get_json() or {}
        ids = data.get("invoices") or []

        if not ids:
            return jsonify({"success": False, "message": "No invoice IDs provided"}), 400

        invoice_coll = get_collection_for_export("invoicecollection")
        vendor_coll = get_collection_for_export("vendorcollection")

        rows = []
        processed_ids = []  # IDs we will mark as exported

        for doc_id in ids:
            # fetch doc by ObjectId or string id
            doc = None
            try:
                if ObjectId.is_valid(doc_id):
                    doc = invoice_coll.find_one({"_id": ObjectId(doc_id)})
            except Exception:
                doc = None
            if doc is None:
                doc = invoice_coll.find_one({"_id": doc_id})

            if not doc:
                # skip missing
                continue

            # Skip documents already exported
            if doc.get("exported"):
                logger.info("Skipping already exported invoice: %s", doc_id)
                continue

            inv = doc.get("invoice_data", {}) or {}

            customer_name = (inv.get("CustomerName") or "").strip()
            invoice_date = inv.get("InvoiceDate", "")

            currency = (
                    inv.get("Currency")
                    or (inv.get("InvoiceTotal") or {}).get("currencySymbol")
                    or (inv.get("TotalTax") or {}).get("currencySymbol")
                    or ""
            )

            amount = (
                    (inv.get("InvoiceTotal") or {}).get("amount")
                    or (inv.get("Total") or {}).get("amount")
                    or (inv.get("TotalTax") or {}).get("amount")
                    or 0
            )

            try:
                amount = float(amount)
            except:
                amount = 0

            invoice_type = "credit" if amount < 0 else "debit"

            # --- VENDOR LOOKUP
            vendor_doc = None
            vendor_code = ""

            invoice_postcode = (inv.get("VendorAddress", {}).get("postal_code") or "").strip()
            if invoice_postcode:
                postcode_pattern = f".*{re.escape(invoice_postcode)}.*"
                vendor_filter = {
                    "Company Code": 1000,
                    "Postcode": {
                        "$regex": postcode_pattern,
                        "$options": "i"
                    }
                }
                vendor_doc = vendor_coll.find_one(vendor_filter)

            vendor_code = vendor_doc.get("Account Code", "") if vendor_doc else ""

            # Append row for this invoice
            rows.append({
                "Vendor SAP Account code": vendor_code,
                "Invoice Date": invoice_date,
                "Reference": customer_name,
                "Currency": currency,
                "Amount": abs(amount),
                "Type": invoice_type
            })

            # remember to mark exported
            processed_ids.append(doc.get("_id"))

        if not rows:
            return jsonify({"success": False, "message": "No selectable invoices to export (they may already be exported)."}), 400

        # Export to Excel
        output = io.BytesIO()
        df = pd.DataFrame(rows)
        df.to_excel(output, index=False)
        output.seek(0)

        # Mark processed docs as exported (persist)
        # Convert processed_ids to ObjectIds if needed
        set_ids = []
        for pid in processed_ids:
            try:
                # if it's already an ObjectId from Mongo result, keep it
                if isinstance(pid, ObjectId):
                    set_ids.append(pid)
                else:
                    if ObjectId.is_valid(pid):
                        set_ids.append(ObjectId(pid))
                    else:
                        set_ids.append(pid)
            except Exception:
                set_ids.append(pid)

        try:
            # update by _id in list (handles ObjectId list)
            invoice_coll.update_many({"_id": {"$in": set_ids}}, {"$set": {"exported": True}})
            logger.info("Marked %d invoices exported", len(set_ids))
        except Exception as e:
            logger.exception("Failed to mark invoices exported: %s", e)

        return send_file(
            output,
            as_attachment=True,
            download_name="FB60.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        logger.exception("export_batch failed")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/purge", methods=["POST"])
def purge_invoices():
    try:
        coll = get_mongo_collection()
        all_docs = list(coll.find({}, {"pdf_file": 1}))

        coll.delete_many({})
        logger.info("Deleted all invoice records from MongoDB.")

        AZ_CONN = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        if AZ_CONN:
            from azure.storage.blob import BlobServiceClient
            blob_service = BlobServiceClient.from_connection_string(AZ_CONN)
            container = blob_service.get_container_client("invoices")
            for d in all_docs:
                pdf_ref = d.get("pdf_file")
                if not pdf_ref:
                    continue
                blob_name = pdf_ref.split("/")[-1] if "/" in pdf_ref else pdf_ref
                try:
                    container.delete_blob(blob_name)
                    logger.info("Deleted blob %s", blob_name)
                except Exception as e:
                    logger.warning("Could not delete blob %s: %s", blob_name, e)

        return jsonify({"success": True, "message": "All invoices and PDFs purged."})
    except Exception as e:
        logger.exception("purge failed")
        return jsonify({"success": False, "message": str(e)}), 500

# Run the app
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "True").lower() in ("1", "true", "yes")
    logger.info("Starting Flask on port %s (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)
