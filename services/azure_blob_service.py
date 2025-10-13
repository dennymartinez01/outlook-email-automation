import os
from azure.storage.blob import BlobServiceClient, StandardBlobTier
from services.logger_utils import logger, retry
from dotenv import load_dotenv

load_dotenv()

connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
container_name = "invoices"

@retry(max_attempts=3)
def upload_pdf_blob(filename, content):
    blob_service = BlobServiceClient.from_connection_string(connection_string)
    container = blob_service.get_container_client(container_name)
    blob_name = filename
    blob_client = container.get_blob_client(blob_name)

    blob_client.upload_blob(content, overwrite=True)
    logger.info(f"📤 Uploaded {blob_name} to Blob storage.")

    try:
        blob_client.set_standard_blob_tier(StandardBlobTier.Cool)
        logger.info(f"🌤️ Moved {blob_name} to Cool tier.")
    except Exception as e:
        logger.warning(f"⚠️ Could not move {blob_name} to Cool tier: {e}")

    return blob_client.url
