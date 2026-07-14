"""
Royal London ARIA — Content Freshness Manager
==============================================
Nightly job that compares the approved URL Excel (from Azure Blob
Storage) against the live Azure AI Search index, detects content
changes using SHA-256 hashing, and keeps the index + Redis cache
in sync with what is actually on the Royal London website.

DECISION LOG (v1.0.0):
  - Excel in Azure Blob is the SINGLE source of truth for which
    URLs ARIA is authorised to answer about. If a URL is removed
    from the Excel it is de-indexed regardless of HTTP status.
  - Internal redirects (same royallondon.com domain) are NOT
    followed silently. They are treated as removed because the
    approved content path no longer exists. The dedicated team
    must add the new URL to the approved Excel explicitly.
  - External redirects (off royallondon.com) are always removed.
  - 404 / 5xx URLs are removed. The nightly run will re-add them
    automatically if they recover and are still in the Excel.
  - Cache invalidation is TARGETED: only rlg:cache:* keys whose
    stored source_url set intersects changed/removed URLs are
    flushed. A full flush on every run would destroy valid cached
    answers for unchanged pages.
  - content_hash stored in Azure AI Search (retrievable=True,
    v5.2.0 of chunk_and_index_hqaV3.py). This is the source of
    truth for what was indexed. Blob hash state (content_hashes.json)
    is written as a backup after every apply run.
  - Dropdown pages (scraper v4.4.0+): pages with routing <select>
    elements produce synthetic URLs (base_url#policy=option_value).
    Health checks use only the base URL — fragment URLs are not
    real HTTP endpoints. When a base URL changes or is removed,
    ALL its #policy= variant chunks are also deleted.
  - Hash input: SHA-256 of content AFTER clean_content() external
    URL stripping (identical to chunk_and_index_hqaV3.py v5.2.0).
    Scraper, indexer, and freshness script all use the same
    algorithm on the same transformed input.

TWO MODES:
  --mode report  (default / safe)
      Full scan, produce Excel report, NO writes to index or cache.
      Use for manual review before enabling nightly automation.

  --mode apply   (nightly job)
      Full scan + execute all changes:
        NEW URLs     → scrape (+ dropdown states) → chunk → embed → index
        CHANGED URLs → delete old chunks → re-scrape → chunk → embed
                       → index → invalidate cache
        REMOVED URLs → delete all chunks (incl. #policy= variants)
                       → invalidate cache

═══════════════════════════════════════════════════════════════
LOCAL USAGE
═══════════════════════════════════════════════════════════════

    # Report only — safe, no writes (uses local Excel)
    python scraper/content_freshness.py --mode report --file scraper/data/Approved_URLs.xlsx

    # Apply mode — local Excel (dev/testing)
    python scraper/content_freshness.py --mode apply --file scraper/data/Approved_URLs.xlsx

    # Report — production (downloads Excel from Blob)
    python scraper/content_freshness.py --mode report

    # Apply — production (nightly job)
    python scraper/content_freshness.py --mode apply

    # Config + connectivity validation only (no scraping, no writes)
    python scraper/content_freshness.py --mode apply --dry-run

    # Override the Blob Excel path for a one-off run
    python scraper/content_freshness.py --mode apply \
        --blob-name approved-urls/Approved_URLs_070726.xlsx

═══════════════════════════════════════════════════════════════
PRODUCTION — AZURE CONTAINER APPS JOB (DevOps)
═══════════════════════════════════════════════════════════════

# TODO (DevOps): Create Container Apps Job: aria-freshness-job
# Schedule: nightly at 02:00 UTC (after CMS publishing windows)
# Image: same image as aria-scraper-job (crawl4ai + dependencies)
#
# Required env vars — set in Azure Key Vault, NOT in code:
#
#   AZURE_STORAGE_CONNECTION   — Blob Storage connection string
#   BLOB_CONTAINER_NAME        — container holding Excel + hash state
#                                Default: "scraper-data"
#   BLOB_APPROVED_EXCEL_NAME   — fixed path of approved Excel in Blob
#                                Dedicated team always overwrites this
#                                fixed name (no date-stamped filenames)
#                                Default: "approved-urls/Approved_URLs.xlsx"
#   BLOB_HASH_STATE_NAME       — hash state blob written after each run
#                                Default: "freshness/content_hashes.json"
#   BLOB_REPORT_PREFIX         — Blob prefix for archived reports
#                                Default: "freshness/reports/"
#   AZURE_SEARCH_ENDPOINT      — Azure AI Search endpoint
#   AZURE_SEARCH_INDEX_NAME    — Target index (default: rlg-faq-index-v3)
#   AZURE_OPENAI_ENDPOINT      — Azure OpenAI endpoint (embeddings)
#   AZURE_OPENAI_EMBEDDING_DEPLOYMENT — text-embedding-3-large
#   AZURE_OPENAI_EMBEDDING_DIMENSIONS — 1024
#   AZURE_OPENAI_DEPLOYMENT_HQA       — gpt-4o-mini (reserved for future
#                                        HQA on delta pages, not used yet)
#   REDIS_URL                  — Azure Cache for Redis connection string
#   FRESHNESS_MODE             — "report" or "apply"
#                                (can also pass via --mode CLI flag)
#
# Trigger (ADO pipeline or Azure Scheduler):
#
#   az containerapp job start \
#       --name aria-freshness-job \
#       --resource-group <rg> \
#       --env-vars FRESHNESS_MODE=apply
#
# Job run order (nightly — automated):
#   aria-freshness-job only. Runs independently every night.
#
# Job run order (manual full re-index — when explicitly triggered):
#   1. aria-scraper-job      → full scrape → Blob JSON
#   2. aria-indexer-job      → full index  → AI Search
#   3. aria-freshness-job    → skipped (or run report mode)
#   The freshness job is NOT part of the manual full re-index flow.
#
# IMPORTANT — first run after deploying v5.2.0 of indexer:
#   The full re-index (--full on chunk_and_index_hqaV3.py) must
#   complete before the freshness job runs for the first time.
#   This refreshes stored hashes from MD5 (old) → SHA-256 (new).
#   After that first re-index the nightly job works correctly.

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — July 2026 | Mukesh Kund
         Initial production version.

         ARCHITECTURE:
         Two-mode design: --mode report (safe, read-only) and
         --mode apply (automated writes to index + cache).
         Azure Blob Storage as source of truth for approved URLs
         (Excel) and hash state backup (JSON).
         Targeted Redis cache invalidation (key-level, not full
         flush) — preserves valid cached answers for unchanged pages.

         DROPDOWN PAGES (scraper v4.4.0+):
         Pages with routing <select> elements produce synthetic
         URLs (base_url#policy=option_value). Health checks use
         base URL only. When base URL changes or is removed, ALL
         #policy= variant chunks are deleted from the index.

         HASH ALIGNMENT (chunk_and_index_hqaV3.py v5.2.0):
         SHA-256 of content after clean_content() external URL
         stripping. Matches scraper v4.4.0 and indexer v5.2.0
         exactly — same algorithm, same input.

         FUNCTIONS (28 total):
         get_credential, get_openai_client, get_search_client,
         get_redis, get_blob_service,
         load_approved_excel_from_blob, load_approved_excel_from_local,
         load_hash_state, save_hash_state, upload_report_to_blob,
         load_urls_from_excel_bytes, load_urls_from_excel_file,
         normalise_url, is_dropdown_url, get_base_url,
         clean_content, compute_content_hash,
         check_single_url, check_all_urls_health,
         fetch_current_hashes_from_index, get_chunk_ids_for_url,
         delete_chunks_for_urls, invalidate_cache_for_urls,
         derive_section, map_category_to_content_type,
         scrape_url_with_dropdowns, scrape_urls_batch,
         chunk_page, get_embeddings_batch, index_pages,
         build_report, run_freshness_job, main

v1.1.0 — July 2026 | Mukesh Kund
         Atomic chunking for dropdown state pages — aligned with
         chunk_and_index_hqaV4.py v5.4.0.

         chunk_page() (delta indexing path) now applies the same
         dropdown_state atomic chunking rule as chunk_pages() in
         the full indexer. Both chunking functions must behave
         identically — if a dropdown state page is re-indexed via
         the nightly freshness job, it must produce the same single
         atomic chunk as the full index run. Inconsistency between
         the two paths would cause chunk count mismatches and
         potentially split policy+contact content on freshness runs
         even if the full index run was correct.

         Signal: page.get("dropdown_state") non-empty string.
         URL pattern not used — see chunk_and_index_hqaV4.py v5.4.0
         changelog for full reasoning.

v1.2.0 — July 2026 | Mukesh Kund
         Playwright-based dropdown scraping in freshness script.
         Aligned with scrape_approved_urls_updatedV4.py v4.5.0.

         PROBLEM WITH v1.0.0/v1.1.0 DROPDOWN APPROACH:
         scrape_url_with_dropdowns() used crawl4ai JS injection
         (arun() calls with wait_until="networkidle") to detect
         and iterate dropdown options — same approach that caused
         dropdown_detect_failed timeouts in the scraper (v4.4.0).
         Additionally, page_timeout values inside the dropdown
         detection blocks were 30000ms — wrong, should be 45000ms.

         FIX — Replace crawl4ai dropdown handling with Playwright:
         scrape_url_with_dropdowns() now uses BeautifulSoup to
         detect <select> elements in the already-fetched crawl4ai
         HTML (zero extra network call), then calls
         _scrape_dropdown_states_playwright() via ThreadPoolExecutor
         — identical to scrape_approved_urls_updatedV4.py v4.5.0.

         Base page timeout (line 1133): kept at 30000ms —
         wait_until="domcontentloaded", no networkidle issue.

         NEW FUNCTIONS (ported from scraper v4.5.0):
         - _DROPDOWN_PLACEHOLDERS: placeholder option text set
         - _scrape_dropdown_states_playwright(): Playwright thread
           mirroring crawler.py v0.1.0 exactly — single page load,
           DOM detection, JS event injection, 1.5s wait, line diff.

         PLAYWRIGHT EXECUTABLE PATH:
         Same VDI fix as scraper v4.5.0 — PLAYWRIGHT_EXECUTABLE_PATH
         constant reads env var (default: system Chrome on Windows),
         passed as executable_path to pw.chromium.launch(). Falls
         back to None on Linux/production containers.

         REMOVED:
         - _JS_DETECT_DROPDOWNS, _JS_GET_BODY_TEXT JS constants
         - crawl4ai arun() dropdown detection blocks inside
           scrape_url_with_dropdowns()
         - Stale _JS_DETECT_DROPDOWNS comment in config section

═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import pickle
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import concurrent.futures
import structlog
from dotenv import find_dotenv, load_dotenv

# ── Load .env — override=True ensures .env always wins over shell vars
_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path, override=True)
log = structlog.get_logger()

# ── Azure SDK ─────────────────────────────────────────────────
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from openai import AzureOpenAI

# ── Optional dependencies (guarded — fail at call time, not import) ──
try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    _OPENPYXL_AVAILABLE = True
except ImportError:
    _OPENPYXL_AVAILABLE = False

try:
    from crawl4ai import AsyncWebCrawler, CacheMode
    from crawl4ai.async_configs import BrowserConfig, CrawlerRunConfig
    _CRAWL4AI_AVAILABLE = True
except ImportError:
    _CRAWL4AI_AVAILABLE = False

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    _LANGCHAIN_AVAILABLE = False


# ══════════════════════════════════════════════════════════════
# CONFIG — mirrors chunk_and_index_hqaV3.py constants exactly
# ══════════════════════════════════════════════════════════════

EXPECTED_DOMAIN          = "royallondon.com"

# Azure AI Search
SEARCH_ENDPOINT          = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
INDEX_NAME               = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v3")

# Azure OpenAI
AZURE_OPENAI_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT     = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")
EMBEDDING_DIMS           = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1024"))

# Azure Blob Storage
BLOB_STORAGE_CONNECTION  = os.getenv("AZURE_STORAGE_CONNECTION", "")
BLOB_CONTAINER_NAME      = os.getenv("BLOB_CONTAINER_NAME", "scraper-data")
BLOB_APPROVED_EXCEL_NAME = os.getenv(
    "BLOB_APPROVED_EXCEL_NAME",
    "approved-urls/Approved_URLs.xlsx",
)
BLOB_HASH_STATE_NAME     = os.getenv(
    "BLOB_HASH_STATE_NAME",
    "freshness/content_hashes.json",
)
BLOB_REPORT_PREFIX       = os.getenv("BLOB_REPORT_PREFIX", "freshness/reports/")

# Redis
REDIS_URL                = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_CACHE_KEY_PREFIX   = "rlg:cache:"

# Chunking — must match chunk_and_index_hqaV3.py exactly
CHUNK_SIZE               = 1600
CHUNK_OVERLAP            = 200

# Upload batch sizes — match indexer
UPLOAD_BATCH_SIZE        = 100
EMBEDDING_BATCH_SIZE     = 50

# HTTP health check
HTTP_CONCURRENCY         = 15
HTTP_TIMEOUT_SECONDS     = 12

# Scraping concurrency — capped at 3 for container memory safety
SCRAPE_CONCURRENCY       = 3

# Local output dir for reports
LOCAL_DATA_DIR           = Path("scraper/data")

# Section map — mirrors scrape_approved_urls_updatedV3.py
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

# Excel Category → content_type — mirrors scraper v4.3.0+
EXCEL_CATEGORY_MAP = {
    "brand":    "article",
    "guidance": "guide",
    "other":    "article",
    "product":  "product",
    "tool":     "tool",
}

# Excel column header sets for auto-detection
URL_HEADERS      = {"url", "page url", "link", "webpage", "web page", "web url"}
TITLE_HEADERS    = {"title", "page title", "name"}
CATEGORY_HEADERS = {"category", "content category", "page category", "type"}

# ── Playwright browser executable path ─────────────────────────
# Mirrors PLAYWRIGHT_EXECUTABLE_PATH in scrape_approved_urls_updatedV4.py.
# VDI/corporate SSL restriction blocks Playwright chromium download.
# Use system Chrome instead — set PLAYWRIGHT_EXECUTABLE_PATH in .env.
#
# VDI (Windows):
#   PLAYWRIGHT_EXECUTABLE_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
#
# Production (Azure Container Apps — Linux):
#   Set in Key Vault or leave unset (container image installs chromium).
#
# TODO (DevOps): ensure Dockerfile includes:
#   RUN playwright install chromium --with-deps
_PLAYWRIGHT_DEFAULT_WIN    = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PLAYWRIGHT_EXECUTABLE_PATH = os.getenv("PLAYWRIGHT_EXECUTABLE_PATH", _PLAYWRIGHT_DEFAULT_WIN)

# ── Singleton clients ─────────────────────────────────────────
_credential:    Optional[DefaultAzureCredential] = None
_openai_client: Optional[AzureOpenAI]            = None
_redis_client                                    = None


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
            get_credential(),
            "https://cognitiveservices.azure.com/.default",
        )
        _openai_client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            azure_ad_token_provider=token_provider,
            api_version="2024-12-01-preview",
        )
    return _openai_client


def get_search_client() -> SearchClient:
    return SearchClient(
        endpoint=SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=get_credential(),
    )


def get_redis():
    """Get Redis client. Returns None if unavailable — all callers handle gracefully."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        pool = redis.ConnectionPool.from_url(
            REDIS_URL,
            decode_responses=False,
            socket_connect_timeout=3,
            socket_timeout=3,
            max_connections=5,
        )
        client = redis.Redis(connection_pool=pool)
        client.ping()
        _redis_client = client
        log.info("redis_connected", url=REDIS_URL)
        return _redis_client
    except Exception as e:
        log.warning("redis_unavailable", error=str(e))
        return None


