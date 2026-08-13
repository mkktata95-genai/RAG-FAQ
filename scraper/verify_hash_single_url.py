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
    get_search_client,
    INDEX_NAME,
)
from crawl4ai import AsyncWebCrawler

SEP = "=" * 70


def fetch_indexed_content(url: str, index_name: str = INDEX_NAME) -> str | None:
    """
    Fetch the actual STORED content (not just the hash) for a URL's
    base-page chunk from the index — lets us show a real diff on
    mismatch instead of just "hashes differ, here's a preview of one
    side". Pages a full match-all scan (same pagination pattern as
    content_freshnessV1.py's own index-query functions) and returns
    the chunk_index=0 content for the first matching source_url.
    """
    client  = get_search_client(index_name)
    norm    = normalise_url(get_base_url(url))
    skip    = 0
    page_sz = 1000
    try:
        while True:
            results = client.search(
                search_text="*",
                select=["source_url", "chunk_index", "content"],
                top=page_sz,
                skip=skip,
            )
            batch = list(results)
            if not batch:
                break
            for r in batch:
                src = r.get("source_url", "")
                if normalise_url(get_base_url(src)) == norm and r.get("chunk_index") == 0:
                    return r.get("content", "")
            if len(batch) < page_sz:
                break
            skip += page_sz
    except Exception as e:
        print(f"  (could not fetch indexed content for diff: {e})")
    return None



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
        indexed_content = fetch_indexed_content(r["url"])
        if indexed_content is None:
            print(f"     Could not fetch indexed content for a real diff —")
            print(f"     fresh content preview (first 300 chars):")
            print(f"     {r['fresh_content'][:300]!r}")
        else:
            diff = list(difflib.unified_diff(
                indexed_content.splitlines(),
                r["fresh_content"].splitlines(),
                fromfile="INDEXED (old)",
                tofile="FRESH (live now)",
                lineterm="",
                n=1,
            ))
            if not diff:
                print(f"     ⚠ Hashes differ but line-level diff is EMPTY —")
                print(f"     likely a whitespace-only or invisible-character")
                print(f"     difference. Indexed length={len(indexed_content)},")
                print(f"     fresh length={len(r['fresh_content'])}.")
            else:
                print(f"     Real diff (indexed vs fresh, {len(diff)} changed lines):")
                for line in diff[:40]:
                    print(f"     {line}")
                if len(diff) > 40:
                    print(f"     ... ({len(diff) - 40} more diff lines truncated)")


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
    # v1.1.0 — Suppress asyncio ProactorEventLoop GC noise on Windows.
    # Exact port of the proven pattern already used in
    # scrape_approved_urls_updatedV5.py and content_freshnessV1.py —
    # NOT a blanket "swallow all stderr" approach. This targets ONLY
    # the one known cosmetic message (ValueError: I/O operation on
    # closed pipe, from crawl4ai's browser subprocess cleanup during
    # interpreter shutdown). Every other error — including genuine
    # bugs in scrape_url_with_dropdowns() or anywhere else in this
    # script — still surfaces normally. A blanket sys.stderr =
    # io.StringIO() around the whole run would hide real tracebacks
    # too, which defeats the entire point of a diagnostic script.
    # On Linux: sys.platform != "win32" — complete no-op.
    if sys.platform == "win32":
        import warnings
        warnings.filterwarnings(
            "ignore",
            message=".*I/O operation on closed pipe.*",
            category=ResourceWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=".*unclosed transport.*",
            category=ResourceWarning,
        )

        _orig_unraisablehook = sys.unraisablehook

        # Suppress the noisy Windows ProactorEventLoop GC warning only.
        def _unraisablehook(unraisable):
            msg = str(unraisable.exc_value)
            if "I/O operation on closed pipe" in msg:
                return  # suppress — Windows asyncio GC noise
            _orig_unraisablehook(unraisable)

        sys.unraisablehook = _unraisablehook

    asyncio.run(main())