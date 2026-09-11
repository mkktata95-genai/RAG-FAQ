"""
check_citation_images.py

Diagnostic / spot-check script — NOT part of the production pipeline.

Purpose: check a handful of approved URLs to see:
  1. Does og:image exist? Is it page-specific or the same site logo everywhere?
  2. Does a CSS feature-banner background-image exist as a fallback?
  3. Is either image reused across multiple URLs (weak as a citation thumbnail)?

Run this against 5-10 sample URLs (mix of page templates: About-us,
product/pension page, FAQ page, insurance page) to decide whether og:image
or the CSS banner-image should be the PRIMARY source before building the
real extraction logic into scrape_approved_urls_updatedV5.py.

Usage:
    python check_citation_images.py urls.txt
    (one URL per line)

    or edit SAMPLE_URLS below and run with no args.
"""

import sys
import re
import requests
from collections import defaultdict

SAMPLE_URLS = [
    "https://www.royallondon.com/about-us/how-we-are-run/governance-and-leadership-teams/group-executive-committee/",
    "https://www.royallondon.com/about-us/our-purpose/social-impact/changemakers/meet-the-changemakers/",
    "https://www.royallondon.com/existing-customers/help-and-support/find-a-lost-pension/",
    "https://www.royallondon.com/existing-customers/help-and-support/update-your-details/pension-plan/",
    "https://www.royallondon.com/pensions/",
    "https://www.royallondon.com/retirement-planning/retirement-guidance/in-retirement/",
    "https://www.royallondon.com/pensions/investment-options/fund-prices/rlmis-climate-reporting/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"
}

OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# OLD (buggy) regex — kept only so this script can report which pages it
# was silently missing. [^)]* stops at the FIRST ")" it sees, which is the
# closing paren of a nested rgba(...) inside a linear-gradient(...) overlay
# — a very common real-world pattern for hero banners. Confirmed this
# actually failed on the GEC page's real gradient-overlay CSS.
BANNER_CSS_RE_OLD = re.compile(
    r'background-image:\s*(?:linear-gradient\([^)]*\)\s*,\s*)?url\(([^)]+\.(?:jpg|jpeg|png))\)',
    re.IGNORECASE,
)

# NEW (v3, fixed) approach: scope extraction to the actual banner
# CONTAINER (id="featurebanner*" or "herobannerblock*"), not the whole
# page. v2 scanned the whole page for ANY background-image, which meant
# on pages using the newer <picture><source srcset> hero template (no
# CSS background-image at all), it fell through and grabbed an unrelated
# "More articles" related-content thumbnail further down the page instead
# of correctly returning "not found". v3 fixes this AND adds support for
# the <picture><source> template itself.
_BANNER_ANCHOR_RE = re.compile(
    r'id=["\'](?:feature|hero)banner(?:block)?\d+["\']', re.IGNORECASE
)
_DECLARATION_RE = re.compile(r'background-image:\s*([^;]+);', re.IGNORECASE)
_URL_RE = re.compile(
    r'url\(\s*[\'"]?([^\'")]+\.(?:jpg|jpeg|png|webp))[\'"]?\s*\)',
    re.IGNORECASE,
)
_SOURCE_TAG_RE = re.compile(r'<source\b[^>]*>', re.IGNORECASE)
_SRCSET_RE = re.compile(
    r'srcset=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']', re.IGNORECASE
)
SIZE_TIER_PREFERENCE = ["medium-1000x570", "small-800x800", "large-1200x700"]
_WINDOW_FALLBACK_SIZE = 8000


def _get_banner_window(html: str):
    anchor = _BANNER_ANCHOR_RE.search(html)
    if not anchor:
        return None
    start = anchor.end()
    next_container = html.find('<div class="container', start + 100)
    end = next_container if next_container != -1 else start + _WINDOW_FALLBACK_SIZE
    return html[start:end]


def extract_banner_images_fixed(html: str) -> list:
    """
    v3: scoped to the banner container, tries <picture><source> first,
    then CSS background-image. Returns a list for reporting purposes
    (the real page-specific image, if found, plus none of the unrelated
    page images that v2 could accidentally surface).
    """
    window = _get_banner_window(html)
    if window is None:
        return []

    results = []

    # picture/source pattern
    candidates = []
    for tag in _SOURCE_TAG_RE.findall(window):
        srcset_match = _SRCSET_RE.search(tag)
        if not srcset_match:
            continue
        has_media = "media=" in tag.lower()
        candidates.append((srcset_match.group(1), has_media))
    if candidates:
        no_media = [p for p, m in candidates if not m]
        results.append(no_media[0] if no_media else candidates[0][0])
        return results  # picture pattern found — this is the hero, done

    # CSS background-image pattern, scoped to same window
    matches = []
    seen = set()
    for decl_match in _DECLARATION_RE.finditer(window):
        declaration = decl_match.group(1)
        urls_in_declaration = _URL_RE.findall(declaration)
        if not urls_in_declaration:
            continue
        rel_path = urls_in_declaration[-1]
        if rel_path not in seen:
            seen.add(rel_path)
            matches.append(rel_path)
    return matches


def extract_banner_images_old(html: str) -> list:
    """Returns deduped list using the OLD buggy regex, for comparison only."""
    matches = []
    seen = set()
    for m in BANNER_CSS_RE_OLD.finditer(html):
        img = m.group(1)
        if img not in seen:
            seen.add(img)
            matches.append(img)
    return matches