# ══════════════════════════════════════════════════════════════
# BLOB STORAGE HELPERS
# ══════════════════════════════════════════════════════════════

def get_blob_service():
    if not BLOB_STORAGE_CONNECTION:
        raise EnvironmentError(
            "AZURE_STORAGE_CONNECTION is not set. "
            "Set it in .env or use --file for local mode."
        )
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient.from_connection_string(BLOB_STORAGE_CONNECTION)


def load_approved_excel_from_blob(blob_name: str | None = None) -> bytes:
    """Download approved URL Excel from Blob Storage. Returns raw bytes."""
    name   = blob_name or BLOB_APPROVED_EXCEL_NAME
    svc    = get_blob_service()
    client = svc.get_blob_client(container=BLOB_CONTAINER_NAME, blob=name)
    data   = client.download_blob().readall()
    log.info("excel_downloaded_from_blob", blob=name, size_bytes=len(data))
    return data


def load_hash_state() -> dict[str, str]:
    """
    Load previous run's hash state.

    Production: from Blob (freshness/content_hashes.json).
    Local:      returns empty dict (index hashes are authoritative).
    First run:  returns empty dict — all URLs treated as new.
    """
    if BLOB_STORAGE_CONNECTION:
        try:
            svc    = get_blob_service()
            client = svc.get_blob_client(
                container=BLOB_CONTAINER_NAME,
                blob=BLOB_HASH_STATE_NAME,
            )
            data  = client.download_blob().readall()
            state = json.loads(data)
            log.info("hash_state_loaded", count=len(state), source="blob")
            return state
        except Exception as e:
            log.info("hash_state_not_found", error=str(e), reason="first_run_or_missing")
            return {}
    return {}


def save_hash_state(state: dict[str, str]) -> None:
    """
    Persist updated hash state after successful apply run.

    Production: upload to Blob (overwrites previous).
    Local:      write to scraper/data/content_hashes.json.
    """
    data = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
    if BLOB_STORAGE_CONNECTION:
        svc    = get_blob_service()
        client = svc.get_blob_client(
            container=BLOB_CONTAINER_NAME,
            blob=BLOB_HASH_STATE_NAME,
        )
        client.upload_blob(data, overwrite=True)
        log.info("hash_state_saved", count=len(state), dest="blob")
    else:
        dest = LOCAL_DATA_DIR / "content_hashes.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        log.info("hash_state_saved", count=len(state), dest=str(dest))


def upload_report_to_blob(report_path: Path) -> str | None:
    """Upload Excel report to Blob for archival. Non-fatal on failure."""
    if not BLOB_STORAGE_CONNECTION:
        return None
    try:
        blob_name = BLOB_REPORT_PREFIX + report_path.name
        svc       = get_blob_service()
        client    = svc.get_blob_client(container=BLOB_CONTAINER_NAME, blob=blob_name)
        with open(report_path, "rb") as f:
            client.upload_blob(f, overwrite=True)
        log.info("report_uploaded_to_blob", blob=blob_name)
        return blob_name
    except Exception as e:
        log.warning("report_blob_upload_failed", error=str(e))
        return None


