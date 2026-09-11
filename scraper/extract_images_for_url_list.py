"""
extract_images_for_url_list.py

Reads URLs from an Excel file, extracts each page's citation thumbnail
image (base_image_url) using the finalized logic, and writes the result
into a new column immediately next to the URL column. Leaves the cell
blank if no image was found or the page failed to fetch.

Uses the FINALIZED extraction approach (validated on 7 sample URLs):
  - og:image is NOT used (confirmed always generic on this site).
  - CSS feature-banner background-image is the source.
  - Isolates the full "background-image: ...;" declaration and takes the
    LAST url(...) in it (handles linear-gradient overlays with nested
    rgba() parens correctly).
  - Prefers medium-1000x570 tier, falls back through small/large.
  - Resolves relative paths with urljoin (not string concatenation).

CONFIGURE these before running:
    INPUT_FILE     - path to your Excel file
    SHEET_NAME     - sheet name, or None for the active sheet
    URL_COLUMN     - the exact header text of the column containing URLs

Output: writes a new file "<original_name>_with_images.xlsx" next to the
input file. Does not modify the original file.

Usage:
    python extract_images_for_url_list.py
"""

import re
import time
from urllib.parse import urljoin
from typing import Optional

import requests
import openpyxl

# ---------------------------------------------------------------------------
# CONFIGURE
# ---------------------------------------------------------------------------
INPUT_FILE = "Approved_URLS_140826_checked.xlsx"
SHEET_NAME = None          # e.g. "Sheet1", or None for the active sheet
URL_COLUMN = "URL"         # exact header text of the column with page URLs
NEW_COLUMN_HEADER = "base_image_url"
REQUEST_TIMEOUT = 15
DELAY_BETWEEN_REQUESTS = 0.5   # seconds, be polite to the site
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"
}

_DECLARATION_RE = re.compile(r'background-image:\s*([^;]+);', re.IGNORECASE)
_URL_RE = re.compile(
    r'url\(\s*[\'"]?([^\'")]+\.(?:jpg|jpeg|png|webp))[\'"]?\s*\)',
    re.IGNORECASE,
)
SIZE_TIER_PREFERENCE = ["medium-1000x570", "small-800x800", "large-1200x700"]


def extract_base_image_url(html: str, page_url: str) -> Optional[str]:
    matches = []
    seen = set()
    for decl_match in _DECLARATION_RE.finditer(html):
        declaration = decl_match.group(1)
        urls_in_declaration = _URL_RE.findall(declaration)
        if not urls_in_declaration:
            continue
        rel_path = urls_in_declaration[-1]
        if rel_path not in seen:
            seen.add(rel_path)
            matches.append(rel_path)

    if not matches:
        return None

    chosen = None
    for tier in SIZE_TIER_PREFERENCE:
        for rel_path in matches:
            if tier in rel_path:
                chosen = rel_path
                break
        if chosen:
            break
    if not chosen:
        chosen = matches[0]

    return urljoin(page_url, chosen)


def fetch_image_for_url(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"    ERROR fetching {url}: {e}")
        return None
    return extract_base_image_url(resp.text, url)


def main():
    wb = openpyxl.load_workbook(INPUT_FILE)
    ws = wb[SHEET_NAME] if SHEET_NAME else wb.active

    # find URL column and header row
    header_row = 1
    headers = {}
    for cell in ws[header_row]:
        if cell.value:
            headers[str(cell.value).strip()] = cell.column

    if URL_COLUMN not in headers:
        print(f"Column '{URL_COLUMN}' not found. Available columns: {list(headers.keys())}")
        return

    url_col_idx = headers[URL_COLUMN]

    # insert the new column right after the URL column
    new_col_idx = url_col_idx + 1
    ws.insert_cols(new_col_idx)
    ws.cell(row=header_row, column=new_col_idx, value=NEW_COLUMN_HEADER)

    total_rows = ws.max_row - header_row
    found_count = 0

    for row_idx in range(header_row + 1, ws.max_row + 1):
        url_cell = ws.cell(row=row_idx, column=url_col_idx)
        url = url_cell.value
        if not url or not str(url).strip().startswith("http"):
            continue

        url = str(url).strip()
        print(f"[{row_idx - header_row}/{total_rows}] {url}")

        image_url = fetch_image_for_url(url)
        if image_url:
            found_count += 1
            print(f"    -> {image_url}")
        else:
            print(f"    -> (none found)")

        ws.cell(row=row_idx, column=new_col_idx, value=image_url or "")

        time.sleep(DELAY_BETWEEN_REQUESTS)

    output_file = INPUT_FILE.rsplit(".", 1)[0] + "_with_images.xlsx"
    wb.save(output_file)

    print("\n" + "=" * 70)
    print(f"Done. {found_count}/{total_rows} URLs had an image found.")
    print(f"Saved to: {output_file}")
    print("=" * 70)


if __name__ == "__main__":
    main()