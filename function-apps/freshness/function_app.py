import logging
import os
import sys
from pathlib import Path

# Offline deploy: dependencies are bundled in ./libs (no pip at runtime).
sys.path.insert(0, str(Path(__file__).parent / "libs"))

import azure.functions as func  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.storage.blob import BlobServiceClient  # noqa: E402

from src import freshness_job as job  # noqa: E402

app = func.FunctionApp()
log = logging.getLogger("freshness_app")


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


# NCRONTAB, UTC, set in the FRESHNESS_SCHEDULE app setting (e.g. "0 0 3 * * *" = 03:00 every day).
@app.timer_trigger(arg_name="timer", schedule="%FRESHNESS_SCHEDULE%", run_on_startup=False)
def freshness_daily(timer: func.TimerRequest):
    cin, cout = _containers()
    job.run_freshness(cin, cout, mode=job.timer_mode())


@app.queue_trigger(arg_name="msg", queue_name="freshness-requests", connection="AzureWebJobsStorage")
def freshness_on_request(msg: func.QueueMessage):
    opts = job.parse_request_message(msg.get_body().decode("utf-8", "replace"))
    cin, cout = _containers()
    job.run_freshness(cin, cout, **opts)
