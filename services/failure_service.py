from services.logger_utils import logger
from datetime import datetime
from services.cosmos_service import get_db_collections

def record_failure(message_id: str, attachment_id: str, reason: str, stage: str):
    collections = get_db_collections()
    processed_col = collections["processed"]
    doc_id = f"{message_id}_{attachment_id}"
    processed_col.update_one(
        {"_id": doc_id},
        {
            "$set": {
                "message_id": message_id,
                "attachment_id": attachment_id,
                "status": "failed",
                "reason": reason,
                "stage": stage,
                "failed_at": datetime.utcnow()
            }
        },
        upsert=True
    )
    logger.error(f" Failure recorded for {doc_id} at stage '{stage}': {reason}")
