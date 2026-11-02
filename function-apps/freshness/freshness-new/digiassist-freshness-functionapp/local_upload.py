"""Local test helper (Azurite).
  python local_upload.py <excel>                      upload the approved-URLs Excel to input/approved-urls/
  python local_upload.py --run [report|apply] [--dry-run] [--allow-mass-removal]
                                                      enqueue a run on freshness-requests (default: report)
  python local_upload.py --outputs                    list what a run uploaded to output/
Nothing here fires the daily timer: use --run to start a run."""
import json
import sys
from azure.storage.blob import BlobServiceClient

CONN = "UseDevelopmentStorage=true"
svc = BlobServiceClient.from_connection_string(CONN)
args = sys.argv[1:]

if args[:1] == ["--run"]:
    from azure.storage.queue import QueueClient  # ships with azure-storage-queue; only for --run
    mode = next((a for a in args[1:] if a in ("report", "apply")), "report")
    body = {"mode": mode, "dry_run": "--dry-run" in args, "allow_mass_removal": "--allow-mass-removal" in args}
    q = QueueClient.from_connection_string(CONN, "freshness-requests")
    try:
        q.create_queue()
    except Exception:
        pass  # already exists
    q.send_message(json.dumps(body))
    print("queued", body)
elif args[:1] == ["--outputs"]:
    for b in svc.get_container_client("output").list_blobs(name_starts_with="freshness-"):
        print(b.name, b.size)
else:
    for c in ("input", "output"):
        try:
            svc.create_container(c)
        except Exception:
            pass  # already exists
    name = args[0].replace("\\", "/").rsplit("/", 1)[-1]
    with open(args[0], "rb") as f:
        svc.get_container_client("input").upload_blob("approved-urls/" + name, f, overwrite=True)
    print("uploaded", name)
