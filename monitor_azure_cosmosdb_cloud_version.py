"""monitor_azure_cosmosdb_cloud_version.py

Regenerated background helper for your project.
Exposes:
  - get_graph_token() -> str
  - check_emails(token) -> list of processed doc ids (or empty list)

This file delegates to the modular service files in services/:
  - services.azure_graph_service.get_graph_token, fetch_new_messages
  - services.azure_blob_service.upload_pdf_blob
  - services.azure_ai_service.analyze_and_store
"""

import logging
import time
from typing import List

# Import service functions (these must exist in services/)
from services.azure_graph_service import get_graph_token as _get_graph_token
from services.azure_graph_service import fetch_new_messages
from services.azure_blob_service import upload_pdf_blob
from services.azure_ai_service import analyze_and_store

logger = logging.getLogger("invoice-monitor.monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def get_graph_token():
    """Return a Graph token by delegating to services.azure_graph_service.get_graph_token."""
    return _get_graph_token()

def check_emails(token: str, top: int = 50) -> List[str]:
    """Fetch new messages and process PDF attachments.

    - Uses fetch_new_messages(token, top) which should already filter processed attachments.
    - For each PDF attachment we:
        1. Upload base64 bytes to Blob via upload_pdf_blob()
        2. Call analyze_and_store() to send to Form Recognizer and save results
    - Returns list of successfully stored document ids.
    """
    processed_docs = []

    if not token:
        logger.error("No Graph token provided to check_emails()")
        return processed_docs

    try:
        messages = fetch_new_messages(token, top=top)
    except Exception as e:
        logger.exception("Failed to fetch messages from Graph: %s", e)
        return processed_docs

    logger.info("Processing %d messages", len(messages or []))

    for msg in messages or []:
        msg_id = msg.get("id") or msg.get("messageId") or "unknown-msg"
        attachments = msg.get("attachments", [])

        for att in attachments:
            try:
                content_type = att.get("contentType", "").lower()
                if content_type != "application/pdf" and not (att.get("name", "").lower().endswith('.pdf')):
                    logger.info("Skipping non-pdf attachment: %s (type=%s)", att.get('name'), content_type)
                    continue

                attachment_id = att.get("id") or att.get("attachmentId") or "unknown-att"
                original_name = att.get("name") or "invoice.pdf"
                safe_name = original_name.replace(" ", "_").replace('/', '_')
                blob_name = f"{msg_id}_{attachment_id}_{safe_name}"

                # contentBytes is expected to be base64 string as returned by Microsoft Graph
                content_b64 = att.get("contentBytes")
                if not content_b64:
                    logger.warning("Attachment %s in message %s has no contentBytes; skipping", attachment_id, msg_id)
                    continue

                logger.info("Uploading blob for message=%s attachment=%s", msg_id, attachment_id)
                try:
                    blob_url = upload_pdf_blob(blob_name, content_b64)
                except Exception as e:
                    logger.exception("Blob upload failed for %s (msg=%s): %s", blob_name, msg_id, e)
                    continue

                logger.info("Analyzing invoice for message=%s attachment=%s", msg_id, attachment_id)
                try:
                    doc_id = analyze_and_store(blob_url, content_b64, msg_id, attachment_id)
                    processed_docs.append(doc_id)
                    logger.info("Processed and stored doc_id=%s", doc_id)
                except Exception as e:
                    logger.exception("Analysis/storage failed for msg=%s att=%s: %s", msg_id, attachment_id, e)
                    # do not mark processed so it can be retried next run
                    continue

            except Exception as e:
                logger.exception("Unexpected error processing attachment: %s", e)
                continue

    return processed_docs


if __name__ == "__main__":
    # quick local smoke test (will only attempt to fetch token if env/configured)
    try:
        tok = get_graph_token()
        print("Token obtained (length):", len(tok) if tok else "None")
    except Exception as e:
        print("Could not get token:", e)