# ══════════════════════════════════════════════════════════════
# EXCEL PARSING
# ══════════════════════════════════════════════════════════════

def load_urls_from_excel_bytes(data: bytes) -> list[dict]:
    """
    Parse approved URL Excel from raw bytes.
    Header-based column detection — no hardcoded positions.
    Handles any column layout the dedicated team produces.
    """
    if not _OPENPYXL_AVAILABLE:
        raise RuntimeError("openpyxl not installed. pip install openpyxl")

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active

    url_idx = title_idx = category_idx = None
    entries = []

    for row in ws.iter_rows(values_only=True):
        if url_idx is None:
            for col_idx, cell in enumerate(row):
                if cell is None:
                    continue
                val = str(cell).strip().lower()
                if val in URL_HEADERS:
                    url_idx = col_idx
                elif val in TITLE_HEADERS:
                    title_idx = col_idx
                elif val in CATEGORY_HEADERS:
                    category_idx = col_idx
            continue  # header row — skip to data

        if url_idx is None:
            continue

        url = str(row[url_idx]).strip() if row[url_idx] else ""
        if not url or not url.startswith("http"):
            continue

        entries.append({
            "url":      url,
            "title":    str(row[title_idx]).strip() if (title_idx is not None and row[title_idx]) else "",
            "category": str(row[category_idx]).strip() if (category_idx is not None and row[category_idx]) else "",
        })

    wb.close()
    return entries


def load_urls_from_excel_file(file_path: str) -> list[dict]:
    """Load approved URLs from a local Excel file."""
    with open(file_path, "rb") as f:
        return load_urls_from_excel_bytes(f.read())


def normalise_url(url: str) -> str:
    """Lowercase + strip trailing slash for consistent comparison."""
    return url.strip().lower().rstrip("/")


def is_dropdown_url(url: str) -> bool:
    """Return True if URL is a synthetic dropdown state URL (#policy=...)."""
    return "#policy=" in url


def get_base_url(url: str) -> str:
    """Return base URL, stripping any #policy= fragment."""
    return url.split("#policy=")[0] if "#policy=" in url else url


# ══════════════════════════════════════════════════════════════
# CONTENT HASHING
# Must match chunk_and_index_hqaV3.py v5.2.0 exactly:
#   SHA-256 of content AFTER clean_content() external URL stripping
# ══════════════════════════════════════════════════════════════

def clean_content(text: str) -> str:
    """
    Remove external URLs from scraped content.

    COPIED from chunk_and_index_hqaV3.py — must stay in sync.
    This is the SAME transformation the indexer applies before
    computing content_hash. Using the same function here ensures
    the hash computed by the freshness script from tonight's
    scrape matches the hash stored in the index from the last
    full index run.

    Keeps royallondon.com URLs (citation system depends on them).
    Strips all other external URLs and markdown links to them.
    """
    def replace_markdown_link(match):
        anchor_text = match.group(1)
        url         = match.group(2)
        if "royallondon.com" in url:
            return match.group(0)
        return anchor_text

    text = re.sub(
        r'\[([^\]]+)\]\((https?://[^\)]+)\)',
        replace_markdown_link,
        text,
    )

    def replace_raw_url(match):
        url = match.group(0)
        if "royallondon.com" in url:
            return url
        return ""

    text = re.sub(
        r'https?://[^\s\)\]"\'<>,]+',
        replace_raw_url,
        text,
    )

    text = re.sub(r'  +', ' ', text)
    return text.strip()


def compute_content_hash(content: str) -> str:
    """
    SHA-256 of content after clean_content() external URL stripping.

    Must match chunk_and_index_hqaV3.py v5.2.0 compute_content_hash()
    exactly. Any divergence causes every URL to appear changed on
    every nightly run (hash mismatch even for unchanged content).

    Input:  raw scraped markdown (before clean_content)
    Steps:  1. clean_content() — strip external URLs
            2. .strip()        — normalise whitespace
            3. SHA-256
    """
    cleaned = clean_content(content).strip()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════
# URL HEALTH CHECK
# ══════════════════════════════════════════════════════════════

async def check_single_url(
    session: aiohttp.ClientSession,
    entry:   dict,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> dict:
    """
    Async HEAD check for a single URL.

    Dropdown synthetic URLs (#policy=...) are NOT HTTP-checked —
    the fragment is client-side only. get_base_url() is called
    before the HEAD request; the result records the base URL as
    the health-checked URL.

    Statuses:
      live              — 200, no redirect
      internal_redirect — redirected within royallondon.com
      external_redirect — redirected off royallondon.com
      dead_404          — HTTP 404
      dead_5xx          — HTTP 5xx
      timeout           — no response within timeout
      error_*           — other exception
    """
    original_url = entry["url"]
    check_url    = get_base_url(original_url)  # strip #policy= fragment

    result = {
        "url":           original_url,
        "check_url":     check_url,
        "title":         entry.get("title", ""),
        "category":      entry.get("category", ""),
        "status":        "unknown",
        "status_code":   None,
        "final_url":     check_url,
        "redirect_note": "",
        "is_dropdown":   is_dropdown_url(original_url),
    }

    try:
        async with session.head(
            check_url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "ARIA-FreshnessBot/1.0 (internal)"},
        ) as resp:
            result["status_code"] = resp.status
            final_url = str(resp.url)
            result["final_url"] = final_url

            if resp.status == 200:
                if normalise_url(final_url) != normalise_url(check_url):
                    final_domain = urlparse(final_url).netloc.lower()
                    if EXPECTED_DOMAIN in final_domain:
                        result["status"]        = "internal_redirect"
                        result["redirect_note"] = f"Redirects to: {final_url}"
                    else:
                        result["status"]        = "external_redirect"
                        result["redirect_note"] = f"Off-domain redirect: {final_url}"
                else:
                    result["status"] = "live"
            elif resp.status == 404:
                result["status"] = "dead_404"
            elif resp.status >= 500:
                result["status"] = "dead_5xx"
            else:
                result["status"] = f"unexpected_{resp.status}"

    except asyncio.TimeoutError:
        result["status"] = "timeout"
    except Exception as e:
        result["status"] = f"error_{type(e).__name__}"

    return result


async def check_all_urls_health(
    entries:     list[dict],
    concurrency: int = HTTP_CONCURRENCY,
    timeout:     int = HTTP_TIMEOUT_SECONDS,
) -> list[dict]:
    """
    Concurrent HEAD checks for all approved URLs.

    Dropdown #policy= URLs share the same base URL — we deduplicate
    before checking so each base URL is only HEAD-checked once, then
    the result is applied to all its #policy= variants.
    """
    # Deduplicate by base URL for health checking
    seen_base:    dict[str, dict] = {}  # base_url -> first entry
    base_to_orig: dict[str, list[dict]] = {}  # base_url -> all entries sharing it

    for entry in entries:
        base = get_base_url(entry["url"])
        if base not in seen_base:
            seen_base[base]    = {**entry, "url": base}
            base_to_orig[base] = []
        base_to_orig[base].append(entry)

    unique_entries = list(seen_base.values())

    sem = asyncio.Semaphore(concurrency)

    async def _bounded(e):
        async with sem:
            return await check_single_url(session, e, timeout)

    connector   = aiohttp.TCPConnector(limit=concurrency, ssl=False)
    timeout_cfg = aiohttp.ClientTimeout(total=timeout + 5)

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout_cfg,
    ) as session:
        base_results = await asyncio.gather(
            *[_bounded(e) for e in unique_entries],
            return_exceptions=True,
        )

    # Build base_url -> health result map
    health_map: dict[str, dict] = {}
    for entry, result in zip(unique_entries, base_results):
        base = entry["url"]
        if isinstance(result, Exception):
            health_map[base] = {
                **entry,
                "status":        f"error_{type(result).__name__}",
                "status_code":   None,
                "final_url":     base,
                "redirect_note": str(result),
                "is_dropdown":   False,
            }
        else:
            health_map[base] = result

    # Expand results back to all original entries (including #policy= variants)
    all_results = []
    for entry in entries:
        base   = get_base_url(entry["url"])
        health = health_map.get(base, {})
        all_results.append({
            **health,
            "url":         entry["url"],
            "title":       entry.get("title", ""),
            "category":    entry.get("category", ""),
            "is_dropdown": is_dropdown_url(entry["url"]),
        })

    return all_results


# ══════════════════════════════════════════════════════════════
# AZURE AI SEARCH — READ
# ══════════════════════════════════════════════════════════════

def fetch_current_hashes_from_index() -> dict[str, str]:
    """
    Read stored content_hash for every indexed URL.

    Requires content_hash to be retrievable=True in the index
    schema (chunk_and_index_hqaV3.py v5.2.0+). Returns empty
    dict if field is not retrievable or index unreachable.

    One hash per URL — all chunks of the same page share the same
    content_hash (computed from page content before chunking).
    We take the first non-empty hash seen per URL.

    Returns: dict mapping normalised URL → SHA-256 hash string.
    """
    client = get_search_client()
    hashes: dict[str, str] = {}
    skip      = 0
    page_size = 1000

    try:
        while True:
            results = client.search(
                search_text="*",
                select=["source_url", "content_hash"],
                top=page_size,
                skip=skip,
            )
            batch = list(results)
            if not batch:
                break

            for r in batch:
                url  = r.get("source_url", "")
                h    = r.get("content_hash", "")
                norm = normalise_url(url)
                if url and h and norm not in hashes:
                    hashes[norm] = h

            if len(batch) < page_size:
                break
            skip += page_size

        log.info("index_hashes_fetched", url_count=len(hashes))
        return hashes

    except Exception as e:
        log.error("fetch_hashes_failed", error=str(e))
        return {}


