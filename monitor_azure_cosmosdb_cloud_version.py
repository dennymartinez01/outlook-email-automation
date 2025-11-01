"""
monitor_azure_cosmosdb_cloud_version.py
---------------------------------------------------------
Background monitor for reading emails, downloading PDF attachments,
uploading to Azure Blob, optionally sending to Form Recognizer,
and recording results to MongoDB (processed_attachments + invoicecollection).

Key features:
 - Imports Graph service functions directly (for compatibility with previous builds)
 - Resilient retry handling and clear logging
 - Focused-folder safe: fetches recent messages (last 3 days)
 - Reprocesses messages with status == "failed"
 - Tracks success/failure in processed_attachments collection
---------------------------------------------------------
"""

import os
import traceback
from datetime import datetime
from services.logger_utils import logger, with_retry
from services import azure_blob_service, cosmos_service
from services.cosmos_service import get_db_collections

# ✅ Import Graph functions explicitly for full compatibility
from services.azure_graph_service import (
    get_graph_token,
    fetch_new_messages,
    list_message_attachments,
    download_attachment_bytes,
)

# Optional AI analyzer (if available)
try:
    from services import azure_ai_service
except Exception:
    azure_ai_service = None

# Cache for DB collections
COLS = None


def _get_cols():
    """
    Safely get MongoDB collections.
    Explicitly checks for None instead of using boolean testing,
    to avoid PyMongo NotImplementedError.
    """
    cols = get_db_collections()

    if not isinstance(cols, dict):
        raise ValueError("get_db_collections() must return a dict of collections.")

    required = ["invoices", "processed", "meta"]
    for key in required:
        val = cols.get(key)
        if val is None:
            raise ValueError(f"Missing Mongo collection: {key}")
        # Log collection connection info
        logger.info(f"✅ Connected to Mongo collection: {key}")

    return cols


def _is_pdf_attachment(att: dict) -> bool:
    """Detect whether an attachment is PDF based on contentType or filename."""
    ctype = (att.get("contentType") or "").lower()
    name = (att.get("name") or "").lower()
    return "pdf" in ctype or name.endswith(".pdf")


