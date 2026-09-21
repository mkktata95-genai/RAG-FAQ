"""
Royal London FAQ — Web Scraper httpV1 (Customer Approved URLs Only)
════════════════════════════════════════════════════════════════════
NEW, SEPARATE scraper generation. Does NOT modify, import, or depend
on scrape_approved_urls_updatedV5.py — that file remains the mature,
untouched production script and is kept only as a reference/learning
source (per explicit project decision: old scripts may still be
needed if the site structure changes or HQA returns, so they are
never deleted or overwritten by this rebuild).

WHY THIS SCRIPT EXISTS
  Investigation (check_js_dependency.py, run against all 294 approved
  URLs) confirmed Royal London's approved pages are traditional
  server-side-rendered CMS pages — zero JavaScript rendering
  required. This was independently confirmed by the Website team.
  The 2 genuine routing dropdowns on the approved list (bereavement/
  contact-by-product, online-services-by-product) already render ALL
  option content in the first HTTP response as hidden panels
  (`data-content` attributes toggled by CSS/ARIA) — confirmed via
  exact value-level matching across all dropdown options, manual
  view-source verification, and independent Website-team confirmation.

  Conclusion: the entire Playwright/crawl4ai browser-automation
  dependency is unnecessary for the scraper. This script replaces it
  with a plain `requests.get()` + BeautifulSoup pipeline.

WHAT CARRIES OVER FROM V5 (reused, unchanged logic — reference only,
not imported):
  clean_content(), remove_duplicate_content(), extract_page_metadata(),
  detect_video_from_html(), derive_content_type(), derive_product_category(),
  derive_audience_from_url(), derive_section(), normalize_url(),
  load_approved_pages(), map_excel_category_to_content_type(),
  _has_routing_dropdowns_in_html(), _has_contact_signals(),
  save_scraped_pages() — all pure HTML/text logic with zero browser
  dependency in V5 already; reproduced here verbatim.

WHAT'S NEW / REPLACED:
  - Page fetch: requests.get() instead of crawl4ai AsyncWebCrawler.
  - Content extraction: BeautifulSoup selecting the same content
    containers V5 targeted (main, article, .content, #content,
    .page-content, .main-content, [role='main']), converted to
    markdown via markdownify so clean_content()'s markdown-oriented
    regex rules keep working unchanged.
  - Dropdown states: parsed directly from the `data-content` hidden
    panels already present in the raw HTML — no Playwright, no JS
    event injection, no per-option page reload, no navigation guard
    (nothing navigates — it's one static HTML document). Same output
    schema as V5's _scrape_dropdown_states_playwright(), same
    _has_contact_signals() filter, same minimum content-length gate.
  - Local fixture mode: fetch_html() can read from a local file
    instead of a live URL — lets Phase 1 local testing simulate
    new/changed/unchanged/broken-content scenarios without depending
    on the B&M team to actually change the live site.
  - Image extraction: ONE field, `thumbnail_url` (same field V5
    already used — not a new/second field), deliberately left as a
    placeholder (None) here. extract_page_metadata() still computes
    a teaser_image/og:image value internally (that logic hasn't
    changed), but scrape_page() discards it and stores None instead
    — the citation-card image approach is being redesigned from
    scratch in a separate effort, and a stale/possibly-wrong value
    is worse than an explicit "not yet populated". Once that logic
    is finalized, patch scrape_page() to store the real value.
  - No async, no crawl4ai, no Playwright, no CDP/Chrome subprocess
    handling — all removed as dead weight for this architecture.

OUTPUT FIELDS PER PAGE (unchanged from V5 — schema compatibility is
required for chunk_and_index to keep working without changes):
    url, title, section, audience, content, scraped_at,
    content_length, content_hash, has_video, content_type,
    product_category, description, thumbnail_url, publish_date,
    collection_name, read_time_mins, dropdown_state, dropdown_value,
    scraper_version, metadata_version, scrape_run_id
  thumbnail_url is present but currently always None — see "Image
  extraction" above.

═══════════════════════════════════════════════════════════════
LOCAL USAGE (Phase 1)
═══════════════════════════════════════════════════════════════

    # Standard scrape — local Excel, live HTTP fetch
    python scrape_approved_urls_httpV1.py --file Approved_URLs.xlsx

    # Dry run — validate Excel + URL detection, no scraping
    python scrape_approved_urls_httpV1.py --file Approved_URLs.xlsx --dry-run

    # Local fixture mode — read HTML from local files instead of the
    # live site (Phase 1 controlled testing of new/changed/unchanged/
    # broken-content scenarios)
    python scrape_approved_urls_httpV1.py --file Approved_URLs.xlsx \\
        --fixture-dir ./fixtures

═══════════════════════════════════════════════════════════════
PROGRAMMATIC
═══════════════════════════════════════════════════════════════

    from scrape_approved_urls_httpV1 import run_scraper

    result = run_scraper()
    result = run_scraper(excel_path="custom.xlsx")
    result = run_scraper(dry_run=True)
    result = run_scraper(fixture_dir="./fixtures")   # Phase 1 local testing

    # Result dict:
    # {
    #   "success":       bool,
    #   "pages_scraped": int,
    #   "pages_failed":  int,
    #   "output_path":   str,
    #   "dry_run":       bool,
    #   "error":         str,
    # }

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
    Fresh, separate rebuild of the offline scraper. Removes the
    Playwright/crawl4ai browser-automation dependency entirely
    (confirmed unnecessary — see module docstring above). Fetches
    via requests.get(), extracts content via BeautifulSoup, converts
    to markdown for clean_content() reuse. Dropdown states parsed
    directly from data-content hidden panels already present in the
    raw HTML response. HQA is out of scope for this script (chunk/
    index concern, not scraper). Image extraction present as a
    placeholder field only, pending finalized logic. V5 script is
    unchanged and untouched — used only as a reference during this
    rebuild.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid as _uuid
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import structlog
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as _markdownify
from openpyxl import load_workbook

log = structlog.get_logger()

# ═══════════════════════════════════════════════════════════════
# Versioning
# ═══════════════════════════════════════════════════════════════
SCRAPER_VERSION  = "1.0.0"
METADATA_VERSION = "1.0.0"
SCRAPE_RUN_ID    = str(_uuid.uuid4())

# ═══════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════
APPROVED_EXCEL      = "scraper/data/Approved_URLs.xlsx"
BATCH_SIZE          = 5
BATCH_DELAY_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 20
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 RLG-Aria-Scraper/1.0"
    ),
}

# CSS selector for the content container — identical priority order
# to V5's crawl4ai css_selector config, so the same page regions are
# targeted regardless of fetch method.
CONTENT_SELECTOR = "main, article, .content, #content, .page-content, .main-content, [role='main']"
EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "script", "style", "noscript"]


# ═══════════════════════════════════════════════════════════════
# Section / content-type / product-category / audience derivation
# — reproduced verbatim from V5 (pure URL-pattern logic, zero
# browser dependency there already).
# ═══════════════════════════════════════════════════════════════
SECTION_MAP = {
    "existing-customers":       "Existing Customers",
    "insurance":                "Insurance",
    "pensions":                 "Pensions",
    "guides-tools":             "Guides and Tools",
    "retirement-planning":      "Retirement Planning",
    "isa":                      "ISA",
    "profitshare":              "ProfitShare",
    "about-us":                 "About Us",
    "find-a-financial-adviser": "Find a Financial Adviser",
    "accessibility":            "Accessibility",
    "informational-pages":      "Information",
}


def derive_section(url: str) -> str:
    """
    Derive the coarse "section" label shown in the UI (e.g. "Pensions",
    "ISA", "Existing Customers") from the FIRST path segment of the
    URL — the one right after the domain.

    Pure lookup against SECTION_MAP; anything not in the map (an
    unrecognised or top-level path segment) falls back to "General"
    rather than raising, since an unmapped section shouldn't block a
    page from being scraped.

    Examples:
        .../pensions/annual-allowance/     -> "Pensions"
        .../existing-customers/my-account/ -> "Existing Customers"
        .../isa/stocks-and-shares/         -> "ISA"
        .../some-new-unmapped-path/        -> "General"
    """
    try:
        path = url.split("://", 1)[-1]
        path = path.split("/", 1)[-1]
        first_segment = path.split("/")[0].lower()
        return SECTION_MAP.get(first_segment, "General")
    except Exception:
        return "General"


VIDEO_CSS_SIGNALS = [
    "video-player", "webinar-player", "brightcove-player", "bc-player",
    "vjs-tech", "kaltura-player", "jwplayer", "data-video-id",
    "data-webinar-id", "data-brightcove",
]
VIDEO_URL_SIGNALS = ["/webinars/", "/videos/", "/video/", "/webinar/"]
VIDEO_COLLECTION_SIGNALS = ["webinar", "video", "podcast"]

PRODUCT_CATEGORY_MAP = [
    ("/pension",              "pensions"),
    ("/retirement",           "retirement"),
    ("/life-insurance",       "life_insurance"),
    ("/life-cover",           "life_insurance"),
    ("/whole-of-life",        "life_insurance"),
    ("/income-protection",    "income_protection"),
    ("/critical-illness",     "critical_illness"),
    ("/illness-income",       "income_protection"),
    ("/isa",                  "isa"),
    ("/investments",          "investments"),
    ("/investment",           "investments"),
    ("/fund",                 "investments"),
    ("/funeral",              "funeral"),
    ("/profitshare",          "profitshare"),
    ("/financial-adviser",    "financial_advice"),
    ("/find-a-financial",     "financial_advice"),
    ("/about-us",             "corporate"),
    ("/media",                "corporate"),
    ("/existing-customers",   "customer_support"),
]

CONTENT_TYPE_MAP = [
    ("/webinars/",            "webinar"),
    ("/videos/",              "video"),
    ("/video/",               "video"),
    ("/guides-tools/",        "guide"),
    ("/pension-calculator",   "tool"),
    ("/retirement-planner",   "tool"),
    ("/lump-sum-calculator",  "tool"),
    ("/risk-profiler",        "tool"),
    ("/calculator",           "tool"),
    ("/planner",              "tool"),
    ("/existing-customers/",  "faq"),
    ("/help-and-support/",    "faq"),
    ("/pensions-explained",   "faq"),
    ("/about-us/",            "corporate"),
    ("/media/",               "news"),
    ("/press-release",        "news"),
    ("/news/",                "news"),
    ("/agm/",                 "corporate"),
]


def derive_content_type(url: str) -> str:
    """
    Classify a page's content_type purely from its URL path — no HTML
    or network call needed.

    Walks CONTENT_TYPE_MAP top to bottom and returns the content_type
    for the FIRST pattern that appears anywhere in the (lowercased)
    URL. Order in CONTENT_TYPE_MAP matters: more specific patterns
    (e.g. "/webinars/") are listed before broader ones so a webinar
    page under /existing-customers/ is still typed "webinar", not "faq".

    Falls back to "article" if nothing in the map matches — the
    safest generic default for a normal content page.

    This is the URL-pattern FALLBACK used by
    map_excel_category_to_content_type() when the Excel Category
    column is blank/unrecognised, and also the value it can be
    overridden BY for high-signal URL patterns even when Excel
    category is present (see that function's docstring).
    """
    url_lower = url.lower()
    for pattern, content_type in CONTENT_TYPE_MAP:
        if pattern in url_lower:
            return content_type
    return "article"


def derive_product_category(url: str) -> str:
    """
    Classify which Royal London product a page is about, purely from
    its URL path (e.g. .../pensions/... -> "pensions",
    .../life-insurance/... -> "life_insurance").

    Same first-match-wins logic as derive_content_type() — walks
    PRODUCT_CATEGORY_MAP top to bottom, returns the category for the
    first pattern found in the lowercased URL. Falls back to
    "general" for pages that aren't about a specific product (e.g.
    generic informational or corporate pages).

    Used by extract_page_metadata() to populate the product_category
    field — this is metadata for the RAG index/UI, not something
    that affects whether or how a page gets scraped.
    """
    url_lower = url.lower()
    for pattern, category in PRODUCT_CATEGORY_MAP:
        if pattern in url_lower:
            return category
    return "general"


def derive_audience_from_url(url: str) -> str:
    """
    Determine who a page is written for — customer, adviser, or
    employer — from its subdomain or URL path.

    Royal London runs separate subdomains/sections per audience
    (adviser.royallondon.com, employer.royallondon.com, or a
    /adviser/ or /employer/ path segment on the main domain).
    Anything that doesn't match either is assumed to be
    customer-facing content, which is the overwhelming majority of
    the approved-pages list and the safe default.
    """
    url_lower = url.lower()
    if "adviser.royallondon.com" in url_lower or "/adviser/" in url_lower:
        return "adviser"
    if "employer.royallondon.com" in url_lower or "/employer/" in url_lower:
        return "employer"
    return "customer"


def detect_video_from_html(html: str, url: str) -> bool:
    """
    Decide whether a page contains video content, using 3 independent
    signals (any ONE true = has_video). Royal London's video player is
    proprietary/JS-rendered, so the actual video embed can't be
    reliably extracted — this only detects PRESENCE of video, and the
    UI is expected to just link back to the source page.

    Signal 1 (most reliable): the URL itself contains a video-ish
        path segment (/webinars/, /videos/, /video/, /webinar/).
        Checked first and cheaply, no HTML parse needed.
    Signal 2 (reliable): the page's own <meta name="Collection_name">
        or <meta property="og:type"> tag says "webinar"/"video"/
        "podcast" — Royal London sets this in <head>, so it's always
        present in the raw HTML regardless of fetch method.
    Signal 3 (fallback, less reliable): the rendered HTML contains a
        known video-player CSS class or data attribute
        (VIDEO_CSS_SIGNALS) — belt-and-suspenders only.

    Returns False (not True) on any parsing exception — a failure to
    detect video should never crash the scrape of an otherwise-good
    page.
    """
    url_lower = url.lower()
    for pattern in VIDEO_URL_SIGNALS:
        if pattern in url_lower:
            return True

    if not html:
        return False

    try:
        soup = BeautifulSoup(html, "html.parser")

        collection_meta = (
            soup.find("meta", attrs={"name": "Collection_name"}) or
            soup.find("meta", attrs={"name": "collection_name"}) or
            soup.find("meta", property="Collection_name")
        )
        if collection_meta:
            collection_val = (collection_meta.get("content", "") or "").lower()
            for signal in VIDEO_COLLECTION_SIGNALS:
                if signal in collection_val:
                    return True

        og_type = soup.find("meta", property="og:type")
        if og_type and "video" in (og_type.get("content", "") or "").lower():
            return True

        html_lower = html.lower()
        for signal in VIDEO_CSS_SIGNALS:
            if signal in html_lower:
                return True

    except Exception as e:
        log.warning("video_detection_parse_error", url=url, error=str(e))

    return False


def extract_page_metadata(html: str, url: str) -> dict:
    """
    Parse the page's <head> (and body, for word count) to build the
    metadata dict that rides alongside every scraped page/chunk into
    the search index — this is what powers rich citation cards in
    the UI (thumbnails, read time, publish date, category badges).

    IMPORTANT: takes the FULL page HTML, not the trimmed <main>
    content fragment — meta tags live in <head>, outside whatever
    container extract_main_html() isolates. Called with the raw
    fetch_html() result, not the markdown-converted content.

    Extraction is entirely defensive: every field has a safe
    URL-derived or empty-string default up front, and any BeautifulSoup
    parsing failure is caught and logged — a metadata miss never
    fails the whole page scrape.

    Field-by-field logic:
      has_video        -> detect_video_from_html()
      content_type      -> derive_content_type(url) (URL-pattern only;
                            the Excel-category override happens later
                            in map_excel_category_to_content_type(),
                            not here)
      product_category  -> derive_product_category(url)
      audience           -> derive_audience_from_url(url)
      description        -> first non-empty of: <meta name="description">,
                            <meta property="og:description">,
                            <meta name="st-description"> — truncated
                            to 300 chars for UI preview use
      thumbnail_url      -> <meta name="teaser_image"> preferred (a
                            page-specific 350x200 image Royal London
                            sets deliberately); falls back to
                            og:image ONLY if it isn't the generic
                            RL-logo placeholder image
                            ("rl-logo-meta-image" filtered out)
      publish_date        -> <meta name="st-publish-date">, human-readable
                            "13 March 2024" parsed to ISO "2024-03-13";
                            if the format doesn't parse, the raw
                            string is kept (truncated to 20 chars)
                            rather than silently dropped
      collection_name    -> <meta name="Collection_name">, truncated
                            to 100 chars
      read_time_mins      -> visible body word count / 200 wpm,
                            rounded, minimum 1 — stored as a STRING
                            (index schema field is Edm.String, not a
                            number)

    Returns the metadata dict unconditionally — even total failure
    (no html, or a parse exception) still returns URL-derived
    defaults, never raises.
    """
    metadata = {
        "has_video":        False,
        "content_type":     derive_content_type(url),
        "product_category": derive_product_category(url),
        "audience":         derive_audience_from_url(url),
        "description":      "",
        "thumbnail_url":    "",
        "publish_date":     "",
        "collection_name":  "",
        "read_time_mins":   "5",
    }

    metadata["has_video"] = detect_video_from_html(html, url)

    if not html:
        return metadata

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Description — meta-description > og:description > st-description
        for attr, key in [
            ({"name": "description"}, "content"),
            ({"property": "og:description"}, "content"),
            ({"name": "st-description"}, "content"),
        ]:
            tag = soup.find("meta", attrs=attr)
            if tag and tag.get(key, "").strip():
                metadata["description"] = tag[key].strip()[:300]
                break

        # Thumbnail URL — meta-teaser_image > og:image (RL-logo filtered)
        teaser = soup.find("meta", attrs={"name": "teaser_image"})
        if teaser and teaser.get("content", "").strip():
            metadata["thumbnail_url"] = teaser["content"].strip()
        else:
            og_image = soup.find("meta", property="og:image")
            if og_image and og_image.get("content", "").strip():
                img_url = og_image["content"].strip()
                if "rl-logo-meta-image" not in img_url:
                    metadata["thumbnail_url"] = img_url

        # Publish date — "13 March 2024" -> "2024-03-13"
        pub_date_tag = soup.find("meta", attrs={"name": "st-publish-date"})
        if pub_date_tag and pub_date_tag.get("content", "").strip():
            raw_date = pub_date_tag["content"].strip()
            try:
                parsed = datetime.strptime(raw_date, "%d %B %Y")
                metadata["publish_date"] = parsed.strftime("%Y-%m-%d")
            except ValueError:
                metadata["publish_date"] = raw_date[:20]

        # Collection name
        collection_tag = soup.find("meta", attrs={"name": "Collection_name"})
        if collection_tag and collection_tag.get("content", "").strip():
            metadata["collection_name"] = collection_tag["content"].strip()[:100]

        # Read time — word count / 200 wpm
        body_text = soup.get_text(separator=" ", strip=True)
        word_count = len(body_text.split())
        metadata["read_time_mins"] = str(max(1, round(word_count / 200)))

    except Exception as e:
        log.warning(
            "metadata_extraction_error", url=url, error=str(e),
            note="Using safe defaults for this page",
        )

    return metadata


def normalize_url(url: str) -> str:
    """
    Produce the canonical form of a URL used for both de-duplication
    (load_approved_pages()) and as the actual stored/indexed `url`
    field (scrape_page()), so the same logical page always maps to
    exactly one document.

    Strips the query string and #fragment, strips a trailing slash,
    and LOWERCASES THE PATH ONLY (domain is left as typed — it's
    already effectively case-insensitive and lowercasing it isn't
    needed). Path lowercasing specifically exists because Royal
    London's own approved-URL Excel has contained both
    "/Should-I-consolidate-my-pensions" and
    "/should-i-consolidate-my-pensions" as separate rows for what is
    the same live page — without lowercasing, both survive
    deduplication and get scraped + indexed twice, causing duplicate
    retrieval hits for the same content.

    Applied in three places: once when loading the Excel (the
    dedup key), again defensively on the final URL inside
    scrape_page() (in case the live site redirected to a
    differently-cased URL), and inside fetch_html()'s fixture-mode
    slug builder (so a fixture file name is deterministic regardless
    of how the URL was typed in the Excel).
    """
    url = url.strip()
    url = url.split("?")[0].split("#")[0]
    url = url.rstrip("/")
    if "://" in url:
        scheme_host, _, path = url.partition("://")
        domain_end = path.find("/")
        if domain_end == -1:
            return f"{scheme_host}://{path}"
        domain = path[:domain_end]
        rest = path[domain_end:].lower()
        return f"{scheme_host}://{domain}{rest}"
    return url.lower()


def load_approved_pages(excel_path: str) -> list[dict]:
    """
    Read the customer-supplied Excel of approved URLs and return
    [{"url": normalized_url, "title": cleaned_title, "excel_category": lowercased}, ...],
    de-duplicated by normalize_url().

    COLUMN DETECTION IS BY HEADER NAME, NOT FIXED POSITION — row 1 is
    read and matched case-insensitively against known header synonym
    sets (URL_HEADERS / TITLE_HEADERS / STATUS_HEADERS /
    CATEGORY_HEADERS). A customer Excel isn't guaranteed to have the
    same column layout every time (sometimes just a URL column,
    sometimes URL+title, occasionally +status/+category), so relying
    on fixed column positions would silently break or skip every row
    the moment the layout changes. A URL column is REQUIRED — if none
    of the recognised header names are found, this raises ValueError
    immediately with the actual headers seen, rather than returning
    an empty list with no explanation.

    STATUS COLUMN HANDLING IS DELIBERATELY LENIENT: a row is only
    skipped here for an UNAMBIGUOUS dead signal — a numeric HTTP code
    >= 400, or a keyword like "dead"/"broken"/"404"/"removed"/"gone"/
    "not found". Anything else (blank, "200", "OK", "Live", or text
    that doesn't parse) is KEPT. The Excel's status column reflects
    verification-time state, possibly weeks old — the real,
    authoritative liveness check happens later, per-page, via the
    live HTTP status code in scrape_page(). This function only
    avoids wasting a scrape attempt on links ALREADY known-dead when
    that information happens to be available.

    Title cleanup: strips a trailing " - Royal London" / " | Royal
    London" suffix (and no-space variants) if present, since Excel
    titles are sometimes copied straight from the page's <title> tag.

    Returns an empty list (with a warning logged) rather than raising
    if the file loads but produces zero usable rows — an empty
    approved-pages list is a legitimate (if unusual) outcome the
    caller should be able to check for, not treated as an error.
    """
    wb = load_workbook(excel_path, read_only=True)
    ws = wb.active

    URL_HEADERS      = {"url", "page url", "link", "webpage", "web page", "web url"}
    TITLE_HEADERS    = {"title", "page title", "name"}
    STATUS_HEADERS   = {"status", "status code", "http status"}
    CATEGORY_HEADERS = {"category", "content category", "page category", "type"}

    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    headers = [str(h).strip().lower() if h is not None else "" for h in header_row]

    def find_col(candidates: set) -> int | None:
        for idx, h in enumerate(headers):
            if h in candidates:
                return idx
        return None

    url_idx      = find_col(URL_HEADERS)
    title_idx    = find_col(TITLE_HEADERS)
    status_idx   = find_col(STATUS_HEADERS)
    category_idx = find_col(CATEGORY_HEADERS)

    if url_idx is None:
        wb.close()
        raise ValueError(
            f"load_approved_pages: no URL column found in {excel_path!r}. "
            f"Expected a header matching one of {sorted(URL_HEADERS)} "
            f"(case-insensitive). Headers actually found: {header_row!r}"
        )

    log.info(
        "approved_pages_columns_detected",
        url_column=headers[url_idx],
        title_column=headers[title_idx] if title_idx is not None else None,
        status_column=headers[status_idx] if status_idx is not None else None,
        category_column=headers[category_idx] if category_idx is not None else None,
    )

    def _is_dead_status(value) -> bool:
        if value is None:
            return False
        s = str(value).strip().lower()
        if not s:
            return False
        try:
            code = int(s)
            return code >= 400
        except ValueError:
            pass
        dead_words = {"dead", "broken", "404", "removed", "gone", "not found"}
        return any(w in s for w in dead_words)

    seen, pages = set(), []
    total_rows, skipped_status, duplicates = 0, 0, []

    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) <= url_idx:
            continue

        url = str(row[url_idx]).strip() if row[url_idx] else ""
        if not url.startswith("http"):
            continue

        raw_title = (
            str(row[title_idx]).strip()
            if title_idx is not None and len(row) > title_idx and row[title_idx]
            else ""
        )
        status_value = (
            row[status_idx] if status_idx is not None and len(row) > status_idx else None
        )
        excel_category = (
            str(row[category_idx]).strip().lower()
            if category_idx is not None and len(row) > category_idx and row[category_idx]
            else ""
        )

        total_rows += 1

        if _is_dead_status(status_value):
            skipped_status += 1
            continue

        normalized = normalize_url(url)
        if normalized in seen:
            duplicates.append(url)
            continue
        seen.add(normalized)

        title = raw_title
        for suffix in [" - Royal London", " | Royal London", "- Royal London", "| Royal London"]:
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
                break

        pages.append({"url": normalized, "title": title, "excel_category": excel_category})

    wb.close()

    log.info(
        "approved_pages_loaded",
        total_rows=total_rows,
        skipped_dead_status=skipped_status,
        duplicates_removed=len(duplicates),
        unique_pages=len(pages),
    )
    if not pages:
        log.warning(
            "approved_pages_empty",
            note="No pages loaded — check that the URL column was detected "
                 "correctly and rows contain http(s) URLs.",
        )

    return pages


# ═══════════════════════════════════════════════════════════════
# Content cleaning — verbatim from V5. Operates on markdown text;
# unchanged here so the same regex rules apply regardless of
# whether the markdown came from crawl4ai (V5) or markdownify
# (this script).
# ═══════════════════════════════════════════════════════════════

def remove_duplicate_content(content: str) -> str:
    """
    Detect and strip an accidental FULL duplicate of the article body
    that can end up appended after the real content (a known crawl4ai
    quirk in V5, where the same page occasionally got scraped twice
    into one markdown string; kept here defensively in case the same
    pattern ever shows up from a differently-templated page).

    Heuristic, not exact-match: finds all H1/H2 headings. If there
    are at least 2, and the SECOND one starts after the 40% mark of
    the total content (i.e. there's a substantial first section
    already), compare the first 200 words before that heading against
    the first 200 words after it. If more than 60% of those words
    overlap, treat everything from the second heading onward as a
    duplicate and cut it — keeping only the first copy.

    Deliberately conservative on both thresholds (40% position + 60%
    word overlap) so a page that legitimately repeats a heading name
    (e.g. two different "Overview" sections) is not wrongly truncated
    — only near-identical repeated content is removed.
    """
    h1_pattern = re.compile(r'^#{1,2}\s+\S', re.MULTILINE)
    matches = list(h1_pattern.finditer(content))

    if len(matches) < 2:
        return content

    second_pos = matches[1].start()
    content_len = len(content)

    if second_pos < content_len * 0.40:
        return content

    first_chunk = content[:second_pos].strip()
    second_chunk = content[second_pos:].strip()

    first_words = set(first_chunk.split()[:200])
    second_words = set(second_chunk.split()[:200])

    if not first_words:
        return content

    overlap_pct = len(first_words & second_words) / len(first_words)

    if overlap_pct > 0.60:
        log.info("duplicate_removed", overlap_pct=round(overlap_pct, 2),
                  chars_removed=len(second_chunk))
        return first_chunk

    return content


def clean_content(content: str) -> str:
    """
    Strip pure UI/navigation noise from the markdown-converted page
    content, in a fixed sequence of narrow, targeted regex passes —
    never a broad strip that could remove real article text.

    Operates on MARKDOWN text (not HTML), so the same rules apply
    regardless of which fetch method produced the markdown — V5's
    crawl4ai markdown generator or this script's
    html_fragment_to_markdown() (markdownify). That equivalence is
    intentional and is why this function was ported unchanged rather
    than rewritten for a different content format.

    What each pass removes, in order:
      1. remove_duplicate_content() — see that function's docstring
      2. Numbered breadcrumb navigation lines, e.g.
         "1. [ Home ](url) >" or a trailing "5. Section Name"
      3. "Share" heading blocks and their following empty bullet
         list of social icons
      4. Twitter/X "intent/tweet" share links
      5. Bare facebook/instagram/linkedin/x/youtube/twitter share
         URLs
      6. Empty markdown links `[ ]( )` and empty bullet-link stubs
      7. "Previous Item" / "Next Item" pagination labels
      8. Footer boilerplate: the browser-support banner, and
         everything from a "Connect with us" / "Products and
         services" / "About Royal London" / "Useful links" heading
         to end-of-content, the Royal London Mutual Insurance legal
         paragraph, the "© Royal London <year>" copyright line, and
         "[Back to top]" links — these all anchor at a known heading
         and intentionally consume everything AFTER it (DOTALL),
         since footer content is always last on the page
      9. Whitespace normalisation: collapses 3+ blank lines to one,
         strips trailing spaces before a newline, collapses a
         whitespace-only line between two blank lines

    External (non-royallondon.com) URL stripping is deliberately NOT
    done here — that's chunk_and_index's responsibility at chunk
    time, kept separate so this function's job stays narrowly
    "remove boilerplate", not "rewrite links".

    NOTE — nav/header/footer TAGS are already stripped earlier by
    extract_main_html() before this function ever runs; this handles
    footer-ish TEXT that still ends up inside the main content region
    on some page templates (e.g. an in-body "Connect with us" block).
    """
    content = remove_duplicate_content(content)

    content = re.sub(r'^\s*\d+\.\s*\[.*?\]\(.*?\)\s*>\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\s*\d+\.\s*\[.*?\]\(.*?\)\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\s*\d+\.\s+[A-Z][^\n]{3,60}$', '', content, flags=re.MULTILINE)

    content = re.sub(r'Share\s*\n(\s*\*\s*(\[?\s*\]?\([^\)]*\))?\s*\n)+', '', content)
    content = re.sub(r'^\s*\*\s*\[?\s*\]?\(\s*[^\)]{0,10}\)\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^Share\s*$', '', content, flags=re.MULTILINE)

    content = re.sub(r'\[?\s*\]?\(https://twitter\.com/intent/tweet[^\)]*\)\s*', '', content)

    content = re.sub(
        r'https://www\.(facebook|instagram|linkedin|x|youtube|twitter)\.com/\S+', '', content,
    )

    content = re.sub(r'\[\s*\]\(\s*\)', '', content)
    content = re.sub(r'^\s*\*\s*\[\s*\]\s*$', '', content, flags=re.MULTILINE)

    content = re.sub(r'^(Previous Item|Next Item)\s*$', '', content, flags=re.MULTILINE | re.IGNORECASE)

    content = re.sub(r'Your browser is not supported\..*?×\s*', '', content, flags=re.DOTALL)
    content = re.sub(r'#{1,3}\s*Connect with us.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'#{1,3}\s*Products and services.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'#{1,3}\s*About Royal London.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'#{1,3}\s*Useful links.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'\*\*The Royal London Mutual Insurance.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'©\s*Royal London \d{4}.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'\[Back to top\].*?\n', '', content)

    content = re.sub(r'\n{3,}', '\n\n', content)
    content = re.sub(r'[ \t]+\n', '\n', content)
    content = re.sub(r'\n[ \t]+\n', '\n\n', content)

    return content.strip()


_EXCEL_CATEGORY_MAP = {
    "brand":    "article",
    "guidance": "guide",
    "other":    "article",
    "product":  "article",
    "tool":     "tool",
}


def map_excel_category_to_content_type(excel_category: str, url: str) -> str:
    """
    Decide the final content_type stored on a page — the customer's
    own Excel Category column is treated as the PRIMARY, more
    authoritative source; URL-pattern detection (derive_content_type)
    is the fallback when Excel category is blank/unrecognised.

    BUT: certain URL patterns are high-signal enough to override even
    a present Excel category — specifically webinar/video/tool/faq/
    news. Example: a page the customer tagged Category="Product" but
    which lives under /webinars/ is still typed "webinar", not
    "article" — the URL pattern is a stronger signal than a generic
    catch-all category label in that case.

    _EXCEL_CATEGORY_MAP translates the customer's own category
    vocabulary (Brand/Guidance/Other/Product/Tool) into this system's
    content_type vocabulary:
        brand, other, product -> article
        guidance              -> guide
        tool                  -> tool

    Falls back to derive_content_type(url) alone whenever
    excel_category is empty or not in the map.
    """
    if excel_category:
        mapped = _EXCEL_CATEGORY_MAP.get(excel_category)
        if mapped:
            url_type = derive_content_type(url)
            if url_type in ("webinar", "video", "tool", "faq", "news"):
                return url_type
            return mapped
    return derive_content_type(url)


# ═══════════════════════════════════════════════════════════════
# Dropdown handling — REPLACED. V5 used Playwright to click through
# each <select> option and diff the resulting DOM. Confirmed via
# check_js_dependency.py (294/294 URLs) and independent Website-team
# verification that all option content is already present in the
# raw HTML as `data-content` hidden panels — no JS execution needed.
# This version parses those panels directly.
# ═══════════════════════════════════════════════════════════════

_DROPDOWN_PLACEHOLDERS = {
    "select...", "select", "please select", "--", "choose...",
    "choose", "please choose", "-- select an option --",
    "- select -", "select an option", "select option",
    "none", "n/a", "0", "all",
    # Unicode ellipsis variant — "Select&hellip;" renders as "Select…"
    # (a single U+2026 character, not three literal dots) once
    # BeautifulSoup decodes the HTML entity. Seen on the online-service
    # dropdown page's default placeholder option.
    "select…",
}

# Kept as a BONUS signal (not a requirement) — see
# _dropdown_group_has_variance() below for why the primary filter
# moved to a content-variance check instead of relying on this alone.
_CONTACT_SIGNAL_PATTERNS = [
    re.compile(r'0\d{3,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}'),
    re.compile(r'1\d{3,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}'),
]
_CONTACT_SIGNAL_KEYWORDS = [
    "call us", "write to us", "lines are open", "excluding bank holidays",
    "fill in our online form", "tell us someone has died",
    "fill out our online form", "monday to friday", "8am to", "9am to",
]


def _has_contact_signals(text: str) -> bool:
    """
    Detects phone-number/opening-hours style contact information in a
    panel's text (the bereavement-page pattern specifically).

    NOT used as a gate on its own any more (see
    _dropdown_group_has_variance() for why — this heuristic correctly
    identified the bereavement page but wrongly rejected the
    "online-service, what you can do per policy type" dropdown, whose
    genuine per-option content is a list of account actions, not
    contact info). Kept only as a fast-path BONUS signal inside
    extract_dropdown_states_from_html(): if a panel obviously contains
    a phone number, that alone is enough to trust it without waiting
    on the group-level variance computation.

    Returns True if the text contains a known contact keyword phrase
    (_CONTACT_SIGNAL_KEYWORDS) or a UK/ROI-shaped phone number
    (_CONTACT_SIGNAL_PATTERNS).
    """
    text_lower = text.lower()
    for kw in _CONTACT_SIGNAL_KEYWORDS:
        if kw in text_lower:
            return True
    for pattern in _CONTACT_SIGNAL_PATTERNS:
        if pattern.search(text):
            return True
    return False


# ── Content-variance filter — REPLACES contact-keywords as the
# primary genuine-vs-noise gate (see extract_dropdown_states_from_html
# docstring for the real-world case that forced this change). ──
#
# Below this Jaccard-similarity value, two panels are considered
# "meaningfully different" content. Tuned conservatively toward
# INCLUSION: a false positive (an odd sort/filter dropdown that
# slips through) is a minor, reviewable index-quality issue; a false
# negative (silently dropping a genuine per-option routing dropdown,
# as happened with the online-service page) directly degrades what
# Aria can answer. 0.65 was chosen so that a pure reordering of the
# same item set (Jaccard = 1.0, since word sets are identical) is
# always rejected, while panels sharing some common boilerplate
# phrasing but differing in their specific details are kept.
_DROPDOWN_NOISE_SIMILARITY_THRESHOLD = 0.65


def _panel_word_set(text: str, cap: int = 200) -> set[str]:
    """
    Reduce a panel's text to a comparable set of lowercased word
    tokens (alphanumeric runs only — punctuation ignored), capped to
    the first `cap` words for performance on long panels. Used purely
    for the Jaccard similarity comparison in
    _dropdown_group_has_variance(); not used for anything that ends
    up in the output content itself.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    return set(words[:cap])


def _jaccard_similarity(set_a: set, set_b: set) -> float:
    """
    Standard Jaccard similarity — |intersection| / |union| — between
    two word sets. Returns 0.0 for two empty sets (treated as having
    nothing in common, not "identical") so an empty panel can never
    accidentally count as similar-enough evidence.
    """
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def _dropdown_group_has_variance(panel_texts: list[str]) -> bool:
    """
    THE core filter that replaces contact-keyword matching. Decides
    whether an entire dropdown (all its resolved option panels taken
    together) carries genuinely distinguishing information, or is
    just UI chrome (a sort/filter control whose options reorder or
    relabel the same underlying item set).

    Logic: compute pairwise Jaccard word-set similarity between every
    pair of panels in the group, and take the MINIMUM. If even one
    pair of options shows clearly different content (similarity below
    _DROPDOWN_NOISE_SIMILARITY_THRESHOLD), the dropdown as a whole is
    treated as genuine and ALL its panels are kept — including any
    individual pair that happens to be identical (e.g. two policy
    types that legitimately share the same available actions; that's
    correct data, not noise, once the group itself is confirmed
    genuine).

    Conversely, if EVERY pair is near-identical (minimum similarity
    stays at/above the threshold), the whole group is treated as
    noise and none of its panels are extracted — this is exactly the
    "Newest/Oldest" sort-order case, where every option's word set is
    identical (same 3 items, just reordered) and nothing has changed
    hands to be worth indexing.

    Deliberately evaluated at the GROUP level, not per-option — a
    per-option check can't tell a coincidentally-shared-content pair
    apart from a systematically-reordered group; only comparing
    across the whole set can.

    Requires at least 2 panels to make a comparison; with fewer than
    that there's nothing to compare, so the caller should not invoke
    this for a group of size < 2.
    """
    word_sets = [_panel_word_set(t) for t in panel_texts]
    similarities = []
    for i in range(len(word_sets)):
        for j in range(i + 1, len(word_sets)):
            similarities.append(_jaccard_similarity(word_sets[i], word_sets[j]))

    if not similarities:
        return False

    return min(similarities) < _DROPDOWN_NOISE_SIMILARITY_THRESHOLD


def _has_routing_dropdowns_in_html(html: str) -> bool:
    """
    Cheap gate, run on EVERY scraped page, that decides whether it's
    even worth attempting dropdown-state extraction at all.

    Scans every <select> element in the HTML; returns True the moment
    ANY <select> has MORE THAN ONE non-placeholder <option> (a single
    real option, or only placeholder text like "Select...", "--",
    "Please choose", doesn't count — see _DROPDOWN_PLACEHOLDERS).

    This is intentionally over-inclusive — a plain sort-order or
    category-filter <select> also passes this check (2+ real
    options). That's fine: this function's only job is "is there
    something here worth looking at closer", not "is this a genuine
    routing dropdown" — that distinction is made downstream, at the
    whole-group level, by _dropdown_group_has_variance() inside
    extract_dropdown_states_from_html(). Out of the ~32 pages on the
    approved list that DO have a <select> and pass this check, only 2
    (bereavement, online-services-by-product) are currently known to
    produce output — but unlike an allowlist, this pipeline doesn't
    need to know that in advance; any future dropdown page is
    evaluated by the same content-variance logic automatically.

    Returns False on any parse exception rather than raising — this
    is a pre-check, so failing safe (skip dropdown handling, keep the
    base page) is the right behaviour, not aborting the whole scrape.
    """
    if not html:
        return False
    try:
        soup = BeautifulSoup(html, "html.parser")
        for select in soup.find_all("select"):
            valid_opts = [
                o for o in select.find_all("option")
                if o.get_text(strip=True).lower() not in _DROPDOWN_PLACEHOLDERS
                and o.get_text(strip=True)
            ]
            if len(valid_opts) > 1:
                return True
        return False
    except Exception:
        return False


def _find_panel_for_option(soup: BeautifulSoup, option_value: str, option_text: str):
    """
    Find the hidden content panel matching a dropdown <option>.

    Confirmed pattern (check_js_dependency.py, validated against all
    294 approved URLs + manual view-source on both genuine dropdown
    pages): panels are elements carrying a `data-content` attribute
    whose value exactly matches the <option value="..."> (case-
    insensitive, whitespace-trimmed) — same matching rule used and
    validated in check_js_dependency.py's match_options_to_panels().

    Falls back to matching on the option's visible text if the value
    match fails (defensive — some panels key off text instead of a
    coded value).
    """
    def _norm(s: str) -> str:
        return (s or "").strip().lower()

    target_value = _norm(option_value)
    target_text  = _norm(option_text)

    if target_value:
        panel = soup.find(attrs={"data-content": lambda v, tv=target_value: v is not None and _norm(v) == tv})
        if panel:
            return panel

    if target_text and target_text != target_value:
        panel = soup.find(attrs={"data-content": lambda v, tt=target_text: v is not None and _norm(v) == tt})
        if panel:
            return panel

    return None


def extract_dropdown_states_from_html(
    html: str,
    url: str,
    base_title: str,
    base_page_data: dict,
) -> list[dict]:
    """
    Parse per-option dropdown content directly from `data-content`
    hidden panels already present in the raw HTML — no browser, no
    per-option page reload, no navigation guard (nothing navigates;
    it's one static document).

    Mirrors V5's _scrape_dropdown_states_playwright() output schema
    exactly. Filtering is TWO layers, evaluated per <select> group:

      Layer 1 (per-panel): minimum content length >= 20 chars —
        drops individually broken/empty panels regardless of the
        group's genuineness.

      Layer 2 (whole-group): _dropdown_group_has_variance() — decides
        whether this <select>'s panels carry genuinely distinguishing
        information, or are just UI chrome (sort/filter/reorder
        controls) that happen to have >1 option. See that function's
        docstring for the full rationale.

        NOTE ON WHY THIS ISN'T A PER-OPTION CONTACT-KEYWORD CHECK
        ANYMORE: an earlier version gated each panel individually on
        _has_contact_signals() (phone numbers / "call us" phrasing).
        That correctly identified the bereavement page, but silently
        DROPPED the equally genuine "online-service, what you can do
        per policy type" dropdown — its content is a list of account
        actions, not contact info, so it contains no such keywords.
        A keyword list can only ever describe patterns already known
        in advance; a content-variance check works on any future
        dropdown without needing to know its subject matter first.
        _has_contact_signals() is kept only as a fast-path BONUS
        signal below — an obvious phone number is trusted immediately
        without waiting on the group computation.

    Returns list of page_data dicts (one per option, for every
    <select> group whose variance check passes). Empty list if no
    genuine dropdown is found — base page is still used either way.
    """
    results: list[dict] = []

    if not html:
        return results

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        log.error("dropdown_html_parse_error", url=url, error=str(e))
        return results

    for select in soup.find_all("select"):
        options = select.find_all("option")
        valid_opts = []
        for opt in options:
            text = opt.get_text(strip=True)
            value = opt.get("value", "") or ""
            if text and text.lower() not in _DROPDOWN_PLACEHOLDERS:
                valid_opts.append({"value": value, "text": text})

        if len(valid_opts) <= 1:
            continue

        log.info("dropdown_detected", url=url, option_count=len(valid_opts))

        # ── Resolve panels for every option first, applying only the
        # per-panel min-length gate (Layer 1). The group-level
        # variance decision (Layer 2) needs ALL resolved panels
        # together, so nothing is emitted yet at this stage.
        resolved = []  # list of (option, panel_text)
        for option in valid_opts:
            opt_value = option["value"]
            opt_text = option["text"]

            panel = _find_panel_for_option(soup, opt_value, opt_text)
            if panel is None:
                log.warning(
                    "dropdown_option_no_matching_panel",
                    url=url, option=opt_text, value=opt_value,
                )
                continue

            panel_text = panel.get_text(separator=" ", strip=True)

            if not panel_text or len(panel_text.strip()) < 20:
                log.warning("dropdown_option_no_content", url=url, option=opt_text)
                continue

            resolved.append((option, panel_text.strip()))

        if len(resolved) < 2:
            log.info(
                "dropdown_group_insufficient_panels",
                url=url, resolved_count=len(resolved),
            )
            continue

        panel_texts = [text for _opt, text in resolved]
        has_variance = _dropdown_group_has_variance(panel_texts)
        # Fast-path bonus: an unambiguous contact signal anywhere in
        # the group is enough evidence on its own, even if variance
        # happens to sit right at the threshold.
        has_contact_evidence = any(_has_contact_signals(t) for t in panel_texts)

        if not (has_variance or has_contact_evidence):
            log.info(
                "dropdown_group_rejected_low_variance",
                url=url, option_count=len(resolved),
                note="Panels look like a reordered/relabeled version "
                     "of the same content (e.g. a sort or filter "
                     "control) rather than genuinely distinct "
                     "per-option information.",
            )
            continue

        log.info(
            "dropdown_group_accepted", url=url, option_count=len(resolved),
            has_variance=has_variance, has_contact_evidence=has_contact_evidence,
        )

        for option, content in resolved:
            opt_value = option["value"]
            opt_text = option["text"]

            safe_value = opt_value if opt_value else opt_text
            state_url = f"{url}#state={urllib.parse.quote(safe_value)}"

            results.append({
                "url":              state_url,
                "parent_url":       url,
                "title":            f"{base_title} — {opt_text}",
                "section":          base_page_data["section"],
                "content":          content,
                "scraped_at":       datetime.now(timezone.utc).isoformat(),
                "content_length":   len(content),
                "content_hash":     hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "scraper_version":  SCRAPER_VERSION,
                "metadata_version": METADATA_VERSION,
                "scrape_run_id":    SCRAPE_RUN_ID,
                "audience":         base_page_data["audience"],
                "has_video":        base_page_data["has_video"],
                "content_type":     base_page_data["content_type"],
                "product_category": base_page_data["product_category"],
                "description":      base_page_data["description"],
                "thumbnail_url":    base_page_data["thumbnail_url"],
                "publish_date":     base_page_data["publish_date"],
                "collection_name":  base_page_data["collection_name"],
                "read_time_mins":   str(max(1, len(content.split()) // 200)),
                "dropdown_state":   opt_text,
                "dropdown_value":   opt_value or "",
            })

            log.info("dropdown_option_scraped", url=state_url, option=opt_text, chars=len(content))

    return results


# ═══════════════════════════════════════════════════════════════
# Fetch — REPLACED. requests.get() instead of crawl4ai. Supports a
# local-fixture override for Phase 1 controlled testing (new/
# changed/unchanged/broken-content scenarios) without depending on
# the live site actually being modified by the B&M team.
# ═══════════════════════════════════════════════════════════════

def fetch_html(url: str, fixture_dir: str | None = None) -> tuple[str | None, int | None, str | None]:
    """
    Fetch raw HTML for a URL.

    If fixture_dir is set, reads from a local file instead of the
    live site: <fixture_dir>/<slug>.html, where slug is the
    normalized URL with non-alphanumeric characters replaced by
    underscores. Lets Phase 1 testing simulate new/changed/unchanged/
    broken-content scenarios by swapping which fixture file is
    present, without needing the B&M team to touch the live site.

    Returns (html, status_code, error_message).
    """
    if fixture_dir:
        slug = re.sub(r'[^a-zA-Z0-9]+', '_', normalize_url(url)).strip('_')
        fixture_path = Path(fixture_dir) / f"{slug}.html"
        if not fixture_path.exists():
            return None, None, f"fixture_not_found:{fixture_path}"
        try:
            html = fixture_path.read_text(encoding="utf-8")
            return html, 200, None
        except Exception as e:
            return None, None, f"fixture_read_error:{e}"

    try:
        resp = requests.get(
            url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return resp.text, resp.status_code, None
    except requests.exceptions.RequestException as e:
        return None, None, str(e)


def extract_main_html(html: str) -> str:
    """
    Isolate the main content container from the full page HTML,
    same selector priority V5 gave crawl4ai's css_selector config,
    and strip the same excluded tags (nav/header/footer/aside/
    script/style/noscript) so unrelated boilerplate never reaches
    the markdown conversion step.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag_name in EXCLUDED_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    container = soup.select_one(CONTENT_SELECTOR)
    if container is None:
        container = soup.body or soup

    return str(container)


def html_fragment_to_markdown(html_fragment: str) -> str:
    """
    Convert an HTML fragment to markdown so clean_content()'s
    markdown-oriented regex rules (breadcrumbs, share-link patterns,
    footer boilerplate) keep working unchanged, regardless of fetch
    method. Mirrors V5's markdown_generator options: links kept,
    images ignored (ignore_images=True in V5's DefaultMarkdownGenerator).
    """
    return _markdownify(html_fragment, strip=["img"], heading_style="ATX")


# ═══════════════════════════════════════════════════════════════
# Per-page scrape — REPLACED. Synchronous requests-based version of
# V5's async scrape_page().
# ═══════════════════════════════════════════════════════════════

def scrape_page(
    page_info: dict,
    index: int,
    total: int,
    fixture_dir: str | None = None,
) -> "list[dict] | dict | None":
    """
    Scrape a single page and return cleaned page data.

    Return type mirrors V5 exactly:
        dict        — standard page
        list[dict]  — multi-state dropdown page: first entry is the
                      base page, subsequent entries are per-option states
        None        — scrape failed
    """
    url = page_info["url"]
    title = page_info["title"]
    excel_category = page_info.get("excel_category", "")

    log.info("scraping_page", url=url, index=index, total=total)

    html, status_code, fetch_error = fetch_html(url, fixture_dir=fixture_dir)

    if fetch_error is not None:
        log.error("scrape_fetch_error", url=url, error=fetch_error)
        return None

    if status_code is not None and status_code >= 400:
        log.error("scrape_http_error", url=url, status_code=status_code)
        return None

    if not html:
        log.warning("content_empty", url=url)
        return None

    try:
        main_html = extract_main_html(html)
        page_content = html_fragment_to_markdown(main_html)

        if not page_content or len(page_content.strip()) < 100:
            log.warning("content_too_short", url=url, length=len(page_content or ""))
            return None

        page_content = clean_content(page_content)

        if len(page_content.strip()) < 50:
            log.warning("content_too_short_after_cleaning", url=url)
            return None

        # Metadata extracted from the FULL page HTML (meta tags live in
        # <head>, outside the content container) — same as V5, which
        # ran extract_page_metadata() on result.html, not the trimmed
        # content region.
        metadata = extract_page_metadata(html, url)

        url = normalize_url(url)

        page_data = {
            "url":            url,
            "title":          title,
            "section":        derive_section(url),
            "content":        page_content.strip(),
            "scraped_at":     datetime.now(timezone.utc).isoformat(),
            "content_length": len(page_content.strip()),
            "content_hash":   hashlib.sha256(page_content.strip().encode("utf-8")).hexdigest(),

            "scraper_version":  SCRAPER_VERSION,
            "metadata_version": METADATA_VERSION,
            "scrape_run_id":    SCRAPE_RUN_ID,

            "audience":         metadata["audience"],
            "has_video":        metadata["has_video"],
            "content_type":     map_excel_category_to_content_type(excel_category, url),
            "product_category": metadata["product_category"],
            "description":      metadata["description"],
            # Placeholder only — deliberately NOT populated from
            # metadata["teaser_image"]/og:image (computed above, but
            # discarded here). The citation-card image field is being
            # redesigned from scratch (single field, not two competing
            # sources) and its extraction logic is still being
            # finalized separately. None is safer than a value that
            # might not match whatever the final logic decides is
            # correct — wire the real value in once that logic lands.
            "thumbnail_url":    None,
            "publish_date":     metadata["publish_date"],
            "collection_name":  metadata["collection_name"],
            "read_time_mins":   metadata["read_time_mins"],
        }

        log.info(
            "scrape_success", url=url, index=index, total=total,
            content_length=page_data["content_length"],
            has_video=metadata["has_video"],
            content_type=page_data["content_type"],
            product_category=metadata["product_category"],
        )

        if _has_routing_dropdowns_in_html(html):
            log.info("dropdown_page_detected_via_html", url=url)

            dropdown_states = extract_dropdown_states_from_html(
                html, url, page_data["title"], page_data,
            )

            if dropdown_states:
                log.info(
                    "multi_state_page_scraped", url=url,
                    option_count=len(dropdown_states),
                )
                return [page_data] + dropdown_states

        return page_data

    except Exception as e:
        log.error("scrape_error", url=url, error=str(e))
        return None


# ═══════════════════════════════════════════════════════════════
# Batch orchestration — REPLACED. Synchronous, thread-pool based
# instead of asyncio (no browser session to keep alive means no
# reason to require an event loop).
# ═══════════════════════════════════════════════════════════════

def load_url_source(excel_path: str) -> list[dict]:
    """
    Thin indirection point for WHERE the approved-URL list comes
    from. Currently just delegates to load_approved_pages() (local
    Excel) — kept as its own function, rather than calling
    load_approved_pages() directly from run_scraper(), so that a
    future production URL source (Blob Storage JSON, SharePoint list,
    CMS API — still TBD with the brand/marketing team and DevOps, per
    the equivalent function in V5) can be added here later as an
    env-var-switched branch without touching run_scraper() itself.
    """
    return load_approved_pages(excel_path)


def save_scraped_pages(results: list[dict], output_file: Path) -> str:
    """
    Write the full list of scraped page dicts to a local JSON file
    (creating the parent directory if needed) and return the path
    written, so run_scraper() can report it back to the caller.

    Guards against writing an EMPTY results list: if every URL failed
    to scrape, this logs an error and returns "" WITHOUT touching the
    output file at all — deliberately, so a bad run can never
    silently overwrite a previous good scrape's output with an empty
    []. run_scraper() checks for this empty-string return and
    surfaces it as a hard failure in the result dict rather than
    reporting success with 0 pages.

    (Local-disk save only, for now — a future production path could
    add an Azure Blob Storage upload branch here, mirroring
    load_url_source()'s intended extension point, once that
    production storage decision is made.)
    """
    if not results:
        log.error(
            "save_scraped_pages_empty",
            note="No pages to save — all URLs failed to scrape. "
                 "Output file NOT written. Check scrape errors above.",
        )
        return ""

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log.info("scraped_pages_saved_locally", file=str(output_file), pages=len(results))
    return str(output_file)


def run_scraper(
    excel_path: str | None = None,
    dry_run: bool = False,
    fixture_dir: str | None = None,
) -> dict:
    """
    Programmatic entry point.

    Args:
        excel_path:  Path to approved URLs Excel file. Defaults to
                     APPROVED_EXCEL.
        dry_run:     If True, detect columns + count URLs, no scraping.
        fixture_dir: If set, reads HTML from local files instead of
                     live URLs (Phase 1 controlled testing).

    Returns dict: success, pages_scraped, pages_failed, output_path,
    dry_run, error — identical shape to V5.
    """
    result = {
        "success": False, "pages_scraped": 0, "pages_failed": 0,
        "output_path": "", "dry_run": dry_run, "error": "",
    }

    try:
        excel = excel_path or os.getenv("APPROVED_EXCEL_PATH") or APPROVED_EXCEL

        if dry_run:
            pages = load_url_source(excel)
            print("\n✅ DRY RUN COMPLETE — no scraping performed.")
            print(f"   Excel file:    {excel}")
            print(f"   URLs detected: {len(pages)}")
            result["success"] = True
            return result

        pages_to_scrape = load_url_source(excel)
        total = len(pages_to_scrape)
        scraped, failed_urls = [], []

        log.info(
            "scraper_pipeline_started", total_urls=total, excel=excel,
            fixture_mode=bool(fixture_dir),
        )

        for batch_start in range(0, total, BATCH_SIZE):
            batch = pages_to_scrape[batch_start:batch_start + BATCH_SIZE]

            with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
                futures = [
                    executor.submit(
                        scrape_page, page_info, batch_start + i + 1, total, fixture_dir,
                    )
                    for i, page_info in enumerate(batch)
                ]
                batch_results = [f.result() for f in futures]

            for page_info, r in zip(batch, batch_results):
                if r is None:
                    failed_urls.append(page_info["url"])
                elif isinstance(r, list):
                    scraped.extend(entry for entry in r if entry.get("content_length", 0) >= 20)
                else:
                    scraped.append(r)

            if batch_start + BATCH_SIZE < total and not fixture_dir:
                time.sleep(BATCH_DELAY_SECONDS)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_file = Path("scraper/data") / f"royal_london_faq_approved_httpV1_{timestamp}.json"
        output_path = save_scraped_pages(scraped, output_file)

        if not output_path:
            result["error"] = (
                "All URLs failed to scrape — output file not written. "
                "Check scrape errors in the log."
            )
            result["pages_failed"] = len(failed_urls)
            return result

        log.info(
            "scraper_pipeline_complete", pages_scraped=len(scraped),
            pages_failed=len(failed_urls), output_path=output_path,
        )

        result["success"] = True
        result["pages_scraped"] = len(scraped)
        result["pages_failed"] = len(failed_urls)
        result["output_path"] = output_path
        return result

    except Exception as e:
        result["error"] = str(e)
        log.error("scraper_pipeline_error", error=str(e))
        return result


# ── Main ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="RLG FAQ Scraper httpV1 — no-browser scrape of customer-approved URLs"
    )
    parser.add_argument("--file", default=None, help="Path to approved URLs Excel file.")
    parser.add_argument("--dry-run", action="store_true", help="Detect columns + count URLs only.")
    parser.add_argument(
        "--fixture-dir", default=None,
        help="Read HTML from local files instead of live URLs (Phase 1 local testing).",
    )
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("RLG FAQ SCRAPER httpV1 — Customer Approved URLs (no browser)")
    print("=" * 60)

    result = run_scraper(
        excel_path=args.file, dry_run=args.dry_run, fixture_dir=args.fixture_dir,
    )

    if not result["success"]:
        print(f"\n❌ FAILED: {result['error']}")
        sys.exit(1)

    if not result["dry_run"]:
        print(f"\n✅ Scraped {result['pages_scraped']} pages "
              f"({result['pages_failed']} failed) → {result['output_path']}")


if __name__ == "__main__":
    main()