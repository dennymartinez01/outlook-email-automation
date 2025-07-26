from flask import Flask, render_template, request, send_file
import threading
import time
from bson.objectid import ObjectId
from pymongo import MongoClient
from urllib.parse import quote_plus
import csv
import io
import datetime

import monitor_azure_cosmosdb_cloud_version

app = Flask(__name__)
invoice_thread_started = False

# ------------------ BACKGROUND THREAD ------------------ #
def run_invoice_checker_loop():
    while True:
        try:
            print("🔁 Running invoice check...")
            token = monitor_azure_cosmosdb_cloud_version.get_graph_token()
            if token:
                monitor_azure_cosmosdb_cloud_version.check_emails(token)
            else:
                print("❌ Failed to get token")
        except Exception as e:
            print(f"❌ Error during invoice check: {e}")
        time.sleep(180)  # Every 3 minutes

def start_background_thread():
    global invoice_thread_started
    if not invoice_thread_started:
        thread = threading.Thread(target=run_invoice_checker_loop, daemon=True)
        thread.start()
        invoice_thread_started = True
        print("✅ Background thread started")

start_background_thread()

# ------------------ MONGO CONNECTION ------------------ #
def get_mongo_collection():
    username = quote_plus("mongoadmin")
    password = quote_plus("P@ssw0rd!!")  # Use env var in real deployment
    host = "email-monitor-cluster-mongodb.mongo.cosmos.azure.com"
    uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"
    client = MongoClient(uri, tls=True)
    db = client["email_invoice_db"]
    return db["invoicecollection"]

# ------------------ FILTER / SORT UTILITY ------------------ #
def filter_and_sort_docs(docs, vendor, from_date, to_date, sort_field, sort_order):
    # Filter by vendor
    if vendor:
        docs = [d for d in docs if d.get("VendorName") == vendor]

    # Filter by date range
    if from_date or to_date:
        def parse_date(s):
            try:
                return datetime.datetime.strptime(s, "%Y-%m-%d")
            except:
                return None

        from_dt = parse_date(from_date) if from_date else None
        to_dt = parse_date(to_date) if to_date else None

        filtered = []
        for d in docs:
            raw_date = d.get("InvoiceDate")
            try:
                doc_date = datetime.datetime.strptime(raw_date, "%Y-%m-%d")
            except:
                continue

            if from_dt and doc_date < from_dt:
                continue
            if to_dt and doc_date > to_dt:
                continue
            filtered.append(d)
        docs = filtered

    # Sorting
    if sort_field:
        reverse = (sort_order == "desc")
        docs.sort(key=lambda d: d.get(sort_field, ""), reverse=reverse)

    return docs

# ------------------ ROUTES ------------------ #
@app.route("/")
def home():
    return "📬 Invoice monitoring is running every 3 minutes. Visit /invoices to view stored data."

@app.route("/invoices")
def invoices_list():
    collection = get_mongo_collection()

    # Query params
    vendor = request.args.get("vendor", "")
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")
    sort_field = request.args.get("sort", "InvoiceDate")  # Default
    sort_order = request.args.get("order", "desc")        # Default
    page = int(request.args.get("page", 1))
    per_page = 10

    # Build MongoDB query
    query = {}
    if vendor:
        query["VendorName"] = vendor

    # Date range filter
    if from_date or to_date:
        query_date = {}
        if from_date:
            query_date["$gte"] = from_date
        if to_date:
            query_date["$lte"] = to_date
        query["InvoiceDate"] = query_date

    # Sorting
    sort_direction = -1 if sort_order == "desc" else 1

    # Count total documents after filter
    total_docs = collection.count_documents(query)
    total_pages = max(1, (total_docs + per_page - 1) // per_page)

    # Fetch only required page (server-side pagination)
    cursor = collection.find(query)\
        .sort(sort_field, sort_direction)\
        .skip((page - 1) * per_page)\
        .limit(per_page)

    invoices = list(cursor)

    # Convert ObjectId to str
    for inv in invoices:
        inv["_id"] = str(inv["_id"])

    # Vendors for dropdown (distinct query)
    vendors = sorted(collection.distinct("VendorName"))

    # Calculate dynamic page range (current ± 2)
    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)

    return render_template(
        "index.html",
        invoices=invoices,
        vendors=vendors,
        vendor=vendor,
        from_date=from_date,
        to_date=to_date,
        sort_field=sort_field,
        sort_order=sort_order,
        page=page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page
    )


@app.route("/invoice/<doc_id>")
def invoice_detail(doc_id):
    collection = get_mongo_collection()

    try:
        # Try ObjectId first
        if ObjectId.is_valid(doc_id):
            doc = collection.find_one({"_id": ObjectId(doc_id)})
        else:
            # Fallback: match string _id
            doc = collection.find_one({"_id": doc_id})
    except Exception as e:
        print("Error fetching invoice:", e)
        return "❌ Error fetching invoice", 500

    if not doc:
        return "❌ Invoice not found", 404

    doc["_id"] = str(doc["_id"])
    return render_template("details.html", doc=doc)


@app.route("/export")
def export_csv():
    collection = get_mongo_collection()
    docs = list(collection.find())
    for d in docs:
        d["_id"] = str(d["_id"])

    # Apply same filter/sort as index
    vendor = request.args.get("vendor", "")
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")
    sort_field = request.args.get("sort", "InvoiceDate")
    sort_order = request.args.get("order", "desc")

    filtered_docs = filter_and_sort_docs(docs, vendor, from_date, to_date, sort_field, sort_order)

    if not filtered_docs:
        return "No data to export."

    keys = sorted(set().union(*(doc.keys() for doc in filtered_docs)))
    si = io.StringIO()
    cw = csv.DictWriter(si, fieldnames=keys)
    cw.writeheader()
    for doc in filtered_docs:
        cw.writerow(doc)

    output = io.BytesIO()
    output.write(si.getvalue().encode())
    output.seek(0)
    return send_file(output, mimetype="text/csv", as_attachment=True, download_name="invoices.csv")

# ------------------ MAIN ------------------ #
if __name__ == "__main__":
    app.run(debug=True)