@with_retry(max_attempts=3, base_delay=2)
def check_emails(token: str):
    """Fetch messages, download new/failed PDFs, upload, analyze, and persist."""
    if not token:
        logger.error("❌ No Graph token provided to check_emails.")
        return 0

    cols = _get_cols()
    processed_col = cols["processed"]
    invoices_col = cols["invoices"]

    new_docs = 0
    try:
        messages = fetch_new_messages(token, top=50, days_back=3)
    except Exception:
        logger.exception("❌ Failed fetching messages from Graph.")
        return 0

    logger.info(f"📬 Retrieved {len(messages)} messages from Graph.")

    for msg in messages:
        msg_id = msg.get("id")
        subject = msg.get("subject", "<no subject>")
        received = msg.get("receivedDateTime")
        folder = msg.get("parentFolderId", "")
        is_read = msg.get("isRead", False)

        try:
            logger.info(
                f"📧 Message found: subject={subject!r} id={msg_id} folder={folder} isRead={is_read}"
            )

            existing = processed_col.find_one({"message_id": msg_id})
            if existing and existing.get("status") == "success":
                logger.info(f"⏩ Skipping already processed message {msg_id}")
                continue
            elif existing and existing.get("status") == "failed":
                logger.info(f"♻️ Reprocessing previously failed message {msg_id}")
            else:
                logger.info(f"🆕 New message to process: {msg_id}")

            # Get attachments
            try:
                attachments = list_message_attachments(token, msg_id)
            except Exception as e:
                logger.exception(f"❌ Failed to list attachments for {msg_id}")
                processed_col.update_one(
                    {"message_id": msg_id},
                    {
                        "$set": {
                            "message_subject": subject,
                            "status": "failed",
                            "error": f"list_attachments_failed: {e}",
                            "processed_at": datetime.utcnow(),
                        }
                    },
                    upsert=True,
                )
                continue

            pdf_found = False
            for att in attachments:
                if not _is_pdf_attachment(att):
                    logger.info(
                        f"📎 Skipping non-PDF attachment: {att.get('name')} ({att.get('contentType')})"
                    )
                    continue

                pdf_found = True
                att_id = att["id"]
                att_name = att.get("name") or f"{msg_id}_{att_id}.pdf"
                logger.info(f"📎 PDF detected: {att_name} — downloading...")

                # Download
                try:
                    content_bytes = download_attachment_bytes(token, msg_id, att_id)
                except Exception as e:
                    logger.exception(f"❌ Failed to download {att_name}")
                    processed_col.update_one(
                        {"message_id": msg_id},
                        {
                            "$set": {
                                "message_subject": subject,
                                "status": "failed",
                                "error": f"download_failed: {e}",
                                "processed_at": datetime.utcnow(),
                            }
                        },
                        upsert=True,
                    )
                    continue

                # Upload
                try:
                    blob_url = azure_blob_service.upload_pdf_blob(att_name, content_bytes)
                    logger.info(f"📤 Uploaded to Blob: {blob_url}")
                except Exception as e:
                    logger.exception(f"❌ Failed to upload {att_name}")
                    processed_col.update_one(
                        {"message_id": msg_id},
                        {
                            "$set": {
                                "message_subject": subject,
                                "status": "failed",
                                "error": f"blob_upload_failed: {e}",
                                "processed_at": datetime.utcnow(),
                            }
                        },
                        upsert=True,
                    )
                    continue

                # Analyze via AI (if available)
                invoice_data = {}
                if azure_ai_service:
                    ai_func = None
                    for fn in (
                        "analyze_invoice_bytes",
                        "analyze_document_bytes",
                        "analyze_bytes",
                        "analyze_invoice",
                    ):
                        if hasattr(azure_ai_service, fn):
                            ai_func = getattr(azure_ai_service, fn)
                            break
                    if ai_func:
                        try:
                            logger.info(f"🤖 Analyzing {att_name} via Azure AI...")
                            invoice_data = ai_func(content_bytes)
                            logger.info(f"🤖 AI analysis completed for {att_name}")
                        except Exception as e:
                            logger.exception("⚠️ Azure AI analysis failed")
                            processed_col.update_one(
                                {"message_id": msg_id},
                                {
                                    "$set": {
                                        "message_subject": subject,
                                        "status": "failed",
                                        "error": f"form_recognizer_failed: {e}",
                                        "processed_at": datetime.utcnow(),
                                    }
                                },
                                upsert=True,
                            )
                            continue
                    else:
                        logger.info("ℹ️ azure_ai_service found but no supported analysis function.")
                else:
                    logger.info("ℹ️ Azure AI service not configured, skipping analysis.")

                # Insert invoice doc
                invoice_doc = {
                    "message_id": msg_id,
                    "message_subject": subject,
                    "received_at": received,
                    "attachment_id": att_id,
                    "attachment_name": att_name,
                    "pdf_file": blob_url,
                    "invoice_data": invoice_data or {},
                    "status": "success",
                    "processed_at": datetime.utcnow(),
                }

                try:
                    invoices_col.insert_one(invoice_doc)
                    new_docs += 1
                    logger.info(f"💾 Stored invoice {att_name} in DB.")
                except Exception as e:
                    logger.exception("❌ Failed to insert invoice document")
                    processed_col.update_one(
                        {"message_id": msg_id},
                        {
                            "$set": {
                                "message_subject": subject,
                                "status": "failed",
                                "error": f"db_insert_failed: {e}",
                                "processed_at": datetime.utcnow(),
                            }
                        },
                        upsert=True,
                    )
                    continue

                processed_col.update_one(
                    {"message_id": msg_id},
                    {
                        "$set": {
                            "message_subject": subject,
                            "status": "success",
                            "attachment_id": att_id,
                            "attachment_name": att_name,
                            "pdf_file": blob_url,
                            "processed_at": datetime.utcnow(),
                        }
                    },
                    upsert=True,
                )

            if not pdf_found:
                logger.info(f"⚠️ No PDF attachments found for {msg_id}")
                processed_col.update_one(
                    {"message_id": msg_id},
                    {
                        "$set": {
                            "message_subject": subject,
                            "status": "failed",
                            "error": "no_pdf_attachments",
                            "processed_at": datetime.utcnow(),
                        }
                    },
                    upsert=True,
                )

        except Exception as e:
            logger.exception(f"❌ Unexpected error while processing {msg_id}")
            processed_col.update_one(
                {"message_id": msg_id},
                {
                    "$set": {
                        "message_subject": subject,
                        "status": "failed",
                        "error": f"unexpected_exception: {e}",
                        "traceback": traceback.format_exc(),
                        "processed_at": datetime.utcnow(),
                    }
                },
                upsert=True,
            )

    logger.info(f"✅ Email check complete. {new_docs} new documents stored.")
    return new_docs


# Manual debug entrypoint
if __name__ == "__main__":
    logger.info("🔍 Running manual email check (local test)")
    try:
        token = get_graph_token()
        check_emails(token)
    except Exception:
        logger.exception("❌ Manual run failed")
