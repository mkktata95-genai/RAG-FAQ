"""
Royal London FAQ — Chunk, Embed, Index (funcV1 — Function App variant)
════════════════════════════════════════════════════════════════════
Derived from chunk_embed_index_v1.py (kept untouched as the local/rebuild
script). Same chunking, hashing, chunk_id and embedding logic — only the
INDEX HANDLING differs, because the Search index is now owned by the
platform team (Terraform) and this app has data-plane access only
(Search Index Data Contributor):

  REMOVED  create_or_update_index(), delete_index, the schema/semantic/vector
           definitions, the --full wipe, the index-name guard prefix.
  ADDED    verify_azure_access(): fails fast if the index does not exist or
           lacks an expected field (data-plane probe, no index-read role needed).
  ADDED    mode "sync" (default): upsert every scraped page, then delete the
           chunks that are no longer in the scrape — (a) old chunks of a page
           that changed or shrank, (b) all chunks of URLs that disappeared.
           Writes happen first (embed + upload), deletes last, so a failure
           never leaves the index emptier than before.
  ADDED    safety: if more than INDEXER_MAX_REMOVAL_RATIO (10%) of the indexed
           URLs would vanish (min 3), the run refuses BEFORE any write unless
           allow_mass_removal=True. Pages listed in the scraper's
           *_failures.json sidecar are protected: their old chunks are kept.
  CHANGED  scraped_at falls back to the index time when a page has none
           (the index field is Edm.DateTimeOffset — "" is not a valid date).
  CHANGED  PIPELINE_VERSION 1.0.0 -> 1.1.0 (index schema: two date fields).

Index name comes ONLY from AZURE_SEARCH_INDEX_NAME (no default).

Usage:
    python chunk_embed_index_funcV1.py                 # sync (default)
    python chunk_embed_index_funcV1.py --new-only      # only pages not yet indexed, no deletes
    python chunk_embed_index_funcV1.py --dry-run       # chunk only, no Azure calls
    python chunk_embed_index_funcV1.py --file f.json --allow-mass-removal

Programmatic:
    run_pipeline(mode="sync", scraped_file=..., dry_run=False, allow_mass_removal=False)
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
from langchain_text_splitters import RecursiveCharacterTextSplitter
from dotenv import load_dotenv, find_dotenv

_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path, override=True)
log = structlog.get_logger()

# ═══════════════════════════════════════════════════════════════
# Versioning
# ═══════════════════════════════════════════════════════════════
# Developer-bumped only. Bump when chunking logic, embedding model/
# dims, or index schema changes — requires a full sync (re-embeds everything).
PIPELINE_VERSION = "1.1.0"

# ═══════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════
SCRAPED_FILE          = "scraper/data/royal_london_faq_approved_httpV1_latest.json"
CHUNK_SIZE            = int(os.getenv("CHUNK_SIZE", "1600"))
CHUNK_OVERLAP         = int(os.getenv("CHUNK_OVERLAP", "200"))
INDEX_NAME            = os.getenv("AZURE_SEARCH_INDEX_NAME", "")  # set by the platform; no default on purpose
EMBEDDING_DIMS        = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1536"))
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
SEARCH_ENDPOINT       = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT  = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")
SEMANTIC_CONFIG_NAME  = os.getenv("AZURE_SEARCH_SEMANTIC_CONFIG", "rlg-semantic-config")
EMBEDDING_BATCH_SIZE  = int(os.getenv("EMBEDDING_BATCH_SIZE", "50"))
UPLOAD_BATCH_SIZE     = 100

# Safety gate for sync: refuse when more than this share of the indexed URLs
# would disappear (and at least MIN_URLS_FOR_REMOVAL_GATE of them) — almost
# always a partial/truncated scrape. Override with allow_mass_removal.
MAX_REMOVAL_RATIO = float(os.getenv("INDEXER_MAX_REMOVAL_RATIO", "0.10"))
MIN_URLS_FOR_REMOVAL_GATE = 3

# Fields the platform index must expose (everything except the non-retrievable vector).
REQUIRED_FIELDS = [
    "chunk_id", "content", "title", "source_url", "section", "audience", "scraped_at",
    "chunk_index", "total_chunks", "content_hash", "pipeline_version", "index_run_id",
    "indexed_at", "scraper_version", "metadata_version", "scrape_run_id", "refresh_count",
    "has_video", "video_url", "content_type", "product_category", "description",
    "page_image_url", "publish_date", "collection_name", "read_time_mins", "dropdown_url",
    "dropdown_title", "page_purpose", "element_type",
]

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
        action_required="Run a full sync or chunk structure will be inconsistent",
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
            # index field is Edm.DateTimeOffset: "" is invalid, so fall back to the index time
            "scraped_at": page.get("scraped_at") or _indexed_at,
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


def verify_azure_access(check_index: bool) -> "int | None":
    """
    Fail-fast preflight, run before any work: acquire AAD tokens for Search and
    OpenAI, then confirm the platform-created index exists and exposes every
    expected field. Data-plane only: a document count plus a 1-row probe that
    SELECTs all required fields (an unknown field is a 400), so no index-read
    role is needed. Returns the index's document count.
    """
    cred = get_credential()
    for scope in ("https://search.azure.com/.default", "https://cognitiveservices.azure.com/.default"):
        with_retry(lambda s=scope: cred.get_token(s), op="credential_preflight", scope=scope)
    if not check_index:
        log.info("azure_access_verified", index=INDEX_NAME, documents=None)
        return None
    client = get_search_client()
    try:
        count = with_retry(lambda: client.get_document_count(), op="index_preflight", index=INDEX_NAME)
    except ResourceNotFoundError:
        raise RuntimeError(
            f"Search index '{INDEX_NAME}' does not exist. Indexes are created by the platform team; "
            f"this app never creates one. Check AZURE_SEARCH_INDEX_NAME."
        ) from None
    try:
        with_retry(lambda: list(client.search(search_text="*", select=REQUIRED_FIELDS, top=1)),
                   op="index_schema_probe", index=INDEX_NAME)
    except HttpResponseError as e:
        raise RuntimeError(
            f"Index '{INDEX_NAME}' does not match the expected schema (a required field is missing "
            f"or not retrievable): {getattr(e, 'message', e)}"
        ) from e
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


def _identity(url: str, dropdown_url: str = "") -> str:
    """Page identity for stale detection: the dropdown #state url when present, else the base url."""
    return (dropdown_url or url or "").rstrip("/")


