"""
Royal London Digital Assistance — Content Freshness Manager (HTTP, no HQA)
============================================================================
Rebuild of content_freshnessV1.py against the new no-Playwright pipeline
(scrape_approved_urls_httpV1.py + chunk_embed_index_v1.py). Decisions locked
in with the user before build:
  - Scraping: HTTP + BeautifulSoup, mirrors scrape_approved_urls_httpV1.py
    exactly (no crawl4ai/Playwright/CDP — dropped entirely).
  - HQA: dropped entirely (matches chunk_embed_index_v1.py, no-HQA index).
  - Index: single index only (no dual main/baseline split).
  - Redis: cache-invalidation step dropped entirely.
  - page_image_url: always "" — matches scraper (image logic not finalized).
  - video_url: added as a comparison signal (content_hash alone won't catch
    a video swap, since markdownify strips <img>/video embeds are metadata).
  - chunk_id / pagination-safe index reads / redirect-aware health check /
    refresh_count / archive-before-delete: carried forward from the old
    file's logic, adapted to a single index and local-file archiving.
  - Standalone: zero cross-file imports, same principle as every other
    script in this pipeline family. Functions duplicated, not imported.

CRITICAL — the two-stage hash chain (verified byte-for-byte against a real
value stored in Azure AI Search — see test_content_freshness_hash.py):

    fetch_html
      -> extract_main_html            (BeautifulSoup, strip nav/header/etc.)
      -> html_fragment_to_markdown    (markdownify)
      -> clean_scraped_content        (scraper's clean_content() — boilerplate
                                        strip; renamed here to avoid a name
                                        clash with the URL-stripper below)
      -> clean_content                (chunk_embed_index_v1.py's clean_content()
                                        — external-URL stripper + whitespace
                                        collapse — applied a SECOND time,
                                        because chunk_pages() applies it again
                                        on top of the scraper's already-cleaned
                                        content before hashing)
      -> compute_content_hash         (SHA-256)

Skipping the second clean_content() pass reproduces the exact bug class that
content_freshnessV1.py's v1.7.9 entry documents.

Dropdown panel text is hashed with a single clean_content() pass only
(hash_from_dropdown_panel_text) — panel text comes straight from
BeautifulSoup .get_text(), no boilerplate to strip.

NOT NEEDED (confirmed by inspection): dropdown truncate-before-hash (old
v1.7.10) — the new scraper extracts dropdown states cleanly into separate
page dicts, never blending panels into one blob.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import urllib.parse
from urllib.parse import urlparse

import aiohttp
import requests
import structlog
from bs4 import BeautifulSoup
from dotenv import find_dotenv, load_dotenv
from markdownify import markdownify as _markdownify
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import AzureOpenAI, RateLimitError

load_dotenv(find_dotenv())
log = structlog.get_logger()

# ═══════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════
PIPELINE_VERSION = "1.0.0"
FRESHNESS_JOB_VERSION = "1.0.0"
FRESHNESS_RUN_ID = str(uuid.uuid4())

SCRAPER_VERSION = "1.1.0"       # matches scrape_approved_urls_httpV1.py
METADATA_VERSION = "1.0.0"
SCRAPE_RUN_ID = FRESHNESS_RUN_ID

SEARCH_ENDPOINT = os.environ.get("AZURE_SEARCH_ENDPOINT", "")
INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v5")

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
EMBEDDING_DEPLOYMENT = "text-embedding-3-large"
EMBEDDING_DIMS = 1536
SEMANTIC_CONFIG_NAME = "rlg-semantic-config"
EMBEDDING_BATCH_SIZE = 50
UPLOAD_BATCH_SIZE = 100

CHUNK_SIZE = 1600
CHUNK_OVERLAP = 200

EXPECTED_DOMAIN = "royallondon.com"
HTTP_CONCURRENCY = 5
HTTP_TIMEOUT_SECONDS = 20

BASE_DIR = Path(__file__).resolve().parent
ARCHIVE_DIR = BASE_DIR / "freshness_archive"
MANIFEST_DIR = BASE_DIR / "freshness_manifests"
REPORT_DIR = BASE_DIR / "freshness_reports"
for _d in (ARCHIVE_DIR, MANIFEST_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

CONTENT_SELECTOR = "main, article, .content, #content, .page-content, .main-content, [role='main']"
EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "script", "style", "noscript"]

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


_FAILURE_REASONS: dict[str, str] = {}
_FAILURE_REASONS_LOCK = threading.Lock()

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
    # Vimeo — confirmed live on royallondon.com (e.g. understanding-
    # compound-growth, about-us/how-we-are-run/mutuality) which were
    # previously silently scraped with has_video=False.
    "vimeoapi", "vimeovideoblock", "player.vimeo.com",
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


VIDEO_IFRAME_HOSTS = [
    "player.vimeo.com",
    "youtube.com/embed",
    "players.brightcove.net",
]


def extract_video_url(html: str, url: str) -> str:
    """
    Extract the actual embedded video URL (not just presence) from an
    <iframe> in the page HTML, for the known video-host patterns in
    VIDEO_IFRAME_HOSTS.

    Checks candidate_attrs in priority order: data-src, src,
    data-lazy-src, data-vimeo-src. data-src is checked FIRST because
    Royal London's Vimeo iframes are lazy-loaded — the real URL lives
    in data-src until a JS swap fires on scroll-into-view, and src is
    often empty or a placeholder until then (confirmed live).

    The full URL including its query string is returned unmodified —
    Vimeo's `?h=<hash>` parameter is a required access token, not a
    trackable/strippable tracking param; the video fails to load
    without it.

    Returns "" (not None) if no matching iframe is found, or on any
    parse exception — this mirrors detect_video_from_html()'s
    fail-safe behaviour (a video-url miss should never crash the
    scrape of an otherwise-good page), and is why has_video=True with
    video_url="" is a valid, expected combination (video detected via
    a CSS/meta signal, but no iframe host we recognise was present).
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        candidate_attrs = ["data-src", "src", "data-lazy-src", "data-vimeo-src"]
        for iframe in soup.find_all("iframe"):
            for attr in candidate_attrs:
                val = (iframe.get(attr) or "").strip()
                if not val:
                    continue
                for host in VIDEO_IFRAME_HOSTS:
                    if host in val.lower():
                        return val
    except Exception as e:
        log.warning("video_url_extraction_error", url=url, error=str(e))
    return ""


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
      page_image_url      -> <meta name="teaser_image"> preferred (a
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
        "page_image_url":    "",
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
            metadata["page_image_url"] = teaser["content"].strip()
        else:
            og_image = soup.find("meta", property="og:image")
            if og_image and og_image.get("content", "").strip():
                img_url = og_image["content"].strip()
                if "rl-logo-meta-image" not in img_url:
                    metadata["page_image_url"] = img_url

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
            dropdown_url = f"{url}#state={urllib.parse.quote(safe_value)}"

            results.append({
                "source_url":       url,
                "dropdown_url":        dropdown_url,
                "title":            base_title,
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
                "video_url":        base_page_data["video_url"],
                "content_type":     base_page_data["content_type"],
                "product_category": base_page_data["product_category"],
                "description":      base_page_data["description"],
                "page_image_url":    base_page_data["page_image_url"],
                "publish_date":     base_page_data["publish_date"],
                "collection_name":  base_page_data["collection_name"],
                "read_time_mins":   str(max(1, len(content.split()) // 200)),
                "dropdown_title":   opt_text,
                "dropdown_value":   opt_value or "",
            })

            log.info("dropdown_option_scraped", url=dropdown_url, option=opt_text, chars=len(content))

    return results


# ═══════════════════════════════════════════════════════════════
def _record_failure(url: str, reason: str) -> None:
    """Thread-safe: record why a given URL failed, for run_scraper() to report."""
    with _FAILURE_REASONS_LOCK:
        _FAILURE_REASONS[url] = reason


def _get_failure_reason(url: str) -> str:
    """Look up a recorded failure reason; falls back to a generic label
    if scrape_page() failed via a path that didn't call _record_failure()
    (shouldn't happen, but never let a missing reason crash reporting)."""
    with _FAILURE_REASONS_LOCK:
        return _FAILURE_REASONS.get(url, "unknown_error: no reason recorded")


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

# ═══════════════════════════════════════════════════════════════
# Stage 1-3: HTML -> markdown -> boilerplate-stripped
# (byte-identical port of scrape_approved_urls_httpV1.py)
# ═══════════════════════════════════════════════════════════════

def extract_main_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in EXCLUDED_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    container = soup.select_one(CONTENT_SELECTOR)
    if container is None:
        container = soup.body or soup
    return str(container)


def html_fragment_to_markdown(html_fragment: str) -> str:
    return _markdownify(html_fragment, strip=["img"], heading_style="ATX")


def _remove_duplicate_content(content: str) -> str:
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
        return first_chunk
    return content


def clean_scraped_content(content: str) -> str:
    """
    Scraper's boilerplate/dedup stripper. Renamed from the scraper's
    clean_content() to avoid colliding with the indexer's clean_content()
    (URL-stripper) below — both are needed here, in sequence.
    """
    content = _remove_duplicate_content(content)

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


# ═══════════════════════════════════════════════════════════════
# Stage 4: URL-stripper (byte-identical port of chunk_embed_index_v1.py's
# clean_content()) — second pass, applied on top of clean_scraped_content()
# ═══════════════════════════════════════════════════════════════

def clean_content(text: str) -> str:
    def replace_markdown_link(match):
        anchor_text, url = match.group(1), match.group(2)
        return match.group(0) if "royallondon.com" in url else anchor_text

    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', replace_markdown_link, text)

    def replace_raw_url(match):
        url = match.group(0)
        return url if "royallondon.com" in url else ""

    text = re.sub(r'https?://[^\s\)\]"\'<>,]+', replace_raw_url, text)
    text = re.sub(r'  +', ' ', text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════
# Stage 5: hash
# ═══════════════════════════════════════════════════════════════

def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def hash_from_raw_html(html: str) -> str:
    """
    Full chain, single entry point: raw page HTML -> the exact hash that
    would end up stored in the index for this page's content. This is
    what content_freshness's scrape-and-compare step calls per URL.
    """
    main_html = extract_main_html(html)
    markdown = html_fragment_to_markdown(main_html)
    scraped_clean = clean_scraped_content(markdown)
    indexer_clean = clean_content(scraped_clean)
    return compute_content_hash(indexer_clean)

# ═══════════════════════════════════════════════════════════════
# URL utilities (byte-identical port of content_freshnessV1.py —
# pure string logic, unrelated to the scraping-engine rebuild)
# ═══════════════════════════════════════════════════════════════

def normalise_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip().rstrip("/").lower())
        return parsed.geturl()
    except Exception:
        return url.strip().rstrip("/").lower()


def normalise_url_path(url: str) -> str:
    """Canonical URL for redirect comparison — lowercase path, strip trailing slash/query/fragment."""
    try:
        parsed = urlparse(url)
        norm = parsed._replace(path=parsed.path.lower().rstrip("/"), query="", fragment="")
        return norm.geturl()
    except Exception:
        return url.strip().rstrip("/").lower()


def is_dropdown_url(url: str) -> bool:
    return "#state=" in url or "#policy=" in url


def get_base_url(url: str) -> str:
    return url.split("#")[0]


# ═══════════════════════════════════════════════════════════════
# Dropdown-panel hashing — separate from hash_from_raw_html() above.
# Panel text comes straight from BeautifulSoup .get_text() (see
# extract_dropdown_states_from_html() below), never through
# clean_scraped_content()'s boilerplate stripper (nothing to strip —
# it's already just the panel's own text). chunk_embed_index_v1.py's
# chunk_pages() applies its clean_content() (URL-stripper) uniformly
# to EVERY page including dropdown states before hashing, so parity
# requires the same single second-pass here, not the full 2-stage
# chain hash_from_raw_html() uses for base pages.
# ═══════════════════════════════════════════════════════════════

def hash_from_dropdown_panel_text(panel_text: str) -> str:
    return compute_content_hash(clean_content(panel_text))


# ═══════════════════════════════════════════════════════════════
# Element-aware chunking + chunk_pages — byte-identical port of
# chunk_embed_index_v1.py (same "no cross-file import" principle
# this whole pipeline family uses — a fix here must be manually
# mirrored there, and vice versa; known, accepted tradeoff).
# ═══════════════════════════════════════════════════════════════

_EA_TABLE_ROWS_PER_CHUNK = 30
_EA_DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]


def _ea_parse_table_block(lines: list) -> dict:
    header_row, separator_row, data_rows = None, None, []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.match(r'^[-:]+$', c) for c in cells if c):
            separator_row = cells
        elif header_row is None and separator_row is None:
            header_row = cells
        else:
            data_rows.append(cells)
    return {"header_row": header_row, "data_rows": data_rows}


def _ea_parse_elements(content: str) -> list:
    lines, elements, i = content.splitlines(), [], 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        header_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if header_match:
            elements.append({
                "type": "header", "level": len(header_match.group(1)),
                "text": header_match.group(2).strip(), "lines": [line],
            })
            i += 1
            continue

        if stripped.startswith("|") and "|" in stripped[1:]:
            block = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            parsed = _ea_parse_table_block(block)
            elements.append({
                "type": "table", "lines": block,
                "header_row": parsed["header_row"], "data_rows": parsed["data_rows"],
            })
            continue

        if not stripped:
            elements.append({"type": "blank", "lines": [line]})
            i += 1
            continue

        block = [line]
        i += 1
        while i < len(lines):
            ns = lines[i].strip()
            if ns.startswith("|") or re.match(r'^#{1,6}\s', ns) or not ns:
                break
            block.append(lines[i])
            i += 1
        elements.append({"type": "prose", "lines": block})

    return elements


def _ea_group_into_sections(elements: list) -> list:
    sections = []
    current = {"header_text": None, "header_level": None, "body": []}
    for el in elements:
        if el["type"] == "header" and el["level"] in (2, 3):
            if current["body"] or current["header_text"] is not None:
                sections.append(current)
            current = {"header_text": el["text"], "header_level": el["level"], "body": []}
        else:
            current["body"].append(el)
    if current["body"] or current["header_text"] is not None:
        sections.append(current)
    return sections


def _ea_render_table_chunk(header_row: list, data_rows: list) -> str:
    if not header_row:
        return "\n".join("| " + " | ".join(row) + " |" for row in data_rows)
    sep = ["-" * max(3, len(h)) for h in header_row]
    lines = ["| " + " | ".join(header_row) + " |", "| " + " | ".join(sep) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in data_rows]
    return "\n".join(lines)


def _ea_chunk_table_element(table_el: dict) -> list:
    header_row = table_el.get("header_row")
    data_rows = table_el.get("data_rows") or []
    if not data_rows:
        return ["\n".join(table_el["lines"])]
    if len(data_rows) <= _EA_TABLE_ROWS_PER_CHUNK:
        return [_ea_render_table_chunk(header_row, data_rows)]
    batches = []
    for start in range(0, len(data_rows), _EA_TABLE_ROWS_PER_CHUNK):
        batches.append(_ea_render_table_chunk(header_row, data_rows[start:start + _EA_TABLE_ROWS_PER_CHUNK]))
    return batches


def _ea_merge_orphaned_header(pieces: list, header_text: str) -> list:
    if len(pieces) < 2:
        return pieces
    header_line = f"### {header_text}".strip()
    if pieces[0].strip() == header_line:
        return [pieces[0] + "\n" + pieces[1]] + pieces[2:]
    return pieces


def _ea_section_to_text_segments(section: dict) -> list:
    segments, prose_buf = [], []

    def _flush_prose():
        if prose_buf:
            text = "\n".join(prose_buf).strip()
            if text:
                segments.append({"text": text, "element_type": "prose"})
            prose_buf.clear()

    for el in section["body"]:
        if el["type"] == "table":
            _flush_prose()
            for chunk_text in _ea_chunk_table_element(el):
                segments.append({"text": chunk_text, "element_type": "table"})
        elif el["type"] == "blank":
            prose_buf.append("")
        else:
            prose_buf.extend(el["lines"])
    _flush_prose()
    return segments


def chunk_content_element_aware(content: str, chunk_size: int = 1600, chunk_overlap: int = 200) -> list:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        separators=_EA_DEFAULT_SEPARATORS,
    )
    elements = _ea_parse_elements(content)
    has_boundary_header = any(e["type"] == "header" and e["level"] in (2, 3) for e in elements)
    has_table = any(e["type"] == "table" for e in elements)

    if not has_boundary_header and not has_table:
        pieces = splitter.split_text(content)
        return [{"text": p, "element_type": "prose"} for p in pieces if p.strip()]

    results = []
    for section in _ea_group_into_sections(elements):
        header_text = section.get("header_text")
        for seg in _ea_section_to_text_segments(section):
            if seg["element_type"] == "table":
                text = f"### {header_text}\n{seg['text']}" if header_text else seg["text"]
                results.append({"text": text, "element_type": "table"})
            else:
                section_text = f"### {header_text}\n{seg['text']}" if header_text else seg["text"]
                pieces = splitter.split_text(section_text)
                if header_text:
                    pieces = _ea_merge_orphaned_header(pieces, header_text)
                results.extend({"text": p, "element_type": "prose"} for p in pieces if p.strip())
    return results


