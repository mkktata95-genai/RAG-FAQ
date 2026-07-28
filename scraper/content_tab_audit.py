"""
content_tab_audit.py  v1.0.0
==============================
Scans all approved URLs and detects pages where PROSE CONTENT
is hidden behind content tabs (not contact-routing dropdowns,
not JS-paginated tables — purely tab-switched prose sections).

Examples:
  equity-release  → 4 tabs: Equity release explained | Interest roll-up |
                             Payment options | Long-term impacts
  webinar pages   → may have transcript tabs

WHY THIS MATTERS
----------------
Current scraper captures only the DEFAULT/ACTIVE tab on page load.
All other tab content is silently missed — never scraped, never indexed,
never retrievable by ARIA.

WHAT THIS SCRIPT DETECTS
-------------------------
  - Pages with role="tablist" or .nav-tabs containing CONTENT (not nav)
  - Tab labels (so we know what content is hidden)
  - Estimated content size per hidden tab (JS injection)
  - Whether default tab is the only content captured currently

WHAT THIS SCRIPT DOES NOT FLAG
-------------------------------
  - Contact-routing dropdowns (handled by scraper already)
  - JS-paginated tables (handled by table_audit.py)
  - Main site navigation tabs (header nav)

OUTPUTS
-------
  Console   : summary + per-page breakdown
  content_tab_audit_report.json  : full detail
  content_tab_audit_report.csv   : client-ready

USAGE
-----
  # Chrome CDP required
  python content_tab_audit.py --file scraper/data/Approved_URLs.xlsx

CHANGELOG
---------
v1.0.0 — Initial content tab auditor. Detects prose-content tabs,
          estimates hidden content size, JSON + CSV output.
"""

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SEP  = "=" * 70
SEP2 = "-" * 50

# ── JS detection ───────────────────────────────────────────────────────────────

_JS_TAB_DETECT = """
() => {
    const result = {
        has_content_tabs: false,
        tab_count: 0,
        tab_labels: [],
        hidden_tab_count: 0,
        default_tab_label: '',
        estimated_hidden_chars: 0,
        evidence: ''
    };

    // Find all tab lists
    const tabLists = document.querySelectorAll(
        '[role="tablist"], .nav-tabs, ul.tabs'
    );
    if (tabLists.length === 0) return result;

    // Collect all tab items
    const tabItems = document.querySelectorAll(
        '[role="tab"], .nav-tabs .nav-link, .nav-tabs li a'
    );
    if (tabItems.length === 0) return result;

    const labels = Array.from(tabItems)
        .map(t => t.textContent.trim())
        .filter(t => t.length > 0 && t.length < 100);

    // Filter out navigation-style tabs (too short/generic or all-caps nav items)
    // Content tabs have descriptive labels like "Equity release explained"
    const contentLike = labels.filter(l =>
        l.length > 5 &&
        !['home','about','contact','menu','next','back','more'].includes(l.toLowerCase())
    );

    if (contentLike.length < 2) return result;

    // Find active (default) tab
    const activeTab = document.querySelector(
        '[role="tab"][aria-selected="true"], .nav-tabs .nav-link.active, .nav-link.active'
    );
    result.default_tab_label = activeTab
        ? activeTab.textContent.trim()
        : (contentLike[0] || '');

    // Find hidden tab panels — content not visible on page load
    const hiddenPanels = document.querySelectorAll(
        '[role="tabpanel"][hidden], [role="tabpanel"][aria-hidden="true"], ' +
        '.tab-pane:not(.active), .tab-content .tab-pane[style*="display: none"]'
    );

    // Estimate chars in hidden panels
    let hiddenChars = 0;
    hiddenPanels.forEach(panel => {
        hiddenChars += (panel.textContent || '').trim().length;
    });

    result.has_content_tabs    = true;
    result.tab_count           = contentLike.length;
    result.tab_labels          = contentLike.slice(0, 8);
    result.hidden_tab_count    = hiddenPanels.length;
    result.estimated_hidden_chars = hiddenChars;
    result.evidence            = `${contentLike.length} content tabs found`;

    return result;
}
"""


# ── Scanner ────────────────────────────────────────────────────────────────────

async def scan_page(crawler, page_info: dict) -> dict:
    from crawl4ai import CrawlerRunConfig

    url   = page_info["url"]
    title = page_info.get("title", "")

    run_config = CrawlerRunConfig(
        wait_until="domcontentloaded",
        page_timeout=25000,
        js_code=_JS_TAB_DETECT,
        verbose=False,
        excluded_tags=["script", "style"],
    )

    try:
        result = await crawler.arun(url=url, config=run_config)
        if not result.success:
            return {
                "url": url, "title": title,
                "has_content_tabs": False,
                "error": result.error_message,
            }

        raw = (getattr(result, "js_return_value", None) or
               getattr(result, "extracted_content", None))

        if isinstance(raw, str):
            try:
                det = json.loads(raw)
            except Exception:
                det = {}
        elif isinstance(raw, dict):
            det = raw
        else:
            det = {}

        return {
            "url":                     url,
            "title":                   title,
            "has_content_tabs":        det.get("has_content_tabs", False),
            "tab_count":               det.get("tab_count", 0),
            "tab_labels":              det.get("tab_labels", []),
            "default_tab_label":       det.get("default_tab_label", ""),
            "hidden_tab_count":        det.get("hidden_tab_count", 0),
            "estimated_hidden_chars":  det.get("estimated_hidden_chars", 0),
            "evidence":                det.get("evidence", ""),
        }

    except Exception as e:
        return {
            "url": url, "title": title,
            "has_content_tabs": False,
            "error": str(e),
        }


