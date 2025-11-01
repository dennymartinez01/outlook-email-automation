from services.logger_utils import logger
from pymongo import MongoClient
import os
from datetime import datetime
from services.cosmos_service import get_db_collections

def mark_message_processed(message_id: str):
    collections = get_db_collections()
    processed_col = collections["processed"]
    processed_col.update_one(
        {"_id": message_id},
        {"$set": {"status": "success", "processed_at": datetime.utcnow()}},
        upsert=True
    )
    logger.info(f"✅ Marked message {message_id} as processed.")

def is_message_processed(message_id: str) -> bool:
    collections = get_db_collections()
    processed_col = collections["processed"]
    return processed_col.find_one({"_id": message_id, "status": "success"}) is not None