def compute_chunk_id(source_url: str, chunk_index: int, content: str) -> str:
    return hashlib.sha256(f"{source_url}|{chunk_index}|{content}".encode("utf-8")).hexdigest()


def chunk_pages(pages: list, refresh_run_id: str = "") -> list:
    """
    Same shape as chunk_embed_index_v1.py's chunk_pages() (dropdown-
    state atomic chunks, element-aware chunking for standard pages,
    all versioning fields, deterministic chunk_id) — called here on
    just the CHANGED/NEW pages for a delta re-index, not the full
    350-page corpus.
    """
    _index_run_id = refresh_run_id or str(uuid.uuid4())
    _indexed_at = datetime.now(timezone.utc).isoformat()

    # Dedup key: dropdown_url when present (dropdown chunk identity),
    # else source_url — two dropdown states under the same base page
    # legitimately share source_url now.
    seen_urls, dedup_pages = set(), []
    for p in pages:
        pu = p.get("dropdown_url") or p.get("source_url", "")
        if pu and pu not in seen_urls:
            seen_urls.add(pu)
            dedup_pages.append(p)
        elif pu:
            log.warning("chunk_pages_duplicate_url_skipped", url=pu)
    pages = dedup_pages

    chunks = []
    for page in pages:
        content = page.get("content", "").strip()
        title = page.get("title", "")
        url = page.get("source_url", "")
        dropdown_url = page.get("dropdown_url", "")
        section = page.get("section", "")
        audience = page.get("audience", "customer")

        if not content or len(content) < 50:
            log.warning("skipping_empty_page", url=url)
            continue

        content = clean_content(content)
        page_hash = compute_content_hash(content)
        content_with_title = f"{title}\n\n{content}" if title else content

        common_fields = {
            "section": section,
            "audience": audience,
            "scraped_at": page.get("scraped_at", ""),
            "content_hash": page_hash,
            "dropdown_url": dropdown_url,
            "dropdown_title": page.get("dropdown_title", ""),
            "pipeline_version": PIPELINE_VERSION,
            "index_run_id": _index_run_id,
            "indexed_at": _indexed_at,
            "scraper_version": page.get("scraper_version", SCRAPER_VERSION),
            "metadata_version": page.get("metadata_version", METADATA_VERSION),
            "scrape_run_id": page.get("scrape_run_id", SCRAPE_RUN_ID),
            "refresh_count": page.get("refresh_count", 0),
            "has_video": page.get("has_video", False),
            "video_url": page.get("video_url") or "",
            "content_type": page.get("content_type", "article"),
            "product_category": page.get("product_category", "general"),
            "description": page.get("description", ""),
            "page_image_url": page.get("page_image_url") or "",
            "publish_date": page.get("publish_date", ""),
            "collection_name": page.get("collection_name", ""),
            "read_time_mins": str(page.get("read_time_mins", "5")),
        }

        is_dropdown_state = bool(page.get("dropdown_title", ""))

        if is_dropdown_state:
            stripped = content_with_title.strip()
            if len(stripped) >= 50:
                chunks.append({
                    # identity for chunk_id must be dropdown_url — source_url
                    # is the shared base page url and would otherwise
                    # collide with the base page's own chunk_index=0.
                    "chunk_id": compute_chunk_id(dropdown_url or url, 0, stripped),
                    "content": stripped,
                    "source_url": url,
                    "title": title,
                    "chunk_index": 0,
                    "total_chunks": 1,
                    "element_type": "dropdown_state",
                    **common_fields,
                })
            continue

        pieces = chunk_content_element_aware(content_with_title, CHUNK_SIZE, CHUNK_OVERLAP)
        valid_pieces = [p for p in pieces if len(p["text"].strip()) >= 50]

        for i, piece in enumerate(valid_pieces):
            split = piece["text"].strip()
            chunks.append({
                "chunk_id": compute_chunk_id(url, i, split),
                "content": split,
                "source_url": url,
                "title": title,
                "chunk_index": i,
                "total_chunks": len(valid_pieces),
                "element_type": piece["element_type"],
                **common_fields,
            })

    log.info("chunking_complete", total_chunks=len(chunks))
    return chunks


