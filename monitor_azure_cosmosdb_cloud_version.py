import msal
import os
import time
import requests
from base64 import b64decode
from pymongo import MongoClient
from urllib.parse import quote_plus

# === Microsoft Graph Auth Config ===
CLIENT_ID = "148cbb38-c335-4670-9a7f-26918ad4d293"
TENANT_ID = "a0c1ede5-77c5-4669-af9d-7edf6858c52a"
CLIENT_SECRET = "x-x8Q~SO9yY3OsNdnU7R4~~_pl-Pj.e36WTtAc3I"
TARGET_USER_EMAIL = "docproject@roxorgroup.com"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["https://graph.microsoft.com/.default"]

# === Azure Form Recognizer Config ===
AZURE_ENDPOINT = "https://email-invoice-ai.cognitiveservices.azure.com/"
AZURE_KEY = "5jGo0u0vVCtR8YK1GdnRNoVIFKZDguEDm6W1KhQhXWJZQiqiIlrbJQQJ99BFACmepeSXJ3w3AAALACOG0Ta4"

# === Local Download Folder ===
SAVE_DIR = "downloads"
os.makedirs(SAVE_DIR, exist_ok=True)

# === Authenticate with MS Graph ===
def get_graph_token():
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY
    )
    result = app.acquire_token_for_client(scopes=SCOPE)

    if "access_token" in result:
        print("✅ Token acquired!")
        return result["access_token"]
    else:
        print("❌ Failed to acquire token")
        print("Error:", result)
        return None

# === Check Emails and Download Attachments ===
def check_emails(token):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/users/{TARGET_USER_EMAIL}/messages?$top=10"

    res = requests.get(url, headers=headers)
    print("Status Code:", res.status_code)
    if res.status_code != 200:
        print("Error:", res.text)
        return

    emails = res.json().get("value", [])
    for email in emails:
        if email.get("hasAttachments", False):
            att_url = f"https://graph.microsoft.com/v1.0/users/{TARGET_USER_EMAIL}/messages/{email['id']}/attachments"
            att_res = requests.get(att_url, headers=headers)
            attachments = att_res.json().get("value", [])

            for att in attachments:
                if att.get("contentType") == "application/pdf":
                    file_name = att.get("name", "invoice.pdf")
                    file_data = b64decode(att["contentBytes"])

                    full_path = os.path.join(SAVE_DIR, file_name)
                    with open(full_path, "wb") as f:
                        f.write(file_data)
                    print(f"📄 Downloaded: {file_name}")

                    analyze_invoice(full_path)

# === Analyze Invoice and Store in CosmosDB (Mongo API) ===
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
        print("❌ Failed to submit document:", res.text)
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
        extracted_data = {}

        print("📊 Extracted Invoice Data:")
        for key, val in fields.items():
            value = (
                val.get("valueNumber")
                or val.get("valueString")
                or val.get("valueDate")
                or val.get("valueAddress")
                or val.get("valuePhoneNumber")
                or val.get("valueTime")
                or val.get("valueInteger")
                or val.get("value")
            )
            extracted_data[key] = value
            confidence = val.get("confidence", 0)
            print(f"{key}: {value} (confidence: {confidence})")

        # === Store to Cosmos DB for MongoDB ===
        try:
            # Securely encode username and password
            username = quote_plus("mongoadmin")
            password = quote_plus("P@ssw0rd!!")  # IMPORTANT: URL encode special characters

            uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"

            client = MongoClient(uri, tls=True)
            db = client["email_invoice_db"]
            collection = db["invoicecollection"]

            document_id = os.path.basename(file_path) + f"-{int(time.time())}"
            collection.insert_one({"_id": document_id, **extracted_data})

            print("✅ Stored in Cosmos DB (MongoDB API)")
        except Exception as e:
            print("❌ Failed to store in MongoDB:", e)

# === MAIN ENTRY POINT ===
if __name__ == "__main__":
    token = get_graph_token()
    if token:
        check_emails(token)
    else:
        print("❌ No token retrieved")