def load_failed_urls(scraped_file: str) -> set:
    """
    URLs the scraper could not fetch this run, from the *_failures.json sidecar
    next to the scraped file. Their existing chunks are KEPT (a transient
    failure must not delete a page). Missing sidecar -> empty set. An unreadable
    sidecar raises: deleting without knowing what failed is the unsafe choice.
    """
    p = Path(scraped_file)
    side = p.with_name(p.stem + "_failures.json")
    if not side.exists():
        return set()
    with open(side, encoding="utf-8") as f:
        data = json.load(f)
    urls = {str(d["url"]).rstrip("/") for d in data if isinstance(d, dict) and d.get("url")}
    log.info("failed_urls_protected", file=str(side), count=len(urls))
    return urls


def plan_stale_deletion(chunks: list[dict], protected_urls: set, allow_mass_removal: bool = False) -> dict:
    """
    READ-ONLY. Scan the index and decide which existing chunks this run makes
    stale: any chunk_id not produced by the new chunks. chunk_id embeds the
    chunk text, so a changed chunk gets a new id and its old one becomes stale;
    shrunk pages and vanished URLs are covered the same way.

    Chunks of protected (scrape-failed) URLs are retained. Raises BEFORE any
    write when too many URLs would vanish (see MAX_REMOVAL_RATIO).
    Returns {stale_ids, retained, urls_removed, indexed_urls}.
    """
    keep_ids = {c["chunk_id"] for c in chunks}
    new_idents = {_identity(c["source_url"], c.get("dropdown_url", "")) for c in chunks}
    indexed_idents, gone, stale, retained = set(), set(), [], 0

    def _on_doc(d):
        nonlocal retained
        ident = _identity(d.get("source_url"), d.get("dropdown_url"))
        indexed_idents.add(ident)
        if d["chunk_id"] in keep_ids:
            return
        if ident in protected_urls or _identity(d.get("source_url")) in protected_urls:
            retained += 1
            return
        stale.append(d["chunk_id"])
        if ident not in new_idents:
            gone.add(ident)

    _scan_index(get_search_client(), "plan_stale_deletion", ["chunk_id", "source_url", "dropdown_url"], _on_doc)

    ratio = len(gone) / len(indexed_idents) if indexed_idents else 0.0
    if (not allow_mass_removal and len(gone) >= MIN_URLS_FOR_REMOVAL_GATE and ratio > MAX_REMOVAL_RATIO):
        raise ValueError(
            f"ABORTED before any write: {len(gone)} of {len(indexed_idents)} indexed URLs ({ratio:.0%}) are not in "
            f"this scrape (limit {MAX_REMOVAL_RATIO:.0%}). Check the scraped file is complete; re-run with "
            f"allow_mass_removal only if this is intended. First missing: {sorted(gone)[:3]}"
        )
    log.info("stale_deletion_planned", stale_chunks=len(stale), urls_removed=len(gone),
             retained_failed=retained, indexed_urls=len(indexed_idents))
    return {"stale_ids": stale, "retained": retained, "urls_removed": len(gone), "indexed_urls": len(indexed_idents)}


