"""
audit_duplicates.py — read-only duplicate-content audit.
Pages through ALL docs in rlg-faq-index-v4, groups by
(source_url, content_hash), and reports:
  - total docs, unique hashes, duplicated hash groups
  - top offending URLs
  - whether duplicate pairs share the same source_url or differ
    (e.g. base URL vs #state= variant)
"""
import os
from collections import Counter, defaultdict

from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient

load_dotenv()

INDEX = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v4")
ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
API_KEY = os.getenv("AZURE_SEARCH_API_KEY") or os.getenv("AZURE_SEARCH_KEY")
credential = AzureKeyCredential(API_KEY) if API_KEY else DefaultAzureCredential()

client = SearchClient(ENDPOINT, INDEX, credential)

docs = []
skip = 0
PAGE = 1000
while True:
    batch = list(client.search(
        search_text="*",
        select=["chunk_id", "source_url", "content_hash"],
        top=PAGE,
        skip=skip,
        order_by=["indexed_at asc"],
    ))
    docs.extend(batch)
    if len(batch) < PAGE:
        break
    skip += PAGE

print(f"{INDEX}: fetched {len(docs)} docs")

by_hash = defaultdict(list)
for d in docs:
    by_hash[d.get("content_hash", "")].append(d)

dup_groups = {h: v for h, v in by_hash.items() if h and len(v) > 1}
dup_docs = sum(len(v) for v in dup_groups.values())
print(f"unique content hashes : {len(by_hash)}")
print(f"duplicated hash groups: {len(dup_groups)}")
print(f"docs involved in dups : {dup_docs} "
      f"({dup_docs - len(dup_groups)} removable)")

same_url = cross_url = 0
url_counter = Counter()
for h, v in dup_groups.items():
    urls = {d["source_url"] for d in v}
    if len(urls) == 1:
        same_url += 1
    else:
        cross_url += 1
    for u in urls:
        url_counter[u] += 1

print(f"groups within one URL : {same_url}")
print(f"groups across URLs    : {cross_url}")
print("\nTop 15 URLs involved in duplicate groups:")
for u, n in url_counter.most_common(15):
    print(f"  {n:3d}  {u}")