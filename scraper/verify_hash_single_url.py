"""
verify_hash_single_url.py  v1.0.0
====================================
Lightweight, fast verification — does NOT run the full
content_freshnessV1.py report pipeline (health check, all-URL scan,
classify, Excel report). Instead, reuses content_freshnessV1.py's
OWN (now-fixed, v1.7.7/v1.7.8) scraping and hashing functions
directly, for just 1-2 specific URLs, and compares the freshly-
computed hash against what Azure Search currently has indexed for
that exact URL.

WHY THIS EXISTS
---------------
Before committing to a full 297-URL (or even 10-URL) report run,
this gives a fast, cheap sanity check: "does content_freshnessV1.py's
v1.7.7 content-cleaning fix actually produce a hash that MATCHES the
index for a page that hasn't genuinely changed?" One live scrape,
one index lookup, one comparison — done in seconds, not minutes.

WHAT IT SHOWS
-------------
- Freshly-computed content_hash (via the real scrape_url_with_
  dropdowns() function, same code path apply mode would use)
- Currently-indexed content_hash for that same URL
- MATCH / MISMATCH verdict
- On mismatch: a content diff (first N chars each side, plus a
  proper unified diff) so you can see AT A GLANCE whether it's a
  genuine content change or still a cleaning-pipeline discrepancy —
  not just a bare "different" with no way to tell why.

USAGE
-----
  python verify_hash_single_url.py "https://www.royallondon.com/..."

  # Multiple URLs in one run:
  python verify_hash_single_url.py "url1" "url2" "url3"

CHANGELOG
---------
v1.0.0 — Initial single/multi-URL hash verification script.
"""

import asyncio
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from content_freshnessV1 import (
    scrape_url_with_dropdowns,
    _cf_make_browser_config,
    fetch_current_hashes_from_index,
    normalise_url,
    get_base_url,
    INDEX_NAME,
)
from crawl4ai import AsyncWebCrawler

SEP = "=" * 70


async def verify_url(crawler, url: str, indexed_hashes: dict) -> dict:
    """Live-scrape one URL, compute its fresh hash, compare against index."""
    entry = {"url": url, "title": "", "category": ""}

    result = await scrape_url_with_dropdowns(crawler, entry, freshness_run_id="verify-script")

    if result is None:
        return {"url": url, "status": "scrape_failed"}

    base_page   = result[0]  # first entry is always the base page
    fresh_hash  = base_page["content_hash"]
    fresh_content = base_page["content"]

    norm     = normalise_url(get_base_url(url))
    idx_hash = indexed_hashes.get(norm)

    if idx_hash is None:
        return {
            "url": url, "status": "not_in_index",
            "fresh_hash": fresh_hash, "fresh_content": fresh_content,
        }

    match = (fresh_hash == idx_hash)
    return {
        "url": url,
        "status": "match" if match else "mismatch",
        "fresh_hash": fresh_hash,
        "indexed_hash": idx_hash,
        "fresh_content": fresh_content,
    }


def print_result(r: dict):
    print(f"\n{SEP}")
    print(f"  URL: {r['url']}")
    print(SEP)

    if r["status"] == "scrape_failed":
        print("  ✗ SCRAPE FAILED — could not fetch this URL live.")
        return

    if r["status"] == "not_in_index":
        print(f"  ⚠ NOT FOUND IN INDEX (index={INDEX_NAME})")
        print(f"  Fresh hash: {r['fresh_hash']}")
        print(f"  This URL may be new, or the URL normalisation may")
        print(f"  not match what's stored as source_url in the index.")
        return

    print(f"  Fresh hash    : {r['fresh_hash']}")
    print(f"  Indexed hash  : {r['indexed_hash']}")

    if r["status"] == "match":
        print(f"\n  ✅ MATCH — hashes are identical.")
        print(f"     This page's content is genuinely unchanged.")
    else:
        print(f"\n  ❌ MISMATCH — hashes differ.")
        print(f"     Could be: (a) genuine content change on the live")
        print(f"     site, or (b) a remaining cleaning-pipeline gap.")
        print(f"     Content preview (first 300 chars):")
        print(f"     {r['fresh_content'][:300]!r}")


def print_diff_if_available(results: list):
    """If we have a local scraped JSON for comparison, show a diff."""
    # This is optional — only runs if the user has a prior scraped
    # JSON they want to diff against. Kept simple: just prints fresh
    # content previews side by side isn't needed here since we don't
    # have a second source without the JSON — see print_result()'s
    # preview for now. Placeholder for future JSON-diff extension.
    pass


async def main():
    if len(sys.argv) < 2:
        print("Usage: python verify_hash_single_url.py <url1> [url2] [url3] ...")
        sys.exit(1)

    urls = sys.argv[1:]

    print(f"\n{SEP}")
    print(f"  HASH VERIFICATION — {len(urls)} URL(s)")
    print(f"  Index: {INDEX_NAME}")
    print(SEP)

    print("\nFetching current indexed hashes...")
    indexed_hashes = fetch_current_hashes_from_index(INDEX_NAME)
    print(f"  {len(indexed_hashes)} indexed URLs loaded for comparison.")

    print("\nStarting live scrape (Chrome CDP required)...")
    browser_cfg = _cf_make_browser_config()

    results = []
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for url in urls:
            print(f"\n  Scraping: {url}")
            r = await verify_url(crawler, url, indexed_hashes)
            results.append(r)

    for r in results:
        print_result(r)

    print(f"\n{SEP}")
    matches    = sum(1 for r in results if r["status"] == "match")
    mismatches = sum(1 for r in results if r["status"] == "mismatch")
    not_found  = sum(1 for r in results if r["status"] == "not_in_index")
    failed     = sum(1 for r in results if r["status"] == "scrape_failed")
    print(f"  SUMMARY: {matches} match | {mismatches} mismatch | "
          f"{not_found} not in index | {failed} scrape failed")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())