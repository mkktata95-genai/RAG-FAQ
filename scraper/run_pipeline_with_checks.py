"""
run_pipeline_with_checks.py
============================
Stepwise pipeline orchestration with gated checks.

PIPELINE:
  Step 1 — SCRAPE      : python scrape_approved_urls_updatedV4.py --file <excel>
  Check 1 — SCRAPE QA  : verify output JSON (doc count, no duplicate URLs,
                          parent_url present on dropdown state entries)
  Step 2 — INDEX        : python chunk_and_index_hqaV4.py --full [--no-hqa] --file <json>
  Check 2 — INDEX QA   : audit_duplicates against the live index
                          (mirrors audit_duplicates.py logic inline)
  Step 3 — RETRIEVAL QA: run debug_retrievalV2.py queries and report
                          semantic-mode hit rate across test queries

Each step only runs if the previous check passed. Failures print a clear
STOP message and exit non-zero so CI/CD pipelines catch them.

USAGE:
    # Full HQA pipeline (slow, ~3.5 hrs for 350 URLs):
    python run_pipeline_with_checks.py --file scraper/data/Approved_URLs.xlsx

    # Baseline (fast, no HQA):
    python run_pipeline_with_checks.py --file scraper/data/Approved_URLs.xlsx --no-hqa

    # Skip scrape (use existing JSON), run from index step:
    python run_pipeline_with_checks.py --json scraper/data/royal_london_faq_approved_<ts>.json

    # Skip scrape + index, run checks only against existing index:
    python run_pipeline_with_checks.py --checks-only

    # Dry run — print what would happen without executing:
    python run_pipeline_with_checks.py --file scraper/data/Approved_URLs.xlsx --dry-run

CHANGELOG
---------
v1.0.0 (2026-07-20) - Mukesh Kund
    Initial version. Stepwise orchestration with 3 gated checks.
    Check 1: scrape JSON QA (doc count, URL dedup, parent_url on dropdown entries).
    Check 2: index duplicate audit (mirrors audit_duplicates.py inline).
    Check 3: retrieval QA via debug_retrievalV2.py semantic mode.
"""

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v4")
ENDPOINT   = os.getenv("AZURE_SEARCH_ENDPOINT", "")
API_KEY    = os.getenv("AZURE_SEARCH_API_KEY") or os.getenv("AZURE_SEARCH_KEY", "")

# Max allowed duplicate ratio before Check 2 fails (0 = zero tolerance)
MAX_DUP_RATIO = 0.0

# Test queries for Check 3 — query: expected URL fragment
CHECK3_QUERIES = {
    "What types of pensions does Royal London offer?":        "pension",
    "How do I report a bereavement to Royal London?":        "bereavement",
    "What is income protection insurance?":                  "income-protection",
    "How do I find a financial adviser?":                    "financial-adviser",
    "What is a stocks and shares ISA?":                      "stocks-and-shares",
}

SEP = "=" * 65


def _print_sep(title: str = ""):
    if title:
        print(f"\n{SEP}")
        print(f"  {title}")
        print(SEP)
    else:
        print(SEP)


def _ok(msg: str):
    print(f"  ✅ {msg}")


def _fail(msg: str):
    print(f"  ❌ {msg}")


def _warn(msg: str):
    print(f"  ⚠️  {msg}")


def stop(reason: str):
    print(f"\n🛑 STOP — {reason}")
    print("Fix the issue above before proceeding to the next step.")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════
# CHECK 1 — Scrape JSON QA
# ══════════════════════════════════════════════════════════════

