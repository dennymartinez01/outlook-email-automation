import os
import base64
import logging
import time
import requests
from pymongo import MongoClient, errors
from datetime import datetime

logger = logging.getLogger("invoice-monitor.ai")

# Simple retry helper used for transient network/API errors
def _with_retries(fn, max_attempts=3, delay_seconds=2, *args, **kwargs):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            logger.warning("Attempt %d/%d failed: %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(delay_seconds)
    # final raise
    raise last_exc


def _call_form_recognizer(endpoint, key, pdf_bytes, max_attempts=3):
    """Call Azure Form Recognizer with simple retries and return parsed JSON."""
    url = f"{endpoint}/formrecognizer/documentModels/prebuilt-invoice:analyze?api-version=2023-07-31"
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/pdf"}

    def _req():
        resp = requests.post(url, headers=headers, data=pdf_bytes, timeout=60)
        resp.raise_for_status()
        return resp.json()

    return _with_retries(_req, max_attempts=max_attempts, delay_seconds=2)


def _safe_field_value(field_obj):
    """Return value and confidence from a Form Recognizer field object in a normalized form."""
    if not field_obj or not isinstance(field_obj, dict):
        return {"value": None, "confidence": 0.0}

    # The SDK returns typed values under keys like valueString, valueNumber, valueDate
    value = field_obj.get("valueString")
    if value is None:
        value = field_obj.get("valueNumber")
    if value is None:
        value = field_obj.get("valueDate")
    if value is None:
        # fallback to raw 'content'
        value = field_obj.get("content")

    return {"value": value, "confidence": float(field_obj.get("confidence", 0.0))}


def analyze_and_store(blob_url, content_b64, msg_id, attachment_id):
    """
    Analyze a PDF invoice via Azure Form Recognizer and store structured data in MongoDB.

    Returns the inserted document id (string) on success.
    Raises on unrecoverable errors.
    """
    endpoint = os.getenv("AZURE_FORMRECOGNIZER_ENDPOINT")
    key = os.getenv("AZURE_FORMRECOGNIZER_KEY")
    mongo_uri = os.getenv("MONGO_URI")

    if not endpoint or not key:
        raise ValueError("AZURE_FORMRECOGNIZER_ENDPOINT and AZURE_FORMRECOGNIZER_KEY must be set")
    if not mongo_uri:
        raise ValueError("MONGO_URI must be set to save results to the database")

    try:
        # Decode PDF bytes
        pdf_bytes = base64.b64decode(content_b64)
    except Exception as e:
        logger.exception("Failed to decode base64 PDF bytes: %s", e)
        raise

    # 1) Call Form Recognizer
    logger.info("🧠 Sending PDF to Form Recognizer (msg=%s att=%s)", msg_id, attachment_id)
    try:
        result = _call_form_recognizer(endpoint, key, pdf_bytes, max_attempts=3)
    except Exception as e:
        logger.exception("❌ Form Recognizer analysis failed for msg=%s att=%s: %s", msg_id, attachment_id, e)
        raise

    # 2) Parse response
    documents = result.get("documents", [])
    if not documents:
        logger.warning("⚠️ No documents returned from Form Recognizer for msg=%s att=%s", msg_id, attachment_id)
        # Still insert a minimal record so the email is tracked
        doc_obj = {
            "Invoice": {},
            "LineItems": [],
            "pdf_file": blob_url,
            "confidence": 0.0,
            "status": "Needs manual review",
            "processed_at": datetime.utcnow().isoformat(),
            "source_msg_id": msg_id,
            "attachment_id": attachment_id,
        }
        client = MongoClient(mongo_uri)
        db = client["email_invoice_db"]
        collection = db["invoicecollection"]
        inserted = collection.insert_one(doc_obj)
        inserted_id = str(inserted.inserted_id)
        logger.info("🧾 Stored minimal invoice record %s (no document parsed)", inserted_id)
        return inserted_id

    doc = documents[0]
    fields = doc.get("fields", {})

    # Build Invoice object with values + confidence
    invoice = {}
    for key_name in ["InvoiceId", "InvoiceDate", "DueDate", "InvoiceCurrencyCode", "SubTotal", "TotalTax", "InvoiceTotal", "AmountDue"]:
        invoice[key_name] = _safe_field_value(fields.get(key_name))

    # Vendor / Customer
    supplier = {"Name": _safe_field_value(fields.get("VendorName")), "Address": _safe_field_value(fields.get("VendorAddress"))}
    customer = {"Name": _safe_field_value(fields.get("CustomerName")), "Address": _safe_field_value(fields.get("CustomerAddress"))}

    # Line items: Form Recognizer returns Items as valueArray of valueObject entries
    raw_items = fields.get("Items", {}).get("valueArray", [])
    line_items = []
    for item in raw_items:
        obj = item.get("valueObject", {})
        li = {
            "Description": _safe_field_value(obj.get("Description")),
            "Net": _safe_field_value(obj.get("Amount")),
            "VAT": _safe_field_value(obj.get("Tax")),
            "Total": _safe_field_value(obj.get("TotalAmount")),
        }
        line_items.append(li)

    # Compute aggregated confidence for key fields (InvoiceId, InvoiceDate, InvoiceTotal)
    confs = [
        invoice.get("InvoiceId", {}).get("confidence", 0.0),
        invoice.get("InvoiceDate", {}).get("confidence", 0.0),
        invoice.get("InvoiceTotal", {}).get("confidence", 0.0),
    ]
    # Avoid division by zero
    avg_conf = sum(confs) / (len(confs) if len(confs) else 1)

    status = "Exported" if avg_conf >= 0.7 else "Needs manual review"

    # Build final document for MongoDB
    doc_obj = {
        "Invoice": {
            "InvoiceNumber": invoice.get("InvoiceId", {}).get("value"),
            "InvoiceDate": invoice.get("InvoiceDate", {}).get("value"),
            "DueDate": invoice.get("DueDate", {}).get("value"),
            "Currency": invoice.get("InvoiceCurrencyCode", {}).get("value"),
            "SubTotal": invoice.get("SubTotal", {}).get("value"),
            "TaxAmount": invoice.get("TotalTax", {}).get("value"),
            "InvoiceTotal": invoice.get("InvoiceTotal", {}).get("value"),
            "AmountDue": invoice.get("AmountDue", {}).get("value"),
            "Supplier": {"Name": supplier.get("Name", {}).get("value"), "Address": supplier.get("Address", {}).get("value")},
            "Customer": {"Name": customer.get("Name", {}).get("value"), "Address": customer.get("Address", {}).get("value")}
        },
        "LineItems": line_items,
        "pdf_file": blob_url,
        "confidence": avg_conf,
        "confidence_detail": {
            "InvoiceId": invoice.get("InvoiceId", {}).get("confidence"),
            "InvoiceDate": invoice.get("InvoiceDate", {}).get("confidence"),
            "InvoiceTotal": invoice.get("InvoiceTotal", {}).get("confidence"),
        },
        "status": status,
        "processed_at": datetime.utcnow().isoformat(),
        "source_msg_id": msg_id,
        "attachment_id": attachment_id,
    }

    # Insert into MongoDB (CosmosDB/Mongo API aware)
    try:
        client = MongoClient(mongo_uri)
        db = client["email_invoice_db"]
        collection = db["invoicecollection"]
        result = collection.insert_one(doc_obj)
        inserted_id = str(result.inserted_id)
        logger.info("🧾 Stored invoice %s (status=%s, confidence=%.3f)", inserted_id, status, avg_conf)
        return inserted_id
    except errors.PyMongoError as e:
        logger.exception("❌ Failed to insert invoice into MongoDB: %s", e)
        raise
    except Exception as e:
        logger.exception("❌ Unexpected error storing invoice: %s", e)
        raise