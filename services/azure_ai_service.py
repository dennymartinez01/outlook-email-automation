import os
import base64
import requests
import logging
from pymongo import MongoClient
from datetime import datetime

logger = logging.getLogger("invoice-monitor.ai")

def analyze_and_store(blob_url, content_b64, msg_id, attachment_id):
    """
    Analyze a PDF invoice via Azure Form Recognizer and store structured data in MongoDB.
    """
    try:
        endpoint = os.getenv("AZURE_FORMRECOGNIZER_ENDPOINT")
        key = os.getenv("AZURE_FORMRECOGNIZER_KEY")
        mongo_uri = os.getenv("MONGO_URI")

        if not endpoint or not key or not mongo_uri:
            raise ValueError("Missing Azure Form Recognizer or MongoDB credentials")

        # 1️⃣ Send to Azure Form Recognizer
        pdf_bytes = base64.b64decode(content_b64)
        analyze_url = f"{endpoint}/formrecognizer/documentModels/prebuilt-invoice:analyze?api-version=2023-07-31"
        headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/pdf"}

        logger.info(f"🧠 Sending PDF to Form Recognizer for analysis (msg={msg_id})")
        resp = requests.post(analyze_url, headers=headers, data=pdf_bytes)
        resp.raise_for_status()
        result = resp.json()

        # 2️⃣ Extract key fields
        doc = result.get("documents", [{}])[0]
        fields = doc.get("fields", {})
        confidence = doc.get("confidence", 0.0)

        invoice_data = {
            "Invoice": {
                "InvoiceNumber": fields.get("InvoiceId", {}).get("valueString"),
                "InvoiceDate": fields.get("InvoiceDate", {}).get("valueDate"),
                "DueDate": fields.get("DueDate", {}).get("valueDate"),
                "Supplier": {"Name": fields.get("VendorName", {}).get("valueString")},
                "Customer": {"Name": fields.get("CustomerName", {}).get("valueString")},
                "InvoiceTotal": fields.get("InvoiceTotal", {}).get("valueNumber"),
                "Currency": fields.get("InvoiceCurrencyCode", {}).get("valueString"),
            },
            "LineItems": fields.get("Items", {}).get("valueArray", []),
        }

        # 3️⃣ Determine status
        status = "Exported" if confidence >= 0.7 else "Needs manual review"

        # 4️⃣ Insert into MongoDB
        client = MongoClient(mongo_uri)
        db = client["email_invoice_db"]
        collection = db["invoicecollection"]

        record = {
            "Invoice": invoice_data["Invoice"],
            "LineItems": invoice_data["LineItems"],
            "pdf_file": blob_url,
            "confidence": confidence,
            "status": status,
            "processed_at": datetime.utcnow().isoformat(),
        }

        result = collection.insert_one(record)
        doc_id = str(result.inserted_id)
        logger.info(f"🧾 Stored invoice {doc_id} (status={status}, confidence={confidence:.2f})")
        return doc_id

    except requests.exceptions.RequestException as e:
        logger.exception(f"❌ Azure Form Recognizer API request failed: {e}")
        raise
    except Exception as e:
        logger.exception(f"❌ Failed to analyze or store invoice (msg={msg_id}): {e}")
        raise