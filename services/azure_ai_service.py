"""
services/azure_ai_service.py

Handles:
  - Sending uploaded PDF blobs to Azure Document Intelligence (Form Recognizer)
  - Extracting structured invoice data
  - Storing the analysis results into MongoDB (CosmosDB API)
  - Confidence-based status handling
  - Automatic Hot→Cool blob tier optimization
"""

import os
import io
import base64
import logging
import time
from datetime import datetime, timedelta

from pymongo import MongoClient
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError
from azure.storage.blob import BlobServiceClient, StandardBlobTier

# ---------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------
FORM_RECOGNIZER_ENDPOINT = os.getenv("AZURE_FORMRECOGNIZER_ENDPOINT")
FORM_RECOGNIZER_KEY = os.getenv("AZURE_FORMRECOGNIZER_KEY")
MONGO_URI = os.getenv("MONGO_URI")
BLOB_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
BLOB_CONTAINER = os.getenv("BLOB_CONTAINER", "invoices")

# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------
logger = logging.getLogger("invoice-monitor.azure-ai-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

form_client = DocumentAnalysisClient(
    endpoint=FORM_RECOGNIZER_ENDPOINT,
    credential=AzureKeyCredential(FORM_RECOGNIZER_KEY)
)
blob_service = BlobServiceClient.from_connection_string(BLOB_CONN_STR)
blob_container_client = blob_service.get_container_client(BLOB_CONTAINER)

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _retry_call(fn, max_retries=3, delay=3):
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning("Retrying after error (%s)... attempt %d/%d", e, attempt + 1, max_retries)
                time.sleep(delay)
            else:
                logger.error("Max retries exceeded: %s", e)
                raise

def _transition_to_cool(blob_name: str):
    """Move blob to Cool tier if older than 7 days."""
    try:
        blob_client = blob_container_client.get_blob_client(blob_name)
        props = blob_client.get_blob_properties()
        last_modified = props['last_modified']

        if datetime.utcnow() - last_modified.replace(tzinfo=None) > timedelta(days=7):
            blob_client.set_standard_blob_tier(StandardBlobTier.Cool)
            logger.info("🌡️ Transitioned blob '%s' to Cool tier", blob_name)
    except Exception as e:
        logger.warning("Could not transition blob '%s' to Cool tier: %s", blob_name, e)

# ---------------------------------------------------------------------
# Core Analyzer
# ---------------------------------------------------------------------
def analyze_and_store(blob_url: str, content_b64: str, msg_id: str, attachment_id: str) -> str:
    """
    Analyzes a PDF invoice via Azure Form Recognizer and stores the structured data into MongoDB.

    Args:
        blob_url:      The Azure Blob URL (already uploaded)
        content_b64:   The base64 PDF content
        msg_id:        Email message ID
        attachment_id: Attachment ID

    Returns:
        str: Mongo document ID if stored successfully, else None.
    """
    logger.info("🧠 Starting analysis for blob=%s", blob_url)
    pdf_bytes = base64.b64decode(content_b64)

    # Analyze with Azure Form Recognizer (with retries)
    def analyze():
        return form_client.begin_analyze_document("prebuilt-invoice", pdf_bytes)

    try:
        poller = _retry_call(analyze)
        result = poller.result()
    except AzureError as e:
        logger.exception("Azure Form Recognizer failed: %s", e)
        return None
    except Exception as e:
        logger.exception("Error calling Form Recognizer: %s", e)
        return None

    # Extract and structure data
    invoice_data = {}
    confidence_scores = []

    try:
        for doc in result.documents:
            for field_name, field_value in doc.fields.items():
                val = field_value.value if field_value else None
                conf = field_value.confidence or 0
                invoice_data[field_name] = val
                confidence_scores.append(conf)

        # Include line items if present
        if result.tables:
            invoice_data["line_items"] = []
            for table in result.tables:
                rows = []
                for cell in table.cells:
                    rows.append({
                        "row": cell.row_index,
                        "column": cell.column_index,
                        "content": cell.content,
                        "confidence": cell.confidence
                    })
                invoice_data["line_items"].extend(rows)

    except Exception as e:
        logger.exception("Error parsing Form Recognizer output: %s", e)

    # Compute confidence and status
    confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0.0
    status = "Exported" if confidence >= 0.7 else "Needs manual review"

    # MongoDB insert
    try:
        client = MongoClient(MONGO_URI)
        db = client["roxor_invoice_db"]
        collection = db["invoices"]

        doc_id = f"{msg_id}_{attachment_id}"
        record = {
            "_id": doc_id,
            "invoice_data": invoice_data,
            "pdf_file": blob_url,
            "confidence": confidence,
            "status": status,
            "processed_at": datetime.utcnow().isoformat()
        }

        collection.insert_one(record)
        logger.info("🧾 Stored invoice %s (status=%s, confidence=%.2f)", doc_id, status, confidence)
    except Exception as e:
        logger.exception("Database insert failed: %s", e)
        return None

    # Schedule tier transition (Hot → Cool)
    try:
        blob_name = blob_url.split("/")[-1]
        _transition_to_cool(blob_name)
    except Exception as e:
        logger.warning("Blob tier management skipped for %s: %s", blob_url, e)

    return doc_id
