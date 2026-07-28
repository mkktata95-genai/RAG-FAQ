"""
content_tab_audit.py  v1.1.0
==============================
Scans all approved URLs and detects pages where PROSE CONTENT
is hidden behind content tabs (not contact-routing dropdowns,
not JS-paginated tables — purely tab-switched prose sections).

Examples:
  equity-release  → 4 tabs: Equity release explained | Interest roll-up |
                             Payment options | Long-term impacts

WHY THIS MATTERS
----------------
Current scraper captures only the DEFAULT/ACTIVE tab on page load.
All other tab content is silently missed — never scraped, never indexed,
never retrievable by ARIA.

WHAT THIS SCRIPT DETECTS
-------------------------
  - Pages with role="tablist" or .nav-tabs containing CONTENT (not nav)
  - Tab labels (so we know what content is hidden)
  - Estimated content size per hidden tab (BeautifulSoup on rendered HTML)
  - Default tab label (what IS currently captured)

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
v1.1.0 — Replaced JS injection + js_return_value with BeautifulSoup HTML
          parsing on result.html — works on all crawl4ai versions. Moved
          imports to top level for fast failure. Fixed CDP already-running
          handling.
v1.0.0 — Initial content tab auditor.
"""

import argparse
import asyncio
import csv
import json
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
        load_approved_pages,
    )
    _SCRAPER_OK = True
except ImportError as e:
    _SCRAPER_OK = False
    _IMPORT_ERR = str(e)

SEP = "=" * 70

_NAV_LABELS = {"home", "about", "contact", "menu", "next", "back", "more"}

# Cookie/consent banner label signals — skip any tablist with these labels
_COOKIE_LABEL_SIGNALS = {
    "your privacy", "strictly necessary", "strictly necessary cookies",
    "performance cookies", "functional cookies", "targeting cookies",
    "always active", "cookie", "consent",
}

# Cookie/consent parent class/id signals
_COOKIE_PARENT_SIGNALS = ("cookie", "consent", "privacy", "gdpr", "cmp", "onetrust")


def _is_cookie_tablist(tablist_el) -> bool:
    """Return True if tablist lives inside a cookie/consent banner."""
    parent = tablist_el
    for _ in range(6):
        parent = getattr(parent, "parent", None)
        if parent is None:
            break
        cls  = " ".join(parent.get("class", []) or []).lower()
        pid  = (parent.get("id") or "").lower()
        if any(sig in cls + " " + pid for sig in _COOKIE_PARENT_SIGNALS):
            return True
    return False


def _is_cookie_label(label: str) -> bool:
    """Return True if label is a cookie/consent label."""
    lower = label.lower()
    return any(sig in lower for sig in _COOKIE_LABEL_SIGNALS)


# ── BeautifulSoup tab detection (replaces JS injection) ───────────────────────

def _detect_tabs_from_html(html: str) -> dict:
    """
    Detect content tabs from rendered HTML using BeautifulSoup.
    Uses result.html which crawl4ai always populates — no js_return_value.
    Filters out cookie/consent banner tablists.
    """
    det = {
        "has_content_tabs":       False,
        "tab_count":              0,
        "tab_labels":             [],
        "default_tab_label":      "",
        "hidden_tab_count":       0,
        "estimated_hidden_chars": 0,
    }
    if not html:
        return det

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Find tab lists — exclude cookie/consent banner tablists
        all_tab_lists = (
            soup.find_all(attrs={"role": "tablist"}) +
            soup.find_all(class_=lambda c: c and "nav-tabs" in c)
        )
        content_tab_lists = [
            tl for tl in all_tab_lists
            if not _is_cookie_tablist(tl)
        ]
        if not content_tab_lists:
            return det

        # Collect tab items only from content tablists
        tab_items = []
        for tl in content_tab_lists:
            tab_items += (
                tl.find_all(attrs={"role": "tab"}) or
                tl.select(".nav-link") or
                tl.find_all("a")
            )

        # Filter labels — exclude cookie labels and nav labels
        labels = [
            t.get_text(strip=True) for t in tab_items
            if 5 < len(t.get_text(strip=True)) < 100
            and t.get_text(strip=True).lower() not in _NAV_LABELS
            and not _is_cookie_label(t.get_text(strip=True))
        ]
        if len(labels) < 2:
            return det

        # Default (active) tab
        active = (
            soup.find(attrs={"role": "tab", "aria-selected": "true"}) or
            soup.select_one(".nav-tabs .nav-link.active") or
            soup.select_one(".nav-link.active")
        )
        active_label = active.get_text(strip=True) if active else ""
        # Only use active label if it's not a cookie label
        det["default_tab_label"] = (
            active_label if active_label and not _is_cookie_label(active_label)
            else labels[0]
        )

        # Hidden tab panels + estimate content size
        hidden_panels = (
            soup.find_all(attrs={"role": "tabpanel", "hidden": True}) +
            soup.find_all(attrs={"role": "tabpanel", "aria-hidden": "true"}) +
            [p for p in soup.select(".tab-pane")
             if "active" not in (p.get("class") or [])]
        )
        seen, unique_hidden = set(), []
        for p in hidden_panels:
            if id(p) not in seen:
                seen.add(id(p))
                unique_hidden.append(p)

        hidden_chars = sum(len(p.get_text(strip=True)) for p in unique_hidden)

        det["has_content_tabs"]       = True
        det["tab_count"]              = len(labels)
        det["tab_labels"]             = labels[:8]
        det["hidden_tab_count"]       = len(unique_hidden)
        det["estimated_hidden_chars"] = hidden_chars

    except Exception:
        pass

    return det