def get_chunk_ids_for_url(url: str) -> list[str]:
    """
    Return all chunk_ids in the index belonging to a given URL.

    Handles both real URLs and synthetic #policy= URLs by using
    exact source_url match. source_url is SearchableField so we
    use a filter on equality.
    """
    client    = get_search_client()
    chunk_ids: list[str] = []
    skip      = 0
    page_size = 1000

    # Escape single quotes in URL for OData filter
    escaped = url.replace("'", "''")

    try:
        while True:
            results = client.search(
                search_text="*",
                filter=f"source_url eq '{escaped}'",
                select=["chunk_id"],
                top=page_size,
                skip=skip,
            )
            batch = list(results)
            if not batch:
                break
            chunk_ids.extend(r["chunk_id"] for r in batch if r.get("chunk_id"))
            if len(batch) < page_size:
                break
            skip += page_size

    except Exception as e:
        log.warning("get_chunk_ids_failed", url=url, error=str(e))

    return chunk_ids


# ══════════════════════════════════════════════════════════════
# AZURE AI SEARCH — WRITE
# ══════════════════════════════════════════════════════════════

def delete_chunks_for_urls(urls: list[str], dry_run: bool = False) -> dict[str, int]:
    """
    Delete ALL index chunks for the given URLs.

    For base URLs that have dropdown variants (#policy=...) stored
    in the index, the caller must pass both the base URL and all its
    known #policy= URLs. get_all_urls_to_delete() handles this.

    Returns: dict mapping URL → chunks deleted count.
    """
    if not urls:
        return {}

    client  = get_search_client()
    summary: dict[str, int] = {}

    for url in urls:
        chunk_ids = get_chunk_ids_for_url(url)
        if not chunk_ids:
            summary[url] = 0
            continue

        if dry_run:
            log.info("dry_run_would_delete", url=url, chunks=len(chunk_ids))
            summary[url] = len(chunk_ids)
            continue

        deleted = 0
        for i in range(0, len(chunk_ids), UPLOAD_BATCH_SIZE):
            batch   = [{"chunk_id": cid} for cid in chunk_ids[i:i + UPLOAD_BATCH_SIZE]]
            results = client.delete_documents(documents=batch)
            deleted += sum(1 for r in results if r.succeeded)

        summary[url] = deleted
        log.info("chunks_deleted", url=url, deleted=deleted, total=len(chunk_ids))

    return summary


def get_all_urls_to_delete(base_urls: list[str]) -> list[str]:
    """
    For a list of base URLs, find ALL URLs in the index that derive
    from them — including #policy= dropdown variants.

    We scan the index source_url field. Any URL that starts with a
    base_url (before the # fragment) is included.

    Returns deduplicated list of all URLs to delete.
    """
    if not base_urls:
        return []

    client    = get_search_client()
    to_delete = set(base_urls)

    # Fetch all unique source_urls from the index
    indexed_urls: set[str] = set()
    skip      = 0
    page_size = 1000

    try:
        while True:
            results = client.search(
                search_text="*",
                select=["source_url"],
                top=page_size,
                skip=skip,
            )
            batch = list(results)
            if not batch:
                break
            for r in batch:
                u = r.get("source_url", "")
                if u:
                    indexed_urls.add(u)
            if len(batch) < page_size:
                break
            skip += page_size
    except Exception as e:
        log.warning("get_all_urls_scan_failed", error=str(e))
        return list(to_delete)

    # Add any #policy= variants whose base URL is in our delete list
    norm_bases = {normalise_url(u) for u in base_urls}
    for indexed_url in indexed_urls:
        base = get_base_url(indexed_url)
        if normalise_url(base) in norm_bases:
            to_delete.add(indexed_url)

    log.info(
        "delete_urls_resolved",
        base_count=len(base_urls),
        total_with_variants=len(to_delete),
    )
    return list(to_delete)


# ══════════════════════════════════════════════════════════════
# REDIS CACHE — TARGETED INVALIDATION
# ══════════════════════════════════════════════════════════════

def invalidate_cache_for_urls(urls: list[str], dry_run: bool = False) -> int:
    """
    Remove Redis cache entries referencing any of the given URLs.

    Inspects each rlg:cache:* key's stored response for source_url
    references. Deletes only keys that cite a changed/removed URL.
    Preserves valid cached answers for unchanged pages.

    Returns: number of cache keys invalidated.
    """
    redis_client = get_redis()
    if not redis_client:
        log.warning("cache_invalidation_skipped", reason="redis_unavailable")
        return 0

    if not urls:
        return 0

    norm_urls   = {normalise_url(u) for u in urls}
    # Also match base URLs for any #policy= variant being removed
    norm_bases  = {normalise_url(get_base_url(u)) for u in urls}
    all_norms   = norm_urls | norm_bases

    invalidated = 0

    try:
        keys = redis_client.keys(f"{REDIS_CACHE_KEY_PREFIX}*")
        if not keys:
            return 0

        for key in keys:
            try:
                data = redis_client.get(key)
                if not data:
                    continue

                entry    = pickle.loads(data)
                response = entry.get("response", {})
                cited: set[str] = set()

                if isinstance(response, dict):
                    for citation in response.get("citations", []):
                        if isinstance(citation, dict) and citation.get("url"):
                            cited.add(normalise_url(get_base_url(citation["url"])))
                    for source in response.get("sources", []):
                        if isinstance(source, dict) and source.get("url"):
                            cited.add(normalise_url(get_base_url(source["url"])))
                    if response.get("source_url"):
                        cited.add(normalise_url(get_base_url(response["source_url"])))

                if isinstance(response, str):
                    for u in all_norms:
                        if u in response.lower():
                            cited.add(u)

                if cited & all_norms:
                    if not dry_run:
                        redis_client.delete(key)
                    invalidated += 1
                    log.info(
                        "cache_key_invalidated",
                        key=key.decode() if isinstance(key, bytes) else key,
                        dry_run=dry_run,
                    )

            except Exception as e:
                # Corrupt entry — remove it
                if not dry_run:
                    try:
                        redis_client.delete(key)
                    except Exception:
                        pass
                log.warning("corrupt_cache_entry", key=str(key), error=str(e))

    except Exception as e:
        log.error("cache_invalidation_error", error=str(e))

    log.info("cache_invalidation_done", invalidated=invalidated, dry_run=dry_run)
    return invalidated


# ══════════════════════════════════════════════════════════════
# SCRAPING — mirrors scrape_approved_urls_updatedV4.py v4.5.0
# ══════════════════════════════════════════════════════════════

# Dropdown placeholder option text — skip these during detection.
# Mirrors _DROPDOWN_PLACEHOLDERS in scrape_approved_urls_updatedV4.py.
_DROPDOWN_PLACEHOLDERS = {
    "select...", "select", "please select", "--", "choose...",
    "choose", "please choose",
}


def _has_routing_dropdowns_in_html(html: str) -> bool:
    """
    Check rendered HTML for routing <select> elements.

    Uses BeautifulSoup on already-fetched crawl4ai HTML — zero
    extra network call. Returns True only if a <select> has more
    than one non-placeholder <option>.

    Ported from scrape_approved_urls_updatedV4.py v4.5.0.
    """
    if not html:
        return False
    try:
        from bs4 import BeautifulSoup
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
    except Exception as e:
        log.warning("dropdown_html_check_failed", error=str(e))
        return False