# ═══════════════════════════════════════════════════════════════
# Azure clients — Search + OpenAI. Single index only (no dual
# main/baseline split, no index CREATION here — the index is
# created once by chunk_embed_index_v1.py --full; freshness only
# ever reads from and writes documents into an already-existing
# index). No core/embeddings.py import — own inline client, same
# pattern as chunk_embed_index_v1.py, for the same reason (query-
# time team's module isn't part of this deployment).
# ═══════════════════════════════════════════════════════════════

_credential = None
_openai_client = None


def get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def get_openai_client() -> AzureOpenAI:
    global _openai_client
    if _openai_client is None:
        if not AZURE_OPENAI_ENDPOINT:
            raise ValueError("AZURE_OPENAI_ENDPOINT is not set in .env")
        token_provider = get_bearer_token_provider(
            get_credential(), "https://cognitiveservices.azure.com/.default",
        )
        _openai_client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            azure_ad_token_provider=token_provider,
            api_version="2024-12-01-preview",
        )
    return _openai_client


def embed_chunks(chunks: list) -> list:
    texts = [c["content"] for c in chunks]
    if not texts:
        return []

    BATCH_SLEEP_SECONDS, MAX_RETRIES, RETRY_BASE_SECONDS = 2, 5, 10
    client = get_openai_client()
    all_embeddings = []
    total_batches = (len(texts) + EMBEDDING_BATCH_SIZE - 1) // EMBEDDING_BATCH_SIZE

    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i:i + EMBEDDING_BATCH_SIZE]
        batch_number = i // EMBEDDING_BATCH_SIZE + 1
        retry = 0
        while True:
            try:
                response = client.embeddings.create(
                    input=batch, model=EMBEDDING_DEPLOYMENT, dimensions=EMBEDDING_DIMS,
                )
                sorted_data = sorted(response.data, key=lambda e: e.index)
                all_embeddings.extend(e.embedding for e in sorted_data)
                log.info("embeddings_batch_done", batch=batch_number, total_batches=total_batches)
                break
            except RateLimitError as e:
                retry += 1
                if retry > MAX_RETRIES:
                    log.error("embeddings_rate_limit_max_retries", batch=batch_number, error=str(e))
                    raise
                wait = RETRY_BASE_SECONDS * (2 ** (retry - 1))
                log.warning("embeddings_rate_limit_retry", batch=batch_number, retry=retry, wait_seconds=wait)
                time.sleep(wait)

        if i + EMBEDDING_BATCH_SIZE < len(texts):
            time.sleep(BATCH_SLEEP_SECONDS)

    return all_embeddings


