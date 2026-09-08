"""
check_js_dependency.py

Reads a list of URLs from an Excel file, fetches each page's RAW HTML
(no browser, no JavaScript execution), and flags whether the page's real
content appears to depend on JavaScript to render.

WHY THIS APPROACH:
This does NOT use Playwright/a real browser. It fetches the page the way a
lightweight HTTP scraper (requests + BeautifulSoup) would see it, and checks
two signals:
  1. How much visible text is actually present in the raw HTML.
  2. Whether common SPA framework markers are present (Angular, React,
     Next.js, Vue, empty root/app divs, etc.)

If a page has very little visible text AND/OR shows SPA markers, it likely
needs JavaScript to render its real content -> flagged "Yes".
If a page has substantial visible text and no SPA markers -> likely static
/ server-rendered -> flagged "No".

THIS IS A HEURISTIC, NOT A GUARANTEE.
- False "No" is unlikely (if there's a lot of real text in raw HTML, JS
  probably isn't required for that content).
- False "Yes" is possible on pages with unusually little text for
  legitimate reasons (e.g. a very short page).
- ALWAYS spot-check a handful of borderline/"Yes" results manually
  (view-source in a browser) before treating this as final.

SETUP (run once):
    pip install pandas openpyxl requests beautifulsoup4

USAGE:
    python check_js_dependency.py --input urls.xlsx --url-column URL --output urls_checked.xlsx

    --input        Path to the input Excel file
    --url-column   Name of the column containing URLs (default: "URL")
    --output       Path to write the output Excel file (default: <input>_checked.xlsx)
    --sheet        Sheet name to read, if not the first sheet (optional)
    --delay        Seconds to wait between requests (default: 1.0) — be polite to the target site
    --min-words    Visible-word-count threshold below which a page is flagged JS-likely (default: 60)
"""

import argparse
import re
import sys
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

# Markers commonly left in raw HTML by JS-framework apps even before
# JavaScript has run — these are strong signals regardless of word count.
SPA_MARKERS = [
    r'id=["\']app["\']',
    r'id=["\']root["\']',
    r'<app-root',                 # Angular
    r'ng-version=',               # Angular
    r'data-reactroot',            # older React
    r'__NEXT_DATA__',             # Next.js
    r'data-server-rendered=["\']false["\']',  # Vue (explicit CSR flag)
    r'ng-app',                    # AngularJS
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def get_visible_text_word_count(soup: BeautifulSoup) -> int:
    """Strip script/style/noscript tags and count remaining visible words."""
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return 0
    return len(text.split(" "))


def detect_spa_markers(raw_html: str) -> list:
    found = []
    for pattern in SPA_MARKERS:
        if re.search(pattern, raw_html, flags=re.IGNORECASE):
            found.append(pattern)
    return found


def check_url(url: str, min_words: int, timeout: int = 15) -> dict:
    """Fetch a single URL's raw HTML and return the analysis result."""
    result = {
        "JS_Likely_Required": "Unknown",
        "Visible_Word_Count": None,
        "SPA_Markers_Found": "",
        "HTTP_Status": None,
        "Error": "",
    }

    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        result["HTTP_Status"] = resp.status_code

        if resp.status_code != 200:
            result["JS_Likely_Required"] = "Unknown"
            result["Error"] = f"Non-200 status: {resp.status_code}"
            return result

        soup = BeautifulSoup(resp.text, "html.parser")
        word_count = get_visible_text_word_count(soup)
        markers = detect_spa_markers(resp.text)

        result["Visible_Word_Count"] = word_count
        result["SPA_Markers_Found"] = ", ".join(markers) if markers else ""

        if markers or word_count < min_words:
            result["JS_Likely_Required"] = "Yes"
        else:
            result["JS_Likely_Required"] = "No"

    except requests.exceptions.RequestException as e:
        result["Error"] = str(e)
        result["JS_Likely_Required"] = "Unknown"

    return result


def main():
    parser = argparse.ArgumentParser(description="Check whether URLs in an Excel file need JS to render.")
    parser.add_argument("--input", required=True, help="Input Excel file path")
    parser.add_argument("--url-column", default="URL", help="Column name containing URLs")
    parser.add_argument("--output", default=None, help="Output Excel file path")
    parser.add_argument("--sheet", default=0, help="Sheet name or index to read (default: first sheet)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay in seconds between requests")
    parser.add_argument("--min-words", type=int, default=60, help="Word-count threshold for flagging JS-likely")
    args = parser.parse_args()

    output_path = args.output or args.input.rsplit(".", 1)[0] + "_checked.xlsx"

    print(f"Reading: {args.input}")
    df = pd.read_excel(args.input, sheet_name=args.sheet)

    if args.url_column not in df.columns:
        print(f"ERROR: Column '{args.url_column}' not found. Available columns: {list(df.columns)}")
        sys.exit(1)

    total = len(df)
    print(f"Found {total} rows. Checking each URL (this will take a while — {args.delay}s delay between requests)...\n")

    js_flags, word_counts, markers_col, statuses, errors = [], [], [], [], []

    for i, url in enumerate(df[args.url_column], start=1):
        if pd.isna(url) or not str(url).strip():
            js_flags.append("Unknown")
            word_counts.append(None)
            markers_col.append("")
            statuses.append(None)
            errors.append("Empty URL")
            continue

        url = str(url).strip()
        print(f"[{i}/{total}] {url}")
        result = check_url(url, min_words=args.min_words)

        js_flags.append(result["JS_Likely_Required"])
        word_counts.append(result["Visible_Word_Count"])
        markers_col.append(result["SPA_Markers_Found"])
        statuses.append(result["HTTP_Status"])
        errors.append(result["Error"])

        time.sleep(args.delay)

    df["JS_Likely_Required"] = js_flags
    df["Visible_Word_Count"] = word_counts
    df["SPA_Markers_Found"] = markers_col
    df["HTTP_Status"] = statuses
    df["Error"] = errors

    df.to_excel(output_path, index=False)

    yes_count = js_flags.count("Yes")
    no_count = js_flags.count("No")
    unknown_count = js_flags.count("Unknown")

    print("\n--- Summary ---")
    print(f"JS likely required (Yes): {yes_count}")
    print(f"Likely static (No):       {no_count}")
    print(f"Unknown/error:            {unknown_count}")
    print(f"\nOutput written to: {output_path}")
    print("\nReminder: this is a heuristic. Spot-check a few 'Yes' and a few borderline")
    print("'No' results manually (right-click > View Page Source in a browser) before")
    print("treating this as final evidence.")


if __name__ == "__main__":
    main()