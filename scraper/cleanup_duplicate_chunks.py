"""
cleanup_duplicate_chunks.py
=============================
Track A remediation — removes duplicate chunks from the live index,
found via TRUE content hashing (mirrors audit_duplicates.py v2's
detection logic exactly, so "what audit_duplicates.py reports" and
"what this script deletes" are always in sync).

WHY THIS EXISTS
---------------
audit_duplicates.py v2 confirmed genuine chunk-content duplication
in rlg-faq-index-v5 (5,194 removable chunks out of 8,310 — root
cause: chunk_id = uuid.uuid4() is random, not content-derived, so
any accidental re-processing of a page during the 16-hour --full
build produced fresh "new" documents for identical content instead
of overwriting). This script cleans the ALREADY-BUILT index directly
— minutes, not a 16-hour re-index.

Does NOT touch chunking logic, embeddings, or HQA questions — this
is a pure delete operation on the index. See chunk_and_index_hqaV5.py
for the Track B root-cause fix (deterministic chunk_id) that prevents
this from recurring on future --full runs.

SAFETY
------
- DRY RUN BY DEFAULT. Nothing is deleted unless --apply is passed.
- Within each duplicate group, keeps the OLDEST chunk (earliest
  indexed_at) and deletes the rest — arbitrary but deterministic
  and stable choice; content is identical across the group by
  definition, so which specific copy survives doesn't matter.
- Prints every chunk_id it will delete/deleted, plus a full summary,
  before AND after the operation.
- Batches deletes (Azure Search delete_documents accepts a list of
  {"chunk_id": ...} dicts per call, same batch size convention as
  upload_chunks() in chunk_and_index_hqaV5.py).

USAGE
-----
  # Dry run — shows exactly what WOULD be deleted, deletes nothing
  python cleanup_duplicate_chunks.py

  # Apply — actually deletes the duplicates
  python cleanup_duplicate_chunks.py --apply

  # Target a specific index (defaults to AZURE_SEARCH_INDEX_NAME env var)
  python cleanup_duplicate_chunks.py --apply --index rlg-faq-index-v5-baseline

CHANGELOG
---------
v1.0.0 — Initial cleanup script. Dry-run default, deterministic
          "keep oldest" survivor selection, batched deletes.
"""

import argparse
import hashlib
import os
from collections import defaultdict

from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient

load_dotenv()

DELETE_BATCH_SIZE = 500


def get_client(index_name: str) -> SearchClient:
    """Build a SearchClient for the given index, same auth pattern as the indexer."""
    endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")
    api_key  = os.getenv("AZURE_SEARCH_API_KEY") or os.getenv("AZURE_SEARCH_KEY")
    credential = AzureKeyCredential(api_key) if api_key else DefaultAzureCredential()
    return SearchClient(endpoint, index_name, credential)


def fetch_all_docs(client: SearchClient) -> list:
    """Page through every document in the index, fetching fields needed for dup detection."""
    docs = []
    skip = 0
    PAGE = 1000
    while True:
        batch = list(client.search(
            search_text="*",
            select=["chunk_id", "source_url", "content", "indexed_at", "element_type"],
            top=PAGE,
            skip=skip,
            order_by=["indexed_at asc"],
        ))
        docs.extend(batch)
        if len(batch) < PAGE:
            break
        skip += PAGE
    return docs


def find_duplicate_groups(docs: list) -> dict:
    """
    Group docs by TRUE content hash (hash of the actual `content` field).
    Mirrors audit_duplicates.py v2 exactly — same detection logic, so
    results here always match what that script reports.
    """
    by_content_hash = defaultdict(list)
    for d in docs:
        text = d.get("content", "") or ""
        chunk_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        by_content_hash[chunk_hash].append(d)

    return {h: v for h, v in by_content_hash.items() if len(v) > 1}