def get_search_client() -> SearchClient:
    return SearchClient(endpoint=SEARCH_ENDPOINT, index_name=INDEX_NAME, credential=get_credential())


def upload_chunks(chunks: list, embeddings: list) -> int:
    client = get_search_client()
    documents = [{**chunk, "embedding": emb} for chunk, emb in zip(chunks, embeddings)]
    total_uploaded = 0
    for i in range(0, len(documents), UPLOAD_BATCH_SIZE):
        batch = documents[i:i + UPLOAD_BATCH_SIZE]
        result = client.upload_documents(documents=batch)
        total_uploaded += sum(1 for r in result if r.succeeded)
    log.info("upload_complete", total=total_uploaded)
    return total_uploaded


# ═══════════════════════════════════════════════════════════════
# Index read/delete helpers — pagination-fixed versions (mirrors
# content_freshnessV1.py's v1.7.8 fix: top=N with no skip loop
# silently missed most documents once the index grew past a few
# thousand chunks — every helper here pages through the full index).
# ═══════════════════════════════════════════════════════════════

def fetch_current_state_from_index() -> dict:
    """
    Read {normalised_identity_url: {content_hash, video_url, is_dropdown,
    source_url}} for every DISTINCT chunk identity currently in the index.

    identity_url = dropdown_url when non-empty, else source_url. Since the
    schema change, source_url is ALWAYS the clean, navigable base page
    URL — including for dropdown-state chunks — so every dropdown state
    under one base page now shares the same source_url. Keying this dict
    by source_url alone would collapse them all into one entry again
    (the exact bug this function was fixed for previously): a dropdown
    state's own content_hash (hashed from just that panel's text) would
    get silently overwritten by/lost to whichever chunk the paginated
    scan saw first. dropdown_url is the real per-chunk identity and must be
    the key whenever it's present.
    """
    client = get_search_client()
    state: dict = {}
    skip, page_sz = 0, 1000
    while True:
        try:
            results = client.search(
                search_text="*",
                select=["source_url", "dropdown_url", "content_hash", "video_url"],
                top=page_sz, skip=skip,
            )
            batch = list(results)
            if not batch:
                break
            for r in batch:
                src = r.get("source_url", "")
                st = r.get("dropdown_url", "")
                identity = st or src
                norm = normalise_url(identity)
                if norm and norm not in state:
                    state[norm] = {
                        "content_hash": r.get("content_hash", ""),
                        "video_url": r.get("video_url", ""),
                        "is_dropdown": bool(st),
                        "source_url": src,
                    }
            if len(batch) < page_sz:
                break
            skip += page_sz
        except Exception as e:
            log.error("fetch_current_state_error", error=str(e))
            break
    log.info("current_state_fetched", count=len(state))
    return state


def get_chunk_ids_for_url(url: str) -> list:
    """
    Every chunk_id whose source_url matches this base page URL.

    Since the schema change, source_url is ALWAYS the clean base page
    URL — for a page's own prose/table chunks AND for every one of its
    dropdown-state chunks (which used to carry the #state=... fragment
    here; that now lives in dropdown_url instead). So a single source_url
    match already captures the base page's own chunks and every
    dropdown variant in one pass — no separate fragment-expansion step
    needed (see the removed get_all_urls_to_delete()).
    """
    client = get_search_client()
    norm = normalise_url(url)
    ids, skip, page_sz = [], 0, 1000
    try:
        while True:
            results = client.search(search_text="*", select=["chunk_id", "source_url"], top=page_sz, skip=skip)
            batch = list(results)
            if not batch:
                break
            for r in batch:
                if normalise_url(r.get("source_url", "")) == norm:
                    ids.append(r["chunk_id"])
            if len(batch) < page_sz:
                break
            skip += page_sz
    except Exception as e:
        log.error("get_chunk_ids_error", url=url, error=str(e))
    return ids


