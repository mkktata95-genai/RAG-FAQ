"""
test_scrape_fund_prices.py  v1.0.0
===================================
Diagnostic: proves current scraper + flat chunker limitations on
the JS-paginated Bootstrap-Vue fund-prices page.

Two checks — fully automatic, no manual review:

  CHECK A — SCRAPER: How many fund rows were captured?
    - Counts |pipe| table rows in scraped markdown
    - Compares against expected 40+ rows (4 pages × 10 rows)
    - PASS: all rows present  |  FAIL: only 10 rows (page 1 only)

  CHECK B — CHUNKER: Do any table rows get split across chunks?
    - Simulates flat RecursiveCharacterTextSplitter (CHUNK_SIZE=1600)
    - Tracks which chunk each table row lands in
    - Detects rows where the row START and row END are in different chunks
    - Prints exact split evidence: row content + chunk numbers

PREREQUISITES
-------------
  Chrome running with CDP:
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    --remote-debugging-port=9222 --headless=new

USAGE
-----
  python test_scrape_fund_prices.py

OUTPUT
------
  Console: CHECK A + CHECK B results
  test_fund_prices_raw.md  — raw scraped markdown (inspect manually if needed)
  test_fund_prices_chunks.txt — all chunks with boundaries marked

CHANGELOG
---------
v1.0.0 — Initial diagnostic. Scraper row count check + automatic
          chunk split detection with evidence printing.
"""

import asyncio
import os
import re
import sys
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

FUND_PRICES_URL = "https://www.royallondon.com/pensions/investment-options/fund-prices/"
EXPECTED_MIN_ROWS = 30   # conservative — real page has 40+ rows across 4 pages
CHUNK_SIZE    = 1600
CHUNK_OVERLAP = 200
EFFECTIVE     = CHUNK_SIZE - CHUNK_OVERLAP

RAW_MD_OUT    = Path("test_fund_prices_raw.md")
CHUNKS_OUT    = Path("test_fund_prices_chunks.txt")

SEP  = "=" * 70
SEP2 = "-" * 50


# ── Import scraper internals ───────────────────────────────────────────────────

# Add project root to path so we can import the scraper
sys.path.insert(0, str(Path(__file__).parent))

try:
    from scraper.scrape_approved_urls_updatedV4 import (
        _make_browser_config,
        clean_content,
        _ensure_chrome_cdp,
    )
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
    from crawl4ai.content_filter_strategy import PruningContentFilter
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    SCRAPER_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Could not import scraper internals: {e}")
    print("       Running chunker-only test on existing raw MD if present.")
    SCRAPER_AVAILABLE = False


# ── CHECK A: Scraper ───────────────────────────────────────────────────────────

async def scrape_fund_prices() -> str | None:
    """Scrape fund-prices using same config as production scraper."""
    if not SCRAPER_AVAILABLE:
        return None

    print(f"\nStarting Chrome CDP ...")
    _ensure_chrome_cdp()

    browser_config = _make_browser_config()

    run_config = CrawlerRunConfig(
        css_selector=(
            "main, article, .content, #content, "
            ".page-content, .main-content, [role='main']"
        ),
        markdown_generator=DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(
                threshold=0.45,
                threshold_type="fixed",
            ),
            options={
                "ignore_links": False,
                "ignore_images": True,
                "skip_internal_links": True,
            }
        ),
        wait_until="domcontentloaded",
        page_timeout=30000,
        verbose=False,
        excluded_tags=["nav", "header", "footer", "aside", "script", "style", "noscript"],
    )

    print(f"Scraping: {FUND_PRICES_URL}")
    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=FUND_PRICES_URL, config=run_config)

    if not result.success:
        print(f"[ERROR] Scrape failed: {result.error_message}")
        return None

    content = clean_content(result.markdown.raw_markdown)
    print(f"Raw content length: {len(content):,} chars")
    return content


