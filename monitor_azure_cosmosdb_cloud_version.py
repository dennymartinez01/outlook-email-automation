"""
monitor_azure_cosmosdb_cloud_version.py

Background invoice-monitoring helper for your Azure App Service deployment.

Responsibilities:
  • Acquire Microsoft Graph token (via services.azure_graph_service)
  • Fetch new email messages and attachments
  • Upload PDF invoices to Azure Blob Storage
  • Send each PDF to Azure AI Form Recognizer
  • Store parsed invoice data into MongoDB (CosmosDB API)
  • Maintain logging for every step
"""

import logging
import os
import time
import base64
from typing import List
from azure.ai.formrecognizer import _models  # for AddressValue, etc.

from services.azure_graph_service import get_graph_token as _get_graph_token
from services.azure_graph_service import fetch_new_messages
from services.azure_blob_service import upload_pdf_blob
from services.azure_ai_service import analyze_and_store

from services.azure_graph_service import fetch_new_messages
from services.azure_graph_service import get_graph_token

# ---------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------
logger = logging.getLogger("invoice-monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ---------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------
def sanitize_for_mongo(data):
    """Recursively convert non-serializable Azure Form Recognizer model objects into JSON-safe dictionaries."""
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
# Token helper
# ---------------------------------------------------------------------
def get_graph_token() -> str:
    """Return a Microsoft Graph token via azure_graph_service."""
    return _get_graph_token()

# ---------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------
def check_emails(token: str, top: int = 50) -> List[str]:
    """
    Fetch new messages from ALL 3 inbox categories and process their PDF attachments.
    Returns list of stored MongoDB document IDs.
    """

    processed_docs: List[str] = []

    if not token:
        logger.error("No Graph token provided to check_emails()")
        return processed_docs

    # THREE TARGET INBOXES
    target_emails = {
        "nonstock": os.getenv("TARGET_EMAIL_NONSTOCK"),
        "stock": os.getenv("TARGET_EMAIL_STOCK"),
        "customer": os.getenv("TARGET_EMAIL_CUSTOMER")
    }

    # MAIN LOOP (FIXED)
    for category, email in target_emails.items():

        if not email:
            logger.warning(f"No inbox configured for category: {category}")
            continue

        logger.info(f"📬 Checking inbox for category={category}: {email}")

        # FETCH EMAILS FOR THIS CATEGORY
        try:
            messages = fetch_new_messages(token, category, email, top=top)
        except Exception as e:
            logger.exception(f"Failed to fetch messages for {category}: {e}")
            continue

        logger.info(f"📥 Retrieved {len(messages)} messages for category={category}")

        # --------------------------------------------
        # FIX: PROCESS MESSAGES *INSIDE* THE LOOP
        # --------------------------------------------

        for msg in messages or []:
            msg_id = msg.get("id") or msg.get("messageId") or "unknown-msg"
            attachments = msg.get("attachments", [])
            logger.info(
                f"Processing message {msg_id} in category={category} with {len(attachments)} attachment(s)"
            )

            for att in attachments:
                try:
                    content_type = att.get("contentType", "").lower()
                    filename = att.get("name") or "invoice.pdf"

                    # Process only PDFs
                    if not (content_type == "application/pdf" or filename.lower().endswith(".pdf")):
                        logger.info(f"Skipping non-PDF attachment: {filename}")
                        continue

                    attachment_id = att.get("id") or att.get("attachmentId") or "unknown-att"
                    safe_name = filename.replace(" ", "_").replace("/", "_")
                    blob_name = f"{msg_id}_{attachment_id}_{safe_name}"

                    # Decode Base64
                    content_b64 = att.get("contentBytes")
                    if not content_b64:
                        logger.warning(f"Attachment {attachment_id} has no contentBytes; skipping")
                        continue

                    try:
                        file_bytes = base64.b64decode(content_b64)
                    except Exception as e:
                        logger.error(f"Base64 decode failed for {attachment_id}: {e}")
                        continue

                    # Upload to Blob
                    try:
                        logger.info(f"Uploading blob: {blob_name}")
                        blob_url = upload_pdf_blob(blob_name, file_bytes)
                        logger.info(f"Blob uploaded: {blob_url}")
                    except Exception as e:
                        logger.exception(f"Blob upload failed for {blob_name}: {e}")
                        continue

                    # Analyze + Store
                    try:
                        logger.info(f"Analyzing invoice: {filename} (category={category})")

                        result = analyze_and_store(
                            blob_url,
                            content_b64,
                            msg_id,
                            attachment_id,
                            category=category     # ← IMPORTANT
                        )

                        if result:
                            safe_result = sanitize_for_mongo(result)
                            processed_docs.append(safe_result.get("_id") or safe_result)
                            logger.info(f"Stored invoice successfully: {safe_result.get('_id')}")
                        else:
                            logger.warning(f"No result returned for {filename}")

                    except Exception as e:
                        logger.exception(
                            f"Analysis/storage failed for msg={msg_id}, att={attachment_id}: {e}"
                        )
                        continue

                except Exception as e:
                    logger.exception(f"Unexpected error processing attachment: {e}")
                    continue

        logger.info(f"✔ Finished category={category}")

    logger.info(f"🎉 Completed all inboxes. Total stored documents: {len(processed_docs)}")
    return processed_docs


# ---------------------------------------------------------------------
# Optional: continuous monitor
# ---------------------------------------------------------------------
def run_forever(interval_seconds: int = 180):
    """Continuously check inbox every few minutes."""
    logger.info(" Starting continuous invoice monitor (interval=%ss)...", interval_seconds)
    while True:
        try:
            token = get_graph_token()
            if token:
                check_emails(token)
            else:
                logger.error("No Graph token retrieved this cycle.")
        except Exception as e:
            logger.exception("Fatal error in monitor loop: %s", e)
        time.sleep(interval_seconds)

# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------
if __name__ == "__main__":
    try:
        token = get_graph_token()
        if not token:
            print("Could not obtain Graph token.")
        else:
            print("Token acquired. Running one-time email check...")
            results = check_emails(token)
            print(f"Processed {len(results)} document(s):", results)
    except Exception as e:
        print("Startup failed:", e)
