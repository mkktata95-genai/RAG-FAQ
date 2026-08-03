"""
verify_tab_headers_in_base_scrape.py  v1.0.0
================================================
Checks whether the STANDARD (non-Playwright) scrape already captures
all tab content as clean markdown headers — i.e. whether the
crawler.arun() pass alone is sufficient, making Playwright tab-click
scraping unnecessary.

For each of the 13 known tab pages (from content_tab_audit_report.csv):
  1. Load its base content from the scraped JSON
  2. Check for `### <tab_label>` headers matching every known tab label
  3. Report: all labels found as headers? any missing? any duplicated
     content suggesting contamination beyond simple section headers?

If ALL 13 pages show clean header-per-tab-label patterns, this
confirms tab_content_scraper.py's Playwright click approach is
unnecessary — the element-aware chunker can treat these as ordinary
section headers, same as accordion header+body chunking.

USAGE
-----
  python verify_tab_headers_in_base_scrape.py \\
      --json scraper/data/<latest>.json \\
      --tab-report content_tab_audit_report.csv

CHANGELOG
---------
v1.0.0 — Initial verification script.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

SEP = "=" * 70


def load_tab_pages_from_csv(csv_path: Path) -> list:
    """Load the 13 known tab pages + their expected labels."""
    pages = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            labels_raw = row.get("All Tab Labels", "")
            labels = [l.strip() for l in labels_raw.split("|") if l.strip()]
            pages.append({
                "url":    row.get("URL", "").strip(),
                "labels": labels,
            })
    return pages


def find_matching_scraped_page(url: str, scraped_pages: list) -> dict:
    """Match a tab-audit URL to its entry in the scraped JSON."""
    url_clean = url.rstrip("/")
    for page in scraped_pages:
        page_url = page.get("url", "").rstrip("/")
        if page_url == url_clean:
            return page
    return None


def check_headers_in_content(content: str, labels: list) -> dict:
    """
    Check if each expected tab label appears as a markdown header
    (### Label or ## Label) in the content, in order.
    """
    result = {
        "labels_expected":     len(labels),
        "labels_found_as_header": [],
        "labels_missing":      [],
        "header_positions":    {},
    }

    for label in labels:
        # Match ### Label, ## Label, or **Label** at line start
        # (mirrors the \n###  Engage\n**Aiming... pattern observed)
        pattern = re.compile(
            r'(?:^|\n)\s*#{1,4}\s*' + re.escape(label) + r'\s*(?:\n|$)',
            re.IGNORECASE
        )
        match = pattern.search(content)

        if not match:
            # Fallback: bold-style **Label**
            pattern2 = re.compile(
                r'(?:^|\n)\s*\*\*' + re.escape(label) + r'\*\*',
                re.IGNORECASE
            )
            match = pattern2.search(content)

        if match:
            result["labels_found_as_header"].append(label)
            result["header_positions"][label] = match.start()
        else:
            result["labels_missing"].append(label)

    return result


def main():
    parser = argparse.ArgumentParser(description="Verify tab header pattern in base scrape")
    parser.add_argument("--json", required=True, help="Scraped JSON file")
    parser.add_argument("--tab-report", default="content_tab_audit_report.csv",
                        help="content_tab_audit_report.csv path")
    args = parser.parse_args()

    json_path = Path(args.json)
    csv_path  = Path(args.tab_report)

    if not json_path.exists():
        print(f"[ERROR] {json_path} not found")
        sys.exit(1)
    if not csv_path.exists():
        print(f"[ERROR] {csv_path} not found")
        sys.exit(1)

    print(f"\n{SEP}")
    print("  VERIFY TAB HEADERS IN BASE SCRAPE")
    print(SEP)

    print(f"\nLoading scraped JSON: {json_path} ...")
    scraped_data = json.loads(json_path.read_text(encoding="utf-8"))
    scraped_pages = [d for d in scraped_data if not d.get("dropdown_state", "")]
    print(f"Pages loaded: {len(scraped_pages)}")

    print(f"Loading tab report: {csv_path} ...")
    tab_pages = load_tab_pages_from_csv(csv_path)
    print(f"Tab pages to verify: {len(tab_pages)}\n")

    all_clean    = []
    has_issues   = []
    not_found    = []

    for tp in tab_pages:
        url    = tp["url"]
        labels = tp["labels"]

        scraped = find_matching_scraped_page(url, scraped_pages)
        if scraped is None:
            not_found.append(url)
            continue

        content = scraped.get("content", "")
        check   = check_headers_in_content(content, labels)

        if not check["labels_missing"]:
            all_clean.append({
                "url": url,
                "labels": labels,
                "content_len": len(content),
                "positions": check["header_positions"],
            })
        else:
            has_issues.append({
                "url": url,
                "expected": labels,
                "found": check["labels_found_as_header"],
                "missing": check["labels_missing"],
                "content_len": len(content),
            })

    # ── Report ───────────────────────────────────────────────────────────
    print(f"{SEP}")
    print("  RESULTS")
    print(SEP)
    print(f"  Total tab pages checked : {len(tab_pages)}")
    print(f"  Clean (all headers found): {len(all_clean)}")
    print(f"  Issues (missing headers) : {len(has_issues)}")
    print(f"  Not found in scraped JSON: {len(not_found)}")

    if all_clean:
        print(f"\n  ✓ CLEAN PAGES ({len(all_clean)}):")
        for p in all_clean:
            print(f"    [{p['content_len']:>6} chars, {len(p['labels'])} labels] {p['url']}")

    if has_issues:
        print(f"\n  ✗ ISSUES ({len(has_issues)}):")
        for p in has_issues:
            print(f"    {p['url']}")
            print(f"      expected: {p['expected']}")
            print(f"      found:    {p['found']}")
            print(f"      missing:  {p['missing']}")

    if not_found:
        print(f"\n  ⚠ NOT FOUND IN SCRAPED JSON ({len(not_found)}):")
        for u in not_found:
            print(f"    {u}")

    print(f"\n{SEP}")
    print("  VERDICT")
    print(SEP)
    if not has_issues and not not_found:
        print("  ✓ ALL 13 pages show clean header-per-tab pattern.")
        print("    Playwright tab-click scraping is NOT necessary.")
        print("    Element-aware chunker can treat ### <label> as section headers.")
    else:
        print(f"  ✗ {len(has_issues) + len(not_found)} page(s) need investigation")
        print("    before retiring the Playwright tab-click approach.")
    print(SEP)


if __name__ == "__main__":
    main()