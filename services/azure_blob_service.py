import os
from azure.storage.blob import BlobServiceClient, StandardBlobTier, ContentSettings
from services.logger_utils import logger, retry
from dotenv import load_dotenv

load_dotenv()

connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
container_name = "invoices"

@retry(max_attempts=3)
def upload_pdf_blob(filename, content):
    """
    Uploads a PDF blob to Azure Storage with correct headers so browsers can render it inline.
    Moves the blob to Cool tier afterward.
    """
    blob_service = BlobServiceClient.from_connection_string(connection_string)
    container = blob_service.get_container_client(container_name)
    blob_client = container.get_blob_client(filename)

    # ✅ Tell Azure this file is a PDF (important for browser rendering)
    content_settings = ContentSettings(
        content_type="application/pdf",
        content_disposition="inline"  # ensure browser displays PDF, not download
    )

    # Upload the PDF with correct headers
    blob_client.upload_blob(
        content,
        overwrite=True,
        content_settings=content_settings
    )
    logger.info(f"📤 Uploaded {filename} to Blob storage with PDF headers.")

    # Try moving to Cool tier
    try:
        blob_client.set_standard_blob_tier(StandardBlobTier.Cool)
        logger.info(f"🌤️ Moved {filename} to Cool tier.")
    except Exception as e:
        logger.warning(f"⚠️ Could not move {filename} to Cool tier: {e}")

    # Return the blob URL
    return blob_client.url