def _scrape_dropdown_states_playwright(
    url:            str,
    base_title:     str,
    base_page_data: dict,
) -> list[dict]:
    """
    Scrape per-option content from a routing dropdown page using Playwright.

    Ported from scrape_approved_urls_updatedV4.py v4.5.0 —
    mirrors _scrape_dropdown_states_playwright() exactly.

    Runs synchronously — called via asyncio ThreadPoolExecutor
    from scrape_url_with_dropdowns() to avoid blocking crawl4ai
    event loop.

    Single page load → detect <select> from live DOM → per-option
    JS event injection → 1.5s DOM wait → body text diff → extract
    only changed lines (phone number, address, hours).
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error(
            "playwright_not_installed",
            note="pip install playwright && playwright install chromium",
        )
        return []

    results: list[dict] = []

    try:
        with sync_playwright() as pw:
            # Use system Chrome if available (VDI/corporate SSL restriction).
            # Falls back to None (Playwright finds its own chromium) in production.
            import os as _os
            _exec    = PLAYWRIGHT_EXECUTABLE_PATH
            _exec_arg = _exec if _exec and _os.path.exists(_exec) else None
            browser = pw.chromium.launch(
                headless=True,
                executable_path=_exec_arg,
            )
            try:
                page = browser.new_page()
                page.route(
                    "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,eot}",
                    lambda route: route.abort(),
                )

                log.info("playwright_navigating", url=url)
                page.goto(url, wait_until="networkidle", timeout=45000)

                try:
                    page.wait_for_selector(
                        "main, article, [role='main']",
                        timeout=10000,
                    )
                except PWTimeout:
                    pass

                # Detect routing dropdowns from live DOM
                selects          = page.query_selector_all("select")
                routing_dropdowns = []

                for select in selects:
                    options    = select.query_selector_all("option")
                    valid_opts = []
                    for opt in options:
                        text  = opt.inner_text().strip()
                        value = opt.get_attribute("value") or ""
                        if text and text.lower() not in _DROPDOWN_PLACEHOLDERS:
                            valid_opts.append({"value": value, "text": text})
                    if len(valid_opts) > 1:
                        routing_dropdowns.append({
                            "select_element": select,
                            "options":        valid_opts,
                        })

                if not routing_dropdowns:
                    log.info("playwright_no_dropdowns_found", url=url)
                    return []

                log.info(
                    "playwright_dropdowns_detected",
                    url=url,
                    dropdown_count=len(routing_dropdowns),
                )

                # Capture default body text for diffing
                default_raw_text = page.inner_text("body")
                default_lines    = {
                    line.strip()
                    for line in default_raw_text.split("\n")
                    if line.strip()
                }

                for dropdown in routing_dropdowns:
                    select_el = dropdown["select_element"]
                    options   = dropdown["options"]

                    for option in options:
                        opt_value = option["value"]
                        opt_text  = option["text"]

                        try:
                            select_el.evaluate(
                                """(el, optText) => {
                                    const options = Array.from(el.options);
                                    const target = options.find(
                                        o => o.text.trim() === optText
                                    );
                                    if (target) {
                                        el.value = target.value;
                                        el.dispatchEvent(
                                            new Event('input', { bubbles: true })
                                        );
                                        el.dispatchEvent(
                                            new Event('change', { bubbles: true })
                                        );
                                    }
                                }""",
                                opt_text,
                            )

                            time.sleep(1.5)

                            new_raw_text  = page.inner_text("body")
                            new_lines     = [
                                line.strip()
                                for line in new_raw_text.split("\n")
                                if line.strip()
                            ]
                            changed_lines = [
                                line for line in new_lines
                                if line not in default_lines
                            ]
                            dynamic_content = "\n".join(changed_lines)

                            if not dynamic_content or len(dynamic_content.strip()) < 20:
                                log.warning(
                                    "playwright_option_no_change",
                                    url=url,
                                    option=opt_text,
                                )
                                continue

                            safe_value = opt_value if opt_value else opt_text
                            state_url  = f"{url}#policy={safe_value}"
                            content    = dynamic_content.strip()

                            results.append({
                                "url":              state_url,
                                "title":            f"{base_title} — {opt_text}",
                                "section":          base_page_data["section"],
                                "content":          content,
                                "scraped_at":       datetime.now(timezone.utc).isoformat(),
                                "content_length":   len(content),
                                "content_hash":     hashlib.sha256(
                                    content.encode("utf-8")
                                ).hexdigest(),
                                "audience":         base_page_data.get("audience", "general"),
                                "has_video":        base_page_data.get("has_video", False),
                                "content_type":     base_page_data.get("content_type", "article"),
                                "product_category": base_page_data.get("product_category", "general"),
                                "description":      base_page_data.get("description", ""),
                                "thumbnail_url":    base_page_data.get("thumbnail_url", ""),
                                "publish_date":     base_page_data.get("publish_date", ""),
                                "collection_name":  base_page_data.get("collection_name", ""),
                                "read_time_mins":   max(1, len(content.split()) // 200),
                                "dropdown_state":   opt_text,
                                "dropdown_value":   opt_value or "",
                            })

                            log.info(
                                "playwright_option_scraped",
                                url=state_url,
                                option=opt_text,
                                chars=len(content),
                            )

                        except Exception as e:
                            log.warning(
                                "playwright_option_error",
                                url=url,
                                option=opt_text,
                                error=str(e),
                            )
                            continue

            finally:
                browser.close()

    except Exception as e:
        log.error("playwright_dropdown_scrape_error", url=url, error=str(e))

    return results



def derive_section(url: str) -> str:
    """Derive section from first URL path segment. Mirrors scraper v3."""
    try:
        path          = url.split("://", 1)[-1]
        path          = path.split("/", 1)[-1]
        first_segment = path.split("/")[0].lower()
        return SECTION_MAP.get(first_segment, "General")
    except Exception:
        return "General"


def map_category_to_content_type(category: str) -> str:
    """Map Excel Category → content_type. Mirrors scraper v4.3.0+."""
    return EXCEL_CATEGORY_MAP.get(category.strip().lower(), "article")


def _diff_dropdown_content(default_lines: set[str], new_text: str) -> str:
    """
    Return only lines in new_text absent from default_lines.
    Mirrors _diff_dropdown_content in scrape_approved_urls_updatedV3.py.
    """
    changed = [
        line.strip()
        for line in new_text.split("\n")
        if line.strip() and line.strip() not in default_lines
    ]
    return "\n".join(changed)


async def scrape_url_with_dropdowns(entry: dict) -> list[dict] | None:
    """
    Scrape a URL and any routing dropdown states it contains.

    Mirrors scrape_page() + _scrape_dropdown_states() from
    scrape_approved_urls_updatedV3.py v4.4.0 exactly.

    Returns:
        list[dict]  — [base_page, dropdown_state_1, ...] or [base_page]
        None        — scrape failed entirely
    """
    if not _CRAWL4AI_AVAILABLE:
        raise RuntimeError(
            "crawl4ai is not installed. "
            "pip install crawl4ai && playwright install chromium"
        )

    url      = entry["url"]
    title    = entry.get("title", "")
    category = entry.get("category", "")

    browser_cfg = BrowserConfig(headless=True, verbose=False)

    # ── Base page scrape ──────────────────────────────────────
    run_cfg = CrawlerRunConfig(
        css_selector=(
            "main, article, .content, #content, "
            ".page-content, .main-content, [role='main']"
        ),
        wait_until="domcontentloaded",
        page_timeout=30000,
        verbose=False,
        excluded_tags=["nav", "header", "footer", "aside", "script", "style", "noscript"],
    )

    try:
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(url=url, config=run_cfg)

            if not result.success or not result.markdown:
                log.warning("scrape_failed", url=url)
                return None

            raw_content = result.markdown.raw_markdown
            if not raw_content or len(raw_content.strip()) < 100:
                log.warning("content_too_short", url=url)
                return None

            # Apply scraper's clean_content (UI noise removal) then our
            # clean_content (external URL stripping) for hash computation.
            # Note: scraper applies its own clean_content; we replicate
            # the external-URL-stripping step here for hash consistency.
            page_content = raw_content.strip()

            # content_hash: SHA-256 after external URL stripping
            # This matches exactly what chunk_and_index_hqaV3.py stores.
            content_hash = compute_content_hash(page_content)

            scraped_at = datetime.now(timezone.utc).isoformat()

            base_page = {
                "url":              url,
                "title":            title,
                "section":          derive_section(url),
                "content":          page_content,
                "scraped_at":       scraped_at,
                "content_length":   len(page_content),
                "content_hash":     content_hash,
                "has_video":        False,
                "content_type":     map_category_to_content_type(category),
                "product_category": "general",
                "description":      "",
                "thumbnail_url":    "",
                "publish_date":     "",
                "collection_name":  "",
                "read_time_mins":   max(1, len(page_content.split()) // 200),
                "audience":         "general",
                # dropdown metadata — empty for base page
                "dropdown_state":   "",
                "dropdown_value":   "",
            }

            # ── Dropdown detection (v1.2.0) ───────────────────
            # BeautifulSoup check on already-fetched HTML — zero
            # extra network call. If dropdowns found, Playwright
            # handles option iteration (mirrors scraper v4.5.0).
            dropdown_states: list[dict] = []
            raw_html = getattr(result, "html", "") or ""

            if _has_routing_dropdowns_in_html(raw_html):
                log.info("dropdown_page_detected_via_html", url=url)
                try:
                    loop     = asyncio.get_event_loop()
                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    dropdown_states = await loop.run_in_executor(
                        executor,
                        _scrape_dropdown_states_playwright,
                        url,
                        title,
                        base_page,
                    )
                except Exception as e:
                    log.warning(
                        "playwright_dropdown_skipped",
                        url=url,
                        error=str(e),
                    )
                    dropdown_states = []

            pages = [base_page] + dropdown_states
            if dropdown_states:
                log.info(
                    "multi_state_page_scraped",
                    url=url,
                    dropdown_states=len(dropdown_states),
                )
            return pages

    except Exception as e:
        log.error("scrape_exception", url=url, error=str(e))
        return None


async def scrape_urls_batch(
    entries:     list[dict],
    concurrency: int = SCRAPE_CONCURRENCY,
) -> list[list[dict] | None]:
    """
    Scrape multiple URLs with limited concurrency.
    concurrency=3: crawl4ai browser memory safety in containers.
    Returns list aligned with entries — None for failed scrapes.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(entry):
        async with sem:
            return await scrape_url_with_dropdowns(entry)

    return await asyncio.gather(
        *[_bounded(e) for e in entries],
        return_exceptions=False,
    )


# ══════════════════════════════════════════════════════════════
# CHUNKING + EMBEDDING + INDEXING
# ══════════════════════════════════════════════════════════════

