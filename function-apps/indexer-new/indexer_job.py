"""Hosting-layer core for the indexer Function App.

Plain functions only (no azure.functions import) so it is testable offline.
The index is owned by the platform team (Terraform). This app has data-plane
access only: it syncs documents into the fixed index named by
AZURE_SEARCH_INDEX_NAME and never creates, deletes or aliases an index.
Rollback = re-run from a scraped JSON in blob storage.
"""
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("indexer_job")

SCRAPED_PREFIX = os.environ.get("INDEXER_INPUT_PREFIX", "scraped-data/")
REQUIRED_ENV = ("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_NAME", "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_EMBEDDING_DIMENSIONS")

# The script keeps config in module globals, so one build at a time per process.
_JOB_LOCK = threading.Lock()


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


def parse_build_message(body: str) -> tuple[str | None, bool]:
    """Optional {"file": "royal_london_..json", "allow_mass_removal": true}; anything else = latest, safe."""
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            name = d.get("file")
            return ((SCRAPED_PREFIX + name) if name else None), bool(d.get("allow_mass_removal"))
    except ValueError:
        pass
    return None, "allow_mass_removal" in body.lower()


def default_run(file_path: str, allow_mass_removal: bool = False) -> dict:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:  # dims has a silent default in the script; it must match the query side
        raise RuntimeError(f"Missing required app settings: {', '.join(missing)}")
    from src import chunk_embed_index_funcV1 as m  # lazy: heavy imports, reads env at import
    log.info("indexer config: index=%s search=%s openai=%s deployment=%s dims=%s",
             m.INDEX_NAME, m.SEARCH_ENDPOINT, m.AZURE_OPENAI_ENDPOINT, m.EMBEDDING_DEPLOYMENT, m.EMBEDDING_DIMS)
    return m.run_pipeline(mode="sync", scraped_file=file_path, allow_mass_removal=allow_mass_removal)


def sync_index(container_out, blob_name: str, allow_mass_removal: bool = False, run=default_run) -> dict:
    """Download scraped JSON (+ failures sidecar if present) -> sync into the platform index. Raises on failure."""
    log.info("index sync waiting for lock: %s", blob_name)
    with _JOB_LOCK, tempfile.TemporaryDirectory(prefix="index_") as tmp:
        src = Path(tmp) / Path(blob_name).name
        src.write_bytes(container_out.download_blob(blob_name).readall())
        side_name = blob_name[:-len(".json")] + "_failures.json"
        try:  # the scraper uploads the sidecar BEFORE the main JSON; absent = a clean run
            (Path(tmp) / Path(side_name).name).write_bytes(container_out.download_blob(side_name).readall())
            log.info("failures sidecar loaded: %s", side_name)
        except Exception as e:  # noqa: BLE001 - only 'not found' is expected
            if "BlobNotFound" not in type(e).__name__ and "NotFound" not in type(e).__name__:
                raise
        result = run(str(src), allow_mass_removal)
        if not result.get("success"):
            raise RuntimeError(f"index sync failed: {result.get('error') or 'unknown'}")
        log.info("INDEX_SYNCED index=%s source=%s pages=%s uploaded=%s deleted=%s urls_removed=%s kept_failed=%s elapsed=%ss",
                 result.get("index_name"), blob_name, result.get("pages_indexed"), result.get("chunks_uploaded"),
                 result.get("chunks_deleted"), result.get("urls_removed"), result.get("chunks_retained_failed"),
                 result.get("elapsed_seconds"))
        return result
