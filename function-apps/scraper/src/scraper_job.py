"""Hosting-layer core for the scraper Function App.

Plain functions only (no azure.functions import) so it is testable offline.
The scraper script itself (src/scrape_approved_urls_httpV1.py) is NOT modified.
"""
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("scraper_job")

INPUT_PREFIX = os.environ.get("SCRAPER_INPUT_PREFIX", "approved-urls/")
OUTPUT_PREFIX = os.environ.get("SCRAPER_OUTPUT_PREFIX", "scraped-data/")
# >5% failed URLs: output kept for diagnosis but kept OUT of scraped-data/ so
# the indexer blob trigger never builds an index from an incomplete page set.
REJECTED_PREFIX = os.environ.get("SCRAPER_REJECTED_PREFIX", "scraper-rejected/")

# The scraper writes to ./scraper/data relative to cwd and we chdir per run,
# so only one run may be active per process (blob + queue triggers can overlap).
_JOB_LOCK = threading.Lock()


def is_excel(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return base.lower().endswith(".xlsx") and not base.startswith("~$")


def latest_excel(container_in) -> str:
    """Newest .xlsx under INPUT_PREFIX (full blob name)."""
    blobs = [b for b in container_in.list_blobs(name_starts_with=INPUT_PREFIX) if is_excel(b.name)]
    if not blobs:
        raise FileNotFoundError(f"No .xlsx found under input/{INPUT_PREFIX}")
    return max(blobs, key=lambda b: b.last_modified).name


def parse_queue_message(body: str) -> str | None:
    """Optional {"excel": "file.xlsx"}; anything else means 'use latest'."""
    try:
        name = json.loads(body).get("excel")
        return (INPUT_PREFIX + name) if name else None
    except (ValueError, AttributeError):
        return None


def default_scrape(excel_path: str) -> dict:
    from src import scrape_approved_urls_httpV1 as scraper  # lazy: heavy imports
    return scraper.run_scraper(excel_path=excel_path)


def _upload(container_out, blob_name: str, path: Path) -> None:
    with open(path, "rb") as f:
        container_out.upload_blob(name=blob_name, data=f, overwrite=True)
    log.info("uploaded %s (%d bytes)", blob_name, path.stat().st_size)


def process_excel(container_in, container_out, blob_name: str, scrape=default_scrape) -> dict:
    """Download Excel -> run scraper in a temp cwd -> upload results.

    Raises on: no output, or failure ratio over the scraper's limit (retried once by the host).
    Partial run (<= limit): uploads output + failures, logs ERROR (alert hook), does NOT raise
    (a retry would re-scrape and create duplicate outputs).
    """
    log.info("scrape job waiting for lock: %s", blob_name)
    with _JOB_LOCK, tempfile.TemporaryDirectory(prefix="scrape_") as tmp:
        tmp = Path(tmp)
        excel = tmp / Path(blob_name).name
        excel.write_bytes(container_in.download_blob(blob_name).readall())
        log.info("downloaded %s -> %s", blob_name, excel)

        prev_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            result = scrape(str(excel))
            out = Path(result["output_path"]).resolve() if result.get("output_path") else None
            fail = Path(result["failures_path"]).resolve() if result.get("failures_path") else None
        finally:
            os.chdir(prev_cwd)

        if out is None or not out.exists():
            raise RuntimeError(f"scrape produced no output: {result.get('error') or 'unknown'}")

        ok = bool(result.get("success"))
        dest = OUTPUT_PREFIX if ok else REJECTED_PREFIX
        if fail:  # failures first, main JSON last (JSON upload is what triggers the indexer)
            _upload(container_out, dest + fail.name, fail)
        _upload(container_out, dest + out.name, out)

        if not ok:
            raise RuntimeError(f"scrape rejected, uploaded to {dest}: {result.get('error')}")
        if result.get("pages_failed"):
            log.error("SCRAPE_PARTIAL pages_failed=%s pages_scraped=%s failures=%s%s",
                      result["pages_failed"], result["pages_scraped"], dest, fail.name if fail else "")
        else:
            log.info("scrape complete pages=%s elapsed=%ss", result["pages_scraped"], result["elapsed_seconds"])
        return result