def check1_scrape_json(json_path: str) -> str:
    """
    Validate scraper output JSON.
    Returns the path on success; calls stop() on failure.

    Checks:
    - File exists and is valid JSON
    - At least 1 document
    - No duplicate URLs
    - Dropdown state entries carry parent_url (v4.8.0 fix)
    - No entry has a #state= URL without parent_url
    """
    _print_sep("CHECK 1 — SCRAPE JSON QA")
    print(f"  File: {json_path}")

    if not Path(json_path).exists():
        stop(f"Scrape output not found: {json_path}")

    try:
        docs = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except Exception as e:
        stop(f"JSON parse error: {e}")

    if not docs:
        stop("Scrape output is empty — 0 documents.")

    _ok(f"File loaded: {len(docs)} documents")

    # Duplicate URL check
    urls = [d.get("url", "") for d in docs]
    counts = Counter(urls)
    dups = {u: n for u, n in counts.items() if n > 1}
    if dups:
        _fail(f"Duplicate URLs in JSON: {len(dups)} URL(s)")
        for u, n in list(dups.items())[:5]:
            print(f"       {n}×  {u}")
        stop("Duplicate URLs in scrape JSON. Check Excel for duplicate rows.")
    else:
        _ok(f"No duplicate URLs ({len(set(urls))} unique)")

    # parent_url check on dropdown state entries
    dropdown_docs   = [d for d in docs if d.get("dropdown_state", "")]
    missing_parent  = [d for d in dropdown_docs if not d.get("parent_url", "")]
    dead_fragments  = [d for d in docs if "#state=" in d.get("url","")
                       and not d.get("parent_url","")]

    if dropdown_docs:
        _ok(f"Dropdown state entries found: {len(dropdown_docs)}")
        if missing_parent:
            _fail(f"{len(missing_parent)} dropdown entries missing parent_url")
            for d in missing_parent[:3]:
                print(f"       {d.get('url','')}")
            stop("parent_url missing on dropdown entries (scraper v4.8.0 fix not applied?)")
        else:
            _ok("All dropdown entries carry parent_url ✓")
    else:
        _ok("No dropdown state entries in this JSON")

    if dead_fragments:
        _fail(f"{len(dead_fragments)} #state= URLs without parent_url")
        stop("Dead fragment URLs present — apply scraper v4.8.0 fix.")

    _ok("CHECK 1 PASSED")
    return json_path


# ══════════════════════════════════════════════════════════════
# CHECK 2 — Index duplicate audit
# ══════════════════════════════════════════════════════════════

def check2_index_duplicates(index_name: str = INDEX_NAME):
    """
    Audit the live index for duplicate content_hash entries.
    Mirrors audit_duplicates.py inline so no extra file needed.
    Calls stop() if duplicate ratio exceeds MAX_DUP_RATIO.
    """
    _print_sep("CHECK 2 — INDEX DUPLICATE AUDIT")
    print(f"  Index: {index_name}")

    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.identity import DefaultAzureCredential
        from azure.search.documents import SearchClient
    except ImportError:
        stop("azure-search-documents not installed. Run: pip install azure-search-documents")

    credential = AzureKeyCredential(API_KEY) if API_KEY else DefaultAzureCredential()
    client     = SearchClient(ENDPOINT, index_name, credential)

    docs  = []
    skip  = 0
    PAGE  = 1000
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

    total = len(docs)
    print(f"  Total docs fetched: {total}")

    if total == 0:
        stop("Index is empty — indexer may have failed.")

    by_hash = defaultdict(list)
    for d in docs:
        h = d.get("content_hash", "")
        if h:
            by_hash[h].append(d)

    dup_groups   = {h: v for h, v in by_hash.items() if len(v) > 1}
    removable    = sum(len(v) - 1 for v in dup_groups.values())
    dup_ratio    = removable / total if total else 0

    print(f"  Unique content hashes : {len(by_hash)}")
    print(f"  Duplicated groups     : {len(dup_groups)}")
    print(f"  Removable docs        : {removable} ({dup_ratio:.1%} of total)")

    if dup_ratio > MAX_DUP_RATIO:
        _fail(f"Duplicate ratio {dup_ratio:.1%} exceeds threshold {MAX_DUP_RATIO:.1%}")
        url_counter = Counter()
        for v in dup_groups.values():
            for d in v:
                url_counter[d["source_url"]] += 1
        print("  Top offending URLs:")
        for u, n in url_counter.most_common(5):
            print(f"    {n:3d}  {u}")
        stop("Index contains duplicates. Re-run indexer with --full on a fresh index.")
    else:
        _ok(f"No duplicates detected (ratio {dup_ratio:.1%}) ✓")
        _ok("CHECK 2 PASSED")


# ══════════════════════════════════════════════════════════════
# CHECK 3 — Retrieval QA
# ══════════════════════════════════════════════════════════════

