import logging
import os
import sys
from pathlib import Path

# Offline deploy: dependencies are bundled in ./libs (no pip at runtime).
sys.path.insert(0, str(Path(__file__).parent / "libs"))

import azure.functions as func  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.storage.blob import BlobServiceClient  # noqa: E402

from src import templated_job as job  # noqa: E402

app = func.FunctionApp()
log = logging.getLogger("templated_app")


def _input_container():
    """Fail fast if the data-storage setting is missing."""
    conn = os.environ.get("DataStorage")  # local dev only (Azurite connection string)
    uri = os.environ.get("DataStorage__blobServiceUri")  # Azure: managed identity
    if conn:
        svc = BlobServiceClient.from_connection_string(conn, retry_total=5)
    elif uri:
        svc = BlobServiceClient(uri, credential=DefaultAzureCredential(), retry_total=5)
    else:
        raise RuntimeError("Neither DataStorage__blobServiceUri nor DataStorage is set")
    return svc.get_container_client("input")


@app.blob_trigger(arg_name="blob", path="input/templated-questions/{name}", connection="DataStorage")
def templated_on_upload(blob: func.InputStream):
    if not job.is_excel(blob.name):  # Excel lock files (~$x.xlsx), csv, etc.
        log.info("ignored blob %s", blob.name)
        return
    job.sync_workbook(_input_container(), blob.name.split("/", 1)[1])  # strip container name


@app.queue_trigger(arg_name="msg", queue_name="templated-requests", connection="AzureWebJobsStorage")
def templated_on_request(msg: func.QueueMessage):
    cin = _input_container()
    name, allow, dry = job.parse_build_message(msg.get_body().decode("utf-8", "replace"))
    job.sync_workbook(cin, name or job.latest_excel(cin), allow_mass_removal=allow, dry_run=dry)