def delete_chunks_for_urls(urls: list, dry_run: bool = False) -> dict:
    client = get_search_client()
    summary = {}
    for url in urls:
        ids = get_chunk_ids_for_url(url)
        if not ids:
            continue
        summary[url] = len(ids)
        if dry_run:
            log.info("dry_run_would_delete", url=url, count=len(ids))
            continue
        for i in range(0, len(ids), 100):
            batch = [{"chunk_id": cid} for cid in ids[i:i + 100]]
            try:
                client.delete_documents(documents=batch)
            except Exception as e:
                log.error("delete_error", url=url, error=str(e))
        log.info("chunks_deleted", url=url, count=len(ids))
    return summary


def get_refresh_count_for_url(identity_url: str) -> int:
    """
    identity_url should be dropdown_url for a dropdown-state chunk, or
    source_url for a regular chunk — matches how fetch_current_state_
    from_index() keys entries, since source_url alone is no longer
    unique for dropdown-state chunks (all share their base page's URL).
    """
    client = get_search_client()
    norm = normalise_url(identity_url)
    skip, page_sz = 0, 1000
    try:
        while True:
            results = client.search(search_text="*", select=["source_url", "dropdown_url", "refresh_count"], top=page_sz, skip=skip)
            batch = list(results)
            if not batch:
                break
            for r in batch:
                if normalise_url(r.get("dropdown_url") or r.get("source_url", "")) == norm:
                    return int(r.get("refresh_count") or 0)
            if len(batch) < page_sz:
                break
            skip += page_sz
    except Exception as e:
        log.warning("get_refresh_count_error", url=identity_url, error=str(e))
    return 0


def archive_deleted_chunks_locally(url: str, ts_str: str) -> str | None:
    """
    Local-file archive (not Blob — this pipeline currently has no
    Blob usage; Search + OpenAI only). Non-fatal — a failure here
    never blocks the delete/reindex. Saves the exact chunk documents
    about to be deleted, for manual rollback if a content change
    causes a response-quality regression.
    """
    client = get_search_client()
    norm = normalise_url(url)
    try:
        chunks, skip, page_sz = [], 0, 500
        while True:
            results = client.search(search_text="*", select=["*"], top=page_sz, skip=skip)
            batch = list(results)
            if not batch:
                break
            for r in batch:
                if normalise_url(r.get("source_url", "")) == norm:
                    chunks.append(dict(r))
            if len(batch) < page_sz:
                break
            skip += page_sz
        if not chunks:
            return None
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        archive_dir = Path(ARCHIVE_DIR)
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"deleted_{url_hash}_{ts_str}.json"
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(chunks, f, indent=2)
        return str(archive_path)
    except Exception as e:
        log.warning("archive_failed", url=url, error=str(e))
        return None


# ═══════════════════════════════════════════════════════════════
# Health check — HTTP HEAD, follows redirects, compares the FINAL
# resolved URL (not just the first hop's Location header) against
# the original, so a transparent trailing-slash/scheme redirect
# (which royallondon.com issues on every URL) isn't a false positive.
# ═══════════════════════════════════════════════════════════════

async def check_single_url(session: "aiohttp.ClientSession", entry: dict) -> dict:
    url = entry["url"]
    result = {"url": url, "status": "unknown", "status_code": None, "redirect_note": ""}
    try:
        async with session.head(
            url, allow_redirects=True, max_redirects=5,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
        ) as resp:
            code = resp.status
            final_url = str(resp.url)
            result["status_code"] = code
            if code < 300:
                if normalise_url_path(final_url) == normalise_url_path(url):
                    result["status"] = "live"
                elif EXPECTED_DOMAIN in final_url:
                    result["status"] = "internal_redirect"
                    result["redirect_note"] = f"Redirects to {final_url} — add new URL to Excel."
                else:
                    result["status"] = "external_redirect"
                    result["redirect_note"] = f"Redirects to {final_url} — removing."
            elif code < 500:
                result["status"] = "dead_404"
            else:
                result["status"] = "dead_5xx"
    except asyncio.TimeoutError:
        result["status"] = "timeout"
    except Exception as e:
        result["status"] = "error"
        log.warning("health_check_error", url=url, error=str(e))
    return result


async def check_all_urls_health(entries: list) -> list:
    sem = asyncio.Semaphore(HTTP_CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=False, limit=HTTP_CONCURRENCY)
    async with aiohttp.ClientSession(
        connector=connector, headers={"User-Agent": "RLG-Aria-ContentFreshness/1.0"},
    ) as session:
        async def _bounded(entry):
            async with sem:
                return await check_single_url(session, entry)
        return await asyncio.gather(*[_bounded(e) for e in entries])


# ═══════════════════════════════════════════════════════════════
# Per-URL scrape for freshness — HTTP + BeautifulSoup, mirrors
# scrape_approved_urls_httpV1.py's scrape_page() exactly (same
# extract_main_html/html_fragment_to_markdown/clean_scraped_content/
# metadata/dropdown pipeline), but computes content_hash via THIS
# file's validated hash_from_raw_html() chain rather than trusting
# a hash baked in by the scraper — freshness must independently
# reproduce the indexer's hash formula, not just copy a stored value.
# ═══════════════════════════════════════════════════════════════

