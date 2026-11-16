import logging
import os
import sys
from pathlib import Path

# Offline deploy: dependencies are bundled in ./libs (no pip at runtime).
sys.path.insert(0, str(Path(__file__).parent / "libs"))

import azure.functions as func  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.storage.blob import BlobServiceClient  # noqa: E402

from src import indexer_job, templated_job  # noqa: E402

app = func.FunctionApp()
log = logging.getLogger("indexing_app")


def _container(name: str):
    """Fail fast if the data-storage setting is missing."""
    conn = os.environ.get("DataStorage")  # local dev only (Azurite connection string)
    uri = os.environ.get("DataStorage__blobServiceUri")  # Azure: managed identity
    if conn:
        svc = BlobServiceClient.from_connection_string(conn, retry_total=5)
    elif uri:
        svc = BlobServiceClient(uri, credential=DefaultAzureCredential(), retry_total=5)
    else:
        raise RuntimeError("Neither DataStorage__blobServiceUri nor DataStorage is set")
    return svc.get_container_client(name)


# ---- Indexer: scraped JSON -> faq index (output container) ----------------------------------
@app.blob_trigger(arg_name="blob", path="output/scraped-data/{name}", connection="DataStorage")
def index_on_scrape(blob: func.InputStream):
    if not indexer_job.is_main_json(blob.name):  # the scraper also uploads *_failures.json here
        log.info("ignored blob %s", blob.name)
        return
    indexer_job.sync_index(_container("output"), blob.name.split("/", 1)[1])  # strip container name


@app.queue_trigger(arg_name="msg", queue_name="index-requests", connection="AzureWebJobsStorage")
def index_on_request(msg: func.QueueMessage):
    cout = _container("output")
    name, allow = indexer_job.parse_build_message(msg.get_body().decode("utf-8", "replace"))
    indexer_job.sync_index(cout, name or indexer_job.latest_scraped(cout), allow)


# ---- Templated: Excel -> templated-questions index (input container) ------------------------
@app.blob_trigger(arg_name="blob", path="input/templated-questions/{name}", connection="DataStorage")
def templated_on_upload(blob: func.InputStream):
    if not templated_job.is_excel(blob.name):  # Excel lock files (~$x.xlsx), csv, etc.
        log.info("ignored blob %s", blob.name)
        return
    templated_job.sync_workbook(_container("input"), blob.name.split("/", 1)[1])  # strip container name


@app.queue_trigger(arg_name="msg", queue_name="templated-requests", connection="AzureWebJobsStorage")
def templated_on_request(msg: func.QueueMessage):
    cin = _container("input")
    name, allow, dry = templated_job.parse_build_message(msg.get_body().decode("utf-8", "replace"))
    templated_job.sync_workbook(cin, name or templated_job.latest_excel(cin), allow_mass_removal=allow, dry_run=dry)
