"""
check_js_dependency.py  (v2 — adds hidden-panel dropdown detection)

Reads a list of URLs from an Excel file, fetches each page's RAW HTML
(no browser, no JavaScript execution), and reports two independent things:

  1. Whether the page's real content likely needs JavaScript to render at all
     (same check as v1 — visible word count + SPA framework markers).

  2. Whether any dropdown on the page uses the "hidden panel" pattern
     confirmed manually on the bereavement page — i.e. ALL dropdown option
     content already sits in the raw HTML as hidden divs (commonly marked
     with data-content=, class="tab-panel", role="tabpanel", or
     aria-hidden="true"), toggled by CSS/ARIA rather than fetched via JS.

     If this pattern is found, and the number of <option> values roughly
     matches the number of hidden content panels, that's strong evidence
     the dropdown's data can be scraped by parsing raw HTML directly —
     no Playwright click-through required for that page.

THIS IS STILL A HEURISTIC — always spot-check a sample manually
(view-source in a browser) before treating results as final, especially
any row flagged "Yes" for JS but "No" for hidden-panel-pattern (that
combination means Playwright is likely still needed for that page).

SETUP (run once):
    pip install pandas openpyxl requests beautifulsoup4

USAGE:
    python check_js_dependency.py --input urls.xlsx --url-column URL

    --input        Path to the input Excel file
    --url-column   Name of the column containing URLs (default: "URL")
    --output       Path to write the output Excel file (default: <input>_checked.xlsx)
    --sheet        Sheet name to read, if not the first sheet (optional)
    --delay        Seconds to wait between requests (default: 1.0)
    --min-words    Visible-word-count threshold below which a page is flagged JS-likely (default: 60)
"""

import argparse
import re
import sys
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

SPA_MARKERS = [
    r'id=["\']app["\']',
    r'id=["\']root["\']',
    r'<app-root',
    r'ng-version=',
    r'data-reactroot',
    r'__NEXT_DATA__',
    r'data-server-rendered=["\']false["\']',
    r'ng-app',
]

