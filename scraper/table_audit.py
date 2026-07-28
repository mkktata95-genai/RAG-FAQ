"""
table_audit.py  v1.0.0
=======================
Two-step table audit across all approved URLs.

STEP 1 — Content scan (from scraped JSON, no CDP, fast)
  Reads existing scraper JSON output. For every page:
  - Detects |pipe| markdown tables
  - Counts tables and total rows
  - Simulates flat chunker → detects mid-table splits
  - Classifies: NO_TABLE / SIMPLE_TABLE / SPLIT_TABLE

STEP 2 — JS interaction scan (CDP, targeted — table pages only)
  Runs only against pages flagged in Step 1 as having tables.
  Detects:
  - STATIC_TABLE    — plain HTML, crawl4ai converts correctly
  - JS_PAGINATED    — Bootstrap-Vue b-table, pagination clicks needed
  - TABBED_TABLE    — table hidden behind a content tab
  - JS_PAG_TABBED   — both pagination AND tabs (e.g. fund-prices)

OUTPUTS
-------
  Console    : step-by-step summary with counts
  table_audit_report.json  : full per-page detail
  table_audit_report.csv   : client-ready (one row per page with tables)

USAGE
-----
  # Step 1 only (no CDP needed):
  python table_audit.py --json scraper/data/<file>.json --step1-only

  # Full run (Step 1 + Step 2, Chrome CDP required):
  python table_audit.py --json scraper/data/<file>.json

  # Step 2 only (reuse Step 1 results):
  python table_audit.py --json scraper/data/<file>.json --step2-only --step1-report table_audit_step1.json

CHANGELOG
---------
v1.0.0 — Initial two-step table audit. Step 1: content scan from JSON.
          Step 2: JS interaction scan on table pages only. JSON + CSV output.
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Top-level imports (fail fast, not silently inside async) ──────────────────
try:
    from bs4 import BeautifulSoup
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
    from scraper.scrape_approved_urls_updatedV4 import (
        _make_browser_config,
        _start_chrome_cdp,
    )
    _SCRAPER_OK = True
except ImportError as e:
    _SCRAPER_OK = False
    _IMPORT_ERR = str(e)

# ── Constants ──────────────────────────────────────────────────────────────────

CHUNK_SIZE    = 1600
CHUNK_OVERLAP = 200
EFFECTIVE     = CHUNK_SIZE - CHUNK_OVERLAP
SEP  = "=" * 70
SEP2 = "-" * 50


# ── Shared: table detection (reused from phase0_scan.py) ─────────────────────

def parse_table_block(lines: list) -> dict:
    header_row    = None
    separator_row = None
    data_rows     = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.match(r'^[-:]+$', c) for c in cells if c):
            separator_row = cells
        elif header_row is None and separator_row is None:
            header_row = cells
        else:
            data_rows.append(cells)
    all_rows  = [r for r in [header_row, separator_row] + data_rows if r]
    col_count = max((len(r) for r in all_rows), default=0)
    return {
        "has_header":    header_row is not None,
        "has_separator": separator_row is not None,
        "col_count":     col_count,
        "data_rows":     len(data_rows),
        "total_lines":   len(lines),
        "header":        (header_row or [])[:4],  # first 4 col names
    }


def detect_tables(content: str) -> list:
    """Return list of table dicts found in markdown content."""
    lines   = content.splitlines()
    tables  = []
    i       = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            block = [lines[i]]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            parsed = parse_table_block(block)
            if parsed["data_rows"] > 0:  # skip empty/header-only tables
                tables.append({
                    "char_count": sum(len(l) for l in block),
                    **parsed,
                })
            continue
        i += 1
    return tables


def simulate_chunks(content: str) -> list:
    """Simulate flat RecursiveCharacterTextSplitter."""
    chunks = []
    start  = 0
    length = len(content)
    while start < length:
        end = min(start + CHUNK_SIZE, length)
        chunks.append((start, end))
        start += EFFECTIVE
    return chunks


def detect_splits(content: str, tables: list) -> int:
    """Count how many tables would be split mid-body by flat chunker."""
    if not tables:
        return 0
    chunks = simulate_chunks(content)
    lines  = content.splitlines(keepends=True)

    # Build char offsets per line
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    # For each table, find its char range
    splits = 0
    line_idx = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            # Start of a table block
            t_start = offsets[line_idx]
            j = line_idx
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            t_end = offsets[j - 1] + len(lines[j - 1]) if j > 0 else t_start
            # Check if any chunk boundary falls inside this table
            for cs, ce in chunks:
                if t_start < ce < t_end:
                    splits += 1
                    break
            line_idx = j
            continue
        line_idx += 1

    return splits


# ── STEP 1: Content scan ───────────────────────────────────────────────────────

def step1_content_scan(pages: list) -> list:
    """
    Scan all pages from scraped JSON.
    Returns per-page result with table classification.
    """
    results = []
    for page in pages:
        content = page.get("content", "")
        url     = page.get("url", "")
        title   = page.get("title", "")

        tables      = detect_tables(content)
        split_count = detect_splits(content, tables) if tables else 0
        total_rows  = sum(t["data_rows"] for t in tables)

        if not tables:
            classification = "NO_TABLE"
        elif split_count > 0:
            classification = "SPLIT_TABLE"
        else:
            classification = "SIMPLE_TABLE"

        results.append({
            "url":            url,
            "title":          title,
            "content_len":    len(content),
            "table_count":    len(tables),
            "total_rows":     total_rows,
            "split_count":    split_count,
            "classification": classification,
            "tables":         tables,
            # Step 2 fields — filled later
            "js_type":        None,
            "btable_pages":   0,
            "tab_count":      0,
            "tab_labels":     [],
        })

    return results


def print_step1(results: list):
    no_table   = [r for r in results if r["classification"] == "NO_TABLE"]
    simple     = [r for r in results if r["classification"] == "SIMPLE_TABLE"]
    split      = [r for r in results if r["classification"] == "SPLIT_TABLE"]
    table_pages = simple + split

    print(f"\n{SEP}")
    print("  STEP 1 — TABLE CONTENT SCAN (from scraped JSON)")
    print(SEP)
    print(f"  Total pages scanned     : {len(results)}")
    print(f"  Pages with NO tables    : {len(no_table)}")
    print(f"  Pages WITH tables       : {len(table_pages)}")
    print(f"  ├─ Simple tables        : {len(simple)}  (fit within chunk, no split)")
    print(f"  └─ Split tables         : {len(split)}   (mid-table split under flat chunker)")

    if split:
        print(f"\n  SPLIT TABLE pages ({len(split)}):")
        for r in sorted(split, key=lambda x: x["split_count"], reverse=True)[:15]:
            print(f"    [{r['table_count']} tables | {r['total_rows']:>4} rows | "
                  f"{r['split_count']} splits] {r['url']}")

    if simple:
        print(f"\n  SIMPLE TABLE pages (top 10 by row count):")
        for r in sorted(simple, key=lambda x: x["total_rows"], reverse=True)[:10]:
            print(f"    [{r['table_count']} tables | {r['total_rows']:>4} rows] {r['url']}")
    print()


# ── STEP 2: JS interaction scan ────────────────────────────────────────────────

_COOKIE_LABEL_SIGNALS = {
    "your privacy", "strictly necessary", "strictly necessary cookies",
    "performance cookies", "functional cookies", "targeting cookies",
    "always active", "cookie", "consent",
}
_COOKIE_PARENT_SIGNALS = ("cookie", "consent", "privacy", "gdpr", "cmp", "onetrust")


def _is_cookie_tablist(tablist_el) -> bool:
    parent = tablist_el
    for _ in range(6):
        parent = getattr(parent, "parent", None)
        if parent is None:
            break
        cls = " ".join(parent.get("class", []) or []).lower()
        pid = (parent.get("id") or "").lower()
        if any(sig in cls + " " + pid for sig in _COOKIE_PARENT_SIGNALS):
            return True
    return False


def _is_cookie_label(label: str) -> bool:
    lower = label.lower()
    return any(sig in lower for sig in _COOKIE_LABEL_SIGNALS)


def _detect_from_html(html: str) -> dict:
    """
    Detect JS-paginated tables and content tabs from rendered HTML.
    Uses BeautifulSoup — no js_return_value dependency.
    Filters cookie/consent banner tablists.
    """
    det = {
        "btable": False, "btable_pages": 0,
        "tabs": False, "tab_count": 0, "tab_labels": [],
    }
    if not html:
        return det

    try:
        soup = BeautifulSoup(html, "html.parser")

        # ── Bootstrap-Vue b-table (JS-paginated) ─────────────────────────────
        btables = (
            soup.find_all("table", attrs={"initialpagesize": True}) or
            soup.find_all("table", class_=lambda c: c and "b-table" in c)
        )
        if btables:
            det["btable"] = True
            page_btns = soup.find_all(
                lambda tag: tag.name in ("button", "li", "a")
                and tag.get_text(strip=True).isdigit()
                and int(tag.get_text(strip=True)) > 1
            )
            nums = [int(b.get_text(strip=True)) for b in page_btns]
            det["btable_pages"] = max(nums) if nums else 1

        # ── Content tabs — exclude cookie/consent banner tablists ─────────────
        all_tab_lists = (
            soup.find_all(attrs={"role": "tablist"}) +
            soup.find_all(class_=lambda c: c and "nav-tabs" in c)
        )
        content_tab_lists = [tl for tl in all_tab_lists if not _is_cookie_tablist(tl)]

        if content_tab_lists:
            tab_items = []
            for tl in content_tab_lists:
                tab_items += (
                    tl.find_all(attrs={"role": "tab"}) or
                    tl.select(".nav-link") or
                    tl.find_all("a")
                )
            labels = [
                t.get_text(strip=True) for t in tab_items
                if 5 < len(t.get_text(strip=True)) < 100
                and not _is_cookie_label(t.get_text(strip=True))
            ]
            if len(labels) >= 2:
                det["tabs"]       = True
                det["tab_count"]  = len(labels)
                det["tab_labels"] = labels[:8]

    except Exception:
        pass

    return det


async def step2_js_scan(table_pages: list) -> list:
    """
    Run detection only on pages flagged as having tables (from Step 1).
    Uses BeautifulSoup on crawl4ai's rendered HTML — no js_return_value.
    Returns updated results with js_type filled.
    """
    if not _SCRAPER_OK:
        print(f"[ERROR] Cannot run Step 2 — import failed: {_IMPORT_ERR}")
        for page in table_pages:
            page["js_type"] = "SCAN_ERROR"
        return table_pages

    print(f"  Starting Chrome CDP ...")
    _start_chrome_cdp()  # no-op if already running

    browser_config = _make_browser_config()
    total = len(table_pages)
    print(f"  Scanning {total} table pages via CDP ...\n")

    async with AsyncWebCrawler(config=browser_config) as crawler:
        for i, page in enumerate(table_pages, 1):
            print(f"  [{i:>3}/{total}] {page['url'][:75]}", end="\r")

            run_config = CrawlerRunConfig(
                wait_until="domcontentloaded",
                page_timeout=25000,
                verbose=False,
                excluded_tags=["script", "style"],
            )

            try:
                result = await crawler.arun(url=page["url"], config=run_config)
                if not result.success:
                    page["js_type"] = "SCAN_ERROR"
                    page["error"]   = result.error_message
                    continue

                # Use rendered HTML — available on all crawl4ai versions
                html = getattr(result, "html", "") or ""
                det  = _detect_from_html(html)

                btable = det["btable"]
                tabs   = det["tabs"]

                if btable and tabs:
                    js_type = "JS_PAG_TABBED"
                elif btable:
                    js_type = "JS_PAGINATED"
                elif tabs:
                    js_type = "TABBED_TABLE"
                else:
                    js_type = "STATIC_TABLE"

                page["js_type"]      = js_type
                page["btable_pages"] = det["btable_pages"]
                page["tab_count"]    = det["tab_count"]
                page["tab_labels"]   = det["tab_labels"]

            except Exception as e:
                page["js_type"] = "SCAN_ERROR"
                page["error"]   = str(e)

    print()
    return table_pages


def print_step2(table_pages: list):
    static   = [r for r in table_pages if r.get("js_type") == "STATIC_TABLE"]
    paginated = [r for r in table_pages if r.get("js_type") == "JS_PAGINATED"]
    tabbed   = [r for r in table_pages if r.get("js_type") == "TABBED_TABLE"]
    pag_tab  = [r for r in table_pages if r.get("js_type") == "JS_PAG_TABBED"]
    errors   = [r for r in table_pages if r.get("js_type") == "SCAN_ERROR"]

    print(f"\n{SEP}")
    print("  STEP 2 — JS INTERACTION SCAN (table pages only)")
    print(SEP)
    print(f"  Table pages scanned     : {len(table_pages)}")
    print(f"  Static HTML tables      : {len(static)}")
    print(f"  JS-paginated tables     : {len(paginated)}")
    print(f"  Tabbed tables           : {len(tabbed)}")
    print(f"  JS-paginated + tabbed   : {len(pag_tab)}")
    print(f"  Scan errors             : {len(errors)}")

    if paginated:
        print(f"\n  JS-PAGINATED ({len(paginated)}):")
        for r in paginated:
            print(f"    [~{r['btable_pages']} pages] {r['url']}")

    if tabbed:
        print(f"\n  TABBED TABLES ({len(tabbed)}):")
        for r in tabbed:
            tabs_str = " | ".join(r.get("tab_labels", []))
            print(f"    [{r['tab_count']} tabs: {tabs_str[:60]}] {r['url']}")

    if pag_tab:
        print(f"\n  JS-PAGINATED + TABBED ({len(pag_tab)}):")
        for r in pag_tab:
            tabs_str = " | ".join(r.get("tab_labels", []))
            print(f"    [~{r['btable_pages']} pages | {r['tab_count']} tabs: "
                  f"{tabs_str[:40]}] {r['url']}")
    print()


# ── Final summary ──────────────────────────────────────────────────────────────

def print_final_summary(results: list, step2_done: bool):
    no_table  = [r for r in results if r["classification"] == "NO_TABLE"]
    simple    = [r for r in results if r["classification"] == "SIMPLE_TABLE"]
    split     = [r for r in results if r["classification"] == "SPLIT_TABLE"]
    static    = [r for r in results if r.get("js_type") == "STATIC_TABLE"]
    paginated = [r for r in results if r.get("js_type") == "JS_PAGINATED"]
    tabbed    = [r for r in results if r.get("js_type") == "TABBED_TABLE"]
    pag_tab   = [r for r in results if r.get("js_type") == "JS_PAG_TABBED"]

    print(f"\n{SEP}")
    print("  FINAL SUMMARY — CLIENT REPORT")
    print(SEP)
    print(f"  Total pages             : {len(results)}")
    print(f"  ├─ No tables            : {len(no_table)}")
    print(f"  └─ Has tables           : {len(results) - len(no_table)}")
    print(f"     ├─ Simple (fits chunk): {len(simple)}")
    print(f"     └─ Split under chunker: {len(split)}")

    if step2_done:
        print(f"        ├─ Static HTML   : {len(static)}")
        print(f"        ├─ JS-paginated  : {len(paginated)}")
        print(f"        ├─ Tabbed        : {len(tabbed)}")
        print(f"        └─ Pag + Tabbed  : {len(pag_tab)}")

    print(SEP)


# ── Save outputs ───────────────────────────────────────────────────────────────

def save_json(results: list, path: Path):
    # Remove verbose tables field for cleaner output
    clean = []
    for r in results:
        c = {k: v for k, v in r.items() if k != "tables"}
        clean.append(c)
    path.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    print(f"  JSON → {path}")


def save_csv(results: list, path: Path):
    table_pages = [r for r in results if r["classification"] != "NO_TABLE"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "URL", "Title", "Content Classification",
            "Table Count", "Total Rows", "Split Count",
            "JS Type", "B-Table Pages", "Tab Count", "Tab Labels"
        ])
        for r in table_pages:
            writer.writerow([
                r["url"],
                r.get("title", ""),
                r["classification"],
                r["table_count"],
                r["total_rows"],
                r["split_count"],
                r.get("js_type", "NOT_SCANNED"),
                r.get("btable_pages", ""),
                r.get("tab_count", ""),
                " | ".join(r.get("tab_labels", [])),
            ])
    print(f"  CSV → {path} ({len(table_pages)} rows)")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Two-step table audit")
    parser.add_argument("--json",        required=True, help="Scraped JSON file")
    parser.add_argument("--step1-only",  action="store_true",
                        help="Run Step 1 content scan only (no CDP needed)")
    parser.add_argument("--step2-only",  action="store_true",
                        help="Run Step 2 JS scan only (requires --step1-report)")
    parser.add_argument("--step1-report", default=None,
                        help="Existing Step 1 JSON to reuse for Step 2")
    parser.add_argument("--out-json", default="table_audit_report.json")
    parser.add_argument("--out-csv",  default="table_audit_report.csv")
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"[ERROR] {json_path} not found")
        sys.exit(1)

    print(f"\n{SEP}")
    print("  TABLE AUDIT — Two-step scan")
    print(SEP)

    # ── Step 1 ────────────────────────────────────────────────────────────────
    if not args.step2_only:
        print(f"\nLoading {json_path} ...")
        data  = json.loads(json_path.read_text(encoding="utf-8"))
        pages = [d for d in data if not d.get("dropdown_state", "")]
        print(f"Pages loaded: {len(pages)}\n")

        print("Running Step 1 — content scan ...")
        results = step1_content_scan(pages)
        print_step1(results)

        # Save Step 1 intermediate
        step1_path = Path("table_audit_step1.json")
        step1_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"  Step 1 results saved → {step1_path}")

    else:
        # Load existing Step 1 results
        if not args.step1_report:
            print("[ERROR] --step2-only requires --step1-report")
            sys.exit(1)
        results = json.loads(Path(args.step1_report).read_text(encoding="utf-8"))
        print(f"  Loaded Step 1 results: {len(results)} pages")

    if args.step1_only:
        save_json(results, Path(args.out_json))
        save_csv(results,  Path(args.out_csv))
        print_final_summary(results, step2_done=False)
        return

    # ── Step 2 ────────────────────────────────────────────────────────────────
    table_pages = [r for r in results if r["classification"] != "NO_TABLE"]
    print(f"\nRunning Step 2 — JS scan on {len(table_pages)} table pages ...")

    table_pages = await step2_js_scan(table_pages)
    print_step2(table_pages)

    # Merge Step 2 results back into full results
    url_map = {r["url"]: r for r in table_pages}
    for r in results:
        if r["url"] in url_map:
            r.update(url_map[r["url"]])

    save_json(results, Path(args.out_json))
    save_csv(results,  Path(args.out_csv))
    print_final_summary(results, step2_done=True)


if __name__ == "__main__":
    asyncio.run(main())