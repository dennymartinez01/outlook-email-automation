"""
services/azure_graph_service.py
---------------------------------------------------------
Microsoft Graph helpers (auth + message listing + attachments)
- Uses GRAPH_* env var names (matching your .env)
- fetch_new_messages returns recent messages (metadata)
- list_message_attachments returns attachment metadata
- download_attachment_bytes streams binary bytes for an attachment
---------------------------------------------------------
"""

import os
import requests
from datetime import datetime, timedelta
from services.logger_utils import logger, with_retry

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Expecting GRAPH_* names in your .env
TENANT_ID = os.getenv("GRAPH_TENANT_ID")
CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET")
USER_EMAIL = os.getenv("TARGET_USER_EMAIL")

GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Early warnings
if not TENANT_ID:
    logger.error("❌ GRAPH_TENANT_ID not set in environment.")
if not CLIENT_ID:
    logger.error("❌ GRAPH_CLIENT_ID not set in environment.")
if not CLIENT_SECRET:
    logger.error("❌ GRAPH_CLIENT_SECRET not set in environment.")
if not USER_EMAIL:
    logger.warning("⚠️ TARGET_USER_EMAIL not set in environment — Graph calls will fail.")


@with_retry(max_attempts=3, base_delay=2)
def get_graph_token() -> str:
    """
    Acquire client_credentials token for Microsoft Graph.
    """
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        raise RuntimeError("Missing GRAPH_TENANT_ID/GRAPH_CLIENT_ID/GRAPH_CLIENT_SECRET environment values.")

    logger.info(f"🔐 Requesting Graph token with Tenant={TENANT_ID}, Client={CLIENT_ID[:8]}..., Secret={'set' if CLIENT_SECRET else 'missing'}")
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": GRAPH_SCOPE,
        "grant_type": "client_credentials"
    }
    resp = requests.post(url, data=data, timeout=30)
    if resp.status_code != 200:
        logger.error(f"❌ Failed to acquire Graph token: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Failed to parse access_token from Graph response.")
    logger.info("🔑 Obtained Microsoft Graph access token.")
    return token


@with_retry(max_attempts=3, base_delay=2)
def fetch_new_messages(token: str, top: int = 50, days_back: int = 3):
    """
    Fetch recent messages across the mailbox. Default: last `days_back` days, up to `top` messages.
    Returns list of message dicts (id, subject, receivedDateTime, hasAttachments, parentFolderId, etc.)
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    date_filter = (datetime.utcnow() - timedelta(days=days_back)).replace(microsecond=0).isoformat() + "Z"
    # Select core fields and order by receivedDateTime desc, fetch up to `top`
    url = (
        f"{GRAPH_BASE_URL}/users/{USER_EMAIL}/messages"
        f"?$filter=receivedDateTime ge {date_filter}"
        f"&$orderby=receivedDateTime desc"
        f"&$select=id,subject,receivedDateTime,hasAttachments,conversationId,parentFolderId,isRead"
        f"&$top={top}"
    )

    logger.info(f"📨 Fetching up to {top} messages since {date_filter} from {USER_EMAIL}")
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        logger.error(f"❌ Failed to fetch messages: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    data = resp.json()
    messages = data.get("value", [])
    logger.info(f"✅ Retrieved {len(messages)} messages.")
    return messages


@with_retry(max_attempts=3, base_delay=2)
def list_message_attachments(token: str, message_id: str):
    """
    Returns metadata for attachments of a message.
    """
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE_URL}/users/{USER_EMAIL}/messages/{message_id}/attachments?$top=50"
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        logger.error(f"❌ list_message_attachments failed ({message_id}): {resp.status_code} {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    attachments = data.get("value", [])
    logger.info(f"📎 Found {len(attachments)} attachments for message {message_id}")
    return attachments


@with_retry(max_attempts=3, base_delay=2)
def download_attachment_bytes(token: str, message_id: str, attachment_id: str) -> bytes:
    """
    Download binary attachment via /$value endpoint (best for file attachments).
    """
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE_URL}/users/{USER_EMAIL}/messages/{message_id}/attachments/{attachment_id}/$value"
    logger.info(f"⬇️ Downloading attachment bytes for msg={message_id}, att={attachment_id}")
    resp = requests.get(url, headers=headers, stream=True, timeout=60)
    if resp.status_code != 200:
        logger.error(f"❌ download_attachment_bytes failed ({attachment_id}): {resp.status_code} {resp.text}")
        resp.raise_for_status()
    return resp.content


# quick manual test helper when run standalone
if __name__ == "__main__":
    logger.info("🔍 Testing Graph connectivity...")
    token = get_graph_token()
    msgs = fetch_new_messages(token, top=10, days_back=3)
    for m in msgs:
        logger.info(f"MSG: {m.get('subject')} | {m.get('receivedDateTime')} | id={m.get('id')}")
    if msgs:
        mid = msgs[0]["id"]
        atts = list_message_attachments(token, mid)
        if atts:
            att = atts[0]
            logger.info(f"First att: {att.get('name')} / {att.get('contentType')}")
            content = download_attachment_bytes(token, mid, att["id"])
            logger.info(f"Downloaded {len(content)} bytes")