def check_a_scraper(content: str) -> dict:
    """Count table rows and detect pagination completeness."""
    print(f"\n{SEP}")
    print("  CHECK A — SCRAPER: Fund rows captured")
    print(SEP)

    lines = content.splitlines()

    # Find all pipe-table rows (exclude header/separator)
    table_rows = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # Skip separator rows (---|---) and header rows (Fund name|SEDOL...)
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.match(r'^[-:]+$', c) for c in cells if c):
            continue  # separator
        # Skip header row
        if any(kw in stripped.lower() for kw in ["fund name", "sedol", "bid price", "offer price"]):
            continue
        # Data row: should contain price pattern (digits + p)
        if re.search(r'\d+\.\d+p', stripped):
            table_rows.append((i + 1, stripped))

    row_count = len(table_rows)

    print(f"  Table data rows found : {row_count}")
    print(f"  Expected minimum      : {EXPECTED_MIN_ROWS}")

    if row_count == 0:
        print("\n  ✗  FAIL — No table rows found at all")
        print("     crawl4ai may not have rendered the b-table component")
    elif row_count <= 10:
        print(f"\n  ✗  FAIL — Only {row_count} rows captured")
        print("     Pagination NOT handled — page 1 only (10 rows/page)")
        print("     Pages 2-4 silently missing")
    elif row_count < EXPECTED_MIN_ROWS:
        print(f"\n  ⚠  PARTIAL — {row_count} rows captured (expected {EXPECTED_MIN_ROWS}+)")
        print("     Some pagination pages may be missing")
    else:
        print(f"\n  ✓  PASS — {row_count} rows captured (all pages)")

    if table_rows:
        print(f"\n  First 3 rows captured:")
        for lineno, row in table_rows[:3]:
            print(f"     L{lineno}: {row[:100]}")
        if len(table_rows) > 3:
            print(f"  Last 2 rows captured:")
            for lineno, row in table_rows[-2:]:
                print(f"     L{lineno}: {row[:100]}")

    return {
        "row_count":        row_count,
        "pass":             row_count >= EXPECTED_MIN_ROWS,
        "pagination_issue": row_count <= 10,
        "rows":             table_rows,
    }


# ── CHECK B: Chunker ───────────────────────────────────────────────────────────

def simulate_chunks(content: str) -> list[tuple[int, int, str]]:
    """
    Simulate flat RecursiveCharacterTextSplitter.
    Returns list of (start_char, end_char, chunk_text).
    """
    chunks = []
    start  = 0
    length = len(content)
    while start < length:
        end  = min(start + CHUNK_SIZE, length)
        chunks.append((start, end, content[start:end]))
        start += EFFECTIVE
    return chunks


def check_b_chunker(content: str) -> dict:
    """Detect mid-table-row splits across chunk boundaries."""
    print(f"\n{SEP}")
    print("  CHECK B — CHUNKER: Table row split detection")
    print(SEP)

    lines   = content.splitlines(keepends=True)
    chunks  = simulate_chunks(content)

    # Build line → char offset map
    line_offsets = []
    pos = 0
    for line in lines:
        line_offsets.append(pos)
        pos += len(line)

    # Find table data rows with their char offsets
    data_rows = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.match(r'^[-:]+$', c) for c in cells if c):
            continue
        if any(kw in stripped.lower() for kw in ["fund name", "sedol", "bid price"]):
            continue
        if re.search(r'\d+\.\d+p', stripped):
            row_start = line_offsets[i]
            row_end   = row_start + len(line)
            data_rows.append({
                "line_no":   i + 1,
                "row_start": row_start,
                "row_end":   row_end,
                "content":   stripped[:100],
            })

    if not data_rows:
        print("  No table data rows found — cannot check splits")
        return {"splits": [], "split_count": 0}

    # Assign each row to a chunk
    def find_chunk(char_pos: int) -> int:
        for idx, (cs, ce, _) in enumerate(chunks):
            if cs <= char_pos < ce:
                return idx
        return len(chunks) - 1

    splits = []
    for row in data_rows:
        chunk_of_start = find_chunk(row["row_start"])
        chunk_of_end   = find_chunk(row["row_end"] - 1)
        if chunk_of_start != chunk_of_end:
            splits.append({
                "line_no":      row["line_no"],
                "content":      row["content"],
                "start_chunk":  chunk_of_start + 1,
                "end_chunk":    chunk_of_end + 1,
            })

    # Also detect table BLOCKS split (row in chunk N but next row in chunk N+2+)
    prev_chunk = None
    block_splits = []
    for row in data_rows:
        c = find_chunk(row["row_start"])
        if prev_chunk is not None and c > prev_chunk + 1:
            block_splits.append({
                "gap":       c - prev_chunk,
                "line_no":   row["line_no"],
                "content":   row["content"],
            })
        prev_chunk = c

    print(f"  Total data rows checked : {len(data_rows)}")
    print(f"  Total chunks produced   : {len(chunks)}")
    print(f"  Avg chars per chunk     : {len(content) // max(1, len(chunks)):,}")

    if splits:
        print(f"\n  ✗  FAIL — {len(splits)} row(s) split mid-row across chunk boundary:")
        for s in splits:
            print(f"\n     Line {s['line_no']}: {s['content']}")
            print(f"     → Row START in chunk {s['start_chunk']}, "
                  f"END in chunk {s['end_chunk']}  ← SPLIT")
    else:
        print("\n  ✓  No mid-row splits detected")

    if block_splits:
        print(f"\n  ⚠  {len(block_splits)} table BLOCK discontinuities "
              f"(rows separated by non-table chunk content):")
        for b in block_splits[:5]:
            print(f"     Line {b['line_no']}: {b['content'][:80]}")
            print(f"     → Gap of {b['gap']} chunks between consecutive rows")
    else:
        print("  ✓  No block-level table discontinuities")

    return {
        "total_rows":    len(data_rows),
        "total_chunks":  len(chunks),
        "split_count":   len(splits),
        "splits":        splits,
        "block_splits":  block_splits,
    }


