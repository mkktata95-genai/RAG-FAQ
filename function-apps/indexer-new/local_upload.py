"""Local test helper (Azurite):
  python local_upload.py <scraped.json>                 upload to output/scraped-data/ (fires the blob trigger)
  python local_upload.py <scraped.json> --failures <f>  also upload the failures sidecar FIRST (as the scraper does)
  python local_upload.py --build [file.json] [--allow-mass-removal]   enqueue index-requests (latest JSON if no file)"""
import json
import sys
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient  # ships with azure-storage-queue; only for --build

CONN = "UseDevelopmentStorage=true"
a = sys.argv[1:]

if a[:1] == ["--build"]:
    rest = [x for x in a[1:] if x != "--allow-mass-removal"]
    body = json.dumps({"file": rest[0] if rest else None, "allow_mass_removal": "--allow-mass-removal" in a})
    q = QueueClient.from_connection_string(CONN, "index-requests")
    try:
        q.create_queue()
    except Exception:
        pass  # already exists
    q.send_message(body)
    print("queued index-requests", body)
else:
    svc = BlobServiceClient.from_connection_string(CONN)
    for c in ("input", "output"):
        try:
            svc.create_container(c)
        except Exception:
            pass  # already exists
    files = [a[a.index("--failures") + 1]] if "--failures" in a else []
    files.append(a[0])  # failures first, main JSON last (the main upload triggers the indexer)
    for p in files:
        name = p.replace("\\", "/").rsplit("/", 1)[-1]
        with open(p, "rb") as f:
            svc.get_container_client("output").upload_blob("scraped-data/" + name, f, overwrite=True)
        print("uploaded", name)
