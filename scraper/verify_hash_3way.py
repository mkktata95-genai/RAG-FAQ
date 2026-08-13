"""
verify_hash_3way.py  v1.0.0
============================
Isolates the hash-formula question from the stale-index question.
Does ONE live scrape, then computes content_hash THREE different
ways using the actual cleaning functions from all three pipeline
scripts, and compares them directly. No Azure Search reads at all.

  [1] scraper_hash   = SHA256(scraper.clean_content(raw_markdown))
                        — exact formula from scrape_approved_urls_updatedV5.py
  [2] freshness_hash = SHA256(freshness.clean_scraped_content(raw_markdown))
                        — content_freshnessV1.py's current v1.7.7 formula
  [3] indexer_hash   = SHA256(indexer.clean_content(
                          scraper.clean_content(raw_markdown)))
                        — what chunk_and_index_hqaV5.py ACTUALLY stores
                          in the index's content_hash field (double-clean)

EXPECTED IF v1.7.7 IS CORRECT:
  [1] == [2] == [3]   (all three identical)

EXPECTED IF THE HYPOTHESIS IN THIS SESSION IS RIGHT:
  [1] == [2]   (freshness correctly mirrors scraper)
  [3] != [1]   (indexer's extra clean_content() pass changes the hash)
  -> confirms content_freshnessV1.py needs the indexer's clean_content()
     step added back before hashing, to match what's actually indexed.

USAGE
-----
  python verify_hash_3way.py "https://www.royallondon.com/..."
"""

import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# [1] scraper's own cleaner + [scraper's raw scrape config]
from scrape_approved_urls_updatedV5 import (
    clean_content as scraper_clean_content,
)

# [2] freshness script's cleaner (v1.7.7) + shared browser config
from content_freshnessV1 import (
    clean_scraped_content as freshness_clean_scraped_content,
    _cf_make_browser_config,
)

# [3] indexer's separate URL-stripping cleaner
from chunk_and_index_hqaV5 import (
    clean_content as indexer_clean_content,
)

from crawl4ai import (
    AsyncWebCrawler,
    CrawlerRunConfig,
    DefaultMarkdownGenerator,
    PruningContentFilter,
)

SEP = "=" * 70


def sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


async def fetch_raw_markdown(crawler, url: str) -> str | None:
    """
    Identical CrawlerRunConfig to both scrape_page() (scraper) and
    scrape_url_with_dropdowns() (freshness) — same css_selector,
    same PruningContentFilter threshold, same excluded_tags. This
    is deliberate: we want ONE raw scrape, then branch into the
    three cleaning pipelines from that single common input, so any
    hash difference can ONLY come from the cleaning functions
    themselves, not from scrape-config drift.
    """
    run_cfg = CrawlerRunConfig(
        css_selector=(
            "main, article, .content, #content, "
            ".page-content, .main-content, [role='main']"
        ),
        markdown_generator=DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(threshold=0.45, threshold_type="fixed"),
            options={"ignore_links": False, "ignore_images": True, "skip_internal_links": True},
        ),
        wait_until="domcontentloaded",
        page_timeout=30000,
        verbose=False,
        excluded_tags=["nav", "header", "footer", "aside", "script", "style", "noscript"],
    )
    result = await crawler.arun(url=url, config=run_cfg)
    if not result.success or not result.markdown:
        print(f"  SCRAPE FAILED: {result.error_message}")
        return None
    raw = result.markdown.raw_markdown
    if not raw or len(raw.strip()) < 100:
        print("  content too short")
        return None
    return raw.strip()


