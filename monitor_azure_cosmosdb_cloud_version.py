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

Delegated service modules:
  - services.azure_graph_service.get_graph_token
  - services.azure_graph_service.fetch_new_messages
  - services.azure_blob_service.upload_pdf_blob
  - services.azure_ai_service.analyze_and_store
"""

import logging
import time
from typing import List
from azure.ai.formrecognizer import _models  # for AddressValue, etc.

from services.azure_graph_service import get_graph_token as _get_graph_token
from services.azure_graph_service import fetch_new_messages
from services.azure_blob_service import upload_pdf_blob
from services.azure_ai_service import analyze_and_store

# ---------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------
logger = logging.getLogger("invoice-monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ---------------------------------------------------------------------
# Serialization helper (critical fix)
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
# Core loop
# ---------------------------------------------------------------------
def check_emails(token: str, top: int = 50) -> List[str]:
    """
    Fetch new messages and process any PDF attachments.

    Flow per email:
      1. Fetch via Graph API (using services.azure_graph_service)
      2. For each PDF attachment:
           • Upload PDF to Blob via upload_pdf_blob()
           • Analyze + store results using analyze_and_store()
      3. Return list of stored document IDs

    Args:
        token: Graph access token
        top:   Max messages to fetch per cycle

    Returns:
        List of successfully processed document IDs.
    """
    processed_docs: List[str] = []

    if not token:
        logger.error("❌ No Graph token provided to check_emails()")
        return processed_docs

    try:
        messages = fetch_new_messages(token, top=top)
    except Exception as e:
        logger.exception("Failed to fetch messages from Graph: %s", e)
        return processed_docs

    msg_count = len(messages or [])
    logger.info("📬 Retrieved %d new messages from Graph", msg_count)

    for msg in messages or []:
        msg_id = msg.get("id") or msg.get("messageId") or "unknown-msg"
        attachments = msg.get("attachments", [])

        logger.info("Processing message %s with %d attachments", msg_id, len(attachments))

        for att in attachments:
            try:
                # --- Validate attachment type ---
                content_type = att.get("contentType", "").lower()
                filename = att.get("name") or "invoice.pdf"
                if not (content_type == "application/pdf" or filename.lower().endswith(".pdf")):
                    logger.info("Skipping non-PDF attachment: %s (type=%s)", filename, content_type)
                    continue

                attachment_id = att.get("id") or att.get("attachmentId") or "unknown-att"
                safe_name = filename.replace(" ", "_").replace("/", "_")
                blob_name = f"{msg_id}_{attachment_id}_{safe_name}"

                # --- Extract content bytes ---
                content_b64 = att.get("contentBytes")
                if not content_b64:
                    logger.warning("Attachment %s in message %s has no contentBytes; skipping", attachment_id, msg_id)
                    continue

                # --- Upload to Azure Blob ---
                try:
                    logger.info("☁️ Uploading blob: %s", blob_name)
                    blob_url = upload_pdf_blob(blob_name, content_b64)
                    logger.info("✅ Blob uploaded successfully: %s", blob_url)
                except Exception as e:
                    logger.exception("Blob upload failed for %s (msg=%s): %s", blob_name, msg_id, e)
                    continue

                # --- Analyze + Store via Azure AI ---
                try:
                    logger.info("🧠 Analyzing invoice for %s...", filename)
                    result = analyze_and_store(blob_url, content_b64, msg_id, attachment_id)
                    if result:
                        # Apply sanitization before MongoDB insert
                        safe_result = sanitize_for_mongo(result)
                        processed_docs.append(safe_result.get("_id") or safe_result)
                        logger.info("🧾 Stored invoice successfully: %s", safe_result.get("_id"))
                    else:
                        logger.warning("⚠️ analyze_and_store returned no doc_id for %s", filename)
                except Exception as e:
                    logger.exception("Analysis/storage failed for msg=%s att=%s: %s", msg_id, attachment_id, e)
                    continue

            except Exception as e:
                logger.exception("Unexpected error processing attachment in msg=%s: %s", msg_id, e)
                continue

    logger.info("✅ Completed processing %d messages; %d stored documents", msg_count, len(processed_docs))
    return processed_docs

# ---------------------------------------------------------------------
# Optional: continuous loop
# ---------------------------------------------------------------------
def run_forever(interval_seconds: int = 180):
    """Continuously check inbox every few minutes."""
    logger.info("🔁 Starting continuous invoice monitor (interval=%ss)...", interval_seconds)
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
