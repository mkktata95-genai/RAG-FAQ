import logging
import os
import sys
from pathlib import Path

# Offline deploy: dependencies are bundled in ./libs (no pip at runtime).
sys.path.insert(0, str(Path(__file__).parent / "libs"))

import azure.functions as func  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.storage.blob import BlobServiceClient  # noqa: E402

from src import scraper_job as job  # noqa: E402

app = func.FunctionApp()
log = logging.getLogger("scraper_app")


def _containers():
    """Fail fast if the data-storage setting is missing."""
    conn = os.environ.get("DataStorage")  # local dev only (Azurite connection string)
    uri = os.environ.get("DataStorage__blobServiceUri")  # Azure: managed identity
    if conn:
        svc = BlobServiceClient.from_connection_string(conn, retry_total=5)
    elif uri:
        svc = BlobServiceClient(uri, credential=DefaultAzureCredential(), retry_total=5)
    else:
        raise RuntimeError("Neither DataStorage__blobServiceUri nor DataStorage is set")
    return svc.get_container_client("input"), svc.get_container_client("output")


@app.blob_trigger(arg_name="blob", path="input/approved-urls/{name}", connection="DataStorage")
def scrape_on_excel(blob: func.InputStream):
    if not job.is_excel(blob.name):
        log.info("ignored non-excel blob %s", blob.name)
        return
    cin, cout = _containers()
    job.process_excel(cin, cout, blob.name.split("/", 1)[1])  # strip container name


@app.queue_trigger(arg_name="msg", queue_name="scrape-requests", connection="AzureWebJobsStorage")
def scrape_on_request(msg: func.QueueMessage):
    cin, cout = _containers()
    name = job.parse_queue_message(msg.get_body().decode("utf-8", "replace")) or job.latest_excel(cin)
    job.process_excel(cin, cout, name)
