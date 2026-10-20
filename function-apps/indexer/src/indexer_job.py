"""Hosting-layer core for the indexer Function App.

Plain functions only (no azure.functions import) so it is testable offline.
The indexer script (src/chunk_embed_index_v1.py) is NOT modified.

Design: every build creates a NEW index (rlg-faq-index-YYYYMMDD-HHMMSS) from a
scraped JSON; nothing live is touched. Going live is a separate, manual step:
promote() points a fixed Search alias at the new index (instant, reversible).
Only indexes matching INDEX_RE are ever deleted, so legacy ones
(e.g. rlg-faq-index-v5) are never touched.
"""
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("indexer_job")

SCRAPED_PREFIX = os.environ.get("INDEXER_INPUT_PREFIX", "scraped-data/")
ALIAS_NAME = os.environ.get("SEARCH_ALIAS_NAME", "rlg-faq-live")
KEEP_INDEXES = int(os.environ.get("INDEX_KEEP_COUNT", "3"))
MIN_PROMOTE_RATIO = float(os.environ.get("INDEX_MIN_PROMOTE_RATIO", "0.5"))
MIN_DOCS_FOR_GATE = 20
# Must start with the script's own guard prefix ("rlg-faq-index").
INDEX_RE = re.compile(r"^rlg-faq-index-\d{8}-\d{6}$")
REQUIRED_ENV = ("AZURE_SEARCH_ENDPOINT", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_EMBEDDING_DIMENSIONS")

# The script keeps config in module globals, so one build/promote at a time per process.
_JOB_LOCK = threading.Lock()


# ── message / name helpers ────────────────────────────────────
def is_main_json(name: str) -> bool:
    """Scraper output only. The *_failures.json sidecar has a different structure."""
    base = name.rsplit("/", 1)[-1]
    return (base.startswith("royal_london_faq_approved_") and base.endswith(".json")
            and not base.endswith("_failures.json"))


def latest_scraped(container_out) -> str:
    blobs = [b for b in container_out.list_blobs(name_starts_with=SCRAPED_PREFIX) if is_main_json(b.name)]
    if not blobs:
        raise FileNotFoundError(f"No scraper JSON found under output/{SCRAPED_PREFIX}")
    return max(blobs, key=lambda b: b.last_modified).name


def parse_build_message(body: str) -> str | None:
    """Optional {"file": "royal_london_..json"}; anything else means 'use latest'."""
    try:
        name = json.loads(body).get("file")
        return (SCRAPED_PREFIX + name) if name else None
    except (ValueError, AttributeError):
        return None


def parse_promote_message(body: str) -> tuple[str | None, bool]:
    """'promote' | 'promote force' | {"index": "rlg-faq-index-..", "force": true} -> (index or None, force)."""
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            return d.get("index") or None, bool(d.get("force"))
    except ValueError:
        pass
    return None, "force" in body.lower()


def new_index_name(now: datetime | None = None) -> str:
    return "rlg-faq-index-" + (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")


# ── Azure Search operations (faked in tests) ──────────────────
class AzureSearchOps:
    def __init__(self):
        from azure.identity import DefaultAzureCredential
        from azure.search.documents.indexes import SearchIndexClient
        self._endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
        if not self._endpoint:
            raise RuntimeError("AZURE_SEARCH_ENDPOINT is not set")
        self._cred = DefaultAzureCredential()
        self._ic = SearchIndexClient(self._endpoint, self._cred)

    def list_indexes(self) -> list[str]:
        return list(self._ic.list_index_names())

    def count(self, index: str) -> int:
        from azure.search.documents import SearchClient
        return SearchClient(self._endpoint, index, self._cred).get_document_count()

    def get_alias(self) -> str | None:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            idx = self._ic.get_alias(ALIAS_NAME).indexes
        except ResourceNotFoundError:
            return None
        return idx[0] if idx else None

    def set_alias(self, index: str) -> None:
        from azure.search.documents.indexes.models import SearchAlias
        self._ic.create_or_update_alias(SearchAlias(name=ALIAS_NAME, indexes=[index]))

    def delete(self, index: str) -> None:
        self._ic.delete_index(index)


# ── build ─────────────────────────────────────────────────────
def default_run(index_name: str, file_path: str) -> dict:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:  # dims has a silent default in the script; it must match the query side
        raise RuntimeError(f"Missing required app settings: {', '.join(missing)}")
    from src import chunk_embed_index_v1 as m  # lazy: heavy imports, reads env at import
    m.INDEX_NAME = index_name  # never the script's default (the live index)
    log.info("indexer config: index=%s search=%s openai=%s deployment=%s dims=%s",
             m.INDEX_NAME, m.SEARCH_ENDPOINT, m.AZURE_OPENAI_ENDPOINT, m.EMBEDDING_DEPLOYMENT, m.EMBEDDING_DIMS)
    return m.run_pipeline(mode="full", scraped_file=file_path)


def build_index(container_out, blob_name: str, run=default_run, ops_factory=AzureSearchOps) -> dict:
    """Download scraped JSON -> build a NEW index. Raises on failure (host retries once)."""
    log.info("index build waiting for lock: %s", blob_name)
    with _JOB_LOCK, tempfile.TemporaryDirectory(prefix="index_") as tmp:
        src = Path(tmp) / Path(blob_name).name
        src.write_bytes(container_out.download_blob(blob_name).readall())
        index_name = new_index_name()
        log.info("building %s from %s", index_name, blob_name)

        result = run(index_name, str(src))
        if not result.get("success"):
            try:  # drop the half-built index so failed attempts do not pile up
                ops_factory().delete(index_name)
                log.info("deleted partial index %s", index_name)
            except Exception as e:  # noqa: BLE001 - best effort, original error matters more
                log.warning("could not delete partial index %s: %s", index_name, e)
            raise RuntimeError(f"index build failed ({index_name}): {result.get('error') or 'unknown'}")

        log.info("INDEX_BUILT index=%s source=%s pages=%s chunks=%s elapsed=%ss (not live: run promote)",
                 index_name, blob_name, result.get("pages_indexed"), result.get("chunks_uploaded"),
                 result.get("elapsed_seconds"))
        return {**result, "index_name": index_name}


# ── promote ───────────────────────────────────────────────────
def promote(ops, index: str | None = None, force: bool = False) -> dict:
    """Point the alias at `index` (default: newest built). Refusals are logged, not raised (a retry cannot help)."""
    with _JOB_LOCK:
        built = sorted(n for n in ops.list_indexes() if INDEX_RE.match(n))
        target = index or (built[-1] if built else None)
        if not target or target not in built:
            log.error("PROMOTE_REFUSED index=%s not found among built indexes %s", target, built[-5:])
            return {"promoted": False, "reason": "index not found"}

        current = ops.get_alias()
        if current == target:
            log.info("alias %s already points at %s", ALIAS_NAME, target)
            return {"promoted": False, "reason": "already live", "index": target}

        if current and not force:
            n_new, n_cur = ops.count(target), ops.count(current)
            if n_cur >= MIN_DOCS_FOR_GATE and n_new < MIN_PROMOTE_RATIO * n_cur:
                log.error("PROMOTE_REFUSED %s has %s docs vs live %s has %s (< %.0f%%); send force to override",
                          target, n_new, current, n_cur, MIN_PROMOTE_RATIO * 100)
                return {"promoted": False, "reason": "shrink gate"}

        ops.set_alias(target)
        log.warning("INDEX_PROMOTED alias=%s from=%s to=%s", ALIAS_NAME, current, target)

        keep = set(built[-KEEP_INDEXES:]) | {target} | ({current} if current else set())
        for name in built:  # only names matching INDEX_RE: legacy indexes are never listed here
            if name not in keep:
                try:
                    ops.delete(name)
                    log.info("deleted old index %s", name)
                except Exception as e:  # noqa: BLE001
                    log.warning("could not delete old index %s: %s", name, e)
        return {"promoted": True, "from": current, "index": target}
