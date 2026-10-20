import logging
import os
import sys
from pathlib import Path

# Offline deploy: dependencies are bundled in ./libs (no pip at runtime).
sys.path.insert(0, str(Path(__file__).parent / "libs"))

import azure.functions as func  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.storage.blob import BlobServiceClient  # noqa: E402

from src import indexer_job as job  # noqa: E402

app = func.FunctionApp()
log = logging.getLogger("indexer_app")


def _output_container():
    """Fail fast if the data-storage setting is missing."""
    conn = os.environ.get("DataStorage")  # local dev only (Azurite connection string)
    uri = os.environ.get("DataStorage__blobServiceUri")  # Azure: managed identity
    if conn:
        svc = BlobServiceClient.from_connection_string(conn, retry_total=5)
    elif uri:
        svc = BlobServiceClient(uri, credential=DefaultAzureCredential(), retry_total=5)
    else:
        raise RuntimeError("Neither DataStorage__blobServiceUri nor DataStorage is set")
    return svc.get_container_client("output")


def _body(msg: func.QueueMessage) -> str:
    return msg.get_body().decode("utf-8", "replace")


@app.blob_trigger(arg_name="blob", path="output/scraped-data/{name}", connection="DataStorage")
def index_on_scrape(blob: func.InputStream):
    if not job.is_main_json(blob.name):  # the scraper also uploads *_failures.json here
        log.info("ignored blob %s", blob.name)
        return
    job.build_index(_output_container(), blob.name.split("/", 1)[1])  # strip container name


@app.queue_trigger(arg_name="msg", queue_name="index-requests", connection="AzureWebJobsStorage")
def index_on_request(msg: func.QueueMessage):
    cout = _output_container()
    job.build_index(cout, job.parse_build_message(_body(msg)) or job.latest_scraped(cout))


@app.queue_trigger(arg_name="msg", queue_name="index-promote", connection="AzureWebJobsStorage")
def promote_index(msg: func.QueueMessage):
    index, force = job.parse_promote_message(_body(msg))
    job.promote(job.AzureSearchOps(), index, force)