# ── Scanner ────────────────────────────────────────────────────────────────────

async def scan_page(crawler, page_info: dict) -> dict:
    url   = page_info["url"]
    title = page_info.get("title", "")

    run_config = CrawlerRunConfig(
        wait_until="domcontentloaded",
        page_timeout=25000,
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

        html = getattr(result, "html", "") or ""
        det  = _detect_tabs_from_html(html)

        return {
            "url":                     url,
            "title":                   title,
            "has_content_tabs":        det["has_content_tabs"],
            "tab_count":               det["tab_count"],
            "tab_labels":              det["tab_labels"],
            "default_tab_label":       det["default_tab_label"],
            "hidden_tab_count":        det["hidden_tab_count"],
            "estimated_hidden_chars":  det["estimated_hidden_chars"],
        }

    except Exception as e:
        return {
            "url": url, "title": title,
            "has_content_tabs": False,
            "error": str(e),
        }


async def run_scan(pages: list) -> list:
    if not _SCRAPER_OK:
        print(f"[ERROR] Import failed: {_IMPORT_ERR}")
        sys.exit(1)

    print("Starting Chrome CDP ...")
    _start_chrome_cdp()  # no-op if already running
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
    tab_pages = [r for r in results if r.get("has_content_tabs")]
    clean     = [r for r in results if not r.get("has_content_tabs") and not r.get("error")]
    errors    = [r for r in results if r.get("error")]

    tab_pages_sorted = sorted(
        tab_pages,
        key=lambda x: x.get("estimated_hidden_chars", 0),
        reverse=True,
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
        print(f"  Sorted by hidden content size\n")
        print(f"  {'Hidden chars':>12}  {'Tabs':>4}  {'Default tab':<30}  URL")
        print(f"  {'-'*12}  {'-'*4}  {'-'*30}  {'-'*40}")
        for r in tab_pages_sorted:
            print(f"  {r.get('estimated_hidden_chars',0):>12,}  "
                  f"{r.get('tab_count',0):>4}  "
                  f"{r.get('default_tab_label','')[:28]:<30}  "
                  f"{r['url'][:60]}")

        print(f"\n  TAB LABELS per page:")
        for r in tab_pages_sorted[:10]:
            print(f"    {r['url'][:70]}")
            print(f"      Tabs: {' | '.join(r.get('tab_labels', []))}")

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for r in errors[:5]:
            print(f"    {r['url']} — {str(r.get('error',''))[:80]}")

    print(SEP)


def print_impact(tab_pages: list):
    total_hidden = sum(r.get("estimated_hidden_chars", 0) for r in tab_pages)
    print(f"\n{SEP}")
    print("  CURRENT ARIA IMPACT — WHAT IS MISSING FROM INDEX")
    print(SEP)
    print(f"  Pages affected              : {len(tab_pages)}")
    print(f"  Total hidden content (est.) : {total_hidden:,} chars")
    print(f"  Avg hidden per page         : "
          f"{total_hidden // max(1, len(tab_pages)):,} chars")
    print(f"\n  Scraper currently captures  : DEFAULT tab only")
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

    if not _SCRAPER_OK:
        print(f"[ERROR] Import failed: {_IMPORT_ERR}")
        sys.exit(1)

    print(f"\n{SEP}")
    print("  CONTENT TAB AUDIT")
    print(SEP)
    print(f"\nLoading approved URLs from {excel_path} ...")
    pages = load_approved_pages(str(excel_path))
    pages = [p for p in pages if not p.get("dropdown_state", "")]
    print(f"URLs to scan: {len(pages)}\n")

    results   = await run_scan(pages)
    tab_pages = [r for r in results if r.get("has_content_tabs")]

    print_report(results)
    print_impact(tab_pages)
    save_json(results, Path(args.out_json))
    save_csv(results,  Path(args.out_csv))
    print()


if __name__ == "__main__":
    asyncio.run(main())