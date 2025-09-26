import os
from flask import Flask, render_template, request, send_file, send_from_directory, jsonify, redirect
import threading
import time
from bson.objectid import ObjectId
from pymongo import MongoClient
from urllib.parse import quote_plus
import csv
import io
import datetime
import shutil
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
    # client = MongoClient("mongodb://localhost:27017/")  # change if needed
    username = quote_plus("mongoadmin")
    password = quote_plus("P@ssw0rd!!")  # Use env var in real deployment
    uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"
    client = MongoClient(uri, tls=True)
    db = client["email_invoice_db"]
    return db["invoicecollection"]

# ------------------ UTILS ------------------ #
def filter_and_sort_docs(docs, vendor, from_date, to_date, sort_field, sort_order):
    # Filter by vendor
    if vendor:
        docs = [d for d in docs if d.get("Invoice", {}).get("Supplier", {}).get("Name") == vendor]

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
            raw_date = d.get("Invoice", {}).get("InvoiceDate")
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
        docs.sort(key=lambda d: d.get("Invoice", {}).get(sort_field, ""), reverse=reverse)

    return docs

# ------------------ ROUTES ------------------ #
@app.route("/")
def home():
    return "📬 Invoice monitoring is running every 3 minutes. Visit /invoices to view stored data."

@app.route("/invoices")
def invoices_list():
    collection = get_mongo_collection()

    vendor = request.args.get("vendor", "")
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")
    sort_field = request.args.get("sort", "InvoiceDate")
    sort_order = request.args.get("order", "desc")
    page = int(request.args.get("page", 1))
    per_page = 10

    query = {}
    if vendor:
        query["Invoice.Supplier.Name"] = vendor

    if from_date or to_date:
        query_date = {}
        if from_date:
            query_date["$gte"] = from_date
        if to_date:
            query_date["$lte"] = to_date
        query["Invoice.InvoiceDate"] = query_date

    sort_direction = -1 if sort_order == "desc" else 1

    total_docs = collection.count_documents(query)
    total_pages = max(1, (total_docs + per_page - 1) // per_page)

    cursor = collection.find(query) \
        .sort(f"Invoice.{sort_field}", sort_direction) \
        .skip((page - 1) * per_page) \
        .limit(per_page)

    invoices = list(cursor)
    for inv in invoices:
        inv["_id"] = str(inv["_id"])

    vendors = sorted(collection.distinct("Invoice.Supplier.Name"))

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

@app.route("/api/invoice/<doc_id>")
def invoice_detail_json(doc_id):
    collection = get_mongo_collection()
    try:
        if ObjectId.is_valid(doc_id):
            doc = collection.find_one({"_id": ObjectId(doc_id)})
        else:
            doc = collection.find_one({"_id": doc_id})
    except Exception as e:
        print("❌ Error fetching invoice:", e)
        return jsonify({"error": "Error fetching invoice"}), 500

    if not doc:
        return jsonify({"error": "Invoice not found"}), 404

    doc["_id"] = str(doc["_id"])
    return jsonify(doc)

@app.route("/invoice/<doc_id>")
def invoice_detail(doc_id):
    collection = get_mongo_collection()

    try:
        if ObjectId.is_valid(doc_id):
            doc = collection.find_one({"_id": ObjectId(doc_id)})
        else:
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

    vendor = request.args.get("vendor", "")
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")
    sort_field = request.args.get("sort", "InvoiceDate")
    sort_order = request.args.get("order", "desc")

    filtered_docs = filter_and_sort_docs(docs, vendor, from_date, to_date, sort_field, sort_order)

    if not filtered_docs:
        return "No data to export."

    flat_docs = []
    for d in filtered_docs:
        inv = d.get("Invoice", {})
        row = {"_id": d["_id"]}
        for k, v in inv.items():
            if isinstance(v, dict):
                for subk, subv in v.items():
                    row[f"{k}_{subk}"] = subv
            else:
                row[k] = v
        flat_docs.append(row)

    keys = sorted(set().union(*(doc.keys() for doc in flat_docs)))
    si = io.StringIO()
    cw = csv.DictWriter(si, fieldnames=keys)
    cw.writeheader()
    for doc in flat_docs:
        cw.writerow(doc)

    output = io.BytesIO()
    output.write(si.getvalue().encode())
    output.seek(0)
    return send_file(output, mimetype="text/csv", as_attachment=True, download_name="invoices.csv")

@app.route("/vendors")
def list_records():
    # client = MongoClient("mongodb://localhost:27017/")  # change if needed
    username = quote_plus("mongoadmin")
    password = quote_plus("P@ssw0rd!!")
    uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"

    client = MongoClient(uri, tls=True)
    db = client["email_invoice_db"]
    collection = db["vendorcollection"]

    page = int(request.args.get("page", 1))
    per_page = 10  # number of records per page

    total_docs = collection.count_documents({})
    total_pages = max(1, (total_docs + per_page - 1) // per_page)

    cursor = collection.find() \
        .skip((page - 1) * per_page) \
        .limit(per_page)

    records = list(cursor)
    for r in records:
        r["_id"] = str(r["_id"])

    # show 5 pages window around current page
    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)

    return render_template(
        "vendors.html",
        records=records,
        page=page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page
    )

@app.route("/companies")
def list_companies():
    # client = MongoClient("mongodb://localhost:27017/")  # change if needed
    username = quote_plus("mongoadmin")
    password = quote_plus("P@ssw0rd!!")
    uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"

    client = MongoClient(uri, tls=True)
    db = client["email_invoice_db"]
    collection = db["companycollection"]
    page = int(request.args.get("page", 1))
    per_page = 10

    # Count total documents
    total_docs = collection.count_documents({})
    total_pages = max(1, (total_docs + per_page - 1) // per_page)

    # Paginated query
    cursor = collection.find().skip((page - 1) * per_page).limit(per_page)
    companies = list(cursor)

    # Convert ObjectId to string
    for c in companies:
        c["_id"] = str(c["_id"])

    # Pagination range (show 5 numbers max)
    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)

    return render_template(
        "companies.html",
        companies=companies,
        page=page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page
    )

@app.route("/v2/invoices")
def invoices_v2():
    # client = MongoClient("mongodb://localhost:27017/")
    username = quote_plus("mongoadmin")
    password = quote_plus("P@ssw0rd!!")
    uri = f"mongodb+srv://{username}:{password}@email-monitor-cluster-mongodb.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000"

    client = MongoClient(uri, tls=True)
    db = client["email_invoice_db"]
    collection = db["invoicecollection"]

    # --- Advanced Search ---
    search_query = request.args.get("vendor", "").strip()
    mongo_query = {}

    if search_query:
        regex = {"$regex": search_query, "$options": "i"}  # case-insensitive substring
        mongo_query["$or"] = [
            {"Invoice.InvoiceNumber": regex},
            {"Invoice.Supplier.Name": regex},
            {"Invoice.Customer.Name": regex},
        ]

    # --- Pagination ---
    page = int(request.args.get("page", 1))
    per_page = 10
    total_docs = collection.count_documents(mongo_query)
    total_pages = max(1, (total_docs + per_page - 1) // per_page)

    cursor = (
        collection.find(mongo_query)
        .sort("Invoice.InvoiceDate", -1)
        .skip((page - 1) * per_page)
        .limit(per_page)
    )

    invoices = []
    for inv in cursor:
        line_items = inv.get("LineItems", [])
        category = "Stock" if line_items and len(line_items) > 0 else "Non-stock"

        invoices.append({
            "_id": str(inv.get("_id")),
            "invoice_no": inv.get("Invoice", {}).get("InvoiceNumber", ""),
            "date": inv.get("Invoice", {}).get("InvoiceDate", ""),
            "vendor": inv.get("Invoice", {}).get("Supplier", {}).get("Name", ""),
            "customer": inv.get("Invoice", {}).get("Customer", {}).get("Name", ""),
            "total": inv.get("Invoice", {}).get("InvoiceTotal", 0),
            "category": category,
            "status": "Exported",
            "pdf_file": inv.get("pdf_file", "")
        })

    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)

    return render_template(
        "roxor_template_2.html",
        invoices=invoices,
        vendor=search_query,
        page=page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page
    )


@app.route("/pdf/<path:filename>")
def serve_pdf(filename):
    USE_BLOB = 1
    try:
        # Case 1: If stored as a full Blob URL (starts with http)
        if filename.startswith("http"):
            return redirect(filename)

        if USE_BLOB:
            # === Serve file directly from Azure Blob Storage ===
            from azure.storage.blob import BlobServiceClient
            AZURE_STORAGE_CONNECTION_STRING = (
                "DefaultEndpointsProtocol=https;AccountName=roxorinvoicefiles;"
                "AccountKey=shqATjIJAAjdiuj6QIQgiIm0I89i5YZUDopT2M5oJIRnulm73qE3k3XfpFVYVkmJFITv6zSS/2Og+AStcSlYuw==;"
                "EndpointSuffix=core.windows.net"
            )
            blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
            container = blob_service.get_container_client("invoices")
            blob_client = container.get_blob_client(filename)

            stream = blob_client.download_blob()
            pdf_bytes = stream.readall()

            # ✅ Force inline display
            from flask import Response
            return Response(
                pdf_bytes,
                mimetype="application/pdf",
                headers={
                    "Content-Disposition": f"inline; filename={filename}"
                }
            )

        else:
            # === Serve file from local downloads/ folder ===
            pdf_dir = os.path.join(os.getcwd(), "downloads")
            return send_from_directory(
                pdf_dir,
                filename,
                mimetype="application/pdf",
                as_attachment=False
            )

    except Exception as e:
        print(f"❌ Error serving PDF {filename}: {e}")
        return f"Error serving PDF: {e}", 500



@app.route("/purge", methods=["POST"])
def purge_invoices():
    try:
        # 1. Delete all documents in MongoDB
        collection = get_mongo_collection()

        # 👇 Fetch pdf_file references before deleting docs
        all_invoices = list(collection.find({}, {"pdf_file": 1}))
        collection.delete_many({})
        print("✅ All invoices deleted from MongoDB")

        # 2. Delete all files (either local or Blob)
        USE_BLOB = 1

        if USE_BLOB:
            # === Delete from Azure Blob Storage ===
            from azure.storage.blob import BlobServiceClient
            AZURE_STORAGE_CONNECTION_STRING = "DefaultEndpointsProtocol=https;AccountName=roxorinvoicefiles;AccountKey=shqATjIJAAjdiuj6QIQgiIm0I89i5YZUDopT2M5oJIRnulm73qE3k3XfpFVYVkmJFITv6zSS/2Og+AStcSlYuw==;EndpointSuffix=core.windows.net"
            blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
            container = blob_service.get_container_client("invoices")

            # 👇 Delete only blobs linked in MongoDB (not blindly all)
            for inv in all_invoices:
                pdf_ref = inv.get("pdf_file")
                if not pdf_ref:
                    continue
                try:
                    if pdf_ref.startswith("http"):
                        # Example: https://roxorinvoicefiles.blob.core.windows.net/invoices/invoice123.pdf
                        blob_name = pdf_ref.split("/")[-1]
                    else:
                        blob_name = pdf_ref

                    container.delete_blob(blob_name)
                    print(f"🗑️ Deleted from Blob: {blob_name}")
                except Exception as e:
                    print(f"⚠️ Could not delete blob {pdf_ref}: {e}")

            # 👇 Old method: delete all blobs (kept for reference, now skipped)
            # blobs = container.list_blobs()
            # for blob in blobs:
            #     try:
            #         container.delete_blob(blob.name)
            #         print(f"🗑️ Deleted from Blob: {blob.name}")
            #     except Exception as e:
            #         print(f"⚠️ Could not delete blob {blob.name}: {e}")

        else:
            # === Delete local files in downloads/ ===
            downloads_dir = os.path.join(os.getcwd(), "downloads")
            if os.path.exists(downloads_dir):
                for filename in os.listdir(downloads_dir):
                    file_path = os.path.join(downloads_dir, filename)
                    try:
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                            print(f"🗑️ Deleted local file: {file_path}")
                    except Exception as e:
                        print(f"⚠️ Could not delete {file_path}: {e}")

        return jsonify({"success": True, "message": "All invoices and PDFs purged successfully."})

    except Exception as e:
        print(f"❌ Purge failed: {e}")
        return jsonify({"success": False, "message": str(e)}), 500




# ------------------ MAIN ------------------ #
if __name__ == "__main__":
    app.run(debug=True)
