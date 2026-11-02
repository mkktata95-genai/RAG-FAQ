"""Local test helper (Azurite): python local_upload.py <excel> | python local_upload.py --queue
Uploads the Excel to input/approved-urls/ (fires the blob trigger) or enqueues scrape-requests."""
import sys
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient  # ships with azure-storage-queue; only for --queue

CONN = "UseDevelopmentStorage=true"

if sys.argv[1:] == ["--queue"]:
    q = QueueClient.from_connection_string(CONN, "scrape-requests")
    try:
        q.create_queue()
    except Exception:
        pass  # already exists
    q.send_message("run")
    print("queued")
else:
    svc = BlobServiceClient.from_connection_string(CONN)
    for c in ("input", "output"):
        try:
            svc.create_container(c)
        except Exception:
            pass  # already exists
    name = sys.argv[1].replace("\\", "/").rsplit("/", 1)[-1]
    with open(sys.argv[1], "rb") as f:
        svc.get_container_client("input").upload_blob("approved-urls/" + name, f, overwrite=True)
    print("uploaded", name)
