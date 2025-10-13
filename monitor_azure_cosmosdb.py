import os, time, logging
from services.azure_graph_service import get_graph_token, fetch_new_messages
from services.azure_blob_service import upload_pdf_blob
from services.azure_ai_service import analyze_and_store
logger = logging.getLogger('invoice-monitor')
_worker_should_stop = False
_worker_status = {'last_run': None, 'processed': 0}

def get_worker_status(): return _worker_status
def stop_worker(): global _worker_should_stop; _worker_should_stop = True

def background_worker(poll_interval=180):
    global _worker_should_stop, _worker_status
    while not _worker_should_stop:
        try:
            token = get_graph_token()
            messages = fetch_new_messages(token)
            for msg in messages:
                for att in msg.get('attachments', []):
                    if att.get('contentType') != 'application/pdf':
                        continue
                    blob_name = f"{msg['id']}_{att['id']}_{att.get('name','invoice.pdf').replace(' ','_')}"
                    blob_url = upload_pdf_blob(blob_name, att['contentBytes'])
                    analyze_and_store(blob_url, att['contentBytes'], msg['id'], att['id'])
                    _worker_status['processed'] += 1
            _worker_status['last_run'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        except Exception:
            logger.exception('Background worker run failed')
        time.sleep(poll_interval)