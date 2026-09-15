"""
debug_two_blank_pages.py

Diagnostic for two pages that visually have a banner image, use the known
id="featurebanner*" pattern, but returned blank from the batch extraction.
Prints every intermediate step so we can see exactly where it breaks,
rather than guessing from screenshots.

Run this on VDI (has real network access to royallondon.com).
"""

import re
import requests

URLS = [
    "https://www.royallondon.com/existing-customers/help-and-support/update-your-details/",
    "https://www.royallondon.com/pensions/investment-options/governed-range/lifestyle-strategies/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"
}

_BANNER_ANCHOR_RE = re.compile(
    r'id=["\'](?:feature|hero)banner(?:block)?\d+["\']', re.IGNORECASE
)
_DECLARATION_RE = re.compile(r'background-image:\s*([^;]+);', re.IGNORECASE)
_URL_RE = re.compile(
    r'url\(\s*[\'"]?([^\'")]+\.(?:jpg|jpeg|png|webp))[\'"]?\s*\)',
    re.IGNORECASE,
)
# ALSO try a version with NO extension requirement, to see if that's the gap
_URL_RE_NO_EXT = re.compile(r'url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)', re.IGNORECASE)


def debug_page(url):
    print("=" * 90)
    print(f"URL: {url}")
    print("=" * 90)

    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    html = resp.text
    print(f"HTML length: {len(html)} chars")

    anchor = _BANNER_ANCHOR_RE.search(html)
    if not anchor:
        print("!! No id=\"featurebanner*\"/\"herobannerblock*\" anchor found at all.")
        return
    print(f"Anchor found: '{anchor.group()}' at position {anchor.start()}")

    start = anchor.end()
    next_container = html.find('<div class="container', start + 100)
    end = next_container if next_container != -1 else start + 8000
    window = html[start:end]
    print(f"Window size: {len(window)} chars (bounded by next 'container' div: {next_container != -1})")

    # show every background-image declaration found, with and without ext requirement
    print("\n--- background-image declarations found in window ---")
    decl_count = 0
    for decl_match in _DECLARATION_RE.finditer(window):
        decl_count += 1
        declaration = decl_match.group(1)
        with_ext = _URL_RE.findall(declaration)
        without_ext = _URL_RE_NO_EXT.findall(declaration)
        print(f"\n  Declaration #{decl_count}:")
        print(f"    raw: {declaration[:200]}")
        print(f"    url() matches WITH required extension: {with_ext}")
        print(f"    url() matches WITHOUT extension requirement: {without_ext}")

    if decl_count == 0:
        print("  !! No 'background-image: ...;' declarations found in window at all.")
        print("  First 500 chars of window for inspection:")
        print(window[:500])


if __name__ == "__main__":
    for url in URLS:
        debug_page(url)
        print()