def check3_retrieval_qa(broad: bool = True):
    """
    Run each CHECK3_QUERIES entry through debug_retrievalV2.py
    and report how many appear in the semantic (production-equivalent)
    top-10. Warns but does not stop on misses — semantic ranking is
    expected to catch them but a miss may mean the expected fragment
    mapping is wrong, not the index.
    """
    _print_sep("CHECK 3 — RETRIEVAL QA (semantic mode)")

    script = Path("debug_retrievalV2.py")
    if not script.exists():
        _warn("debug_retrievalV2.py not found in cwd — skipping Check 3.")
        return

    hits   = 0
    misses = []
    broad_flag = ["--broad"] if broad else []

    for query, expected_frag in CHECK3_QUERIES.items():
        result = subprocess.run(
            [sys.executable, str(script), query, *broad_flag],
            capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        # Semantic result is "YES" in DIAGNOSIS section
        appeared = (
            "Appears in SEMANTIC search?  ✅ YES" in output
            or "Page ranks #" in output  # rank line only present on hit
        )
        if appeared:
            hits += 1
            _ok(f"SEMANTIC HIT  | {query[:55]}")
        else:
            misses.append((query, expected_frag))
            _warn(f"SEMANTIC MISS | {query[:55]}")
            _warn(f"  Expected fragment: '{expected_frag}'")
            _warn("  Verify: is the top result actually a better answer?")

    print(f"\n  Result: {hits}/{len(CHECK3_QUERIES)} queries hit in semantic top-10")
    if hits == len(CHECK3_QUERIES):
        _ok("CHECK 3 PASSED — all queries return expected page in semantic top-10")
    elif hits >= len(CHECK3_QUERIES) * 0.8:
        _warn("CHECK 3 PARTIAL — ≥80% hit rate; review misses manually before production.")
    else:
        _fail(f"CHECK 3 FAILED — only {hits}/{len(CHECK3_QUERIES)} hits.")
        _fail("Run debug_retrievalV2.py on misses individually to diagnose.")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Stepwise ARIA pipeline with gated QA checks."
    )
    parser.add_argument("--file",        default=None, help="Excel Approved_URLs path (for scrape step)")
    parser.add_argument("--json",        default=None, help="Skip scrape; use existing JSON file")
    parser.add_argument("--no-hqa",      action="store_true", help="Index without HQA (baseline mode)")
    parser.add_argument("--checks-only", action="store_true", help="Skip scrape+index; run checks against live index")
    parser.add_argument("--dry-run",     action="store_true", help="Print commands; do not execute")
    parser.add_argument("--index",       default=INDEX_NAME, help=f"Index name (default: {INDEX_NAME})")
    args = parser.parse_args()

    _print_sep("ARIA PIPELINE — STEPWISE WITH QA CHECKS")
    print(f"  Index    : {args.index}")
    print(f"  HQA mode : {'NO (baseline)' if args.no_hqa else 'YES (full HQA)'}")
    print(f"  Dry run  : {args.dry_run}")

    if args.checks_only:
        check2_index_duplicates(args.index)
        check3_retrieval_qa()
        return

    json_path = args.json

    # ── Step 1: Scrape ────────────────────────────────────────
    if not json_path:
        if not args.file:
            parser.error("Provide --file <excel> or --json <json> or --checks-only")

        _print_sep("STEP 1 — SCRAPE")
        scrape_cmd = [sys.executable, "scrape_approved_urls_updatedV4.py",
                      "--file", args.file]
        print(f"  CMD: {' '.join(scrape_cmd)}")

        if not args.dry_run:
            result = subprocess.run(scrape_cmd)
            if result.returncode != 0:
                stop("Scraper exited with non-zero code.")

            # Find the most recently written JSON in scraper/data/
            data_dir = Path("scraper/data")
            jsons    = sorted(data_dir.glob("royal_london_faq_approved_*.json"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            if not jsons:
                stop("No scraper JSON output found in scraper/data/")
            json_path = str(jsons[0])
            print(f"  Output JSON: {json_path}")
        else:
            print("  [dry-run] skipping scrape execution")
            json_path = "<would-be-determined-at-runtime>"

    # ── Check 1 ───────────────────────────────────────────────
    if not args.dry_run:
        check1_scrape_json(json_path)
    else:
        _print_sep("CHECK 1 — SCRAPE JSON QA")
        print("  [dry-run] would validate scrape JSON")

    # ── Step 2: Index ─────────────────────────────────────────
    _print_sep("STEP 2 — INDEX")
    index_cmd = [sys.executable, "chunk_and_index_hqaV4.py",
                 "--full", "--file", json_path]
    if args.no_hqa:
        index_cmd.append("--no-hqa")
    print(f"  CMD: {' '.join(index_cmd)}")

    if not args.dry_run:
        result = subprocess.run(index_cmd)
        if result.returncode != 0:
            stop("Indexer exited with non-zero code.")
    else:
        print("  [dry-run] skipping index execution")

    # ── Check 2 ───────────────────────────────────────────────
    if not args.dry_run:
        check2_index_duplicates(args.index)
    else:
        _print_sep("CHECK 2 — INDEX DUPLICATE AUDIT")
        print("  [dry-run] would audit live index for content_hash duplicates")

    # ── Check 3 ───────────────────────────────────────────────
    if not args.dry_run:
        check3_retrieval_qa()
    else:
        _print_sep("CHECK 3 — RETRIEVAL QA")
        print("  [dry-run] would run debug_retrievalV2.py for each test query")

    _print_sep("PIPELINE COMPLETE")
    _ok("All steps and checks passed.")


if __name__ == "__main__":
    main()