async def run_scan(pages: list) -> list:
    try:
        from scraper.scrape_approved_urls_updatedV4 import (
            _make_browser_config,
            _start_chrome_cdp,
        )
        from crawl4ai import AsyncWebCrawler
    except ImportError as e:
        print(f"[ERROR] Cannot import scraper: {e}")
        sys.exit(1)

    print("Starting Chrome CDP ...")
    _start_chrome_cdp()
    browser_config = _make_browser_config()

    results = []
    total   = len(pages)

    async with AsyncWebCrawler(config=browser_config) as crawler:
        for i, page in enumerate(pages, 1):
            print(f"  [{i:>3}/{total}] {page['url'][:75]}", end="\r")
            r = await scan_page(crawler, page)
            results.append(r)

    print()
    return results


# ── Reporters ──────────────────────────────────────────────────────────────────

def print_report(results: list):
    tab_pages  = [r for r in results if r.get("has_content_tabs")]
    clean      = [r for r in results if not r.get("has_content_tabs")
                  and not r.get("error")]
    errors     = [r for r in results if r.get("error")]

    # Sort by hidden content size
    tab_pages_sorted = sorted(
        tab_pages,
        key=lambda x: x.get("estimated_hidden_chars", 0),
        reverse=True
    )

    print(f"\n{SEP}")
    print("  CONTENT TAB AUDIT — RESULTS")
    print(SEP)
    print(f"  Total URLs scanned          : {len(results)}")
    print(f"  Pages with content tabs     : {len(tab_pages)}")
    print(f"  Standard pages (no tabs)    : {len(clean)}")
    print(f"  Scan errors                 : {len(errors)}")

    if tab_pages_sorted:
        print(f"\n  PAGES WITH CONTENT TABS ({len(tab_pages_sorted)})")
        print(f"  (sorted by hidden content size)\n")
        print(f"  {'Hidden chars':>12}  {'Tabs':>4}  {'Default tab':<30}  URL")
        print(f"  {'-'*12}  {'-'*4}  {'-'*30}  {'-'*40}")
        for r in tab_pages_sorted:
            hidden  = r.get("estimated_hidden_chars", 0)
            n_tabs  = r.get("tab_count", 0)
            default = r.get("default_tab_label", "")[:28]
            url     = r["url"][:60]
            print(f"  {hidden:>12,}  {n_tabs:>4}  {default:<30}  {url}")

        print(f"\n  TAB LABELS per page:")
        for r in tab_pages_sorted[:10]:
            labels = " | ".join(r.get("tab_labels", []))
            print(f"    {r['url'][:60]}")
            print(f"      Tabs: {labels}")

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for r in errors[:5]:
            print(f"    {r['url']} — {str(r.get('error',''))[:80]}")

    print(SEP)


def print_impact(tab_pages: list):
    """Show what ARIA is currently missing."""
    total_hidden = sum(r.get("estimated_hidden_chars", 0) for r in tab_pages)
    print(f"\n{SEP}")
    print("  CURRENT ARIA IMPACT — WHAT IS MISSING FROM INDEX")
    print(SEP)
    print(f"  Pages affected              : {len(tab_pages)}")
    print(f"  Total hidden content (est.) : {total_hidden:,} chars")
    print(f"  Avg hidden per page         : "
          f"{total_hidden // max(1, len(tab_pages)):,} chars")
    print(f"\n  Scraper currently captures  : DEFAULT tab only per page")
    print(f"  Content missed              : all non-default tabs")
    print(f"  Fix required                : click each tab, scrape, merge content")
    print(SEP)


def save_json(results: list, path: Path):
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"  JSON → {path}")


def save_csv(results: list, path: Path):
    tab_pages = [r for r in results if r.get("has_content_tabs")]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "URL", "Title", "Tab Count", "Default Tab",
            "Hidden Tabs", "Est. Hidden Chars", "All Tab Labels"
        ])
        for r in tab_pages:
            writer.writerow([
                r["url"],
                r.get("title", ""),
                r.get("tab_count", ""),
                r.get("default_tab_label", ""),
                r.get("hidden_tab_count", ""),
                r.get("estimated_hidden_chars", ""),
                " | ".join(r.get("tab_labels", [])),
            ])
    print(f"  CSV → {path} ({len(tab_pages)} rows)")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Content tab audit")
    parser.add_argument("--file",     required=True, help="Approved_URLs.xlsx path")
    parser.add_argument("--out-json", default="content_tab_audit_report.json")
    parser.add_argument("--out-csv",  default="content_tab_audit_report.csv")
    args = parser.parse_args()

    excel_path = Path(args.file)
    if not excel_path.exists():
        print(f"[ERROR] {excel_path} not found")
        sys.exit(1)

    print(f"\n{SEP}")
    print("  CONTENT TAB AUDIT")
    print(SEP)

    try:
        from scraper.scrape_approved_urls_updatedV4 import load_approved_pages
    except ImportError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    print(f"\nLoading approved URLs from {excel_path} ...")
    pages = load_approved_pages(str(excel_path))
    pages = [p for p in pages if not p.get("dropdown_state", "")]
    print(f"URLs to scan: {len(pages)}")
    print("\nScanning (Chrome CDP required) ...\n")

    results   = await run_scan(pages)
    tab_pages = [r for r in results if r.get("has_content_tabs")]

    print_report(results)
    print_impact(tab_pages)
    save_json(results, Path(args.out_json))
    save_csv(results,  Path(args.out_csv))
    print()


if __name__ == "__main__":
    asyncio.run(main())