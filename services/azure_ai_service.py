import os
import base64
import json
from datetime import datetime, date
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from pymongo import MongoClient
from services.logger_utils import logger
from azure.ai.formrecognizer import _models  # For AddressValue, CurrencyValue, etc.
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------
# MongoDB and Azure setup
# ---------------------------------------------------------------------
FORM_RECOGNIZER_ENDPOINT = os.getenv("AZURE_FORMRECOGNIZER_ENDPOINT")
FORM_RECOGNIZER_KEY = os.getenv("AZURE_FORMRECOGNIZER_KEY")
MONGO_URI = os.getenv("MONGO_URI")

if not (FORM_RECOGNIZER_ENDPOINT and FORM_RECOGNIZER_KEY and MONGO_URI):
    logger.warning("⚠️ Missing some required environment variables for Azure AI or MongoDB connection.")

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def sanitize_for_mongo(data):
    from azure.ai.formrecognizer import _models

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
    elif isinstance(data, _models.DocumentField):
        if hasattr(data, "value"):
            if data.value_type == "date":
                return str(data.value) if data.value else None
            elif data.value_type == "time":
                return str(data.value) if data.value else None
            elif data.value_type == "phoneNumber":
                return str(data.value) if data.value else None
            elif data.value_type == "email":
                return str(data.value) if data.value else None
            elif data.value_type in ["string", "number", "integer", "float", "boolean"]:
                return data.value
            elif data.value_type in ["list", "dictionary"]:
                return sanitize_for_mongo(data.value)
            else:
                return str(data.value)
        else:
            return None
    elif hasattr(data, "__dict__"):
        return {k: sanitize_for_mongo(v) for k, v in vars(data).items()}
    else:
        return data


def make_bson_safe(obj):
    """Recursively convert datetime.date objects to ISO strings."""
    if isinstance(obj, dict):
        return {k: make_bson_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_bson_safe(i) for i in obj]
    elif isinstance(obj, (datetime, date)):
        return obj.isoformat()
    else:
        return obj


# ---------------------------------------------------------------------
# Main: Analyze and store
# ---------------------------------------------------------------------
def analyze_and_store(blob_url, content_b64, msg_id=None, attachment_id=None):
    try:
        client = DocumentAnalysisClient(
            endpoint=FORM_RECOGNIZER_ENDPOINT,
            credential=AzureKeyCredential(FORM_RECOGNIZER_KEY)
        )

        logger.info(f"🧠 Starting analysis for blob={blob_url}")

        content_bytes = base64.b64decode(content_b64)
        poller = client.begin_analyze_document("prebuilt-invoice", document=content_bytes)
        result = poller.result()

        if not result.documents:
            logger.warning("⚠️ No invoice documents detected in analysis result.")
            return None

        document = result.documents[0]
        invoice_data = {}

        for name, field in document.fields.items():
            value = getattr(field, "value", None)
            if value is None:
                logger.warning(f"⚠️ Field '{name}' has no value.")
            invoice_data[name] = sanitize_for_mongo(value)

        confidence_values = [
            f.confidence for f in document.fields.values()
            if hasattr(f, "confidence") and f.confidence is not None
        ]
        confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
        status = "Exported" if confidence >= 0.7 else "Needs manual review"

        doc_id = f"{msg_id}_{attachment_id}" if msg_id and attachment_id else f"doc_{datetime.utcnow().timestamp()}"
        record = {
            "_id": doc_id,
            "invoice_data": invoice_data,
            "pdf_file": blob_url,
            "confidence": confidence,
            "status": status,
            "processed_at": datetime.utcnow().isoformat(),
        }

        # Convert record into BSON-safe structure
        record = make_bson_safe(record)

        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client["email_invoice_db"]
        collection = db["invoicecollection"]

        try:
            collection.insert_one(record)
            logger.info(f"🧾 Stored invoice {doc_id} (status={status}, confidence={confidence:.2f})")
        except Exception as mongo_err:
            logger.exception(f"❌ Failed to insert record in MongoDB: {mongo_err}")
            return None

        return doc_id

    except Exception as e:
        logger.exception(f"❌ Failed to analyze/store invoice: {e}")
        return None
