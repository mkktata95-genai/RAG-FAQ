import json, os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for k, v in json.load(open("local.settings.json"))["Values"].items():
    os.environ.setdefault(k, v)
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient

c = SearchClient(os.environ["AZURE_SEARCH_ENDPOINT"], os.environ["AZURE_SEARCH_INDEX_NAME"], DefaultAzureCredential())
print("documents:", c.get_document_count())
url = sys.argv[1].rstrip("/")
for u in (url, url + "/"):
    rows = list(c.search("*", filter=f"source_url eq '{u}'", order_by=["chunk_index"], top=50,
                         select=["chunk_index", "total_chunks", "indexed_at", "refresh_count", "content_hash"]))
    if rows:
        break
for r in rows:
    print(dict(r))