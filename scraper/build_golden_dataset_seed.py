"""
build_golden_dataset_seed.py — Golden Dataset Seed Generator

═══════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════
Pulls augmented_questions (HQA) from rlg-faq-index-v5 to seed the
Task 1 golden dataset (coverage accelerator only — NOT a drop-in
replacement). See project discussion: augmented_questions were
LLM-generated FROM each chunk to aid retrieval, so using them
as-is for eval is circular. This script produces a STARTER CSV
with question / source_url / product_category / chunk_id filled,
and expected_answer left BLANK for manual/SME review — that
review step is mandatory before any row is "golden".

Sampling: at most MAX_PER_URL questions per source_url, so
coverage spreads across all 297 approved URLs rather than
clustering on a few content-heavy pages.

═══════════════════════════════════════════════════════════════
USAGE
═══════════════════════════════════════════════════════════════
    python build_golden_dataset_seed.py
    python build_golden_dataset_seed.py --max-per-url 2 --out golden_seed.csv

REQUIRES
    .env with AZURE_SEARCH_ENDPOINT set
    pip install azure-search-documents azure-identity python-dotenv
    DefaultAzureCredential auth (RLG policy — no API keys)

OUTPUT
    CSV: id, product_category, question, expected_answer (blank),
         source_url, chunk_id, chunk_index

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════
v1.0.0 — Aug 2026 | Mukesh Kund
         Initial version. Pulls augmented_questions + title_questions
         from rlg-faq-index-v5, dedupes near-identical questions,
         caps per-URL sampling, writes CSV seed for golden dataset.
         ROLLBACK: n/a — new standalone script, no prior version.
"""

import os
import csv
import argparse
import re
from collections import defaultdict

from dotenv import load_dotenv, find_dotenv
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient

load_dotenv(find_dotenv(usecwd=False))

AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
INDEX_NAME            = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v5")
FETCH_BATCH_SIZE      = 1000
DEFAULT_MAX_PER_URL   = 3
DEFAULT_OUT           = "golden_dataset_seed.csv"

SELECT_FIELDS = [
    "chunk_id",
    "source_url",
    "product_category",
    "chunk_index",
    "title_questions",
    "augmented_questions",
]


def get_client() -> SearchClient:
    if not AZURE_SEARCH_ENDPOINT:
        raise SystemExit("AZURE_SEARCH_ENDPOINT not set in .env")
    credential = DefaultAzureCredential()
    return SearchClient(
        endpoint=AZURE_SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=credential,
    )


def fetch_all_chunks(client: SearchClient) -> list[dict]:
    """Paginate through the full index, retrieving only the fields needed."""
    chunks = []
    skip = 0
    print(f"Fetching chunks from '{INDEX_NAME}'...")
    while True:
        results = list(client.search(
            search_text="*",
            select=SELECT_FIELDS,
            top=FETCH_BATCH_SIZE,
            skip=skip,
        ))
        if not results:
            break
        chunks.extend(results)
        print(f"  skip={skip:>6}: {len(results)} fetched (running total {len(chunks)})")
        if len(results) < FETCH_BATCH_SIZE:
            break
        skip += FETCH_BATCH_SIZE
    return chunks


def split_questions(raw: str) -> list[str]:
    """augmented_questions / title_questions are stored as a single
    string; split on newlines, filter blanks."""
    if not raw:
        return []
    parts = [q.strip(" -\u2022\t") for q in raw.splitlines()]
    return [q for q in parts if len(q) > 5]


def normalise(q: str) -> str:
    """Lowercase, strip punctuation/whitespace for dedup comparison."""
    return re.sub(r"[^a-z0-9 ]", "", q.lower()).strip()


def build_seed_rows(chunks: list[dict], max_per_url: int) -> list[dict]:
    rows = []
    seen_norms = set()
    per_url_count = defaultdict(int)

    for c in chunks:
        url = c.get("source_url", "")
        if not url:
            continue

        candidates = split_questions(c.get("title_questions", "")) + \
                     split_questions(c.get("augmented_questions", ""))

        for q in candidates:
            if per_url_count[url] >= max_per_url:
                break

            norm = normalise(q)
            if not norm or norm in seen_norms:
                continue
            seen_norms.add(norm)

            rows.append({
                "id":               f"SEED-{len(rows)+1:04d}",
                "product_category": c.get("product_category", "general"),
                "question":         q,
                "expected_answer":  "",   # MUST be filled by reviewer
                "source_url":       url,
                "chunk_id":         c.get("chunk_id", ""),
                "chunk_index":      c.get("chunk_index", 0),
            })
            per_url_count[url] += 1

    return rows


def write_csv(rows: list[dict], out_path: str):
    fieldnames = [
        "id", "product_category", "question", "expected_answer",
        "source_url", "chunk_id", "chunk_index",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Build golden dataset seed CSV from HQA augmented_questions")
    parser.add_argument("--max-per-url", type=int, default=DEFAULT_MAX_PER_URL,
                         help=f"Max questions sampled per source_url (default {DEFAULT_MAX_PER_URL})")
    parser.add_argument("--out", type=str, default=DEFAULT_OUT,
                         help=f"Output CSV path (default {DEFAULT_OUT})")
    args = parser.parse_args()

    client = get_client()
    chunks = fetch_all_chunks(client)
    print(f"\nTotal chunks fetched: {len(chunks)}")

    rows = build_seed_rows(chunks, args.max_per_url)
    write_csv(rows, args.out)

    urls_covered = len({r["source_url"] for r in rows})
    print(f"\nSeed rows written: {len(rows)}")
    print(f"Distinct source URLs covered: {urls_covered}")
    print(f"Output: {args.out}")
    print("\nNEXT STEP (mandatory): review each row, fill expected_answer,")
    print("rewrite a subset into realistic customer phrasing, then move")
    print("reviewed rows into the version-controlled golden_dataset.json.")


if __name__ == "__main__":
    main()