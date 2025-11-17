import os
import json
import requests
from pathlib import Path
from dotenv import load_dotenv
from services.logger_utils import logger, with_retry

load_dotenv()

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


@with_retry(max_attempts=3)
def get_graph_token():
    """Obtain Microsoft Graph API token using client credentials."""
    tenant_id = os.getenv("GRAPH_TENANT_ID")
    client_id = os.getenv("GRAPH_CLIENT_ID")
    client_secret = os.getenv("GRAPH_CLIENT_SECRET")

    if not tenant_id:
        raise ValueError("GRAPH_TENANT_ID not configured in .env")

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
    }

    resp = requests.post(url, data=data)
    resp.raise_for_status()
    token = resp.json().get("access_token")

    logger.info("✅ Microsoft Graph token acquired.")
    return token


@with_retry(max_attempts=3)
def fetch_new_messages(token, category, target_email, top=10):
    """
    Fetch new messages (with attachments expanded) from the target inbox.

    Uses last_processed.json as a checkpoint to avoid reprocessing.
    Returns only messages that contain attachments (typically PDFs).
    """
    # target_email = os.getenv("TARGET_USER_EMAIL")
    target_emails = {
        "nonstock": os.getenv("TARGET_EMAIL_NONSTOCK"),
        "stock": os.getenv("TARGET_EMAIL_STOCK"),
        "customer": os.getenv("TARGET_EMAIL_CUSTOMER")
    }
    if not target_emails:
        raise ValueError("TARGET_USER_EMAIL not configured in .env")

    checkpoint_file = Path(__file__).resolve().parent.parent / "last_processed.json"
    last_id = None
    if checkpoint_file.exists():
        try:
            last_id = json.loads(checkpoint_file.read_text()).get("last_message_id")
        except Exception as e:
            logger.warning(f"⚠️ Failed to read checkpoint: {e}")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    # 🔑 Expand attachments so PDFs are included directly in the response
    url = (
        f"{GRAPH_BASE_URL}/users/{target_email}/mailFolders/inbox/messages"
        f"?$top={top}"
        f"&$orderby=receivedDateTime desc"
        f"&$expand=attachments"
        f"&$select=id,subject,receivedDateTime,hasAttachments"
    )

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    messages = resp.json().get("value", [])
    logger.info(f"📬 Retrieved {len(messages)} new messages from Graph")

    new_messages = []
    for msg in messages:
        msg_id = msg.get("id")
        if msg_id == last_id:
            break  # stop once we reach last processed message
        new_messages.append(msg)

    if new_messages:
        # Update checkpoint with the latest (most recent) message ID
        checkpoint_file.write_text(json.dumps({"last_message_id": new_messages[0]["id"]}))
        logger.info(f"💾 Updated checkpoint to message {new_messages[0]['id']}")
    else:
        logger.info("No new messages found since last checkpoint.")

    # Filter messages with attachments
    messages_with_attachments = [m for m in new_messages if m.get("attachments")]
    logger.info(f"📎 Found {len(messages_with_attachments)} messages with attachments")

    return messages_with_attachments
