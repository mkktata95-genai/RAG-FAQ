"""
test_single_url_tabs.py  v1.0.0
==================================
Minimal single-URL test for the new tab-click handler.

Calls scrape_page() directly against ONE known tab page — bypasses
the full Excel URL list entirely. Verifies:

  1. Tab detection fires correctly
  2. Playwright click-through captures all tab content
  3. Output is a list[dict] with 3 tab_state entries (not 1 dict)
  4. Each entry has DIFFERENT content (proves tabs weren't just
     re-scraping the same default panel)
  5. url/parent_url are identical across all entries (citation safety)

USAGE
-----
  # Chrome CDP must be running for the initial crawler.arun() call
  python test_single_url_tabs.py

TEST URL
--------
  investing-responsibly — known 3-tab page (Invest | Engage | Embed),
  confirmed via screenshot inspection earlier in this session.

CHANGELOG
---------
v1.0.0 — Initial single-URL diagnostic for tab-click handler.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from crawl4ai import AsyncWebCrawler

from scraper.scrape_approved_urls_updatedV4_old import (
    scrape_page,
    _make_browser_config,
)

TEST_URL = "https://www.royallondon.com/pensions/investment-options/investing-responsibly/"
TEST_TITLE = "How we invest responsibly"

SEP = "=" * 70


async def main():
    print(SEP)
    print("  SINGLE URL TAB TEST")
    print(f"  URL: {TEST_URL}")
    print(SEP)

    page_info = {
        "url":            TEST_URL,
        "title":          TEST_TITLE,
        "excel_category": "",
    }

    browser_config = _make_browser_config()

    print("\nStarting scrape (crawler.arun + tab detection + Playwright click)...\n")

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await scrape_page(crawler, page_info, index=1, total=1)

    print(f"\n{SEP}")
    print("  RESULT")
    print(SEP)

    if result is None:
        print("  ✗ FAIL — scrape_page returned None")
        sys.exit(1)

    if isinstance(result, dict):
        print("  ✗ FAIL — returned single dict, not a list")
        print("    Tab detection did NOT fire — check has_content_tabs_in_html()")
        print(f"\n  Content preview:\n  {result.get('content', '')[:300]}")
        sys.exit(1)

    if isinstance(result, list):
        print(f"  ✓ Returned list of {len(result)} entries")

        if len(result) < 2:
            print(f"  ✗ FAIL — expected 3 tabs, got {len(result)} entries")
            sys.exit(1)

        print(f"\n  Expected: 3 entries (Invest | Engage | Embed)")
        print(f"  Actual:   {len(result)} entries\n")

        urls_seen    = set()
        parent_urls  = set()
        contents     = []

        for i, entry in enumerate(result, 1):
            tab_state = entry.get("tab_state", "MISSING")
            content   = entry.get("content", "")
            url       = entry.get("url", "")
            parent    = entry.get("parent_url", "")

            urls_seen.add(url)
            parent_urls.add(parent)
            contents.append(content)

            print(f"  [{i}] tab_state: {tab_state}")
            print(f"      url:        {url}")
            print(f"      parent_url: {parent}")
            print(f"      chars:      {len(content)}")
            print(f"      preview:    {content[:120]}")
            print()

        # ── Validation checks ────────────────────────────────────────────
        print(SEP)
        print("  VALIDATION")
        print(SEP)

        checks_passed = []
        checks_failed = []

        # Check 1: all entries share same url
        if len(urls_seen) == 1:
            checks_passed.append("All entries share same url")
        else:
            checks_failed.append(f"url mismatch across entries: {urls_seen}")

        # Check 2: all entries share same parent_url
        if len(parent_urls) == 1:
            checks_passed.append("All entries share same parent_url")
        else:
            checks_failed.append(f"parent_url mismatch: {parent_urls}")

        # Check 3: content is genuinely different per tab (not duplicated)
        unique_contents = set(contents)
        if len(unique_contents) == len(contents):
            checks_passed.append(
                f"All {len(contents)} tab contents are unique (no duplication)"
            )
        else:
            checks_failed.append(
                f"Only {len(unique_contents)}/{len(contents)} unique contents — "
                f"some tabs may have captured the same panel"
            )

        # Check 4: all expected tab labels present
        expected_labels = {"Invest", "Engage", "Embed"}
        actual_labels = {e.get("tab_state", "") for e in result}
        missing = expected_labels - actual_labels
        if not missing:
            checks_passed.append("All expected labels found: Invest, Engage, Embed")
        else:
            checks_failed.append(f"Missing expected labels: {missing}")

        for c in checks_passed:
            print(f"  ✓ {c}")
        for c in checks_failed:
            print(f"  ✗ {c}")

        print(SEP)
        if checks_failed:
            print("  OVERALL: FAIL — review issues above")
            sys.exit(1)
        else:
            print("  OVERALL: PASS — tab handler working correctly")

        # Save full output for manual inspection
        out_path = Path("test_single_url_tabs_output.json")
        out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\n  Full output saved → {out_path}")


if __name__ == "__main__":
    asyncio.run(main())