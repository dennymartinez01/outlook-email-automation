from pymongo import MongoClient
import os
MONGO_URI = os.getenv('MONGO_URI')

def get_db_collections():
    client = MongoClient(MONGO_URI, tls=True)
    db = client['email_invoice_db']
    return {'invoices': db['invoicecollection'], 'processed': db['processed_attachments'], 'meta': db['meta']}