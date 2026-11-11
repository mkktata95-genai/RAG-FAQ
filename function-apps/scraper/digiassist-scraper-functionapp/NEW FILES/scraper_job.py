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
# Post-scrape content validation (src/scrape_validator.py):
#   off   - skipped
#   warn  - issues logged (SCRAPE_VALIDATION ...), nothing else changes  [default]
#   block - content ERRORs route the output to REJECTED_PREFIX like the failure-ratio gate
# To override a block after reviewing the ERRORs, set SCRAPER_VALIDATION_MODE=warn and re-run.
VALIDATION_MODE = os.environ.get("SCRAPER_VALIDATION_MODE", "warn").strip().lower()

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


def _previous_output(container_out, tmp: Path) -> Path | None:
    """Newest earlier ACCEPTED scrape JSON under OUTPUT_PREFIX; None on first run or lookup failure.
    Must be called before the new output is uploaded."""
    try:
        blobs = [b for b in container_out.list_blobs(name_starts_with=OUTPUT_PREFIX)
                 if b.name.lower().endswith(".json") and not b.name.lower().endswith("_failures.json")]
        if not blobs:
            return None
        newest = max(blobs, key=lambda b: b.last_modified)
        p = tmp / "prev_scrape.json"
        p.write_bytes(container_out.download_blob(newest.name).readall())
        log.info("validation baseline: %s", newest.name)
        return p
    except Exception:
        log.warning("validation: previous scrape lookup failed - comparison checks skipped", exc_info=True)
        return None


def _validate(container_out, out: Path, tmp: Path) -> list:
    """Run content validation on the scraped JSON. Returns the ERROR issues.
    Never raises: a validator problem is logged and the scrape carries on."""
    if VALIDATION_MODE == "off":
        return []
    try:
        from src import scrape_validator as sv  # lazy
        prev = _previous_output(container_out, tmp)
        issues = sv.validate_pages(sv.load_pages(out), sv.load_pages(prev) if prev else None)
    except Exception:
        log.warning("validation: validator failed - skipped", exc_info=True)
        return []
    errs = [i for i in issues if i[0] == "ERROR"]
    if issues:
        for sev, url, variant, msg in issues[:50]:
            log.warning("SCRAPE_VALIDATION %s %s%s: %s", sev, url, f" [{variant}]" if variant else "", msg)
        log.warning("SCRAPE_VALIDATION mode=%s errors=%d warnings=%d", VALIDATION_MODE, len(errs), len(issues) - len(errs))
    else:
        log.info("SCRAPE_VALIDATION clean")
    return errs


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
        if ok:
            errs = _validate(container_out, out, tmp)
            if errs and VALIDATION_MODE == "block":
                ok = False
                result["error"] = (f"content validation failed: {len(errs)} error(s), "
                                   f"first: {errs[0][1]}: {errs[0][3]}")
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
