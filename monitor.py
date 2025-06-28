import msal
import os
import time
import requests
from msal import PublicClientApplication
from base64 import b64decode

# Load environment variables
CLIENT_ID = "148cbb38-c335-4670-9a7f-26918ad4d293"
TENANT_ID = "a0c1ede5-77c5-4669-af9d-7edf6858c52a"
# CLIENT_SECRET = "x-x8Q~SO9yY3OsNdnU7R4~~_pl-Pj.e36WTtAc3I"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ['Mail.Read']
# SUBSCRIPTION_ID = "ea64786c-8181-4b37-8756-1247af4d3940"
# AZURE_MANAGEMENT_SCOPE = ["https://management.azure.com/.default"]

SAVE_DIR = "downloads"
os.makedirs(SAVE_DIR, exist_ok=True)

AZURE_ENDPOINT = "https://email-invoice-ai.cognitiveservices.azure.com/"
AZURE_KEY = "5jGo0u0vVCtR8YK1GdnRNoVIFKZDguEDm6W1KhQhXWJZQiqiIlrbJQQJ99BFACmepeSXJ3w3AAALACOG0Ta4"

# Authenticate with Microsoft Graph
def get_graph_token():
    app = PublicClientApplication(client_id=CLIENT_ID, authority=AUTHORITY)
    result = app.acquire_token_interactive(SCOPES)
    if "access_token" in result:
        print("✅ Token acquired!")
        token = result["access_token"]
        return token
    else:
        print("❌ Failed to acquire token")
        print("Error:", result)

# Download PDF attachments from recent emails
def check_emails(token):
    print("Fetching emails...")
    headers = {"Authorization": f"Bearer {token}"}
    url = "https://graph.microsoft.com/v1.0/me/messages?$top=10"
    res = requests.get(url, headers=headers)
    print("Status Code:", res.status_code)
    print("Response Text:", res.text)

    emails = res.json().get("value", [])
    # print("emails:", emails)

    for email in emails:
        if email.get("hasAttachments", False):
            att_url = f"https://graph.microsoft.com/v1.0/me/messages/{email['id']}/attachments"
            att_res = requests.get(att_url, headers=headers)
            attachments = att_res.json().get("value", [])

            for att in attachments:
                if att.get("contentType") == "application/pdf":
                    file_name = att.get("name", "invoice.pdf")
                    file_data = b64decode(att["contentBytes"])

                    with open(file_name, "wb") as f:
                        f.write(file_data)
                        print(f"Downloaded attachment: {file_name}")

            analyze_invoice(file_name)

# Analyze PDF with Azure Document Intelligence
def analyze_invoice(file_path):
    url = f"{AZURE_ENDPOINT}/formrecognizer/documentModels/prebuilt-invoice:analyze?api-version=2023-07-31"
    headers = {
        "Content-Type": "application/pdf",
        "Ocp-Apim-Subscription-Key": AZURE_KEY
    }

    with open(file_path, "rb") as f:
        data = f.read()

    res = requests.post(url, headers=headers, data=data)
    if res.status_code != 202:
        print("Failed to submit document:", res.text)
        return

    result_url = res.headers["operation-location"]
    while True:
        poll = requests.get(result_url, headers={"Ocp-Apim-Subscription-Key": AZURE_KEY})
        result = poll.json()
        if result.get("status") in ["succeeded", "failed"]:
            break
        time.sleep(2)

    if result["status"] == "succeeded":
        fields = result["analyzeResult"]["documents"][0]["fields"]
        print("Extracted Invoice Data:")
        for key, val in fields.items():
            print(f"{key}: {val.get('value')}")

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("Authenticate and get token")
    token = get_graph_token()
    if token:
        print(f"fetch token: {token}")
        check_emails(token)
    else:
        print("Failed to acquire Microsoft Graph token.")
