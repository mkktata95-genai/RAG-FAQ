"""Local test helper (Azurite):
  python local_upload.py <scraped.json>           upload to output/scraped-data/ (fires the blob trigger)
  python local_upload.py --build [file.json]      enqueue index-requests (latest JSON if no file)
  python local_upload.py --promote [index] [--force]   enqueue index-promote (newest built index if none)"""
import json
import sys
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient  # ships with azure-storage-queue; only for --build/--promote

CONN = "UseDevelopmentStorage=true"
a = sys.argv[1:]


def send(queue, body):
    q = QueueClient.from_connection_string(CONN, queue)
    try:
        q.create_queue()
    except Exception:
        pass  # already exists
    q.send_message(body)
    print("queued", queue, body)


if a[:1] == ["--build"]:
    send("index-requests", json.dumps({"file": a[1]}) if a[1:] else "run")
elif a[:1] == ["--promote"]:
    rest = [x for x in a[1:] if x != "--force"]
    send("index-promote", json.dumps({"index": rest[0] if rest else None, "force": "--force" in a}))
else:
    svc = BlobServiceClient.from_connection_string(CONN)
    for c in ("input", "output"):
        try:
            svc.create_container(c)
        except Exception:
            pass  # already exists
    name = a[0].replace("\\", "/").rsplit("/", 1)[-1]
    with open(a[0], "rb") as f:
        svc.get_container_client("output").upload_blob("scraped-data/" + name, f, overwrite=True)
    print("uploaded", name)
