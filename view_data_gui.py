from flask import Flask, render_template, request, Response
from pymongo import MongoClient
from bson.objectid import ObjectId
import re
import math
import csv
import io

app = Flask(__name__)
client = MongoClient("mongodb://localhost:27017")
db = client["email_invoice_db"]
collection = db["invoicecollection"]

@app.route('/')
def index():
    query = {}
    vendor = request.args.get('vendor', '')
    from_date = request.args.get('from_date', '')
    to_date = request.args.get('to_date', '')
    sort_field = request.args.get('sort', 'InvoiceDate')
    sort_order = request.args.get('order', 'desc')
    page = int(request.args.get('page', 1))
    per_page = 10

    # Filter by Vendor dropdown
    if vendor:
        query['VendorName'] = {'$regex': f'^{re.escape(vendor)}$', '$options': 'i'}

    # Filter by date range (string comparison if stored as YYYY-MM-DD)
    if from_date and to_date:
        query['InvoiceDate'] = {'$gte': from_date, '$lte': to_date}
    elif from_date:
        query['InvoiceDate'] = {'$gte': from_date}
    elif to_date:
        query['InvoiceDate'] = {'$lte': to_date}

    sort_direction = -1 if sort_order == 'desc' else 1
    total = collection.count_documents(query)
    total_pages = math.ceil(total / per_page)

    invoices = list(collection.find(query)
                    .sort(sort_field, sort_direction)
                    .skip((page - 1) * per_page)
                    .limit(per_page))

    # Get unique vendors for dropdown
    vendors = collection.distinct("VendorName")

    return render_template('index.html',
                           invoices=invoices,
                           vendors=vendors,
                           vendor=vendor,
                           from_date=from_date,
                           to_date=to_date,
                           sort_field=sort_field,
                           sort_order=sort_order,
                           page=page,
                           total_pages=total_pages)

@app.route('/invoice/<string:doc_id>')
def invoice_detail(doc_id):
    invoice = collection.find_one({"_id": doc_id})
    return render_template('details.html', invoice=invoice)

@app.route('/export')
def export_csv():
    query = {}
    vendor = request.args.get('vendor', '')
    from_date = request.args.get('from_date', '')
    to_date = request.args.get('to_date', '')
    sort_field = request.args.get('sort', 'InvoiceDate')
    sort_order = request.args.get('order', 'desc')

    # Apply filters
    if vendor:
        query['VendorName'] = {'$regex': f'^{re.escape(vendor)}$', '$options': 'i'}
    if from_date and to_date:
        query['InvoiceDate'] = {'$gte': from_date, '$lte': to_date}
    elif from_date:
        query['InvoiceDate'] = {'$gte': from_date}
    elif to_date:
        query['InvoiceDate'] = {'$lte': to_date}

    sort_direction = -1 if sort_order == 'desc' else 1
    invoices = list(collection.find(query).sort(sort_field, sort_direction))

    # Generate CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    if invoices:
        header = ['_id'] + [k for k in invoices[0].keys() if k != '_id']
        writer.writerow(header)

        # Rows
        for doc in invoices:
            row = [doc.get(k, '') for k in header]
            writer.writerow(row)

    # Return CSV response
    output.seek(0)
    return Response(output, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=invoices_export.csv"})

if __name__ == "__main__":
    app.run(debug=True)