def delete_chunks(chunk_ids: list[str]) -> int:
    """Delete chunk documents by key, batched, every result checked, bounded retries. Raises on failure."""
    if not chunk_ids:
        return 0
    client = get_search_client()
    deleted = 0
    for i in range(0, len(chunk_ids), UPLOAD_BATCH_SIZE):
        pending = [{"chunk_id": cid} for cid in chunk_ids[i:i + UPLOAD_BATCH_SIZE]]
        batch_total = len(pending)
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            results = with_retry(lambda p=pending: client.delete_documents(documents=p),
                                 op="delete_documents", batch_start=i, batch_size=len(pending))
            failed = {r.key: (r.error_message or r.status_code) for r in results if not r.succeeded}
            deleted += len(pending) - len(failed)
            if not failed:
                break
            pending = [d for d in pending if d["chunk_id"] in failed]
            if attempt == RETRY_ATTEMPTS:
                raise RuntimeError(f"{len(pending)} of {batch_total} chunk(s) could not be deleted "
                                   f"after {RETRY_ATTEMPTS} attempts; first errors: {list(failed.items())[:3]}")
            time.sleep(min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** (attempt - 1))) * random.uniform(0.75, 1.25))
    log.info("stale_chunks_deleted", count=deleted)
    return deleted


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
    After a sync the index must hold exactly the uploaded chunks (+ retained failed-URL chunks).
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
    Post-sync sanity check — runs one hybrid+semantic test query
    against the synced index and confirms it returns results.
    Catches a broken/empty index (wrong field name, embedding
    dimension mismatch, semantic config typo) right after a sync,
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


def index_chunks(chunks: list[dict], embeddings: list[list[float]]) -> int:
    """Upsert chunks+embeddings into the (platform-created) index. Same key = overwrite."""
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
    # file must never reach a sync (which would delete every stale chunk).
    if not isinstance(pages, list) or not pages:
        raise ValueError(f"{file_path} contains no pages — refusing to index (an empty file would remove every chunk)")
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

