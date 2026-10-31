"""Local test helper (Azurite):
  python local_upload.py <file.xlsx>                      upload to input/templated-questions/ (fires the blob trigger)
  python local_upload.py --build [file.xlsx] [--allow-mass-removal] [--dry-run]
                                                          enqueue templated-requests (newest .xlsx if no file)
--build needs: pip install azure-storage-queue   (local testing only, not part of the deployed zip)"""
import json
import sys
from azure.storage.blob import BlobServiceClient

CONN = "UseDevelopmentStorage=true"
a = sys.argv[1:]

if a[:1] == ["--build"]:
    from azure.storage.queue import QueueClient
    rest = [x for x in a[1:] if not x.startswith("--")]
    body = json.dumps({"file": rest[0] if rest else None, "allow_mass_removal": "--allow-mass-removal" in a,
                       "dry_run": "--dry-run" in a})
    q = QueueClient.from_connection_string(CONN, "templated-requests")
    try:
        q.create_queue()
    except Exception:
        pass  # already exists
    q.send_message(body)
    print("queued templated-requests", body)
elif a:
    svc = BlobServiceClient.from_connection_string(CONN)
    for c in ("input", "output"):
        try:
            svc.create_container(c)
        except Exception:
            pass  # already exists
    name = a[0].replace("\\", "/").rsplit("/", 1)[-1]
    with open(a[0], "rb") as f:
        svc.get_container_client("input").upload_blob("templated-questions/" + name, f, overwrite=True)
    print("uploaded", name)
else:
    print(__doc__)