GENERIC_IMAGE_PATTERNS = [
    "teaser-images/",
    "logos/",
    "rl-logo",
    "brand-photography/teaser-images",
]


def is_generic(image_url: str) -> bool:
    lower = image_url.lower()
    return any(p in lower for p in GENERIC_IMAGE_PATTERNS)


def check_url(url: str) -> dict:
    result = {
        "url": url,
        "og_images": [],
        "banner_images": [],
        "banner_images_old_regex": [],
        "error": None,
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        result["error"] = str(e)
        return result

    # capture ALL og:image tags — pages can have duplicate (invalid) og:image
    # properties; browsers use the first, but we want to see all of them to
    # spot the generic-teaser / logo-fallback pattern.
    seen_og = set()
    for m in OG_IMAGE_RE.finditer(html):
        img = m.group(1)
        if img not in seen_og:
            seen_og.add(img)
            result["og_images"].append({"url": img, "generic": is_generic(img)})

    result["banner_images"] = extract_banner_images_fixed(html)
    result["banner_images_old_regex"] = extract_banner_images_old(html)

    return result


def main():
    urls = SAMPLE_URLS
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip()]

    if not urls:
        print("No URLs to check. Edit SAMPLE_URLS or pass a file of URLs.")
        return

    results = [check_url(u) for u in urls]

    # track reuse across pages
    og_image_usage = defaultdict(list)
    banner_image_usage = defaultdict(list)
    for r in results:
        for og in r["og_images"]:
            og_image_usage[og["url"]].append(r["url"])
        for img in r["banner_images"]:
            banner_image_usage[img].append(r["url"])

    print("=" * 90)
    print("PER-URL RESULTS")
    print("=" * 90)
    for r in results:
        print(f"\nURL: {r['url']}")
        if r["error"]:
            print(f"  ERROR: {r['error']}")
            continue
        if r["og_images"]:
            print(f"  og:image tags : {len(r['og_images'])} found"
                  + (" (DUPLICATE og:image property on page)" if len(r["og_images"]) > 1 else ""))
            for og in r["og_images"]:
                flag = "  <-- GENERIC (teaser/logo, not page-specific)" if og["generic"] else ""
                print(f"    - {og['url']}{flag}")
        else:
            print(f"  og:image tags : (none found)")
        if r["banner_images"]:
            print(f"  banner images (FIXED regex) : {len(r['banner_images'])} found")
            for img in r["banner_images"]:
                print(f"    - {img}")
        else:
            print(f"  banner images (FIXED regex) : (none found)")

        old_count = len(r["banner_images_old_regex"])
        new_count = len(r["banner_images"])
        if old_count < new_count:
            missed = set(r["banner_images"]) - set(r["banner_images_old_regex"])
            print(f"  ^^ OLD regex would have found only {old_count}/{new_count} "
                  f"— MISSED (gradient-overlay case):")
            for img in missed:
                print(f"       - {img}")

    print("\n" + "=" * 90)
    print("REUSE CHECK (image URL -> pages using it)")
    print("=" * 90)
    print("\nog:image reuse:")
    if not og_image_usage:
        print("  none found across sample")
    for img, pages in og_image_usage.items():
        flag = "  <-- REUSED, likely generic/logo" if len(pages) > 1 else ""
        print(f"  {img}  ({len(pages)} page(s)){flag}")

    print("\nCSS banner-image reuse:")
    if not banner_image_usage:
        print("  none found across sample")
    for img, pages in banner_image_usage.items():
        flag = "  <-- REUSED across pages, weak as unique citation thumbnail" if len(pages) > 1 else ""
        print(f"  {img}  ({len(pages)} page(s)){flag}")

    print("\n" + "=" * 90)
    print("SUMMARY / RECOMMENDATION SIGNAL")
    print("=" * 90)
    total = len(results)
    og_found = sum(1 for r in results if r["og_images"])
    og_all_generic = sum(
        1 for r in results if r["og_images"] and all(o["generic"] for o in r["og_images"])
    )
    og_has_duplicates = sum(1 for r in results if len(r["og_images"]) > 1)
    banner_found = sum(1 for r in results if r["banner_images"])
    pages_old_regex_missed = sum(
        1 for r in results if len(r["banner_images_old_regex"]) < len(r["banner_images"])
    )

    print(f"URLs checked                 : {total}")
    print(f"og:image tag present         : {og_found}/{total}")
    print(f"og:image ALL generic         : {og_all_generic}/{og_found} (logo/teaser, not page-specific)")
    print(f"pages with duplicate og:image: {og_has_duplicates}/{total}")
    print(f"CSS banner image present     : {banner_found}/{total}")
    print(f"pages OLD regex undercounted : {pages_old_regex_missed}/{total} "
          f"(gradient-overlay banners silently missed before the fix)")
    print()
    print("Read this as: if og:image is present but flagged GENERIC on most pages,")
    print("og:image is not usable as a page-specific citation thumbnail on this site")
    print("-> CSS banner-image should be treated as PRIMARY source, not fallback.")
    print("Still check banner reuse below before treating it as reliably unique.")


if __name__ == "__main__":
    main()