async def main():
    if len(sys.argv) < 2:
        print("Usage: python verify_hash_3way.py <url>")
        sys.exit(1)

    url = sys.argv[1]

    print(f"\n{SEP}\n  3-WAY HASH COMPARISON\n  {url}\n{SEP}")

    browser_cfg = _cf_make_browser_config()
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        raw = await fetch_raw_markdown(crawler, url)

    if raw is None:
        sys.exit(1)

    # [1] scraper formula
    scraper_cleaned = scraper_clean_content(raw)
    scraper_hash = sha256(scraper_cleaned)

    # [2] freshness formula (current v1.7.7)
    freshness_cleaned = freshness_clean_scraped_content(raw)
    freshness_hash = sha256(freshness_cleaned)

    # [3] indexer formula — indexer's clean_content() applied
    # ON TOP of the scraper's cleaned output, matching exactly
    # what chunk_and_index_hqaV5.py's chunk_pages() does at
    # lines ~1808/1814 before compute_content_hash().
    indexer_cleaned = indexer_clean_content(scraper_cleaned)
    indexer_hash = sha256(indexer_cleaned)

    print(f"\n  [1] scraper_hash    (scraper.clean_content)              : {scraper_hash}")
    print(f"  [2] freshness_hash  (freshness.clean_scraped_content)    : {freshness_hash}")
    print(f"  [3] indexer_hash    (indexer.clean_content on top of [1]): {indexer_hash}")

    print(f"\n{SEP}")
    print(f"  [1] == [2]  (freshness mirrors scraper) : {'YES' if scraper_hash == freshness_hash else 'NO'}")
    print(f"  [1] == [3]  (indexer changes nothing)   : {'YES' if scraper_hash == indexer_hash else 'NO'}")
    print(f"  [2] == [3]  (freshness matches INDEX)   : {'YES' if freshness_hash == indexer_hash else 'NO'}")
    print(SEP)

    if scraper_hash == freshness_hash and freshness_hash != indexer_hash:
        print("\n  DIAGNOSIS: freshness correctly mirrors the scraper, but the")
        print("  indexer's extra clean_content() (external-URL stripper) pass")
        print("  changes the hash. content_freshnessV1.py must apply the SAME")
        print("  indexer.clean_content() step after its own cleaning, before")
        print("  hashing, to match what's actually stored in the index.")
        print(f"\n  Char diff after indexer.clean_content(): "
              f"{len(scraper_cleaned)} -> {len(indexer_cleaned)} chars")
        if scraper_cleaned != indexer_cleaned:
            # Show what indexer.clean_content() actually stripped
            import difflib
            diff = list(difflib.unified_diff(
                scraper_cleaned.splitlines(),
                indexer_cleaned.splitlines(),
                fromfile="[1] scraper-cleaned",
                tofile="[3] +indexer.clean_content",
                lineterm="", n=1,
            ))
            print(f"\n  What indexer.clean_content() stripped ({len(diff)} diff lines):")
            for line in diff[:30]:
                print(f"     {line}")
    elif scraper_hash == freshness_hash == indexer_hash:
        print("\n  DIAGNOSIS: all three formulas agree on THIS page (likely no")
        print("  external URLs on the page, so indexer.clean_content() was a")
        print("  no-op here). The earlier MISMATCH against the live index is")
        print("  therefore NOT a hash-formula bug — points to a stale/broken")
        print("  index row instead (H1: pre-dates recent chunking fixes).")
    else:
        print("\n  UNEXPECTED: [1] != [2] — freshness does NOT actually mirror")
        print("  the scraper's clean_content() despite the v1.7.7 changelog")
        print("  claim. Needs its own investigation.")


if __name__ == "__main__":
    if sys.platform == "win32":
        import warnings
        warnings.filterwarnings("ignore", message=".*I/O operation on closed pipe.*", category=ResourceWarning)
        warnings.filterwarnings("ignore", message=".*unclosed transport.*", category=ResourceWarning)
        _orig_unraisablehook = sys.unraisablehook

        def _unraisablehook(unraisable):
            msg = str(unraisable.exc_value)
            if "I/O operation on closed pipe" in msg:
                return
            _orig_unraisablehook(unraisable)

        sys.unraisablehook = _unraisablehook

    asyncio.run(main())