def scrape_url_for_freshness(entry: dict, fixture_dir: str | None = None) -> "list[dict] | None":
    url = entry["url"]
    title = entry.get("title", "")
    excel_category = entry.get("excel_category", "")

    html, status_code, fetch_error = fetch_html(url, fixture_dir=fixture_dir)
    if fetch_error is not None:
        log.error("scrape_fetch_error", url=url, error=fetch_error)
        return None
    if status_code is not None and status_code >= 400:
        log.warning("scrape_http_error", url=url, status_code=status_code)
        return None
    if not html:
        log.warning("content_empty", url=url)
        return None

    try:
        main_html = extract_main_html(html)
        markdown = html_fragment_to_markdown(main_html)
        if not markdown or len(markdown.strip()) < 100:
            log.warning("content_too_short", url=url)
            return None

        page_content = clean_scraped_content(markdown)
        if len(page_content.strip()) < 50:
            log.warning("content_too_short_after_cleaning", url=url)
            return None

        content_hash = compute_content_hash(clean_content(page_content))
        metadata = extract_page_metadata(html, url)
        url = normalize_url(url)

        page_data = {
            "source_url": url,
            "dropdown_url": "",
            "title": title,
            "section": derive_section(url),
            "content": page_content.strip(),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "content_length": len(page_content.strip()),
            "content_hash": content_hash,
            "scraper_version": SCRAPER_VERSION,
            "metadata_version": METADATA_VERSION,
            "scrape_run_id": SCRAPE_RUN_ID,
            "audience": metadata["audience"],
            "has_video": metadata["has_video"],
            "video_url": extract_video_url(html, url) if metadata["has_video"] else "",
            "content_type": map_excel_category_to_content_type(excel_category, url),
            "product_category": metadata["product_category"],
            "description": metadata["description"],
            # Always "" — matches scrape_approved_urls_httpV1.py. Image
            # logic not finalized; wire the real value in once it lands
            # (both here and in the scraper, together).
            "page_image_url": "",
            "publish_date": metadata["publish_date"],
            "collection_name": metadata["collection_name"],
            "read_time_mins": metadata["read_time_mins"],
            "dropdown_title": "",
            "dropdown_value": "",
        }

        pages = [page_data]

        if _has_routing_dropdowns_in_html(html):
            dropdown_states = extract_dropdown_states_from_html(html, url, title, page_data)
            for state in dropdown_states:
                # Recompute content_hash for the dropdown state through
                # the same clean_content() second pass chunk_pages()
                # applies uniformly to every page — the inline hash
                # extract_dropdown_states_from_html() sets is a raw
                # SHA-256 of the unstripped panel text, not what the
                # indexer actually stores.
                state["content_hash"] = hash_from_dropdown_panel_text(state["content"])
            if dropdown_states:
                pages.extend(dropdown_states)
                log.info("multi_state_page_scraped", url=url, states=len(dropdown_states))

        return pages

    except Exception as e:
        log.error("scrape_error", url=url, error=str(e))
        return None


# ═══════════════════════════════════════════════════════════════
# Pre-flight chunking validation — run BEFORE Step 8 deletes
# anything, so a chunking bug on new/changed pages never leaves a
# URL's existing chunks deleted with nothing successfully re-indexed
# in their place. Ports content_freshnessV1.py's v1.7.0 safety gate.
# ═══════════════════════════════════════════════════════════════

_MAX_VALIDATION_FAILURE_RATIO = 0.05


def validate_chunking_preflight(scraped_pages: list) -> dict:
    result = {"ok": True, "total_pages": len(scraped_pages), "failed_pages": [], "failure_ratio": 0.0}
    if not scraped_pages:
        return result

    for page in scraped_pages:
        url = page.get("dropdown_url") or page.get("source_url", "")
        try:
            chunks = chunk_pages([page])
            if not chunks and len(page.get("content", "").strip()) >= 100:
                result["failed_pages"].append({"url": url, "reason": "zero_chunks_for_nontrivial_content"})
                continue
            for c in chunks:
                if len(c["content"]) > CHUNK_SIZE * 2:
                    result["failed_pages"].append({"url": url, "reason": f"oversized_chunk:{len(c['content'])}chars"})
                    break
                if not c.get("element_type"):
                    result["failed_pages"].append({"url": url, "reason": "missing_element_type"})
                    break
        except Exception as e:
            result["failed_pages"].append({"url": url, "reason": f"chunking_exception:{e}"})

    result["failure_ratio"] = len(result["failed_pages"]) / len(scraped_pages)
    result["ok"] = result["failure_ratio"] <= _MAX_VALIDATION_FAILURE_RATIO
    return result


# ═══════════════════════════════════════════════════════════════
# Report — single index, no HQA/cache columns. Adds Video Changed /
# Thumbnail Changed columns (see scoping discussion: content_hash
# alone doesn't catch a video swap or attribute-only edits).
# ═══════════════════════════════════════════════════════════════

def _style_header_row(ws, row_num: int, bg_hex: str):
    fill = PatternFill("solid", fgColor=bg_hex)
    font = Font(bold=True, color="FFFFFF")
    for cell in ws[row_num]:
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)


def _fill_row(ws, row_num: int, bg_hex: str):
    fill = PatternFill("solid", fgColor=bg_hex)
    for cell in ws[row_num]:
        cell.fill = fill


def _auto_width(ws, max_width: int = 70):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 4, max_width)


def build_report(scan_results: list, run_summary: dict, output_path: Path) -> Path:
    C = {
        "unchanged": "D4EDDA", "changed": "FFF3CD", "new": "CCE5FF",
        "removed": "F8D7DA", "int_redirect": "FFE0B2", "ext_redirect": "E0E0E0",
        "header": "2C3E50",
    }
    ACTION_COLOUR = {
        "unchanged": C["unchanged"], "new": C["new"], "changed": C["changed"],
        "removed_404": C["removed"], "removed_5xx": C["removed"],
        "removed_delisted": C["removed"], "removed_int_redir": C["int_redirect"],
        "removed_ext_redir": C["ext_redirect"], "scrape_failed": C["removed"],
    }
    HEADERS = [
        "URL", "Title", "Category", "Dropdown?", "HTTP Status", "Action",
        "Chunks Before", "Chunks After", "Video Changed", "Notes",
    ]

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Full Results"
    ws1.append(HEADERS)
    _style_header_row(ws1, 1, C["header"])
    for r in scan_results:
        colour = ACTION_COLOUR.get(r.get("action", ""), "FFFFFF")
        ws1.append([
            r.get("url", ""), r.get("title", ""), r.get("category", ""),
            "Yes" if r.get("is_dropdown") else "No", r.get("status_code", ""),
            r.get("action", ""), r.get("chunks_before", 0), r.get("chunks_after", 0),
            "Yes" if r.get("video_changed") else "No", r.get("notes", ""),
        ])
        _fill_row(ws1, ws1.max_row, colour)
    _auto_width(ws1)

    ws2 = wb.create_sheet("Action Required")
    ws2.append(HEADERS)
    _style_header_row(ws2, 1, C["header"])
    for r in scan_results:
        if r.get("action", "unchanged") != "unchanged":
            ws2.append([
                r.get("url", ""), r.get("title", ""), r.get("category", ""),
                "Yes" if r.get("is_dropdown") else "No", r.get("status_code", ""),
                r.get("action", ""), r.get("chunks_before", 0), r.get("chunks_after", 0),
                "Yes" if r.get("video_changed") else "No", r.get("notes", ""),
            ])
    _auto_width(ws2)

    ws3 = wb.create_sheet("Summary")
    for row_data in [
        ["Aria Content Freshness Report (HTTP, no HQA)", ""],
        ["Run At (UTC)", run_summary.get("run_at", "")],
        ["Mode", run_summary.get("mode", "").upper()],
        ["Index", INDEX_NAME],
        ["Freshness Job Version", FRESHNESS_JOB_VERSION],
        ["Pipeline Version", PIPELINE_VERSION],
        ["Scraper Version", SCRAPER_VERSION],
        ["Metadata Version", METADATA_VERSION],
        ["Run ID", run_summary.get("freshness_run_id", "")],
        ["", ""],
        ["Total Approved URLs", run_summary.get("total_approved", 0)],
        ["Unchanged", run_summary.get("live_unchanged", 0)],
        ["Changed (re-indexed)", run_summary.get("changed", 0)],
        ["New (indexed)", run_summary.get("new", 0)],
        ["Dead (404)", run_summary.get("dead_404", 0)],
        ["Dead (5xx)", run_summary.get("dead_5xx", 0)],
        ["Internal redirect", run_summary.get("internal_redirect", 0)],
        ["External redirect", run_summary.get("external_redirect", 0)],
        ["De-listed", run_summary.get("delisted", 0)],
        ["Scrape failed", run_summary.get("scrape_failed", 0)],
        ["", ""],
        ["Chunks added", run_summary.get("chunks_added", 0)],
        ["Chunks deleted", run_summary.get("chunks_deleted", 0)],
        ["", ""],
        ["POLICY NOTES", ""],
        ["Single index", "One index only — no main/baseline split."],
        ["No HQA", "Dropped entirely — matches chunk_embed_index_v1.py."],
        ["Internal redirects", "Treated as removed — new URL must be added to Excel."],
        ["De-listed URLs", "Removed from index — not in approved Excel."],
        ["Dropdown variants", "All #state= URLs deleted when base URL changes/removed."],
        ["Video/thumbnail", "content_hash does not cover video swaps or image changes — "
                             "video_url compared separately; page_image_url always empty "
                             "for now (image logic not finalized)."],
    ]:
        ws3.append(row_data)
    _auto_width(ws3)

    ws4 = wb.create_sheet("Removed from Index")
    ws4.append(["URL", "Title", "Reason", "Chunks Deleted"])
    _style_header_row(ws4, 1, C["header"])
    removed_actions = {"removed_404", "removed_5xx", "removed_delisted", "removed_int_redir", "removed_ext_redir", "scrape_failed"}
    for r in scan_results:
        if r.get("action") in removed_actions:
            ws4.append([r.get("url", ""), r.get("title", ""), r.get("notes", ""), r.get("chunks_before", 0)])
            _fill_row(ws4, ws4.max_row, C["removed"])
    _auto_width(ws4)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    log.info("report_saved", path=str(output_path))
    return output_path


