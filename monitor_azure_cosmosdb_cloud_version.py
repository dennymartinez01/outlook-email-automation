# monitor_azure_cosmosdb_cloud_version.py
"""
Monitors Outlook mailbox for invoice PDFs:
 - Fetches new messages via Microsoft Graph
 - Skips messages already processed (Mongo 'processed_attachments')
 - Downloads PDF attachments via Graph /$value
 - Uploads to Azure Blob (inline-displayed PDFs)
 - Analyzes invoices using Azure Form Recognizer
 - Stores results in MongoDB ('invoicecollection')
 - Records success/failure status in 'processed_attachments'
"""

from typing import List
from services.logger_utils import logger
from services.checkpoint_service import mark_message_processed, is_message_processed
from services.failure_service import record_failure
from services.azure_blob_service import upload_pdf_blob
from services.azure_ai_service import analyze_and_store
from services.azure_graph_service import (
    get_graph_token,
    fetch_new_messages,
    list_message_attachments,
    download_attachment_bytes,
)


def check_emails(token: str, top: int = 50) -> List[str]:
    """
    Fetch new messages and process PDF attachments.
    Returns list of processed document IDs (from analyze_and_store).
    """
    processed_docs: List[str] = []

    if not token:
        logger.error("❌ No Graph token provided to check_emails()")
        return processed_docs

    try:
        messages = fetch_new_messages(token, top=top)
    except Exception as e:
        logger.exception("❌ Failed to fetch messages from Graph: %s", e)
        return processed_docs

    logger.info("📬 Retrieved %d messages from Graph.", len(messages or []))

    for msg in messages or []:
        msg_id = msg.get("id") or msg.get("messageId")
        if not msg_id:
            logger.warning("⚠️ Skipping message with no id: %s", msg)
            continue

        # Skip if message already processed successfully
        if is_message_processed(msg_id):
            logger.info("⏩ Skipping already processed message %s", msg_id)
            continue

        logger.info("🔍 Processing message %s (%s)", msg_id, msg.get("subject", "")[:100])

        try:
            attachments = list_message_attachments(token, msg_id)
        except Exception as e:
            logger.exception("❌ Failed to list attachments for message %s: %s", msg_id, e)
            record_failure(msg_id, "", str(e), "list_attachments")
            continue

        if not attachments:
            logger.info("📭 No attachments found for message %s — marking as processed", msg_id)
            mark_message_processed(msg_id)
            continue

        for att in attachments:
            try:
                attachment_id = att.get("id") or att.get("attachmentId")
                filename = att.get("name") or att.get("displayName") or "attachment.pdf"
                content_type = (att.get("contentType") or "").lower()

                # Process only PDFs
                if not (content_type == "application/pdf" or filename.lower().endswith(".pdf")):
                    logger.info("Skipping non-PDF attachment: %s (%s)", filename, content_type)
                    continue

                logger.info("📥 Downloading attachment %s (%s)", attachment_id, filename)
                try:
                    file_bytes = download_attachment_bytes(token, msg_id, attachment_id)
                except Exception as e:
                    record_failure(msg_id, attachment_id, str(e), "download_attachment")
                    continue

                blob_name = f"{msg_id}_{attachment_id}_{filename.replace(' ', '_')}"
                try:
                    blob_url = upload_pdf_blob(blob_name, file_bytes)
                    logger.info("☁️ Uploaded blob: %s", blob_name)
                except Exception as e:
                    record_failure(msg_id, attachment_id, str(e), "upload_blob")
                    continue

                try:
                    doc_id = analyze_and_store(blob_url, file_bytes, msg_id, attachment_id)
                    if doc_id:
                        processed_docs.append(doc_id)
                        logger.info("✅ Stored invoice doc_id=%s (msg=%s att=%s)", doc_id, msg_id, attachment_id)
                    else:
                        record_failure(msg_id, attachment_id, "analyze_and_store returned None", "form_recognizer")
                except Exception as e:
                    record_failure(msg_id, attachment_id, str(e), "form_recognizer")
                    continue

            except Exception as e:
                logger.exception("💥 Unexpected error processing attachment in msg %s: %s", msg_id, e)
                record_failure(msg_id, att.get("id", ""), str(e), "processing")
                continue

        # After all attachments are handled (success or fail), mark message processed
        try:
            mark_message_processed(msg_id)
        except Exception as e:
            logger.exception("⚠️ Could not mark message %s processed: %s", msg_id, e)

    logger.info("✅ Email check cycle complete. %d new documents stored.", len(processed_docs))
    return processed_docs


def get_graph_token_safe():
    """Obtain Graph token safely for background thread use."""
    try:
        return get_graph_token()
    except Exception as e:
        logger.exception("❌ Could not obtain Graph token: %s", e)
        return None


# Manual testing entry point
if __name__ == "__main__":
    token = get_graph_token_safe()
    if not token:
        logger.error("No Graph token available for manual run.")
    else:
        check_emails(token, top=50)
