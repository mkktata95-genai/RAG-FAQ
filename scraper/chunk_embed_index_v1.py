"""
Royal London FAQ — Chunk, Embed, Index (v1 — standalone, no HQA)
════════════════════════════════════════════════════════════════════
NEW, SEPARATE indexer generation. Rebuilt from chunk_and_index_hqaV5.py
(kept untouched as reference/rollback — never modified by this file).
Pairs with scrape_approved_urls_httpV1.py, which is this script's only
expected upstream input format.

WHY A NEW FILE (not a v5.11.0 patch):
  1. HQA is finalized as fully dropped for this MVP (project decision,
     not "hold flat"). V5 carried ~1,500 lines of HQA/title_questions
     generation, validation, deduplication and cost-tracking logic —
     all now dead weight. Stripped entirely, not left dormant behind
     a flag: no --no-hqa / --pilot, because there is no HQA to opt out
     of. augmented_questions / title_questions schema fields, the
     rl-retrieval-profile scoring profile, and their semantic-config
     entries are all removed.
  2. Single index target. V5 split HQA-on vs HQA-off into two indexes
     (primary vs baseline) for A/B comparison. That distinction is
     meaningless now — one INDEX_NAME only. (A future standby/mirror
     index for failover is a content_freshness-side concern, not this
     script's.)
  3. Fully standalone — ZERO imports of other project files. V5
     imported element_detector.py (table/header-aware chunking,
     shared with content_freshness.py) and, in its cache-clear step,
     core/cache.py. Both are inlined or dropped here:
       - element_detector.py's chunk_content_element_aware() logic is
         inlined verbatim below (prefixed _ea_), same pattern
         content_freshnessV1.py already uses for the same reason: this
         script must never break because an unrelated file changed.
       - The post-`--full` Redis cache-clear step (core/cache.py) is
         DROPPED — that's a query-time concern belonging to the other
         team's deployment (core/), which won't exist in this
         repo/deployment. Clearing the cache after a reindex is now a
         separate, explicit step for whoever needs it.
     core/embeddings.py (the query-time team's shared embedding
     client) is likewise not imported — this file's embed_chunks()
     has its own inline Azure OpenAI client, same model/dims/auth
     pattern, so query-time and index-time embeddings stay identical
     in practice even with the deployments separated.

WHAT CARRIES OVER FROM V5 (unchanged logic):
  clean_content() — external URL stripping.
  compute_content_hash() / compute_chunk_id() — SHA-256, deterministic
    chunk IDs (self-healing on re-run, no duplicate-chunk risk).
  chunk_pages() — dropdown_state atomic-chunk detection, element-aware
    chunking for standard pages, URL dedup guard, all versioning
    fields (pipeline_version, scrape_run_id, index_run_id, indexed_at,
    refresh_count, scraper_version, metadata_version), all v3.0.0
    enrichment fields, dropdown_url (dropdown-chunk identity field;
    source_url is always the clean, navigable base page URL).
  Index schema — all non-HQA fields unchanged, including the
  chunk-duplication root-cause fix (deterministic chunk_id) and the
  content_hash retrievable=True / SHA-256 fix content_freshness.py
  depends on.

WHAT'S NEW:
  video_url — SimpleField, passthrough from scraper v1.1.0's
    video_url field (same pattern as page_image_url). "" when
    has_video is False or no matching iframe was found.

WHAT'S FIXED:
  find_latest_scraped_file() — V5's glob pattern
  "royal_london_faq_approved_*.json" also matches the httpV1
  scraper's new "..._failures.json" sidecar file, which is written
  with a NEWER mtime than the main output (right after it) and has a
  completely different structure ([{"url","reason"}], not page
  dicts) — chunk_pages() would crash if that file got picked as
  "latest". Now explicitly excludes any filename ending
  "_failures.json".
  (page_image_url None-safety is NOT needed here — the scraper itself
  now sets the placeholder to "" instead of None at the source, so
  chunk_pages()'s existing page.get("page_image_url", "") default
  already works correctly. See scrape_approved_urls_httpV1.py.)

THREE REUSABLE ENTRY POINTS (segregated so other code — e.g. a future
content-freshness script — can call any one independently instead of
only the full run_pipeline()):
  chunk_pages(pages)                      -> chunks
  embed_chunks(chunks)                    -> embeddings
  index_chunks(chunks, embeddings, fresh) -> uploaded_count
  run_pipeline() composes them: load -> chunk_pages -> embed_chunks
  -> index_chunks.

═══════════════════════════════════════════════════════════════
USAGE
═══════════════════════════════════════════════════════════════

    # Full re-index (delete + recreate index)
    python chunk_embed_index_v1.py --full

    # Only index pages not already in the index (default)
    python chunk_embed_index_v1.py --new-only

    # Validate config + chunking, no index changes, no uploads
    python chunk_embed_index_v1.py --full --dry-run

    # Custom scraped-file path
    python chunk_embed_index_v1.py --full --file path/to/file.json

Programmatic:
    from chunk_embed_index_v1 import run_pipeline
    result = run_pipeline(mode="full")

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
    Fresh, standalone rebuild of chunk_and_index_hqaV5.py. HQA fully
    removed (not deferred). Single index target. video_url field
    added. find_latest_scraped_file() no longer picks up the
    scraper's *_failures.json sidecar. element_detector.py's
    chunking logic inlined — zero cross-file project imports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import structlog
from azure.core.exceptions import (
    ClientAuthenticationError, HttpResponseError, ResourceNotFoundError,
    ServiceRequestError, ServiceResponseError,
)
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import APIConnectionError, APITimeoutError, AzureOpenAI, InternalServerError, RateLimitError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from dotenv import load_dotenv, find_dotenv

_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path, override=True)
log = structlog.get_logger()

# ═══════════════════════════════════════════════════════════════
# Versioning
# ═══════════════════════════════════════════════════════════════
# Developer-bumped only. Bump when chunking logic, embedding model/
# dims, or index schema changes — requires --full reindex.
PIPELINE_VERSION = "1.0.0"

# ═══════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════
SCRAPED_FILE          = "scraper/data/royal_london_faq_approved_httpV1_latest.json"
CHUNK_SIZE            = int(os.getenv("CHUNK_SIZE", "1600"))
CHUNK_OVERLAP         = int(os.getenv("CHUNK_OVERLAP", "200"))
INDEX_NAME            = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v5")
# Guard prefix — --full deletes INDEX_NAME outright. If
# AZURE_SEARCH_INDEX_NAME is mistyped/stale in .env or Key Vault,
# --full would silently wipe whatever that name points to. This is
# a cheap sanity check, not a full allowlist (V5's two-index
# CURRENT_TARGETS guard doesn't apply now there's only one index) —
# it just refuses to run --full against a name that clearly isn't
# one of ours.
_INDEX_NAME_GUARD_PREFIX = "rlg-faq-index"
EMBEDDING_DIMS        = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1536"))
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
SEARCH_ENDPOINT       = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT  = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")
SEMANTIC_CONFIG_NAME  = os.getenv("AZURE_SEARCH_SEMANTIC_CONFIG", "rlg-semantic-config")
EMBEDDING_BATCH_SIZE  = int(os.getenv("EMBEDDING_BATCH_SIZE", "50"))
UPLOAD_BATCH_SIZE     = 100

# Safety gate for --full: refuse to delete + rebuild an index that already
# holds >= MIN_DOCS_FOR_SHRINK_GATE documents when the rebuild would hold
# fewer than INDEX_MIN_REBUILD_RATIO of them (a truncated/old scraped JSON,
# a partial scrape). Override with --allow-shrink.
INDEX_MIN_REBUILD_RATIO = float(os.getenv("INDEX_MIN_REBUILD_RATIO", "0.5"))
MIN_DOCS_FOR_SHRINK_GATE = 20

# ═══════════════════════════════════════════════════════════════
# Resilience helpers — retry with exponential backoff + jitter, and a
# transient-vs-permanent error classifier. Duplicated inline in every
# pipeline script on purpose (zero cross-file imports); a fix here must
# be mirrored by hand into scrape_approved_urls_httpV1.py and
# content_freshness_httpV1.py.
#
# Policy: transient failures (network, timeouts, throttling, 5xx, token
# acquisition blips) are retried and every attempt is logged. Permanent
# failures, or transient ones that exhaust their retries, are logged at
# ERROR and RAISED — never swallowed — so a run fails loudly instead of
# leaving the index half-built with no trace.
# ═══════════════════════════════════════════════════════════════
RETRY_ATTEMPTS = int(os.environ.get("PIPELINE_RETRY_ATTEMPTS", "5"))
RETRY_BASE_SECONDS = float(os.environ.get("PIPELINE_RETRY_BASE_SECONDS", "2"))
RETRY_MAX_SECONDS = 60.0
TRANSIENT_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def is_transient_error(exc: BaseException) -> bool:
    """True for errors worth retrying: network/timeout, throttling, 5xx, token-acquisition blips."""
    if isinstance(exc, (
        ServiceRequestError, ServiceResponseError, ClientAuthenticationError,
        RateLimitError, APIConnectionError, APITimeoutError, InternalServerError,
        TimeoutError, ConnectionError,
        requests.exceptions.ConnectionError, requests.exceptions.Timeout,
    )):
        return True
    if isinstance(exc, HttpResponseError):
        return getattr(exc, "status_code", None) in TRANSIENT_HTTP_STATUS
    return False


def with_retry(fn, *, op: str, attempts: int | None = None, base_seconds: float | None = None, **ctx):
    """
    Call fn() with retry on transient errors (exponential backoff + jitter).
    Every retry is logged at WARNING; a permanent error, or exhausted
    retries, is logged at ERROR and re-raised. ctx is attached to every log line.
    """
    attempts = attempts or RETRY_ATTEMPTS
    base = RETRY_BASE_SECONDS if base_seconds is None else base_seconds
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            transient = is_transient_error(e)
            if not transient or attempt >= attempts:
                log.error("operation_failed", op=op, attempt=attempt, attempts=attempts,
                          transient=transient, error_type=type(e).__name__, error=str(e), **ctx)
                raise
            wait = min(RETRY_MAX_SECONDS, base * (2 ** (attempt - 1))) * random.uniform(0.75, 1.25)
            log.warning("operation_retry", op=op, attempt=attempt, attempts=attempts,
                        wait_seconds=round(wait, 1), error_type=type(e).__name__, error=str(e), **ctx)
            time.sleep(wait)

if os.getenv("CHUNK_SIZE") or os.getenv("CHUNK_OVERLAP"):
    log.warning(
        "chunk_dimensions_overridden",
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
        action_required="Run --full re-index or chunk structure will be inconsistent",
    )


# ═══════════════════════════════════════════════════════════════
# Element-aware chunking — INLINED from element_detector.py.
# Zero cross-file project imports (this script's design principle —
# see module docstring). A fix here must be manually mirrored into
# any other script that also inlines this logic (e.g. a future
# content_freshness script) — known, accepted tradeoff, not an
# oversight; same tradeoff content_freshnessV1.py already made for
# element_detector.py itself.
#
# Behaviour: pages with no tables/##/### headers get byte-identical
# output to a flat RecursiveCharacterTextSplitter pass. Tables are
# chunked atomically (row-capped safety net); ##/### headers act as
# hard section boundaries so a chunk never bleeds across a topic
# change (tab pages, FAQ-accordion-style pages).
# ═══════════════════════════════════════════════════════════════

_EA_TABLE_ROWS_PER_CHUNK = 30
_EA_DEFAULT_SEPARATORS   = ["\n\n", "\n", ". ", " ", ""]


def _ea_parse_table_block(lines: list) -> dict:
    """Parse a contiguous block of |pipe| lines into header/rows."""
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
    """Parse raw markdown into ordered {type: header|table|prose|blank} elements."""
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
    """Group elements into sections using ## / ### as hard boundaries."""
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
    """Render a (possibly batched) set of table rows back to markdown."""
    if not header_row:
        return "\n".join("| " + " | ".join(row) + " |" for row in data_rows)
    sep = ["-" * max(3, len(h)) for h in header_row]
    lines = ["| " + " | ".join(header_row) + " |", "| " + " | ".join(sep) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in data_rows]
    return "\n".join(lines)