def save_run_manifest(manifest: dict, ts_str: str) -> str:
    out_dir = Path(MANIFEST_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"run_manifest_{ts_str}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, default=str)
    return str(path)


# ═══════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════

def run_freshness_job(mode: str = "report", excel_path: str | None = None,
                       fixture_dir: str | None = None, dry_run: bool = False) -> dict:
    """
    report mode: full scan, produce Excel report, NO index writes.
    apply mode:  full scan + delete/re-index changed+new+removed URLs.
    """
    run_started_at = time.monotonic()
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    freshness_run_id = str(uuid.uuid4())

    if not SEARCH_ENDPOINT:
        raise ValueError("AZURE_SEARCH_ENDPOINT not set in .env")
    if mode == "apply" and not AZURE_OPENAI_ENDPOINT:
        raise ValueError("AZURE_OPENAI_ENDPOINT not set in .env (needed to embed changed/new pages)")

    excel_path = excel_path or APPROVED_EXCEL

    print("📋 Step 1: Loading approved URLs from Excel...")
    entries = load_approved_pages(excel_path)

    print(f"🔍 Step 2: Reading current state from '{INDEX_NAME}'...")
    current_state = fetch_current_state_from_index()

    print(f"🌐 Step 3: Health-checking {len(entries):,} URLs...")
    health_results = asyncio.run(check_all_urls_health(entries))
    health_by_url = {normalise_url(h["url"]): h for h in health_results}

    print("🗂️  Step 4: Classifying actions...")
    approved_norm_urls = {normalise_url(e["url"]) for e in entries}
    scan_results = []
    urls_to_scrape = []

    for entry in entries:
        url = entry["url"]
        norm = normalise_url(url)
        health = health_by_url.get(norm, {"status": "unknown", "status_code": None})
        row = {
            "url": url, "title": entry.get("title", ""), "category": entry.get("excel_category", ""),
            "is_dropdown": False, "status_code": health.get("status_code"),
            "action": "unchanged", "chunks_before": 0, "chunks_after": 0,
            "video_changed": False, "notes": "",
        }

        status = health["status"]
        if status == "dead_404":
            row["action"], row["notes"] = "removed_404", "404 Not Found."
        elif status == "dead_5xx":
            row["action"], row["notes"] = "removed_5xx", "5xx server error."
        elif status == "internal_redirect":
            row["action"], row["notes"] = "removed_int_redir", health.get("redirect_note", "")
        elif status == "external_redirect":
            row["action"], row["notes"] = "removed_ext_redir", health.get("redirect_note", "")
        elif status in ("timeout", "error"):
            row["action"], row["notes"] = "scrape_failed", f"Health check {status}."
        else:
            base_norm = normalise_url(get_base_url(url))
            stored = current_state.get(base_norm)
            if stored is None:
                row["action"] = "new"
                urls_to_scrape.append(entry)
            else:
                row["notes"] = "pending_content_check"
                urls_to_scrape.append(entry)

        scan_results.append(row)

    # De-listed: in the index but no longer in the approved Excel.
    # Only check base-page entries (no #state=/#policy=) — a dropdown
    # variant's exact URL never appears in the Excel by design, so
    # checking dropdown keys here would false-flag every one of them.
    delisted_bases = [
        b for b in current_state
        if not current_state[b]["is_dropdown"] and b not in approved_norm_urls
    ]
    for base in delisted_bases:
        scan_results.append({
            "url": base, "title": "", "category": "", "is_dropdown": False,
            "status_code": None, "action": "removed_delisted", "chunks_before": 0,
            "chunks_after": 0, "video_changed": False, "notes": "No longer in approved Excel.",
        })

    print(f"🕷️  Step 5: Scraping {len(urls_to_scrape):,} URLs pending content check...")
    scraped_pages: list = []
    row_by_url = {r["url"]: r for r in scan_results}
    for i, entry in enumerate(urls_to_scrape, 1):
        pages = scrape_url_for_freshness(entry, fixture_dir=fixture_dir)
        row = row_by_url.get(entry["url"])
        if pages is None:
            if row:
                row["action"], row["notes"] = "scrape_failed", "Scrape returned no content."
            continue

        # Compare EVERY page scrape_url_for_freshness() returned — the
        # base page AND each dropdown state — against its own exact
        # source_url entry in current_state. Each dropdown state has
        # its own independent content_hash (hashed from just that
        # panel's text), so comparing only pages[0] (the base page)
        # against a single collapsed value would miss a state-only
        # edit entirely, or — the bug this replaced — compare the
        # base page's fresh hash against a dropdown state's stored
        # hash and false-flag "changed" on no real edit.
        any_changed = False
        any_video_changed = False
        diff_notes = []
        for pg in pages:
            pg_identity = pg.get("dropdown_url") or pg.get("source_url", "")
            pg_norm = normalise_url(pg_identity)
            stored = current_state.get(pg_norm)
            if stored is None:
                any_changed = True
                diff_notes.append(f"new dropdown state: {pg_identity}" if pg.get("dropdown_title") else "content_hash differs")
                continue
            hash_diff = stored["content_hash"] != pg["content_hash"]
            video_diff = (stored.get("video_url") or "") != (pg.get("video_url") or "")
            if hash_diff or video_diff:
                any_changed = True
                if video_diff:
                    any_video_changed = True
                    diff_notes.append("video_url differs" if not hash_diff else "content_hash + video_url differ")
                else:
                    diff_notes.append("content_hash differs")

        if row and row["action"] != "new":
            if not any_changed:
                row["action"], row["notes"] = "unchanged", ""
                continue
            row["action"] = "changed"
            row["video_changed"] = any_video_changed
            row["notes"] = "; ".join(dict.fromkeys(diff_notes))  # dedupe, preserve order

        scraped_pages.extend(pages)

    print(f"🛡️  Step 6: Pre-flight chunking validation on {len(scraped_pages):,} scraped pages...")
    preflight = validate_chunking_preflight(scraped_pages)
    if not preflight["ok"] and mode == "apply":
        raise RuntimeError(
            f"ABORTED before any index write: chunking pre-flight failure ratio "
            f"{preflight['failure_ratio']:.1%} exceeds {_MAX_VALIDATION_FAILURE_RATIO:.0%}. "
            f"Failed pages: {preflight['failed_pages'][:5]}"
        )

    chunks_added = 0
    chunks_deleted = 0

    if mode == "apply" and not dry_run:
        print("⚡ Step 7: Applying changes to the index...")
        # No fragment-expansion step needed here: source_url is now the
        # clean base URL for every chunk under a page, dropdown-state
        # chunks included, so get_chunk_ids_for_url(base_url) — called
        # inside delete_chunks_for_urls() — already matches the base
        # page's own chunks AND every dropdown variant in one pass.
        urls_needing_delete = [
            r["url"] for r in scan_results
            if r["action"] in ("changed", "removed_404", "removed_5xx", "removed_delisted",
                                "removed_int_redir", "removed_ext_redir")
        ]
        if urls_needing_delete:
            for url in urls_needing_delete:
                archive_deleted_chunks_locally(url, ts_str)
            deleted_summary = delete_chunks_for_urls(urls_needing_delete)
            chunks_deleted = sum(deleted_summary.values())

        pages_to_index = [p for p in scraped_pages]
        for p in pages_to_index:
            # refresh_count tracks THIS chunk's own prior count — for a
            # dropdown-state page that's keyed by dropdown_url (its real
            # identity), not source_url (now shared with the base page
            # and every other dropdown state under it).
            p_identity = p.get("dropdown_url") or p.get("source_url", "")
            base_norm = normalise_url(p.get("source_url", ""))
            p["refresh_count"] = get_refresh_count_for_url(p_identity) + 1 if base_norm in current_state or normalise_url(p_identity) in current_state else 0

        if pages_to_index:
            new_chunks = chunk_pages(pages_to_index, refresh_run_id=freshness_run_id)
            embeddings = embed_chunks(new_chunks)
            chunks_added = upload_chunks(new_chunks, embeddings)

            chunks_by_url: dict = {}
            for c in new_chunks:
                chunks_by_url.setdefault(c["source_url"], 0)
                chunks_by_url[c["source_url"]] += 1
            for r in scan_results:
                r["chunks_after"] = chunks_by_url.get(r["url"], 0)
    else:
        print("   Step 7: Report mode (or dry-run) — no index writes.")

    print("📊 Step 8: Generating Excel report...")
    run_summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "freshness_run_id": freshness_run_id,
        "total_approved": len(entries),
        "live_unchanged": sum(1 for r in scan_results if r["action"] == "unchanged"),
        "changed": sum(1 for r in scan_results if r["action"] == "changed"),
        "new": sum(1 for r in scan_results if r["action"] == "new"),
        "dead_404": sum(1 for r in scan_results if r["action"] == "removed_404"),
        "dead_5xx": sum(1 for r in scan_results if r["action"] == "removed_5xx"),
        "internal_redirect": sum(1 for r in scan_results if r["action"] == "removed_int_redir"),
        "external_redirect": sum(1 for r in scan_results if r["action"] == "removed_ext_redir"),
        "delisted": sum(1 for r in scan_results if r["action"] == "removed_delisted"),
        "scrape_failed": sum(1 for r in scan_results if r["action"] == "scrape_failed"),
        "chunks_added": chunks_added,
        "chunks_deleted": chunks_deleted,
    }
    report_path = build_report(
        scan_results, run_summary,
        Path(REPORT_DIR) / f"freshness_report_{mode}_{ts_str}.xlsx",
    )

    print("💾 Step 9: Saving run manifest...")
    manifest_path = save_run_manifest({"run_summary": run_summary, "preflight": preflight}, ts_str)

    elapsed = round(time.monotonic() - run_started_at, 2)
    return {
        "success": True, "elapsed_seconds": elapsed, "run_summary": run_summary,
        "report_path": str(report_path), "manifest_path": manifest_path,
        "preflight": preflight,
    }


