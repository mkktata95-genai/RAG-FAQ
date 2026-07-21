"""
inspect_worst_dup.py — find the worst content_hash duplicate group
and print all source_urls in it to understand the duplication pattern.
"""
import os
from collections import defaultdict
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient

load_dotenv()

INDEX = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v4")
client = SearchClient(os.getenv("AZURE_SEARCH_ENDPOINT"), INDEX, DefaultAzureCredential())

docs = []
skip = 0
while True:
    batch = list(client.search(
        search_text="*",
        select=["chunk_id", "source_url", "content_hash", "title", "chunk_index"],
        top=1000, skip=skip, order_by=["indexed_at asc"],
    ))
    docs.extend(batch)
    if len(batch) < 1000:
        break
    skip += 1000

print(f"Total docs: {len(docs)}")

by_hash = defaultdict(list)
for d in docs:
    h = d.get("content_hash", "")
    if h:
        by_hash[h].append(d)

dup_groups = sorted(
    [(h, v) for h, v in by_hash.items() if len(v) > 1],
    key=lambda x: -len(x[1])
)

print(f"Worst {min(5, len(dup_groups))} duplicate groups:\n")
for h, docs_in_group in dup_groups[:5]:
    urls = [d["source_url"] for d in docs_in_group]
    unique_urls = set(urls)
    print(f"Hash: {h[:16]}...  copies: {len(docs_in_group)}  unique URLs: {len(unique_urls)}")
    for u in list(unique_urls)[:5]:
        count = urls.count(u)
        print(f"  {count}×  {u[:100]}")
    print()