# Markers of the "hidden panel" dropdown pattern confirmed manually on the
# bereavement page: content for every dropdown option already sits in the
# raw HTML, toggled visible/hidden via CSS/ARIA rather than fetched by JS.
HIDDEN_PANEL_MARKERS = [
    r'data-content=',
    r'tab-panel',
    r'role=["\']tabpanel["\']',
    r'aria-hidden=["\']true["\']',
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def get_visible_text_word_count(soup: BeautifulSoup) -> int:
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return 0
    return len(text.split(" "))


def detect_markers(raw_html: str, patterns: list) -> list:
    found = []
    for pattern in patterns:
        if re.search(pattern, raw_html, flags=re.IGNORECASE):
            found.append(pattern)
    return found


def analyze_dropdowns(soup: BeautifulSoup) -> dict:
    """
    Look for <select> dropdowns and count their <option> values, then look
    for elements that carry a data-content attribute (the hidden-panel
    pattern confirmed on the bereavement page). Compare counts as a rough
    signal of whether every dropdown option has a matching hidden panel.
    """
    result = {
        "Dropdown_Found": "No",
        "Option_Count": 0,
        "Hidden_Panel_Count": 0,
        "Options_Panels_Match": "N/A",
    }

    selects = soup.find_all("select")
    if not selects:
        return result

    result["Dropdown_Found"] = "Yes"

    option_count = 0
    for select in selects:
        for option in select.find_all("option"):
            value = option.get("value", "").strip()
            if value:
                option_count += 1
    result["Option_Count"] = option_count

    panels = soup.find_all(attrs={"data-content": True})
    result["Hidden_Panel_Count"] = len(panels)

    if option_count == 0:
        result["Options_Panels_Match"] = "N/A"
    elif len(panels) == 0:
        result["Options_Panels_Match"] = "No"
    elif abs(len(panels) - option_count) <= 1:
        result["Options_Panels_Match"] = "Yes"
    else:
        result["Options_Panels_Match"] = "Partial"

    return result


def check_url(url: str, min_words: int, timeout: int = 15) -> dict:
    result = {
        "JS_Likely_Required": "Unknown",
        "Visible_Word_Count": None,
        "SPA_Markers_Found": "",
        "Dropdown_Found": "No",
        "Option_Count": 0,
        "Hidden_Panel_Count": 0,
        "Hidden_Panel_Pattern_Found": "No",
        "Options_Panels_Match": "N/A",
        "HTTP_Status": None,
        "Error": "",
    }

    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        result["HTTP_Status"] = resp.status_code

        if resp.status_code != 200:
            result["Error"] = f"Non-200 status: {resp.status_code}"
            return result

        soup = BeautifulSoup(resp.text, "html.parser")

        word_count = get_visible_text_word_count(soup)
        spa_markers = detect_markers(resp.text, SPA_MARKERS)
        result["Visible_Word_Count"] = word_count
        result["SPA_Markers_Found"] = ", ".join(spa_markers) if spa_markers else ""
        result["JS_Likely_Required"] = "Yes" if (spa_markers or word_count < min_words) else "No"

        # Fresh soup for dropdown analysis, since the word-count step above
        # strips <script>/<style> tags in place on the first soup object.
        soup_for_panels = BeautifulSoup(resp.text, "html.parser")
        panel_markers = detect_markers(resp.text, HIDDEN_PANEL_MARKERS)
        dropdown_info = analyze_dropdowns(soup_for_panels)

        result["Dropdown_Found"] = dropdown_info["Dropdown_Found"]
        result["Option_Count"] = dropdown_info["Option_Count"]
        result["Hidden_Panel_Count"] = dropdown_info["Hidden_Panel_Count"]
        result["Options_Panels_Match"] = dropdown_info["Options_Panels_Match"]
        result["Hidden_Panel_Pattern_Found"] = "Yes" if panel_markers else "No"

    except requests.exceptions.RequestException as e:
        result["Error"] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description="Check JS dependency and dropdown hidden-panel pattern for URLs in an Excel file.")
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

    columns = {
        "JS_Likely_Required": [],
        "Visible_Word_Count": [],
        "SPA_Markers_Found": [],
        "Dropdown_Found": [],
        "Option_Count": [],
        "Hidden_Panel_Count": [],
        "Hidden_Panel_Pattern_Found": [],
        "Options_Panels_Match": [],
        "HTTP_Status": [],
        "Error": [],
    }

    numeric_or_none_cols = {"Visible_Word_Count", "Option_Count", "Hidden_Panel_Count", "HTTP_Status"}

    for i, url in enumerate(df[args.url_column], start=1):
        if pd.isna(url) or not str(url).strip():
            for key in columns:
                if key == "JS_Likely_Required":
                    columns[key].append("Unknown")
                elif key == "Error":
                    columns[key].append("Empty URL")
                elif key in numeric_or_none_cols:
                    columns[key].append(None)
                else:
                    columns[key].append("")
            continue

        url = str(url).strip()
        print(f"[{i}/{total}] {url}")
        result = check_url(url, min_words=args.min_words)

        for key in columns:
            columns[key].append(result[key])

        time.sleep(args.delay)

    for key, values in columns.items():
        df[key] = values

    df.to_excel(output_path, index=False)

    js_yes = columns["JS_Likely_Required"].count("Yes")
    js_no = columns["JS_Likely_Required"].count("No")
    dropdown_pages = columns["Dropdown_Found"].count("Yes")
    hidden_pattern_yes = columns["Hidden_Panel_Pattern_Found"].count("Yes")
    full_match = columns["Options_Panels_Match"].count("Yes")
    partial_match = columns["Options_Panels_Match"].count("Partial")
    no_match = columns["Options_Panels_Match"].count("No")

    print("\n--- Summary ---")
    print(f"JS likely required:             Yes={js_yes}, No={js_no}")
    print(f"Pages with a <select> dropdown: {dropdown_pages}")
    print(f"Pages with hidden-panel markers: {hidden_pattern_yes}")
    print(f"Options-vs-panels match:        Full={full_match}, Partial={partial_match}, No match={no_match}")
    print(f"\nOutput written to: {output_path}")
    print("\nHow to read the two new checks together, per row:")
    print("  Dropdown_Found=Yes + Options_Panels_Match=Yes")
    print("    -> Strong evidence this dropdown's data can be parsed from raw HTML,")
    print("       no Playwright click-through needed for this page.")
    print("  Dropdown_Found=Yes + Options_Panels_Match=No or Partial")
    print("    -> Some/all option content is NOT sitting in the raw HTML —")
    print("       Playwright is likely still needed for this specific page.")
    print("       Spot-check these manually before concluding either way.")
    print("\nAs before: this is a heuristic. Spot-check a handful of results")
    print("manually (View Page Source in a browser) before treating this as final.")


if __name__ == "__main__":
    main()