def main():
    parser = argparse.ArgumentParser(description="RLG Aria Content Freshness (HTTP, no HQA)")
    parser.add_argument("--mode", choices=["report", "apply"], default="report")
    parser.add_argument("--file", type=str, default=None, help="Path to approved-URLs Excel")
    parser.add_argument("--fixture-dir", type=str, default=None, help="Local HTML fixtures instead of live HTTP")
    parser.add_argument("--dry-run", action="store_true", help="Validate config/connectivity, no index writes")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("RLG ARIA CONTENT FRESHNESS (HTTP, no HQA)")
    print("=" * 60)
    print(f"   Mode:      {args.mode.upper()}")
    print(f"   Dry run:   {'YES' if args.dry_run else 'No'}")
    print(f"   Excel:     {args.file or APPROVED_EXCEL}")
    print(f"   Index:     {INDEX_NAME}")
    print("=" * 60 + "\n")

    result = run_freshness_job(
        mode=args.mode, excel_path=args.file,
        fixture_dir=args.fixture_dir, dry_run=args.dry_run,
    )

    s = result["run_summary"]
    print(f"\nDone in {result['elapsed_seconds']}s")
    print(f"   Unchanged: {s['live_unchanged']}   Changed: {s['changed']}   New: {s['new']}")
    print(f"   Removed (404/5xx/redirect/delisted/failed): "
          f"{s['dead_404'] + s['dead_5xx'] + s['internal_redirect'] + s['external_redirect'] + s['delisted'] + s['scrape_failed']}")
    print(f"   Chunks added: {s['chunks_added']}   Chunks deleted: {s['chunks_deleted']}")
    print(f"   Report:   {result['report_path']}")
    print(f"   Manifest: {result['manifest_path']}")


if __name__ == "__main__":
    main()