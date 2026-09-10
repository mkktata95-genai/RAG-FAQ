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
    # add more page templates here: a pension product page, an FAQ page, an insurance page, etc.
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"
}

OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
BANNER_CSS_RE = re.compile(
    r'background-image:\s*(?:linear-gradient\([^)]*\)\s*,\s*)?url\(([^)]+\.(?:jpg|jpeg|png))\)',
    re.IGNORECASE,
)


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
    result = {"url": url, "og_images": [], "banner_images": [], "error": None}
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

    # dedupe banner matches, keep order
    seen = set()
    for m in BANNER_CSS_RE.finditer(html):
        img = m.group(1)
        if img not in seen:
            seen.add(img)
            result["banner_images"].append(img)

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
            print(f"  banner images : {len(r['banner_images'])} found")
            for img in r["banner_images"]:
                print(f"    - {img}")
        else:
            print(f"  banner images : (none found)")

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

    print(f"URLs checked                 : {total}")
    print(f"og:image tag present         : {og_found}/{total}")
    print(f"og:image ALL generic         : {og_all_generic}/{og_found} (logo/teaser, not page-specific)")
    print(f"pages with duplicate og:image: {og_has_duplicates}/{total}")
    print(f"CSS banner image present     : {banner_found}/{total}")
    print()
    print("Read this as: if og:image is present but flagged GENERIC on most pages,")
    print("og:image is not usable as a page-specific citation thumbnail on this site")
    print("-> CSS banner-image should be treated as PRIMARY source, not fallback.")
    print("Still check banner reuse below before treating it as reliably unique.")


if __name__ == "__main__":
    main()