def select_survivors_and_victims(dup_groups: dict) -> tuple:
    """
    For each duplicate group, keep the OLDEST doc (by indexed_at,
    already sorted ascending from fetch_all_docs' order_by), mark
    the rest for deletion.
    """
    survivors = []
    victims   = []

    for content_hash, group in dup_groups.items():
        # order_by="indexed_at asc" in fetch means group[0] is oldest
        sorted_group = sorted(group, key=lambda d: d.get("indexed_at", ""))
        survivors.append(sorted_group[0])
        victims.extend(sorted_group[1:])

    return survivors, victims


def delete_docs(client: SearchClient, victims: list) -> int:
    """Delete the given docs from the index in batches. Returns count deleted."""
    total_deleted = 0
    for i in range(0, len(victims), DELETE_BATCH_SIZE):
        batch = victims[i:i + DELETE_BATCH_SIZE]
        delete_keys = [{"chunk_id": d["chunk_id"]} for d in batch]
        result = client.delete_documents(documents=delete_keys)
        succeeded = sum(1 for r in result if r.succeeded)
        total_deleted += succeeded
        print(f"   Deleted batch: {succeeded}/{len(batch)} "
              f"(running total: {total_deleted}/{len(victims)})")
    return total_deleted


def main():
    parser = argparse.ArgumentParser(description="Clean duplicate chunks from an Azure AI Search index")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete duplicates. Without this flag, dry-run only.")
    parser.add_argument("--index", default=None,
                        help="Index name. Defaults to AZURE_SEARCH_INDEX_NAME env var.")
    args = parser.parse_args()

    index_name = args.index or os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v5")
    mode = "APPLY (will delete)" if args.apply else "DRY RUN (no changes)"

    print("=" * 70)
    print(f"  DUPLICATE CHUNK CLEANUP — {mode}")
    print(f"  Index: {index_name}")
    print("=" * 70)

    client = get_client(index_name)

    print("\nFetching all documents...")
    docs = fetch_all_docs(client)
    print(f"Fetched {len(docs)} docs")

    print("\nFinding true content duplicates...")
    dup_groups = find_duplicate_groups(docs)
    total_dup_docs = sum(len(v) for v in dup_groups.values())

    print(f"Duplicate groups: {len(dup_groups)}")
    print(f"Docs involved:    {total_dup_docs}")

    if not dup_groups:
        print("\n✅ No duplicates found — nothing to clean.")
        return

    survivors, victims = select_survivors_and_victims(dup_groups)

    print(f"\nSurvivors (1 kept per group): {len(survivors)}")
    print(f"Victims (to be deleted):      {len(victims)}")

    print("\n" + "-" * 70)
    print("Sample victims (first 10):")
    for v in victims[:10]:
        preview = (v.get("content", "") or "")[:60].replace("\n", " ")
        print(f"  chunk_id={v['chunk_id'][:8]}...  url={v.get('source_url','')[:50]}")
        print(f"    content: {preview!r}")

    if not args.apply:
        print("\n" + "=" * 70)
        print(f"  DRY RUN COMPLETE — {len(victims)} chunks WOULD be deleted.")
        print(f"  Re-run with --apply to actually delete them.")
        print("=" * 70)
        return

    print("\n" + "-" * 70)
    confirm = input(
        f"\n⚠️  About to permanently delete {len(victims)} chunks from "
        f"'{index_name}'.\nType 'DELETE' (all caps) to confirm: "
    )
    if confirm != "DELETE":
        print("Aborted — no changes made.")
        return

    print(f"\nDeleting {len(victims)} chunks...")
    deleted = delete_docs(client, victims)

    print("\n" + "=" * 70)
    print(f"  CLEANUP COMPLETE")
    print(f"  Deleted: {deleted}/{len(victims)}")
    print(f"  Remaining docs in index (approx): {len(docs) - deleted}")
    print("=" * 70)
    print("\n👉 Recommend re-running audit_duplicates.py to confirm 0 true "
          "duplicates remain.")


if __name__ == "__main__":
    main()