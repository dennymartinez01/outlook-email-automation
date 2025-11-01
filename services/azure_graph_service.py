"""
services/azure_graph_service.py
---------------------------------------------------------
Handles all interactions with Microsoft Graph API:
 - Auth (OAuth2 client credentials)
 - Fetching recent messages
 - Listing message attachments
 - Downloading attachment bytes via /$value endpoint
---------------------------------------------------------
"""

import os
import requests
from datetime import datetime
from services.logger_utils import logger, with_retry

# -----------------------------------------------------
# Config
# -----------------------------------------------------
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
USER_EMAIL = os.getenv("TARGET_EMAIL")  # mailbox to read invoices from
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

if not USER_EMAIL:
    logger.warning("⚠️ TARGET_EMAIL not set in environment — Graph calls will fail.")


# -----------------------------------------------------
# Auth: Get access token
# -----------------------------------------------------
@with_retry(max_attempts=3, base_delay=2)
def get_graph_token() -> str:
    """
    Authenticate to Microsoft Graph using client credentials.
    Returns an access token string.
    """
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
    logger.info("🔑 Obtained Microsoft Graph access token.")
    return token


# -----------------------------------------------------
# Fetch messages (metadata only, no attachments)
# -----------------------------------------------------
@with_retry(max_attempts=3, base_delay=2)
def fetch_new_messages(token: str, top: int = 10):
    """
    Fetch recent messages (metadata only) from the configured mailbox.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    # Get recent messages (ordered descending)
    url = (
        f"{GRAPH_BASE_URL}/users/{USER_EMAIL}/messages"
        f"?$top={top}&$select=id,subject,receivedDateTime,hasAttachments"
        f"&$orderby=receivedDateTime desc"
    )

    logger.info(f"📨 Fetching up to {top} recent messages from {USER_EMAIL}")
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        logger.error(f"❌ Failed to fetch messages: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    data = resp.json()
    messages = data.get("value", [])
    logger.info(f"✅ Retrieved {len(messages)} messages.")
    return messages


# -----------------------------------------------------
# List attachments metadata for a message
# -----------------------------------------------------
@with_retry(max_attempts=3, base_delay=2)
def list_message_attachments(token: str, message_id: str):
    """
    Returns metadata for all attachments of a specific message.
    Example response:
      [{ "id": "...", "name": "invoice.pdf", "contentType": "application/pdf" }, ...]
    """
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE_URL}/users/{USER_EMAIL}/messages/{message_id}/attachments?$top=50"
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        logger.error(f"❌ list_message_attachments failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    data = resp.json()
    attachments = data.get("value", [])
    logger.info(f"📎 Found {len(attachments)} attachments for message {message_id}")
    return attachments


# -----------------------------------------------------
# Download attachment binary content via /$value
# -----------------------------------------------------
@with_retry(max_attempts=3, base_delay=2)
def download_attachment_bytes(token: str, message_id: str, attachment_id: str) -> bytes:
    """
    Downloads the binary contents of an attachment via Graph API /$value endpoint.
    Returns raw bytes suitable for upload to Azure Blob.
    """
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE_URL}/users/{USER_EMAIL}/messages/{message_id}/attachments/{attachment_id}/$value"

    logger.info(f"⬇️ Downloading attachment bytes for msg={message_id}, att={attachment_id}")
    resp = requests.get(url, headers=headers, stream=True, timeout=60)

    if resp.status_code != 200:
        logger.error(f"❌ download_attachment_bytes failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    return resp.content


# -----------------------------------------------------
# Utility for testing manually
# -----------------------------------------------------
if __name__ == "__main__":
    logger.info("🔍 Testing Microsoft Graph connectivity...")
    token = get_graph_token()
    msgs = fetch_new_messages(token, top=5)
    if not msgs:
        logger.warning("No messages found.")
    else:
        first_msg = msgs[0]
        mid = first_msg["id"]
        logger.info(f"Message subject: {first_msg.get('subject')}")
        attachments = list_message_attachments(token, mid)
        if attachments:
            first_att = attachments[0]
            logger.info(f"First attachment: {first_att.get('name')}")
            content = download_attachment_bytes(token, mid, first_att["id"])
            logger.info(f"Downloaded {len(content)} bytes for attachment {first_att['id']}")
        else:
            logger.info("No attachments found.")
