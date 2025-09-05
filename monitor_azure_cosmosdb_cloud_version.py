import msal
import os
import time
import requests
import re
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

# === Azure Document Intelligence Config ===
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
    try:
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
    except Exception as e:
        print("❌ Error during email check:", e)

# === Helpers ===
def normalize_number(val):
    if not val:
        return None
    return val.replace(",", "").replace(" ", "")

# === Analyze Invoice and Store in CosmosDB ===
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
        doc = result["analyzeResult"]["documents"][0]
        fields = doc.get("fields", {})

        invoice_data = {
            "InvoiceNumber": fields.get("InvoiceId", {}).get("valueString") or fields.get("InvoiceNumber", {}).get("valueString"),
            "InvoiceDate": fields.get("InvoiceDate", {}).get("valueDate"),
            "DueDate": fields.get("DueDate", {}).get("valueDate"),
            "Supplier": {
                "Name": fields.get("VendorName", {}).get("valueString"),
                "Address": fields.get("VendorAddress", {}).get("valueAddress")
            },
            "Customer": {
                "Name": fields.get("CustomerName", {}).get("valueString"),
                "Address": fields.get("CustomerAddress", {}).get("valueAddress")
            },
            "Currency": fields.get("Currency", {}).get("valueString"),
            "SubTotal": fields.get("SubTotal", {}).get("valueNumber"),
            "TaxAmount": fields.get("TaxAmount", {}).get("valueNumber"),
            "InvoiceTotal": fields.get("InvoiceTotal", {}).get("valueNumber"),
            "AmountDue": fields.get("AmountDue", {}).get("valueNumber")
        }

        # === Fallbacks ===
        full_text = result["analyzeResult"].get("content", "")
        if isinstance(full_text, list):
            full_text = " ".join([p.get("content", "") for p in full_text if isinstance(p, dict)])

        regex_patterns = {
            "InvoiceTotal": r"(?:TOTAL\s*\(.*?\)|INVOICE\s*TOTAL|TOTAL)\s*[:\-]?\s*([\d.,]+)",
            "SubTotal": r"(?:SUBTOTAL|SUB TOTAL)\s*[:\-]?\s*([\d.,]+)",
            "TaxAmount": r"(?:TAX\s*AMOUNT|VAT)\s*[:\-]?\s*([\d.,]+)",
            "AmountDue": r"(?:AMOUNT\s*DUE|BALANCE\s*DUE)\s*[:\-]?\s*([\d.,]+)",
            "Currency": r"\b(USD|EUR|GBP|PHP|JPY)\b",
            "DueDate": r"(?:DUE\s*DATE)\s*[:\-]?\s*(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})"
        }

        for key, pattern in regex_patterns.items():
            if not invoice_data.get(key):
                matches = re.findall(pattern, full_text, re.IGNORECASE)
                if matches:
                    val = matches[-1]
                    if key in ["InvoiceTotal", "SubTotal", "TaxAmount", "AmountDue"]:
                        val = normalize_number(val)
                    invoice_data[key] = val
                    print(f"✅ Fallback {key} from text: {invoice_data[key]}")

        # === Extract Line Items ===
        line_items = []
        tables = result["analyzeResult"].get("tables", [])
        if isinstance(tables, list):
            for t in tables:
                rows = {}
                for cell in t.get("cells", []):
                    rows.setdefault(cell.get("rowIndex"), {})[cell.get("columnIndex")] = cell.get("content", "")

                for row_idx, cols in rows.items():
                    row_values = [cols[c] for c in sorted(cols.keys())]
                    if not row_values or any(h in row_values[0].upper() for h in ["DESCRIPTION", "PRODUCT", "SERVICE", "NET", "TOTAL"]):
                        continue

                    numeric_values = [normalize_number(v) for v in row_values if re.match(r"^[\d.,]+$", v)]
                    if numeric_values:
                        item = {"Description": row_values[0]}
                        if len(numeric_values) == 3:
                            item.update({"Net": numeric_values[0], "VAT": numeric_values[1], "Total": numeric_values[2]})
                        elif len(numeric_values) == 2:
                            item.update({"Net": numeric_values[0], "Total": numeric_values[1]})
                        elif len(numeric_values) == 1:
                            item.update({"Total": numeric_values[0]})
                        line_items.append(item)

        # === Store to Cosmos DB ===
        try:
            username = quote_plus("mongoadmin")
            password = quote_plus("P@ssw0rd!!")
            uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"

            client = MongoClient(uri, tls=True)
            db = client["email_invoice_db"]
            collection = db["invoicecollection"]

            invoice_number = invoice_data.get("InvoiceNumber")
            invoice_date = invoice_data.get("InvoiceDate")
            document_id = f"{invoice_number}_{invoice_date}" if invoice_number and invoice_date else os.path.basename(file_path)

            if collection.find_one({"_id": document_id}):
                print(f"⚠️ Duplicate skipped: {document_id}")
            else:
                collection.insert_one({
                    "_id": document_id,
                    "Invoice": invoice_data,
                    "LineItems": line_items
                })
                print(f"✅ Stored in Cosmos DB: {document_id}")

        except Exception as e:
            print("❌ Failed to store in MongoDB:", e)

# === MAIN ===
if __name__ == "__main__":
    token = get_graph_token()
    if token:
        check_emails(token)
    else:
        print("❌ No token retrieved")