def chunk_page(page: dict) -> list[dict]:
    """
    Split a page into index-ready chunks.

    Mirrors chunk_pages() in chunk_and_index_hqaV4.py v5.4.0:
    - Same CHUNK_SIZE / CHUNK_OVERLAP
    - Same separator hierarchy
    - Same external URL stripping (clean_content) before hashing
    - Title prepended to chunk content
    - HQA fields left empty (delta indexing — speed over HQA quality)
    - v1.1.0: Atomic chunking for dropdown state pages

    v1.1.0 — ATOMIC CHUNKING FOR DROPDOWN STATE PAGES:
    If page.get("dropdown_state") is non-empty, this page is a
    dropdown state entry (one per policy option). Produce exactly
    1 chunk — no splitting — to guarantee policy context (title)
    and contact details (content) are always in the same chunk.
    Mirrors chunk_pages() v5.4.0 behaviour exactly.

    TODO: add --hqa flag if HQA on delta pages becomes a requirement.
    """
    if not _LANGCHAIN_AVAILABLE:
        raise RuntimeError("langchain_text_splitters not installed.")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    raw_content = page.get("content", "").strip()
    if not raw_content or len(raw_content) < 50:
        return []

    # Apply external URL stripping — same as indexer does pre-chunking
    content = clean_content(raw_content)
    if len(content) < 50:
        return []

    title              = page.get("title", "")
    content_with_title = f"{title}\n\n{content}" if title else content

    # v1.1.0 — ATOMIC CHUNKING: dropdown state pages get exactly 1 chunk.
    # dropdown_state is the authoritative signal — not the URL pattern.
    # Mirrors chunk_pages() in chunk_and_index_hqaV4.py v5.4.0 exactly.
    is_dropdown_state = bool(page.get("dropdown_state", ""))

    if is_dropdown_state:
        if len(content_with_title.strip()) < 50:
            return []
        log.info(
            "dropdown_atomic_chunk",
            url=page.get("url", ""),
            dropdown_state=page.get("dropdown_state"),
            chars=len(content_with_title),
        )
        return [{
            "chunk_id":            str(uuid.uuid4()),
            "content":             content_with_title.strip(),
            "source_url":          page["url"],
            "title":               title,
            "section":             page.get("section", "General"),
            "audience":            page.get("audience", "general"),
            "scraped_at":          page.get("scraped_at", ""),
            "chunk_index":         0,
            "total_chunks":        1,
            "content_hash":        page.get("content_hash", ""),
            "has_video":           page.get("has_video", False),
            "content_type":        page.get("content_type", "article"),
            "product_category":    page.get("product_category", "general"),
            "description":         page.get("description", ""),
            "thumbnail_url":       page.get("thumbnail_url", ""),
            "publish_date":        page.get("publish_date", ""),
            "collection_name":     page.get("collection_name", ""),
            "read_time_mins":      page.get("read_time_mins", 1),
            "augmented_questions": "",
            "title_questions":     "",
        }]

    # Standard page — normal splitting
    splits = splitter.split_text(content_with_title)
    total  = len(splits)
    chunks = []

    for idx, split in enumerate(splits):
        if len(split.strip()) < 50:
            continue
        chunks.append({
            "chunk_id":            str(uuid.uuid4()),
            "content":             split.strip(),
            "source_url":          page["url"],
            "title":               title,
            "section":             page.get("section", "General"),
            "audience":            page.get("audience", "general"),
            "scraped_at":          page.get("scraped_at", ""),
            "chunk_index":         idx,
            "total_chunks":        total,
            "content_hash":        page.get("content_hash", ""),
            "has_video":           page.get("has_video", False),
            "content_type":        page.get("content_type", "article"),
            "product_category":    page.get("product_category", "general"),
            "description":         page.get("description", ""),
            "thumbnail_url":       page.get("thumbnail_url", ""),
            "publish_date":        page.get("publish_date", ""),
            "collection_name":     page.get("collection_name", ""),
            "read_time_mins":      page.get("read_time_mins", 1),
            # HQA fields — empty for delta indexing
            "augmented_questions": "",
            "title_questions":     "",
        })

    return chunks


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """
    Generate embeddings with batch size + retry matching indexer.
    EMBEDDING_BATCH_SIZE=50 for S0 TPM safety.
    Exponential backoff on 429 RateLimitError.
    """
    from openai import RateLimitError

    client     = get_openai_client()
    all_embeds: list[list[float]] = []

    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i:i + EMBEDDING_BATCH_SIZE]

        for attempt in range(6):
            try:
                resp = client.embeddings.create(
                    model=EMBEDDING_DEPLOYMENT,
                    input=batch,
                    dimensions=EMBEDDING_DIMS,
                )
                all_embeds.extend([e.embedding for e in resp.data])
                break
            except RateLimitError:
                wait = 10 * (2 ** attempt)
                log.warning("rate_limit_hit", attempt=attempt + 1, wait_seconds=wait)
                time.sleep(wait)
        else:
            raise RuntimeError(f"Embedding failed after 6 attempts (batch {i})")

        if i + EMBEDDING_BATCH_SIZE < len(texts):
            time.sleep(2)

    return all_embeds


def index_pages(pages: list[dict], dry_run: bool = False) -> int:
    """
    Chunk, embed, and upload pages. Returns total chunks uploaded.
    """
    if not pages:
        return 0

    all_chunks: list[dict] = []
    for page in pages:
        all_chunks.extend(chunk_page(page))

    if not all_chunks:
        log.warning("no_chunks_produced", page_count=len(pages))
        return 0

    if dry_run:
        log.info("dry_run_would_index", chunk_count=len(all_chunks))
        return len(all_chunks)

    embedding_texts = [c["content"] for c in all_chunks]
    embeddings      = get_embeddings_batch(embedding_texts)

    client    = get_search_client()
    documents = [
        {**chunk, "embedding": emb}
        for chunk, emb in zip(all_chunks, embeddings)
    ]

    uploaded = 0
    for i in range(0, len(documents), UPLOAD_BATCH_SIZE):
        batch   = documents[i:i + UPLOAD_BATCH_SIZE]
        results = client.upload_documents(documents=batch)
        uploaded += sum(1 for r in results if r.succeeded)

    log.info("chunks_indexed", uploaded=uploaded, total=len(documents))
    return uploaded


# ══════════════════════════════════════════════════════════════
# REPORT GENERATION
# ══════════════════════════════════════════════════════════════

def build_report(
    scan_results: list[dict],
    run_summary:  dict,
    output_path:  Path,
) -> Path:
    """
    Produce 4-sheet Excel report.

    Sheet 1 — Full Results    : every URL with action + counts
    Sheet 2 — Action Required : only URLs with pending changes
    Sheet 3 — Summary         : counts + run metadata
    Sheet 4 — Removed URLs    : URLs removed from index this run

    Colour coding:
      Green  — live, unchanged
      Yellow — changed (re-indexed)
      Blue   — new (added)
      Red    — removed (404 / 5xx / de-listed)
      Orange — internal redirect (treated as removed)
      Grey   — external redirect (treated as removed)
    """
    if not _OPENPYXL_AVAILABLE:
        log.warning("openpyxl_unavailable", note="Report skipped.")
        return output_path

    C = {
        "unchanged":    "D4EDDA",
        "changed":      "FFF3CD",
        "new":          "CCE5FF",
        "removed":      "F8D7DA",
        "int_redirect": "FFE0B2",
        "ext_redirect": "E0E0E0",
        "header":       "2C3E50",
    }

    ACTION_COLOUR = {
        "unchanged":           C["unchanged"],
        "new":                 C["new"],
        "changed":             C["changed"],
        "removed_404":         C["removed"],
        "removed_5xx":         C["removed"],
        "removed_delisted":    C["removed"],
        "removed_int_redir":   C["int_redirect"],
        "removed_ext_redir":   C["ext_redirect"],
        "scrape_failed":       C["removed"],
        "pending_content_check": C["changed"],
    }

    HEADERS = [
        "URL", "Title", "Category", "Dropdown?",
        "HTTP Status", "Action",
        "Chunks Before", "Chunks After",
        "Cache Keys Invalidated", "Notes",
    ]

    wb = Workbook()

    # ── Sheet 1: Full Results ──────────────────────────────────
    ws1 = wb.active
    ws1.title = "Full Results"
    ws1.append(HEADERS)
    _style_header_row(ws1, 1, C["header"])

    for r in scan_results:
        colour = ACTION_COLOUR.get(r.get("action", ""), "FFFFFF")
        ws1.append([
            r.get("url", ""),
            r.get("title", ""),
            r.get("category", ""),
            "Yes" if r.get("is_dropdown") else "No",
            r.get("status_code", ""),
            r.get("action", ""),
            r.get("chunks_before", 0),
            r.get("chunks_after", 0),
            r.get("cache_invalidated", 0),
            r.get("notes", ""),
        ])
        _fill_row(ws1, ws1.max_row, colour)
    _auto_width(ws1)

    # ── Sheet 2: Action Required ───────────────────────────────
    ws2 = wb.create_sheet("Action Required")
    ws2.append(HEADERS)
    _style_header_row(ws2, 1, C["header"])
    for r in scan_results:
        if r.get("action", "unchanged") != "unchanged":
            ws2.append([
                r.get("url", ""), r.get("title", ""), r.get("category", ""),
                "Yes" if r.get("is_dropdown") else "No",
                r.get("status_code", ""), r.get("action", ""),
                r.get("chunks_before", 0), r.get("chunks_after", 0),
                r.get("cache_invalidated", 0), r.get("notes", ""),
            ])
    _auto_width(ws2)

    # ── Sheet 3: Summary ───────────────────────────────────────
    ws3 = wb.create_sheet("Summary")
    mode   = run_summary.get("mode", "report")
    run_ts = run_summary.get("run_at", "")

    for row_data in [
        ["ARIA Content Freshness Report", ""],
        ["Run At (UTC)",          run_ts],
        ["Mode",                  mode.upper()],
        ["Index",                 INDEX_NAME],
        ["", ""],
        ["APPROVED URLS", ""],
        ["Total in Excel",        run_summary.get("total_approved", 0)],
        ["", ""],
        ["URL HEALTH", ""],
        ["Live (unchanged)",      run_summary.get("live_unchanged", 0)],
        ["Live (content changed)", run_summary.get("changed", 0)],
        ["New (not yet indexed)", run_summary.get("new", 0)],
        ["Dead (404)",            run_summary.get("dead_404", 0)],
        ["Dead (5xx)",            run_summary.get("dead_5xx", 0)],
        ["Internal redirect",     run_summary.get("internal_redirect", 0)],
        ["External redirect",     run_summary.get("external_redirect", 0)],
        ["De-listed",             run_summary.get("delisted", 0)],
        ["Scrape failed",         run_summary.get("scrape_failed", 0)],
        ["", ""],
        ["INDEX CHANGES", ""],
        ["Chunks added",          run_summary.get("chunks_added", 0)],
        ["Chunks deleted",        run_summary.get("chunks_deleted", 0)],
        ["Cache keys invalidated", run_summary.get("cache_invalidated", 0)],
        ["", ""],
        ["POLICY NOTES", ""],
        ["Internal redirects",  "Treated as removed — new URL must be added to Excel."],
        ["De-listed URLs",      "Removed from index — still live but not in approved Excel."],
        ["Dropdown variants",   "All #policy= URLs deleted when base URL changes/removed."],
        ["HQA questions",       "Not generated for delta-indexed pages (speed). Full re-index for HQA."],
    ]:
        ws3.append(row_data)
    _auto_width(ws3)

    # ── Sheet 4: Removed from Index ───────────────────────────
    ws4 = wb.create_sheet("Removed from Index")
    ws4.append(["URL", "Title", "Reason", "Chunks Deleted", "Cache Keys Invalidated"])
    _style_header_row(ws4, 1, C["header"])
    removed_actions = {
        "removed_404", "removed_5xx", "removed_delisted",
        "removed_int_redir", "removed_ext_redir", "scrape_failed",
    }
    for r in scan_results:
        if r.get("action") in removed_actions:
            ws4.append([
                r.get("url", ""),
                r.get("title", ""),
                r.get("notes", ""),
                r.get("chunks_before", 0),
                r.get("cache_invalidated", 0),
            ])
            _fill_row(ws4, ws4.max_row, C["removed"])
    _auto_width(ws4)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    log.info("report_saved", path=str(output_path))
    return output_path