# ── Save outputs ───────────────────────────────────────────────────────────────

def save_outputs(content: str, chunks: list):
    RAW_MD_OUT.write_text(content, encoding="utf-8")
    print(f"\n  Raw markdown → {RAW_MD_OUT}")

    lines_out = []
    for i, (cs, ce, text) in enumerate(chunks):
        lines_out.append(f"{'─' * 60}")
        lines_out.append(f"CHUNK {i + 1}  [chars {cs}–{ce}]")
        lines_out.append(f"{'─' * 60}")
        lines_out.append(text)
        lines_out.append("")
    CHUNKS_OUT.write_text("\n".join(lines_out), encoding="utf-8")
    print(f"  Chunk breakdown → {CHUNKS_OUT}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print(SEP)
    print("  FUND-PRICES DIAGNOSTIC — Scraper + Chunker Test")
    print(f"  URL: {FUND_PRICES_URL}")
    print(SEP)

    # Try to scrape fresh, else use cached raw MD if present
    content = None
    if SCRAPER_AVAILABLE:
        content = await scrape_fund_prices()
    
    if not content and RAW_MD_OUT.exists():
        print(f"\n[INFO] Using cached: {RAW_MD_OUT}")
        content = RAW_MD_OUT.read_text(encoding="utf-8")

    if not content:
        print("\n[ERROR] No content available — run with Chrome CDP active")
        sys.exit(1)

    # Run checks
    result_a = check_a_scraper(content)
    result_b = check_b_chunker(content)

    # Save files
    chunks = simulate_chunks(content)
    save_outputs(content, chunks)

    # Final verdict
    print(f"\n{SEP}")
    print("  OVERALL VERDICT")
    print(SEP)
    issues = []
    if result_a["pagination_issue"]:
        issues.append("✗  Scraper: pagination NOT handled — page 1 only")
    elif not result_a["pass"]:
        issues.append(f"✗  Scraper: partial rows only ({result_a['row_count']} < {EXPECTED_MIN_ROWS})")
    if result_b["split_count"] > 0:
        issues.append(f"✗  Chunker: {result_b['split_count']} table row(s) split mid-row")
    if result_b.get("block_splits"):
        issues.append(f"✗  Chunker: {len(result_b['block_splits'])} block discontinuities")

    if not issues:
        print("  ✓  All checks passed (unexpected — re-verify manually)")
    else:
        print("  PROBLEMS CONFIRMED:")
        for i in issues:
            print(f"    {i}")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())