"""Local test helper (Azurite):
  python local_upload.py <scraped.json>                 upload to output/scraped-data/ (fires index_on_scrape)
  python local_upload.py <scraped.json> --failures <f>  also upload the failures sidecar FIRST (as the scraper does)
  python local_upload.py --build [file.json] [--allow-mass-removal]   enqueue index-requests (latest JSON if no file)
  python local_upload.py <file.xlsx>                    upload to input/templated-questions/ (fires templated_on_upload)
  python local_upload.py --templated [file.xlsx] [--allow-mass-removal] [--dry-run]
                                                        enqueue templated-requests (newest .xlsx if no file)
--build / --templated need azure-storage-queue (local testing only, not part of the deployed zip)."""
import json
import sys

from azure.storage.blob import BlobServiceClient

CONN = "UseDevelopmentStorage=true"
a = sys.argv[1:]


def enqueue(queue, body):
    from azure.storage.queue import QueueClient
    q = QueueClient.from_connection_string(CONN, queue)
    try:
        q.create_queue()
    except Exception:
        pass  # already exists
    q.send_message(body)
    print("queued", queue, body)


def upload(container, prefix, path):
    svc = BlobServiceClient.from_connection_string(CONN)
    for c in ("input", "output"):
        try:
            svc.create_container(c)
        except Exception:
            pass  # already exists
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    with open(path, "rb") as f:
        svc.get_container_client(container).upload_blob(prefix + name, f, overwrite=True)
    print("uploaded", name)


if a[:1] == ["--build"]:
    rest = [x for x in a[1:] if not x.startswith("--")]
    enqueue("index-requests", json.dumps({"file": rest[0] if rest else None,
                                          "allow_mass_removal": "--allow-mass-removal" in a}))
elif a[:1] == ["--templated"]:
    rest = [x for x in a[1:] if not x.startswith("--")]
    enqueue("templated-requests", json.dumps({"file": rest[0] if rest else None,
                                              "allow_mass_removal": "--allow-mass-removal" in a,
                                              "dry_run": "--dry-run" in a}))
elif a and a[0].lower().endswith(".xlsx"):
    upload("input", "templated-questions/", a[0])
elif a:
    if "--failures" in a:  # failures first, main JSON last (the main upload triggers the indexer)
        upload("output", "scraped-data/", a[a.index("--failures") + 1])
    upload("output", "scraped-data/", a[0])
else:
    print(__doc__)