def _style_header_row(ws, row_num: int, bg_hex: str):
    fill = PatternFill("solid", fgColor=bg_hex)
    font = Font(bold=True, color="FFFFFF")
    for cell in ws[row_num]:
        cell.fill      = fill
        cell.font      = font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)


def _fill_row(ws, row_num: int, bg_hex: str):
    fill = PatternFill("solid", fgColor=bg_hex)
    for cell in ws[row_num]:
        cell.fill = fill


def _auto_width(ws, max_width: int = 70):
    for col in ws.columns:
        max_len    = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 4, max_width)


# ══════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

def run_freshness_job(
    mode:      str        = "report",
    file_path: str | None = None,
    blob_name: str | None = None,
    dry_run:   bool       = False,
) -> dict:
    """
    Full orchestration: scan → classify → act or report.

    Args:
        mode:      "report" — produce report, no writes.
                   "apply"  — execute all changes.
        file_path: Local Excel path — bypasses Blob (dev/CI use).
        blob_name: Override BLOB_APPROVED_EXCEL_NAME for one-off run.
        dry_run:   No index or cache writes even in apply mode.

    Returns: dict with run summary stats + output_report path.
    """
    run_at = datetime.now(timezone.utc)
    ts_str = run_at.strftime("%Y%m%d_%H%M%S")

    result = {
        "success":           False,
        "mode":              mode,
        "run_at":            run_at.isoformat(),
        "total_approved":    0,
        "live_unchanged":    0,
        "changed":           0,
        "new":               0,
        "dead_404":          0,
        "dead_5xx":          0,
        "internal_redirect": 0,
        "external_redirect": 0,
        "delisted":          0,
        "scrape_failed":     0,
        "chunks_added":      0,
        "chunks_deleted":    0,
        "cache_invalidated": 0,
        "output_report":     "",
        "error":             "",
    }

    print("\n" + "=" * 65)
    print(f"   ARIA Content Freshness Manager — {mode.upper()}")
    print(f"   Run at: {run_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if dry_run:
        print("   ⚠️  DRY RUN — no index or cache writes")
    print("=" * 65 + "\n")

    try:
        # ── Step 1: Load approved URLs ─────────────────────────
        print("📋 Step 1: Loading approved URLs from Excel...")
        if file_path:
            entries = load_urls_from_excel_file(file_path)
            print(f"   {len(entries):,} URLs from local file: {file_path}")
        else:
            excel_bytes = load_approved_excel_from_blob(blob_name)
            entries     = load_urls_from_excel_bytes(excel_bytes)
            print(f"   {len(entries):,} URLs from Blob: {blob_name or BLOB_APPROVED_EXCEL_NAME}")

        if not entries:
            raise ValueError("No URLs found in approved Excel — aborting.")

        result["total_approved"] = len(entries)
        approved_norm = {normalise_url(e["url"]) for e in entries}
        # For de-listing check, we only care about base URLs in the Excel
        approved_norm_base = {normalise_url(get_base_url(e["url"])) for e in entries}

        # ── Step 2: Load previous hash state ──────────────────
        print("\n💾 Step 2: Loading previous hash state...")
        hash_state = load_hash_state()
        print(f"   {len(hash_state):,} URLs in hash state.")

        # ── Step 3: Fetch current hashes from index ────────────
        print("\n🔍 Step 3: Reading content_hash from AI Search index...")
        index_hashes = fetch_current_hashes_from_index()
        print(f"   {len(index_hashes):,} indexed URLs found in '{INDEX_NAME}'.")

        if not index_hashes:
            print("   ⚠️  No hashes returned — check content_hash is retrievable=True")
            print("      in chunk_and_index_hqaV3.py (requires v5.2.0+ and re-index).")

        # ── Step 4: Health check all URLs ─────────────────────
        print(f"\n🌐 Step 4: Health-checking {len(entries):,} URLs...")
        health_results = asyncio.run(check_all_urls_health(entries))
        health_by_norm = {normalise_url(r["url"]): r for r in health_results}
        live_count     = sum(1 for r in health_results if r["status"] == "live")
        print(f"   {live_count:,} live / {len(entries) - live_count:,} not-live.")

        # ── Step 5: Detect de-listed URLs ─────────────────────
        print("\n📊 Step 5: Detecting de-listed URLs...")
        # URLs in index (base URLs) that are NOT in the approved Excel
        indexed_base_norm = {
            normalise_url(get_base_url(u.replace("://", "").split("/")[0] + "dummy"))
            if "#policy=" not in u else normalise_url(get_base_url(u))
            for u in index_hashes
        }
        # Simpler: just get all indexed source_urls and compare base
        all_indexed_norms = set(index_hashes.keys())
        delisted_norm = {
            normalise_url(get_base_url(u))
            for u in all_indexed_norms
            if normalise_url(get_base_url(u)) not in approved_norm_base
            and not is_dropdown_url(u)  # dropdown variants handled via base
        }
        print(f"   {len(delisted_norm):,} de-listed base URLs (in index, not in Excel).")

        # ── Step 6: Classify each approved URL ────────────────
        print("\n🗂️  Step 6: Classifying actions...")
        scan_results:   list[dict]  = []
        urls_to_scrape: list[dict]  = []  # entries needing scrape
        base_urls_to_delete: list[str] = []  # base URLs confirmed for deletion

        for entry in entries:
            norm   = normalise_url(entry["url"])
            health = health_by_norm.get(norm, {"status": "unknown", "status_code": None})
            status = health.get("status", "unknown")

            base = {
                "url":               entry["url"],
                "title":             entry.get("title", ""),
                "category":          entry.get("category", ""),
                "is_dropdown":       is_dropdown_url(entry["url"]),
                "status_code":       health.get("status_code"),
                "chunks_before":     0,
                "chunks_after":      0,
                "cache_invalidated": 0,
                "notes":             "",
            }

            if status in ("dead_404", "dead_5xx"):
                short = status.replace("dead_", "")
                base.update({
                    "action": f"removed_{short}",
                    "notes":  f"HTTP {health.get('status_code','N/A')} — removing from index.",
                })
                base_urls_to_delete.append(get_base_url(entry["url"]))
                result[status] += 1

            elif status == "internal_redirect":
                base.update({
                    "action": "removed_int_redir",
                    "notes":  health.get("redirect_note", "Internal redirect — not followed."),
                })
                base_urls_to_delete.append(get_base_url(entry["url"]))
                result["internal_redirect"] += 1

            elif status == "external_redirect":
                base.update({
                    "action": "removed_ext_redir",
                    "notes":  health.get("redirect_note", "External redirect — removing."),
                })
                base_urls_to_delete.append(get_base_url(entry["url"]))
                result["external_redirect"] += 1

            elif status == "live":
                stored_hash = index_hashes.get(norm) or hash_state.get(norm, "")

                if norm not in index_hashes:
                    base.update({
                        "action": "new",
                        "notes":  "New URL — will be scraped and indexed.",
                    })
                    urls_to_scrape.append(entry)
                    result["new"] += 1
                else:
                    # Mark as pending — resolved after scraping
                    base.update({
                        "action":        "pending_content_check",
                        "notes":         "Live — content hash check pending.",
                        "_stored_hash":  stored_hash,
                    })
                    urls_to_scrape.append(entry)

            else:
                # Timeout / unknown — skip to avoid false removal
                base.update({
                    "action": "unchanged",
                    "notes":  f"Health check: {status} — skipped (no false removal).",
                })
                result["live_unchanged"] += 1

            scan_results.append(base)

        # Add de-listed URLs
        for norm_base in delisted_norm:
            scan_results.append({
                "url":               norm_base,
                "title":             "",
                "category":          "",
                "is_dropdown":       False,
                "status_code":       "N/A",
                "action":            "removed_delisted",
                "chunks_before":     0,
                "chunks_after":      0,
                "cache_invalidated": 0,
                "notes":             "Removed from approved Excel — de-indexing.",
            })
            base_urls_to_delete.append(norm_base)
            result["delisted"] += 1

        # ── Step 7: Scrape pending URLs ────────────────────────
        scraped_pages:    list[dict] = []  # pages to index (new + changed)
        changed_base_urls: list[str] = []  # base URLs of changed pages → delete old first

        if urls_to_scrape:
            print(f"\n🕷️  Step 7: Scraping {len(urls_to_scrape):,} URLs...")
            scrape_results = asyncio.run(scrape_urls_batch(urls_to_scrape))

            for entry, pages in zip(urls_to_scrape, scrape_results):
                norm = normalise_url(entry["url"])
                record = next(
                    (r for r in scan_results if normalise_url(r["url"]) == norm),
                    None,
                )

                if pages is None:
                    if record:
                        record["action"] = "scrape_failed"
                        record["notes"]  = "Scrape returned no content — skipped."
                    result["scrape_failed"] += 1
                    continue

                # pages is list[dict]: [base_page, *dropdown_states]
                base_page    = pages[0]
                live_hash    = base_page["content_hash"]
                stored_hash  = (record or {}).get("_stored_hash", "")

                if stored_hash and live_hash == stored_hash:
                    if record:
                        record["action"] = "unchanged"
                        record["notes"]  = "Content hash matches — no change."
                    result["live_unchanged"] += 1
                else:
                    # Changed or new — queue all pages (base + dropdown states)
                    if record:
                        if record.get("action") == "pending_content_check":
                            record["action"] = "changed"
                            record["notes"]  = "Content hash mismatch — re-indexing."
                            result["changed"] += 1
                    scraped_pages.extend(pages)
                    # If previously indexed, queue base URL for deletion
                    # (get_all_urls_to_delete will find #policy= variants too)
                    if norm in index_hashes:
                        changed_base_urls.append(get_base_url(entry["url"]))

            print(
                f"   Scraped {len(urls_to_scrape)}  |  "
                f"Changed/New: {len(scraped_pages)}  |  "
                f"Unchanged: {result['live_unchanged']}"
            )
        else:
            print("\n   Step 7: No URLs to scrape.")

        # ── Step 8: Apply changes ──────────────────────────────
        all_base_to_delete = list(set(base_urls_to_delete + changed_base_urls))

        if mode == "apply":
            print("\n⚡ Step 8: Applying changes...")

            if all_base_to_delete:
                # Resolve all URLs to delete including #policy= variants
                all_urls_to_delete = get_all_urls_to_delete(all_base_to_delete)
                print(
                    f"   Deleting chunks for {len(all_base_to_delete):,} base URLs "
                    f"({len(all_urls_to_delete):,} total incl. dropdown variants)..."
                )
                deletion_summary = delete_chunks_for_urls(all_urls_to_delete, dry_run=dry_run)
                total_deleted    = sum(deletion_summary.values())
                result["chunks_deleted"] += total_deleted
                print(f"   Deleted {total_deleted:,} chunks.")

                print(f"   Invalidating cache for {len(all_urls_to_delete):,} URLs...")
                cache_count = invalidate_cache_for_urls(all_urls_to_delete, dry_run=dry_run)
                result["cache_invalidated"] += cache_count
                print(f"   Invalidated {cache_count:,} cache keys.")

                # Update scan records with deletion counts
                for url, count in deletion_summary.items():
                    base_norm = normalise_url(get_base_url(url))
                    for r in scan_results:
                        if normalise_url(get_base_url(r["url"])) == base_norm:
                            r["chunks_before"]     = r.get("chunks_before", 0) + count
                            r["cache_invalidated"] = cache_count

            if scraped_pages:
                print(f"   Indexing {len(scraped_pages):,} pages...")
                chunks_added = index_pages(scraped_pages, dry_run=dry_run)
                result["chunks_added"] += chunks_added
                print(f"   Indexed {chunks_added:,} chunks.")

                # Update scan records with chunk counts
                chunk_counts: dict[str, int] = {}
                for page in scraped_pages:
                    base_norm = normalise_url(get_base_url(page["url"]))
                    chunk_counts[base_norm] = chunk_counts.get(base_norm, 0) + len(chunk_page(page))
                for r in scan_results:
                    base_norm = normalise_url(get_base_url(r["url"]))
                    if base_norm in chunk_counts:
                        r["chunks_after"] = chunk_counts[base_norm]

            # Save updated hash state
            if not dry_run:
                new_hash_state = dict(hash_state)
                for page in scraped_pages:
                    new_hash_state[normalise_url(page["url"])] = page["content_hash"]
                for url in all_base_to_delete:
                    new_hash_state.pop(normalise_url(url), None)
                save_hash_state(new_hash_state)
                print(f"   Hash state saved ({len(new_hash_state):,} URLs).")

        else:
            print("\n   Step 8: Report mode — no index writes.")

        # ── Step 9: Generate report ────────────────────────────
        print("\n📊 Step 9: Generating Excel report...")
        report_name = f"freshness_report_{mode}_{ts_str}.xlsx"
        report_path = LOCAL_DATA_DIR / report_name

        build_report(
            scan_results=scan_results,
            run_summary={**result, "mode": mode},
            output_path=report_path,
        )
        result["output_report"] = str(report_path)
        print(f"   Report: {report_path}")

        blob_report = upload_report_to_blob(report_path)
        if blob_report:
            print(f"   Report uploaded to Blob: {blob_report}")

        result["success"] = True

        # ── Final summary ──────────────────────────────────────
        print("\n" + "=" * 65)
        print("   RUN COMPLETE")
        print("=" * 65)
        print(f"   Total approved URLs  : {result['total_approved']:,}")
        print(f"   Unchanged            : {result['live_unchanged']:,}")
        print(f"   New                  : {result['new']:,}")
        print(f"   Changed              : {result['changed']:,}")
        print(f"   Removed (404/5xx)    : {result['dead_404'] + result['dead_5xx']:,}")
        print(f"   Redirected           : {result['internal_redirect'] + result['external_redirect']:,}")
        print(f"   De-listed            : {result['delisted']:,}")
        print(f"   Scrape failed        : {result['scrape_failed']:,}")
        if mode == "apply":
            print(f"   Chunks added         : {result['chunks_added']:,}")
            print(f"   Chunks deleted       : {result['chunks_deleted']:,}")
            print(f"   Cache invalidated    : {result['cache_invalidated']:,}")
        print("=" * 65 + "\n")

    except Exception as e:
        result["error"] = str(e)
        log.error("freshness_job_failed", error=str(e), traceback=traceback.format_exc())
        print(f"\n❌ FATAL ERROR: {e}\n")

    return result


# ══════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description=(
            "ARIA Content Freshness Manager\n"
            "Nightly job — detects changed/removed URLs and keeps\n"
            "Azure AI Search index + Redis cache in sync.\n\n"
            "  report mode: Read-only scan + Excel report (safe).\n"
            "  apply mode:  Scan + execute all index/cache changes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["report", "apply"],
        default=os.getenv("FRESHNESS_MODE", "report"),
        help="report = scan only (default). apply = execute changes.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Local Excel path. Bypasses Blob (local dev / CI).",
    )
    parser.add_argument(
        "--blob-name",
        default=None,
        help="Override Blob Excel path for this run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config + connectivity. No writes (even in apply mode).",
    )
    args = parser.parse_args()

    # Validate required env vars
    if args.mode == "apply" and not args.dry_run:
        missing = []
        if not SEARCH_ENDPOINT:
            missing.append("AZURE_SEARCH_ENDPOINT")
        if not AZURE_OPENAI_ENDPOINT:
            missing.append("AZURE_OPENAI_ENDPOINT")
        if not args.file and not BLOB_STORAGE_CONNECTION:
            missing.append("AZURE_STORAGE_CONNECTION (or use --file for local)")
        if missing:
            print(f"\n❌ Missing env vars for apply mode:\n   {', '.join(missing)}\n")
            sys.exit(1)

    result = run_freshness_job(
        mode=args.mode,
        file_path=args.file,
        blob_name=args.blob_name,
        dry_run=args.dry_run,
    )
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()