def run_pipeline(mode: str = "sync", scraped_file: str | None = None, dry_run: bool = False,
                 allow_mass_removal: bool = False) -> dict:
    """
    load -> chunk_pages -> (plan stale, read-only) -> embed_chunks -> upsert -> delete stale.

    mode "sync":     upsert every page, then delete chunks no longer in the scrape.
    mode "new-only": only pages whose URL is not in the index yet; never deletes.
    dry_run: load + chunk only, no Azure calls at all.

    Returns dict: success, pages_indexed, chunks_created, chunks_uploaded,
    chunks_deleted, urls_removed, chunks_retained_failed, dry_run, index_name,
    run_id, verified, elapsed_seconds, error.
    """
    run_started_at = time.monotonic()
    result = {
        "success": False, "pages_indexed": 0, "chunks_created": 0, "chunks_uploaded": 0,
        "chunks_deleted": 0, "urls_removed": 0, "chunks_retained_failed": 0,
        "dry_run": dry_run, "index_name": INDEX_NAME, "pipeline_version": PIPELINE_VERSION,
        "run_id": "", "verified": None, "elapsed_seconds": 0.0, "error": "",
    }

    try:
        if mode not in ("sync", "new-only"):
            raise ValueError(f"invalid mode {mode!r}: use 'sync' or 'new-only'")
        sync = (mode == "sync")

        if not AZURE_OPENAI_ENDPOINT:
            raise ValueError("AZURE_OPENAI_ENDPOINT not set in .env")
        if not SEARCH_ENDPOINT:
            raise ValueError("AZURE_SEARCH_ENDPOINT not set in .env")
        if not INDEX_NAME:
            raise ValueError("AZURE_SEARCH_INDEX_NAME not set (the index is created by the platform team)")

        # Fail fast (and warm the credential chain) before any work.
        if not dry_run:
            verify_azure_access(check_index=True)

        scraped_file = scraped_file or find_latest_scraped_file()
        pages = load_pages(scraped_file)
        log.info("pages_loaded", total=len(pages))

        if sync:
            pages_to_index = pages
        else:
            indexed_urls = get_indexed_urls()
            known = {u.rstrip("/") for u in indexed_urls}
            pages_to_index = [p for p in pages if p.get("source_url", "").rstrip("/") not in known]
            if not pages_to_index:
                log.info("no_new_pages_to_index")
                result["success"] = True
                result["elapsed_seconds"] = round(time.monotonic() - run_started_at, 2)
                return result

        result["pages_indexed"] = len(pages_to_index)

        chunks = chunk_pages(pages_to_index)
        result["chunks_created"] = len(chunks)
        if not chunks:
            raise ValueError("chunk_pages produced 0 chunks from a non-empty page list — refusing to touch the index")
        result["run_id"] = chunks[0].get("index_run_id", "")

        if dry_run:
            log.info("dry_run_complete", pages=result["pages_indexed"], chunks=result["chunks_created"])
            result["success"] = True
            result["elapsed_seconds"] = round(time.monotonic() - run_started_at, 2)
            return result

        # Read-only planning BEFORE any write, so a refused run changes nothing.
        plan = plan_stale_deletion(chunks, load_failed_urls(scraped_file), allow_mass_removal) if sync else None

        embeddings = embed_chunks(chunks)
        result["chunks_uploaded"] = index_chunks(chunks, embeddings)

        if plan is not None:  # deletes LAST: a failure above never leaves the index emptier than before
            result["chunks_deleted"] = delete_chunks(plan["stale_ids"])
            result["urls_removed"] = plan["urls_removed"]
            result["chunks_retained_failed"] = plan["retained"]
            verify_document_count(len(chunks) + plan["retained"])
            result["verified"] = verify_index()
            if not result["verified"]:
                raise RuntimeError(
                    "post-sync verification query failed — the index is not serving results; see log"
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
    parser = argparse.ArgumentParser(description="RLG FAQ Chunk+Embed+Index funcV1 (platform-owned index)")
    parser.add_argument("--new-only", action="store_true", help="Only index pages not already indexed; never deletes")
    parser.add_argument("--dry-run", action="store_true", help="Load + chunk only, no Azure calls")
    parser.add_argument("--file", type=str, default=None, help="Path to scraped JSON file")
    parser.add_argument("--allow-mass-removal", action="store_true",
                        help="Allow a sync that removes more than the safety limit of indexed URLs")
    args = parser.parse_args()

    mode = "new-only" if args.new_only else "sync"
    scraped_file = args.file or find_latest_scraped_file()

    print("\n" + "=" * 60)
    print("RLG CHUNK + EMBED + INDEX funcV1 (platform-owned index)")
    print("=" * 60)
    print(f"   Mode:      {mode}")
    print(f"   Dry run:   {'YES — no Azure calls' if args.dry_run else 'No'}")
    print(f"   File:      {scraped_file}")
    print(f"   Index:     {INDEX_NAME}")
    print(f"   Embed:     {EMBEDDING_DEPLOYMENT} ({EMBEDDING_DIMS}d)")
    print(f"   Search:    {SEARCH_ENDPOINT}")
    print(f"   OpenAI:    {AZURE_OPENAI_ENDPOINT}")
    print("=" * 60)

    result = run_pipeline(mode=mode, scraped_file=scraped_file, dry_run=args.dry_run,
                          allow_mass_removal=args.allow_mass_removal)

    if not result["success"]:
        print(f"\nFAILED after {result['elapsed_seconds']}s: {result['error']}")
        sys.exit(1)

    print(f"\nDone in {result['elapsed_seconds']}s")
    print(f"   Pages indexed:    {result['pages_indexed']}")
    print(f"   Chunks created:   {result['chunks_created']}")
    if not result["dry_run"]:
        print(f"   Chunks uploaded:  {result['chunks_uploaded']}")
        print(f"   Chunks deleted:   {result['chunks_deleted']} ({result['urls_removed']} URLs gone, "
              f"{result['chunks_retained_failed']} kept for failed URLs)")
    print(f"   Index:            {result['index_name']}")
    if result["verified"] is not None:
        print(f"   Verified:         {'OK' if result['verified'] else 'FAILED — check logs'}")


if __name__ == "__main__":
    main()
