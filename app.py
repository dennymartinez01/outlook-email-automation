
from flask import Flask, jsonify
import os
import requests
import msal

# Initialize Flask app
app = Flask(__name__)

# Load config from environment
CLIENT_ID = os.getenv('OUTLOOK_CLIENT_ID')
CLIENT_SECRET = os.getenv('OUTLOOK_CLIENT_SECRET')
TENANT_ID = os.getenv('OUTLOOK_TENANT_ID')
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["https://graph.microsoft.com/.default"]
MAILBOX = "shared-inbox@yourdomain.com"

def get_access_token():
    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=SCOPE)
    if "access_token" in result:
        return result["access_token"]
    else:
        raise Exception("Could not obtain access token: " + str(result))

def get_recent_emails(access_token):
    url = f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/mailFolders/inbox/messages"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    params = {
        "$top": 5,
        "$orderby": "receivedDateTime desc",
        "$filter": "hasAttachments eq true"
    }
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        messages = response.json().get("value", [])
        return [{
            "subject": msg["subject"],
            "receivedDateTime": msg["receivedDateTime"],
            "hasAttachments": msg["hasAttachments"]
        } for msg in messages]
    else:
        raise Exception("Failed to fetch emails: " + response.text)

@app.route("/run-now", methods=["GET"])
def run_now():
    try:
        token = get_access_token()
        emails = get_recent_emails(token)
        return jsonify({"status": "success", "emails": emails})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
