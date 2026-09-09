"""
check_js_dependency.py  (v3 — adds value-level dropdown/panel matching + detail sheet)

Reads a list of URLs from an Excel file, fetches each page's RAW HTML
(no browser, no JavaScript execution), and reports:

  1. Whether the page's real content likely needs JavaScript to render at all
     (visible word count + SPA framework markers).

  2. Whether the page has the "hidden panel" dropdown pattern confirmed
     manually on the bereavement and online-services-by-product pages:
     every <option value="X"> in a <select> has a matching
     <div data-content="X" ...> already present in the raw HTML, just
     hidden via CSS/ARIA (role="tabpanel", aria-hidden="true") rather
     than fetched by JavaScript.

     v3 upgrade over v2: instead of just comparing COUNTS of options vs
     panels, this version matches each option's VALUE directly against
     each panel's data-content VALUE, and reports exactly which options
     (if any) have no matching panel. This is a much stronger check than
     a count comparison, since counts could match by coincidence while
     specific options are still missing content.

  3. A separate "Dropdown Detail" sheet in the output workbook, one row
     per <option> found on any dropdown page, showing: the option's
     label and value, whether a matching data-content panel was found,
     and how much text that panel contains (so you can visually confirm
     real content is present, not an empty shell).

THIS IS STILL A HEURISTIC for JS-dependency and SPA detection — but the
option-to-panel matching is a direct structural check, not a heuristic:
if this script says an option has NO matching panel, that is a fact you
can verify immediately in view-source, not a guess.

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

OUTPUT:
    Writes an Excel workbook with two sheets:
      "Summary"          - one row per URL (same as v2, plus a stronger
                            All_Options_Matched column)
      "Dropdown Detail"   - one row per dropdown option found, across all
                            URLs, for manual/automated review
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


def get_panel_text(panel_div) -> str:
    """Visible text inside a data-content panel, collapsed to one line."""
    text = panel_div.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def match_options_to_panels(soup: BeautifulSoup, url: str) -> dict:
    """
    For each <option value="X" ...> inside any <select> on the page, look
    for a <... data-content="X" ...> element elsewhere on the page. Value
    comparison is case-insensitive and whitespace-trimmed, since HTML
    authors are not always consistent about exact casing/spacing.

    Returns a summary dict plus a list of per-option detail rows.
    """
    selects = soup.find_all("select")
    detail_rows = []

    if not selects:
        return {
            "Dropdown_Found": "No",
            "Option_Count": 0,
            "Matched_Count": 0,
            "Unmatched_Count": 0,
            "All_Options_Matched": "N/A",
            "Unmatched_Options": "",
            "detail_rows": detail_rows,
        }

    # Build a lookup of data-content value (normalised) -> panel element(s)
    panels = soup.find_all(attrs={"data-content": True})
    panel_lookup = {}
    for panel in panels:
        key = panel.get("data-content", "").strip().lower()
        panel_lookup.setdefault(key, []).append(panel)

    matched_count = 0
    unmatched_count = 0
    unmatched_labels = []

    for select in selects:
        for option in select.find_all("option"):
            value = option.get("value", "").strip()
            label = option.get_text(strip=True)
            if not value:
                continue  # skip the empty "Select..." placeholder option

            key = value.lower()
            matching_panels = panel_lookup.get(key, [])

            if matching_panels:
                matched_count += 1
                panel_text = get_panel_text(matching_panels[0])
                detail_rows.append({
                    "URL": url,
                    "Option_Value": value,
                    "Option_Label": label,
                    "Matching_Panel_Found": "Yes",
                    "Panel_Text_Length": len(panel_text),
                    "Panel_Text_Preview": panel_text[:150],
                })
            else:
                unmatched_count += 1
                unmatched_labels.append(label or value)
                detail_rows.append({
                    "URL": url,
                    "Option_Value": value,
                    "Option_Label": label,
                    "Matching_Panel_Found": "No",
                    "Panel_Text_Length": 0,
                    "Panel_Text_Preview": "",
                })

    option_count = matched_count + unmatched_count

    if option_count == 0:
        all_matched = "N/A"
    elif unmatched_count == 0:
        all_matched = "Yes"
    elif matched_count == 0:
        all_matched = "No"
    else:
        all_matched = "Partial"

    return {
        "Dropdown_Found": "Yes",
        "Option_Count": option_count,
        "Matched_Count": matched_count,
        "Unmatched_Count": unmatched_count,
        "All_Options_Matched": all_matched,
        "Unmatched_Options": "; ".join(unmatched_labels),
        "detail_rows": detail_rows,
    }


def check_url(url: str, min_words: int, timeout: int = 15) -> dict:
    result = {
        "JS_Likely_Required": "Unknown",
        "Visible_Word_Count": None,
        "SPA_Markers_Found": "",
        "Dropdown_Found": "No",
        "Option_Count": 0,
        "Matched_Count": 0,
        "Unmatched_Count": 0,
        "All_Options_Matched": "N/A",
        "Unmatched_Options": "",
        "Hidden_Panel_Pattern_Found": "No",
        "HTTP_Status": None,
        "Error": "",
        "detail_rows": [],
    }

    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        result["HTTP_Status"] = resp.status_code

        if resp.status_code != 200:
            result["Error"] = f"Non-200 status: {resp.status_code}"
            return result

        raw_html = resp.text

        # --- JS-dependency check ---
        soup_words = BeautifulSoup(raw_html, "html.parser")
        word_count = get_visible_text_word_count(soup_words)
        spa_markers = detect_markers(raw_html, SPA_MARKERS)
        result["Visible_Word_Count"] = word_count
        result["SPA_Markers_Found"] = ", ".join(spa_markers) if spa_markers else ""
        result["JS_Likely_Required"] = "Yes" if (spa_markers or word_count < min_words) else "No"

        # --- Hidden-panel dropdown check (value-level matching) ---
        panel_markers = detect_markers(raw_html, HIDDEN_PANEL_MARKERS)
        result["Hidden_Panel_Pattern_Found"] = "Yes" if panel_markers else "No"

        soup_for_match = BeautifulSoup(raw_html, "html.parser")
        match_result = match_options_to_panels(soup_for_match, url)

        result["Dropdown_Found"] = match_result["Dropdown_Found"]
        result["Option_Count"] = match_result["Option_Count"]
        result["Matched_Count"] = match_result["Matched_Count"]
        result["Unmatched_Count"] = match_result["Unmatched_Count"]
        result["All_Options_Matched"] = match_result["All_Options_Matched"]
        result["Unmatched_Options"] = match_result["Unmatched_Options"]
        result["detail_rows"] = match_result["detail_rows"]

    except requests.exceptions.RequestException as e:
        result["Error"] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description="Check JS dependency and value-level dropdown/panel matching for URLs in an Excel file.")
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

    summary_cols = {
        "JS_Likely_Required": [],
        "Visible_Word_Count": [],
        "SPA_Markers_Found": [],
        "Dropdown_Found": [],
        "Option_Count": [],
        "Matched_Count": [],
        "Unmatched_Count": [],
        "All_Options_Matched": [],
        "Unmatched_Options": [],
        "Hidden_Panel_Pattern_Found": [],
        "HTTP_Status": [],
        "Error": [],
    }
    numeric_or_none_cols = {"Visible_Word_Count", "Option_Count", "Matched_Count", "Unmatched_Count", "HTTP_Status"}

    all_detail_rows = []

    for i, url in enumerate(df[args.url_column], start=1):
        if pd.isna(url) or not str(url).strip():
            for key in summary_cols:
                if key == "JS_Likely_Required":
                    summary_cols[key].append("Unknown")
                elif key == "Error":
                    summary_cols[key].append("Empty URL")
                elif key in numeric_or_none_cols:
                    summary_cols[key].append(None)
                else:
                    summary_cols[key].append("")
            continue

        url = str(url).strip()
        print(f"[{i}/{total}] {url}")
        result = check_url(url, min_words=args.min_words)

        for key in summary_cols:
            summary_cols[key].append(result[key])

        all_detail_rows.extend(result["detail_rows"])

        time.sleep(args.delay)

    for key, values in summary_cols.items():
        df[key] = values

    detail_df = pd.DataFrame(all_detail_rows) if all_detail_rows else pd.DataFrame(
        columns=["URL", "Option_Value", "Option_Label", "Matching_Panel_Found", "Panel_Text_Length", "Panel_Text_Preview"]
    )

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Summary", index=False)
        detail_df.to_excel(writer, sheet_name="Dropdown Detail", index=False)

    js_yes = summary_cols["JS_Likely_Required"].count("Yes")
    js_no = summary_cols["JS_Likely_Required"].count("No")
    dropdown_pages = summary_cols["Dropdown_Found"].count("Yes")
    full_match = summary_cols["All_Options_Matched"].count("Yes")
    partial_match = summary_cols["All_Options_Matched"].count("Partial")
    no_match = summary_cols["All_Options_Matched"].count("No")

    print("\n--- Summary ---")
    print(f"JS likely required:              Yes={js_yes}, No={js_no}")
    print(f"Pages with a <select> dropdown:  {dropdown_pages}")
    print(f"All options matched to a panel:  Full={full_match}, Partial={partial_match}, No match={no_match}")
    print(f"Total dropdown options found across all pages: {len(all_detail_rows)}")
    print(f"\nOutput written to: {output_path}")
    print('  -> "Summary" sheet: one row per URL')
    print('  -> "Dropdown Detail" sheet: one row per dropdown option, with')
    print('     Matching_Panel_Found=No flagging any option whose content')
    print('     is NOT sitting in the raw HTML — check these manually, since')
    print('     that specific option may still need Playwright to retrieve.')
    print("\nAs before: the JS-dependency check is a heuristic. The option-to-panel")
    print("matching is a direct structural check — trust 'No' matches as real findings,")
    print("and verify a sample in view-source before treating results as final.")


if __name__ == "__main__":
    main()