import os
import base64
import json
from datetime import datetime
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from pymongo import MongoClient
from services.logger_utils import logger
from azure.ai.formrecognizer import _models  # For AddressValue, CurrencyValue, etc.

# ---------------------------------------------------------------------
# MongoDB and Azure setup
# ---------------------------------------------------------------------
FORM_RECOGNIZER_ENDPOINT = os.getenv("AZURE_FORM_RECOGNIZER_ENDPOINT")
FORM_RECOGNIZER_KEY = os.getenv("AZURE_FORM_RECOGNIZER_KEY")
MONGO_URI = os.getenv("MONGO_URI")

if not (FORM_RECOGNIZER_ENDPOINT and FORM_RECOGNIZER_KEY and MONGO_URI):
    logger.warning("⚠️ Missing some required environment variables for Azure AI or MongoDB connection.")

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def sanitize_for_mongo(data):
    """Recursively convert Azure SDK model objects into Mongo-safe Python types."""
    if isinstance(data, dict):
        return {k: sanitize_for_mongo(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_for_mongo(v) for v in data]
    elif isinstance(data, _models.AddressValue):
        return {
            "house_number": data.house_number,
            "road": data.road,
            "city": data.city,
            "postal_code": data.postal_code,
            "country_region": data.country_region,
            "street_address": data.street_address,
            "state_district": data.state_district,
        }
    elif isinstance(data, _models.CurrencyValue):
        return {
            "amount": data.amount,
            "symbol": data.symbol,
            "code": data.code,
        }
    elif hasattr(data, "__dict__"):
        return {k: sanitize_for_mongo(v) for k, v in vars(data).items()}
    else:
        return data

# ---------------------------------------------------------------------
# Main: Analyze and store
# ---------------------------------------------------------------------
def analyze_and_store(blob_url, content_b64, msg_id=None, attachment_id=None):
    """
    Analyze a PDF invoice stored in Azure Blob and store structured data in MongoDB.
    Returns the stored document ID or None on failure.
    """
    try:
        client = DocumentAnalysisClient(
            endpoint=FORM_RECOGNIZER_ENDPOINT,
            credential=AzureKeyCredential(FORM_RECOGNIZER_KEY)
        )

        logger.info(f"🧠 Starting analysis for blob={blob_url}")

        # Decode PDF content bytes
        content_bytes = base64.b64decode(content_b64)

        poller = client.begin_analyze_document("prebuilt-invoice", document=content_bytes)
        result = poller.result()

        if not result.documents:
            logger.warning("⚠️ No invoice documents detected in analysis result.")
            return None

        document = result.documents[0]
        invoice_data = {}

        # Flatten extracted fields
        for name, field in document.fields.items():
            invoice_data[name] = field.value if hasattr(field, "value") else str(field)

        confidence_values = [f.confidence for f in document.fields.values() if hasattr(f, "confidence")]
        confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
        status = "Exported" if confidence >= 0.7 else "Needs manual review"

        # Prepare record
        doc_id = f"{msg_id}_{attachment_id}" if msg_id and attachment_id else f"doc_{datetime.utcnow().timestamp()}"
        record = {
            "_id": doc_id,
            "invoice_data": sanitize_for_mongo(invoice_data),
            "pdf_file": blob_url,
            "confidence": confidence,
            "status": status,
            "processed_at": datetime.utcnow().isoformat(),
        }

        # Store in MongoDB (Cosmos)
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client["roxor_invoice_db"]
        collection = db["invoices"]

        # Insert safely
        collection.insert_one(record)
        logger.info(f"🧾 Stored invoice {doc_id} (status={status}, confidence={confidence:.2f})")

        return doc_id

    except Exception as e:
        logger.exception(f"❌ Failed to analyze/store invoice: {e}")
        return None
