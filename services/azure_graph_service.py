import os, requests, json
from pathlib import Path
from services.logger_utils import logger, with_retry
from dotenv import load_dotenv

load_dotenv()

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

@with_retry(max_attempts=3)
def get_graph_token():
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
def fetch_new_messages(token, top=10):
    target_email = os.getenv("TARGET_USER_EMAIL")
    checkpoint_file = Path(__file__).resolve().parent.parent / "last_processed.json"
    last_id = None
    if checkpoint_file.exists():
        try:
            last_id = json.loads(checkpoint_file.read_text()).get("last_message_id")
        except Exception:
            pass

    headers = {"Authorization": f"Bearer {token}"}
    url = (f"{GRAPH_BASE_URL}/users/{target_email}/messages"
           f"?$top={top}&$select=id,subject,receivedDateTime,hasAttachments"
           f"&$orderby=receivedDateTime desc")

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    messages = resp.json().get("value", [])

    new_messages = []
    for m in messages:
        if m["id"] == last_id:
            break
        new_messages.append(m)

    if new_messages:
        checkpoint_file.write_text(json.dumps({"last_message_id": new_messages[0]["id"]}))
        logger.info(f"💾 Updated checkpoint to message {new_messages[0]['id']}")
    else:
        logger.info("No new messages found since last checkpoint.")

    return new_messages
