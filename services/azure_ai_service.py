import os, requests
from datetime import datetime
from services.logger_utils import logger, retry
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

FORM_ENDPOINT = os.getenv("AZURE_FORMRECOGNIZER_ENDPOINT")
FORM_KEY = os.getenv("AZURE_FORMRECOGNIZER_KEY")
MONGO_URI = os.getenv("MONGO_URI")

@retry(max_attempts=3)
def analyze_and_store(blob_url, doc_id, invoice_data):
    headers = {
        "Ocp-Apim-Subscription-Key": FORM_KEY,
        "Content-Type": "application/json"
    }
    data = {"urlSource": blob_url}
    analyze_url = f"{FORM_ENDPOINT}/formrecognizer/documentModels/prebuilt-invoice:analyze?api-version=2023-07-31"
    resp = requests.post(analyze_url, headers=headers, json=data)
    resp.raise_for_status()
    result = resp.json()

    confidence = result.get("confidence", 1.0)
    status = "Exported" if confidence >= 0.7 else "Needs manual review"

    client = MongoClient(MONGO_URI)
    db = client["roxor_invoice_db"]
    collection = db["invoices"]

    record = {
        "_id": doc_id,
        "invoice_data": invoice_data,
        "pdf_file": blob_url,
        "confidence": confidence,
        "status": status,
        "processed_at": datetime.utcnow().isoformat()
    }
    collection.insert_one(record)
    logger.info(f"🧾 Stored invoice {doc_id} (status={status}, confidence={confidence:.2f})")
    return record