def _ea_chunk_table_element(table_el: dict) -> list:
    """Convert a table element into one or more atomic (row-capped) chunk texts."""
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
    """Merge a chunk containing ONLY the section header into the following chunk."""
    if len(pieces) < 2:
        return pieces
    header_line = f"### {header_text}".strip()
    if pieces[0].strip() == header_line:
        return [pieces[0] + "\n" + pieces[1]] + pieces[2:]
    return pieces


def _ea_section_to_text_segments(section: dict) -> list:
    """Convert one section's body into ordered {text, element_type} segments."""
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
    """
    Split page content into {"text", "element_type"} pieces, respecting
    table atomicity and ##/### header boundaries. No tables/headers ->
    identical output to a flat RecursiveCharacterTextSplitter pass.
    """
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


# ═══════════════════════════════════════════════════════════════
# Content cleaning / hashing
# ═══════════════════════════════════════════════════════════════

def clean_content(text: str) -> str:
    """
    Strip external (non-royallondon.com) URLs from page content before
    chunking. Markdown links keep their anchor text; bare external
    URLs are removed entirely. royallondon.com URLs are always kept
    (citation system needs them). Runs only at index time.
    """
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


def compute_content_hash(content: str) -> str:
    """SHA-256 hash of page content — used by content-freshness change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_chunk_id(identity_url: str, chunk_index: int, content: str) -> str:
    """
    Deterministic chunk_id — SHA-256 of (identity_url, chunk_index, content).
    Same URL + same position + same text always produces the same ID,
    so re-processing (retry, re-run) overwrites instead of duplicating.
    chunk_index is included so two genuinely different chunks that
    happen to share identical text don't collide into one ID.

    identity_url must be the DISTINGUISHING url for this chunk — for a
    dropdown-state chunk that is dropdown_url (the #state=... one), not
    source_url (which is now always the clean base page url and would
    otherwise collide with chunk 0 of the base page's own chunks).
    """
    return hashlib.sha256(f"{identity_url}|{chunk_index}|{content}".encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════
# Chunking
# ═══════════════════════════════════════════════════════════════

def chunk_pages(pages: list[dict]) -> list[dict]:
    """
    Split scraped pages into chunk dicts ready for embedding + upload.

    Dropdown-state pages (page.get("dropdown_title") non-empty) are
    NEVER split — exactly 1 atomic chunk each, so policy context
    (title) and per-option content (e.g. contact details) always stay
    together regardless of content length. Signal is dropdown_state,
    not URL pattern — scraper-version-independent.

    Standard pages go through chunk_content_element_aware() (tables
    atomic, ##/### headers as hard boundaries).
    """
    _index_run_id = str(uuid.uuid4())
    _indexed_at = datetime.now(timezone.utc).isoformat()

    # Dedup key: dropdown_url when present (dropdown chunk identity),
    # else source_url — two dropdown states under the same base page
    # legitimately share source_url now, so deduping on source_url
    # alone would drop all but one of them.
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
            "scraper_version": page.get("scraper_version", "unknown"),
            "metadata_version": page.get("metadata_version", "unknown"),
            "scrape_run_id": page.get("scrape_run_id", "unknown"),
            "refresh_count": 0,
            "has_video": page.get("has_video", False),
            "video_url": page.get("video_url") or "",
            "content_type": page.get("content_type", "article"),
            "product_category": page.get("product_category", "general"),
            "description": page.get("description", ""),
            "page_image_url": page.get("page_image_url") or "",
            "publish_date": page.get("publish_date", ""),
            "collection_name": page.get("collection_name", ""),
            "read_time_mins": str(page.get("read_time_mins", "5")),
            "page_purpose": page.get("page_purpose") or "",
        }

        is_dropdown_state = bool(page.get("dropdown_title", ""))

        if is_dropdown_state:
            stripped = content_with_title.strip()
            if len(stripped) >= 50:
                chunks.append({
                    # identity for chunk_id must be dropdown_url — source_url
                    # is now the shared base page url, and would otherwise
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
                log.info("dropdown_atomic_chunk", url=url, dropdown_url=dropdown_url,
                         dropdown_title=page.get("dropdown_title"), chars=len(stripped))
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
# Embedding
# ═══════════════════════════════════════════════════════════════

_credential = None
_openai_client = None


def get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def get_openai_client() -> AzureOpenAI:
    """
    Standalone Azure OpenAI client — mirrors core/embeddings.py's
    setup (same endpoint var, same auth, same default model/dims) so
    query-time and index-time embeddings stay identical in practice,
    without this file importing that module (see module docstring).
    """
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
        log.info("openai_client_created", endpoint=AZURE_OPENAI_ENDPOINT,
                  deployment=EMBEDDING_DEPLOYMENT, dimensions=EMBEDDING_DIMS)
    return _openai_client


def embed_chunks(chunks: list[dict]) -> list[list[float]]:
    """
    Generate one embedding per chunk (content only — no HQA question
    text to combine with now that HQA is dropped).

    Batches of EMBEDDING_BATCH_SIZE with a 2s inter-batch sleep. Every
    batch is retried on transient errors (rate limit, timeout, connection,
    5xx, token blips) with a longer backoff for throttling, and each
    response is validated (vector count + dimensions) — a short or
    wrong-sized response raises instead of producing misaligned embeddings.
    """
    texts = [c["content"] for c in chunks]
    if not texts:
        return []

    BATCH_SLEEP_SECONDS = 2
    client = get_openai_client()
    all_embeddings = []
    total_batches = (len(texts) + EMBEDDING_BATCH_SIZE - 1) // EMBEDDING_BATCH_SIZE

    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i:i + EMBEDDING_BATCH_SIZE]
        batch_number = i // EMBEDDING_BATCH_SIZE + 1
        response = with_retry(
            lambda b=batch: client.embeddings.create(
                input=b, model=EMBEDDING_DEPLOYMENT, dimensions=EMBEDDING_DIMS,
            ),
            op="embeddings_create", base_seconds=10, batch=batch_number, total_batches=total_batches,
        )
        sorted_data = sorted(response.data, key=lambda e: e.index)
        if len(sorted_data) != len(batch):
            raise RuntimeError(
                f"Embedding batch {batch_number}: expected {len(batch)} vectors, got {len(sorted_data)}"
            )
        for e in sorted_data:
            if len(e.embedding) != EMBEDDING_DIMS:
                raise RuntimeError(
                    f"Embedding batch {batch_number}: expected {EMBEDDING_DIMS} dims, got {len(e.embedding)}"
                )
        all_embeddings.extend(e.embedding for e in sorted_data)
        log.info("embeddings_batch_done", batch=batch_number,
                 total_batches=total_batches, chunk_count=len(all_embeddings))

        if i + EMBEDDING_BATCH_SIZE < len(texts):
            time.sleep(BATCH_SLEEP_SECONDS)

    if len(all_embeddings) != len(texts):
        raise RuntimeError(f"Embedded {len(all_embeddings)} of {len(texts)} chunks")
    return all_embeddings


# ═══════════════════════════════════════════════════════════════
# Search / Index
# ═══════════════════════════════════════════════════════════════

def get_search_client() -> SearchClient:
    return SearchClient(endpoint=SEARCH_ENDPOINT, index_name=INDEX_NAME, credential=get_credential())


def get_search_index_client() -> SearchIndexClient:
    return SearchIndexClient(endpoint=SEARCH_ENDPOINT, credential=get_credential())


def verify_azure_access(check_index: bool) -> "int | None":
    """
    Fail-fast preflight, run before any work: acquire AAD tokens for Search
    and OpenAI, and (when check_index) read the index's document count.
    Warms the credential chain, so a slow/broken `az` login surfaces here —
    with retries and a clear error — instead of mid-run. Returns the
    index's document count, or None if the index does not exist yet.
    """
    cred = get_credential()
    for scope in ("https://search.azure.com/.default", "https://cognitiveservices.azure.com/.default"):
        with_retry(lambda s=scope: cred.get_token(s), op="credential_preflight", scope=scope)
    if not check_index:
        log.info("azure_access_verified", index=INDEX_NAME, documents=None)
        return None
    try:
        count = with_retry(lambda: get_search_client().get_document_count(), op="index_preflight", index=INDEX_NAME)
    except ResourceNotFoundError:
        log.info("azure_access_verified", index=INDEX_NAME, documents=None, note="index does not exist yet")
        return None
    log.info("azure_access_verified", index=INDEX_NAME, documents=count)
    return count


def _search_page(client, op: str, **kwargs):
    """One search page with retry; returns (documents, total_count)."""
    def _call():
        r = client.search(include_total_count=True, **kwargs)
        return list(r), r.get_count()
    return with_retry(_call, op=op, skip=kwargs.get("skip"))


def _scan_index(client, op: str, select: list, on_doc) -> int:
    """Paginate the whole index, call on_doc(doc) for each; raise if the scan is incomplete."""
    skip, page_sz, scanned, total = 0, 1000, 0, None
    while True:
        batch, count = _search_page(client, op, search_text="*", select=select, top=page_sz, skip=skip)
        if total is None:
            total = count
        if not batch:
            break
        for r in batch:
            on_doc(r)
        scanned += len(batch)
        if len(batch) < page_sz:
            break
        skip += page_sz
    if total is not None and scanned != total:
        log.error("index_scan_incomplete", op=op, scanned=scanned, expected=total)
        raise RuntimeError(f"{op}: index scan incomplete — read {scanned} of {total} documents")
    return scanned


def get_indexed_urls() -> set:
    """
    All source_url values already in the index — used for --new-only mode.
    A missing index is a legitimate "nothing indexed yet" (empty set). Any
    other failure RAISES: a silently empty/partial set would make every
    page look new and re-upload changed pages next to their stale copies.
    """
    urls: set = set()

    def _on_doc(r):
        if r.get("source_url"):
            urls.add(r["source_url"])

    try:
        scanned = _scan_index(get_search_client(), "get_indexed_urls", ["source_url"], _on_doc)
    except ResourceNotFoundError:
        log.info("index_not_found_treating_as_empty", index=INDEX_NAME)
        return set()
    log.info("indexed_urls_fetched", count=len(urls), documents=scanned)
    return urls


def create_or_update_index(fresh: bool = False):
    """
    Create the index with every field attribute explicitly set (no
    Azure defaults relied upon). fresh=True deletes + recreates;
    fresh=False creates only if it doesn't already exist.
    """
    client = get_search_index_client()

    if fresh:
        # Only "index does not exist" is acceptable here. Any other failure
        # must stop the run: swallowing it would leave the OLD index in
        # place, and the upload would then mix new chunks into stale data
        # while the operator believes a fresh rebuild happened.
        try:
            with_retry(lambda: client.delete_index(INDEX_NAME), op="delete_index", index=INDEX_NAME)
            log.info("existing_index_deleted", index=INDEX_NAME)
        except ResourceNotFoundError:
            log.info("no_existing_index_to_delete", index=INDEX_NAME)

    fields = [
        SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True,
                    searchable=False, filterable=False, sortable=False, facetable=False, retrievable=True),
        SearchableField(name="content", type=SearchFieldDataType.String,
                         searchable=True, filterable=False, sortable=False, facetable=False, retrievable=True),
        SearchableField(name="title", type=SearchFieldDataType.String,
                         searchable=True, filterable=True, sortable=False, facetable=False, retrievable=True),
        SearchableField(name="source_url", type=SearchFieldDataType.String,
                         searchable=True, filterable=True, sortable=False, facetable=False, retrievable=True),
        SearchableField(name="section", type=SearchFieldDataType.String,
                         searchable=True, filterable=True, sortable=False, facetable=False, retrievable=True),
        SimpleField(name="audience", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=True, retrievable=True),
        SimpleField(name="scraped_at", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=True, facetable=False, retrievable=True),
        SimpleField(name="chunk_index", type=SearchFieldDataType.Int32,
                    searchable=False, filterable=True, sortable=True, facetable=False, retrievable=True),
        SimpleField(name="total_chunks", type=SearchFieldDataType.Int32,
                    searchable=False, filterable=True, sortable=False, facetable=False, retrievable=True),
        # content_hash: retrievable=True — content_freshness.py reads it back
        # via select=["content_hash"] to compare against a fresh scrape hash.
        SimpleField(name="content_hash", type=SearchFieldDataType.String,
                    searchable=False, filterable=False, sortable=False, facetable=False, retrievable=True),
        SimpleField(name="pipeline_version", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=False, retrievable=True),
        SimpleField(name="index_run_id", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=False, retrievable=True),
        SimpleField(name="indexed_at", type=SearchFieldDataType.String,
                    searchable=False, filterable=False, sortable=True, facetable=False, retrievable=True),
        SimpleField(name="scraper_version", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=False, retrievable=True),
        SimpleField(name="metadata_version", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=False, retrievable=True),
        SimpleField(name="scrape_run_id", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=False, retrievable=True),
        SimpleField(name="refresh_count", type=SearchFieldDataType.Int32,
                    searchable=False, filterable=True, sortable=True, facetable=False, retrievable=True),
        SimpleField(name="has_video", type=SearchFieldDataType.Boolean,
                    searchable=False, filterable=True, sortable=False, facetable=True, retrievable=True),
        # video_url — NEW. Passthrough from scraper v1.1.0. "" when
        # has_video is False or no matching iframe host was found.
        SimpleField(name="video_url", type=SearchFieldDataType.String,
                    searchable=False, filterable=False, sortable=False, facetable=False, retrievable=True),
        SearchableField(name="content_type", type=SearchFieldDataType.String,
                         searchable=True, filterable=True, sortable=False, facetable=True, retrievable=True),
        SearchableField(name="product_category", type=SearchFieldDataType.String,
                         searchable=True, filterable=True, sortable=False, facetable=True, retrievable=True),
        SearchableField(name="description", type=SearchFieldDataType.String,
                         searchable=True, filterable=False, sortable=False, facetable=False, retrievable=True),
        SimpleField(name="page_image_url", type=SearchFieldDataType.String,
                    searchable=False, filterable=False, sortable=False, facetable=False, retrievable=True),
        SimpleField(name="publish_date", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=True, facetable=False, retrievable=True),
        SearchableField(name="collection_name", type=SearchFieldDataType.String,
                         searchable=True, filterable=True, sortable=False, facetable=True, retrievable=True),
        SimpleField(name="read_time_mins", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=False, retrievable=True),
        # source_url is ALWAYS the clean, navigable base page URL — for a
        # dropdown-state chunk too, not just standard chunks. dropdown_url
        # carries the #state=... fragment ONLY for dropdown-state chunks
        # ("" otherwise) — it exists purely as an internal distinguishing
        # identity (chunk_id, freshness comparison), never for display.
        # Any consumer can just read source_url with no OR-fallback.
        SimpleField(name="dropdown_url", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=False, retrievable=True),
        # dropdown_title — the per-option label (e.g. "Scottish Life"), "" for
        # non-dropdown chunks. title stays the plain B&M page title for ALL
        # chunks, never compounded with this, since title is what consumers
        # render as the citation hyperlink text and it must match an entry
        # in the approved-URL list. Consumers wanting the option label for
        # display (e.g. "Related" cards) read this field explicitly instead.
        SearchableField(name="dropdown_title", type=SearchFieldDataType.String,
                         searchable=True, filterable=True, sortable=False, facetable=False, retrievable=True),
        # page_purpose — B&M's Excel label (Information / Action / Directional /
        # Reassurance / Engagement). Filterable so consumers can split answer
        # sources from CTA pages; not searchable so label words never add BM25 noise.
        SimpleField(name="page_purpose", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=True, retrievable=True),
        SimpleField(name="element_type", type=SearchFieldDataType.String,
                    searchable=False, filterable=True, sortable=False, facetable=True, retrievable=True),
        SearchField(name="embedding", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True, filterable=False, sortable=False, facetable=False, retrievable=False,
                    vector_search_dimensions=EMBEDDING_DIMS, vector_search_profile_name="rl-vector-profile"),
    ]

    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(
            name="rl-hnsw",
            parameters={"metric": "cosine", "m": 4, "efConstruction": 400, "efSearch": 500},
        )],
        profiles=[VectorSearchProfile(name="rl-vector-profile", algorithm_configuration_name="rl-hnsw")],
    )

    semantic_search = SemanticSearch(configurations=[SemanticConfiguration(
        name=SEMANTIC_CONFIG_NAME,
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="title"),
            content_fields=[
                SemanticField(field_name="content"),
                SemanticField(field_name="description"),
            ],
            keywords_fields=[
                SemanticField(field_name="section"),
                SemanticField(field_name="collection_name"),
                SemanticField(field_name="product_category"),
            ],
        ),
    )])

    try:
        with_retry(
            lambda: client.create_index(SearchIndex(
                name=INDEX_NAME, fields=fields,
                vector_search=vector_search, semantic_search=semantic_search,
            )),
            op="create_index", index=INDEX_NAME,
        )
        log.info("index_created", index=INDEX_NAME, semantic_config=SEMANTIC_CONFIG_NAME)
    except Exception as e:
        # "already exists" is fine when appending, but after a --full delete it
        # means the delete did not take effect — never carry on into stale data.
        if "already exists" in str(e).lower() and not fresh:
            log.info("index_already_exists", index=INDEX_NAME)
        else:
            raise


def upload_chunks(chunks: list[dict], embeddings: list[list[float]]) -> int:
    """
    Upload chunk+embedding documents to Azure AI Search, batched. Every
    document's result is checked: failed documents are retried (bounded);
    if any still fail, raises — a partial upload is never reported as success.
    """
    if len(chunks) != len(embeddings):
        raise RuntimeError(f"upload_chunks: {len(chunks)} chunks but {len(embeddings)} embeddings")
    client = get_search_client()
    documents = [{**chunk, "embedding": emb} for chunk, emb in zip(chunks, embeddings)]
    total_uploaded = 0
    for i in range(0, len(documents), UPLOAD_BATCH_SIZE):
        pending = documents[i:i + UPLOAD_BATCH_SIZE]
        batch_total = len(pending)
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            results = with_retry(
                lambda p=pending: client.upload_documents(documents=p),
                op="upload_documents", batch_start=i, batch_size=len(pending),
            )
            failed = {r.key: (r.error_message or r.status_code) for r in results if not r.succeeded}
            total_uploaded += len(pending) - len(failed)
            if not failed:
                break
            pending = [d for d in pending if d["chunk_id"] in failed]
            if attempt == RETRY_ATTEMPTS:
                log.error("upload_documents_failed", failed=len(pending), sample=list(failed.items())[:3])
                raise RuntimeError(
                    f"{len(pending)} of {batch_total} document(s) failed to upload after "
                    f"{RETRY_ATTEMPTS} attempts (batch starting at {i}); first errors: {list(failed.items())[:3]}"
                )
            wait = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** (attempt - 1))) * random.uniform(0.75, 1.25)
            log.warning("upload_partial_failure_retry", attempt=attempt, failed=len(failed), wait_seconds=round(wait, 1))
            time.sleep(wait)
        log.info("upload_batch_done", uploaded=total_uploaded, total=len(documents))
    log.info("upload_complete", total=total_uploaded)
    return total_uploaded


def verify_document_count(expected: int, attempts: int = 8, wait_seconds: float = 5.0) -> int:
    """
    After a --full rebuild the index must hold exactly the uploaded chunks.
    The service's document count is eventually consistent, so poll briefly;
    raise if it never matches (lost or duplicated documents).
    """
    client = get_search_client()
    actual = -1
    for attempt in range(1, attempts + 1):
        actual = with_retry(lambda: client.get_document_count(), op="postload_document_count", index=INDEX_NAME)
        if actual == expected:
            log.info("document_count_verified", index=INDEX_NAME, documents=actual)
            return actual
        log.warning("document_count_pending", index=INDEX_NAME, expected=expected, actual=actual, attempt=attempt)
        time.sleep(wait_seconds)
    log.error("document_count_mismatch", index=INDEX_NAME, expected=expected, actual=actual)
    raise RuntimeError(f"Index '{INDEX_NAME}' holds {actual} documents after upload, expected {expected}")


def verify_index() -> bool:
    """
    Post-build sanity check — runs one hybrid+semantic test query
    against the freshly built index and confirms it returns results.
    Catches a broken/empty index (wrong field name, embedding
    dimension mismatch, semantic config typo) right after --full,
    instead of discovering it via a real customer query later.
    """
    from azure.search.documents.models import VectorizedQuery

    try:
        embedding = embed_chunks([{"content": "What is a pension?"}])[0]
        client = get_search_client()
        vector_query = VectorizedQuery(vector=embedding, k_nearest_neighbors=5, fields="embedding")
        results = list(client.search(
            search_text="What is a pension?",
            vector_queries=[vector_query],
            query_type="semantic",
            semantic_configuration_name=SEMANTIC_CONFIG_NAME,
            select=["chunk_id", "title", "source_url"],
            top=5,
        ))
        if not results:
            log.error("verify_index_empty_results", index=INDEX_NAME)
            return False
        log.info("verify_index_ok", index=INDEX_NAME, result_count=len(results),
                  sample_url=results[0].get("source_url", ""))
        return True
    except Exception as e:
        log.error("verify_index_failed", index=INDEX_NAME, error=str(e))
        return False


def index_chunks(chunks: list[dict], embeddings: list[list[float]], fresh: bool = False) -> int:
    """
    Thin composition wrapper: create/update the index schema (if
    fresh), then upload chunks+embeddings. Kept separate from
    embed_chunks() so a caller that already has embeddings (e.g. a
    delta re-index of one changed page) can skip straight to indexing.
    """
    create_or_update_index(fresh=fresh)
    return upload_chunks(chunks, embeddings)


# ═══════════════════════════════════════════════════════════════
# Load pages
# ═══════════════════════════════════════════════════════════════

def find_latest_scraped_file() -> str:
    """
    Auto-detect the most recently modified scraper output JSON in
    scraper/data/. Matches "royal_london_faq_approved_*.json" but
    explicitly EXCLUDES anything ending "_failures.json" — the
    httpV1 scraper's failures sidecar matches the same prefix and is
    written with a newer mtime than the main output, so without this
    exclusion it could get picked as "latest" and crash chunk_pages()
    (different structure entirely: [{"url","reason"}], not page dicts).
    """
    data_dir = Path("scraper/data")
    if not data_dir.exists():
        return SCRAPED_FILE

    candidates = [
        p for p in data_dir.glob("royal_london_faq_approved_*.json")
        if not p.name.endswith("_failures.json")
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    if candidates:
        latest = str(candidates[0])
        log.info("auto_detected_scraped_file", file=latest, candidates_found=len(candidates))
        return latest

    log.warning("no_scraped_file_found", data_dir=str(data_dir),
                note="Run scrape_approved_urls_httpV1.py first, or pass --file explicitly")
    return SCRAPED_FILE


def load_pages(scraped_file: str | None = None) -> list[dict]:
    """Load scraped pages from a local JSON file."""
    file_path = scraped_file or find_latest_scraped_file()
    if not Path(file_path).exists():
        raise FileNotFoundError(
            f"Scraped file not found: {file_path}\n"
            f"Run scrape_approved_urls_httpV1.py first to generate it."
        )
    with open(file_path, encoding="utf-8") as f:
        pages = json.load(f)
    # Validate before anything downstream can act on it: an empty or malformed
    # file must never reach a --full rebuild (which deletes the index first).
    if not isinstance(pages, list) or not pages:
        raise ValueError(f"{file_path} contains no pages — refusing to index (a --full run would wipe the index)")
    bad = [i for i, p in enumerate(pages)
           if not isinstance(p, dict) or not p.get("source_url") or not p.get("content")]
    if bad:
        raise ValueError(
            f"{file_path}: {len(bad)} malformed page record(s) (missing source_url/content); "
            f"first record indexes: {bad[:5]}. Is this the scraper's *_failures.json?"
        )
    log.info("pages_loaded_from_local", file=file_path, total=len(pages))
    return pages


# ═══════════════════════════════════════════════════════════════
# Pipeline
# ═══════════════════════════════════════════════════════════════

def run_pipeline(mode: str = "new-only", scraped_file: str | None = None, dry_run: bool = False,
                 allow_shrink: bool = False) -> dict:
    """
    Programmatic entry point: load -> chunk_pages -> embed_chunks ->
    index_chunks.

    Returns dict: success, pages_indexed, chunks_created,
    chunks_uploaded, dry_run, index_name, run_id, error.
    """
    run_started_at = time.monotonic()
    result = {
        "success": False, "pages_indexed": 0, "chunks_created": 0,
        "chunks_uploaded": 0, "dry_run": dry_run, "index_name": INDEX_NAME,
        "pipeline_version": PIPELINE_VERSION, "run_id": "", "verified": None,
        "elapsed_seconds": 0.0, "error": "",
    }

    try:
        fresh = (mode == "full")

        if not AZURE_OPENAI_ENDPOINT:
            raise ValueError("AZURE_OPENAI_ENDPOINT not set in .env")
        if not SEARCH_ENDPOINT:
            raise ValueError("AZURE_SEARCH_ENDPOINT not set in .env")
        if fresh and not INDEX_NAME.startswith(_INDEX_NAME_GUARD_PREFIX):
            raise ValueError(
                f"ABORTED: --full would delete '{INDEX_NAME}', which doesn't "
                f"start with '{_INDEX_NAME_GUARD_PREFIX}'. Check "
                f"AZURE_SEARCH_INDEX_NAME in .env / Key Vault."
            )

        # Fail fast (and warm the credential chain) before any work.
        existing_docs = None if dry_run else verify_azure_access(check_index=True)

        pages = load_pages(scraped_file)
        log.info("pages_loaded", total=len(pages))

        if not fresh:
            indexed_urls = get_indexed_urls()
            pages_to_index = [
                p for p in pages
                if p.get("source_url", "").rstrip("/") not in {u.rstrip("/") for u in indexed_urls}
            ]
            if not pages_to_index:
                log.info("no_new_pages_to_index")
                result["success"] = True
                result["elapsed_seconds"] = round(time.monotonic() - run_started_at, 2)
                return result
        else:
            pages_to_index = pages

        result["pages_indexed"] = len(pages_to_index)

        chunks = chunk_pages(pages_to_index)
        result["chunks_created"] = len(chunks)
        if not chunks:
            raise ValueError("chunk_pages produced 0 chunks from a non-empty page list — refusing to touch the index")
        result["run_id"] = chunks[0].get("index_run_id", "")

        # Shrink gate (--full only): a rebuild that would hold far fewer
        # documents than the live index is almost always a wrong/partial input.
        if (fresh and not dry_run and existing_docs and existing_docs >= MIN_DOCS_FOR_SHRINK_GATE
                and len(chunks) < INDEX_MIN_REBUILD_RATIO * existing_docs and not allow_shrink):
            raise ValueError(
                f"ABORTED before touching the index: the rebuild would hold {len(chunks)} chunks but "
                f"'{INDEX_NAME}' currently holds {existing_docs} (< {INDEX_MIN_REBUILD_RATIO:.0%}). "
                f"Check the scraped file is complete; re-run with --allow-shrink only if this is intended."
            )

        if dry_run:
            log.info("dry_run_complete", pages=result["pages_indexed"], chunks=result["chunks_created"])
            result["success"] = True
            result["elapsed_seconds"] = round(time.monotonic() - run_started_at, 2)
            return result

        embeddings = embed_chunks(chunks)
        result["chunks_uploaded"] = index_chunks(chunks, embeddings, fresh=fresh)

        # Sanity-check the index right after a full rebuild — catches a
        # broken build (schema/dims/semantic-config mismatch) here
        # instead of via a real customer query later.
        if fresh:
            verify_document_count(len(chunks))
            result["verified"] = verify_index()
            if not result["verified"]:
                raise RuntimeError(
                    "post-build verification query failed — the rebuilt index is not serving results; see log"
                )

        result["success"] = True
        result["elapsed_seconds"] = round(time.monotonic() - run_started_at, 2)
        return result

    except Exception as e:
        import traceback
        result["error"] = f"{type(e).__name__}: {e}"
        result["elapsed_seconds"] = round(time.monotonic() - run_started_at, 2)
        log.error("pipeline_error", error_type=type(e).__name__, error=str(e), traceback=traceback.format_exc())
        return result


# ── Main (CLI entry point) ────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="RLG FAQ Chunk+Embed+Index v1 (no HQA)")
    parser.add_argument("--full", action="store_true", help="Delete and recreate index (fresh start)")
    parser.add_argument("--new-only", action="store_true", help="Only index pages not already indexed (default)")
    parser.add_argument("--dry-run", action="store_true", help="Validate config + chunk, no index changes/uploads")
    parser.add_argument("--file", type=str, default=None, help="Path to scraped JSON file")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="With --full: allow rebuilding into far fewer documents than the live index holds")
    args = parser.parse_args()

    mode = "full" if args.full else "new-only"
    scraped_file = args.file or find_latest_scraped_file()

    print("\n" + "=" * 60)
    print("RLG CHUNK + EMBED + INDEX v1 (no HQA)")
    print("=" * 60)
    print(f"   Mode:      {'FULL (fresh index)' if mode == 'full' else 'NEW ONLY (append)'}")
    print(f"   Dry run:   {'YES — index will NOT be modified' if args.dry_run else 'No'}")
    print(f"   File:      {scraped_file}")
    print(f"   Index:     {INDEX_NAME}")
    print(f"   Embed:     {EMBEDDING_DEPLOYMENT} ({EMBEDDING_DIMS}d)")
    print(f"   Search:    {SEARCH_ENDPOINT}")
    print(f"   OpenAI:    {AZURE_OPENAI_ENDPOINT}")
    print("=" * 60)

    result = run_pipeline(mode=mode, scraped_file=scraped_file, dry_run=args.dry_run,
                          allow_shrink=args.allow_shrink)

    if not result["success"]:
        print(f"\nFAILED after {result['elapsed_seconds']}s: {result['error']}")
        sys.exit(1)

    print(f"\nDone in {result['elapsed_seconds']}s")
    print(f"   Pages indexed:   {result['pages_indexed']}")
    print(f"   Chunks created:  {result['chunks_created']}")
    if not result["dry_run"]:
        print(f"   Chunks uploaded: {result['chunks_uploaded']}")
    print(f"   Index:           {result['index_name']}")
    if result["verified"] is not None:
        print(f"   Verified:        {'OK' if result['verified'] else 'FAILED — check logs'}")


if __name__ == "__main__":
    main()