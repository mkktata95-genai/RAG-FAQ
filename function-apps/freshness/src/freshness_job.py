"""Hosting-layer core for the Freshness Function App.

Plain functions only (no azure.functions import) so it is testable offline.
The Freshness script (src/content_freshness_httpV1.py) is NOT modified apart from
the v1.1.2 FRESHNESS_WORK_DIR override (its archive/manifest/report folders must
live on a writable disk; the deployed package is read-only).

A run: download the newest approved-URLs Excel from blob -> run the script
(report or apply) -> upload the report, manifest and the archive of deleted
chunks to blob, even when the run fails -> raise if the script reported errors.
"""
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("freshness_job")

INPUT_PREFIX = os.environ.get("FRESHNESS_INPUT_PREFIX", "approved-urls/")
# folder the script writes into -> blob prefix in the output container
OUTPUT_PREFIXES = {
    "freshness_reports": os.environ.get("FRESHNESS_REPORT_PREFIX", "freshness-reports/"),
    "freshness_manifests": os.environ.get("FRESHNESS_MANIFEST_PREFIX", "freshness-manifests/"),
    "freshness_archive": os.environ.get("FRESHNESS_ARCHIVE_PREFIX", "freshness-archive/"),
}
MODES = ("report", "apply")

# Must be set BEFORE the script is imported: it creates its folders at import time.
WORK_DIR = Path(os.environ.get("FRESHNESS_WORK_DIR") or Path(tempfile.gettempdir()) / "freshness_work")
os.environ["FRESHNESS_WORK_DIR"] = str(WORK_DIR)

# The script keeps config in module globals and writes into one shared work dir,
# so one run per process (timer and queue triggers can overlap).
_JOB_LOCK = threading.Lock()


# ── input helpers ─────────────────────────────────────────────
def is_excel(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return base.lower().endswith(".xlsx") and not base.startswith("~$")


def latest_excel(container_in) -> str:
    """Newest .xlsx under INPUT_PREFIX (full blob name): the same approved list the scraper uses."""
    blobs = [b for b in container_in.list_blobs(name_starts_with=INPUT_PREFIX) if is_excel(b.name)]
    if not blobs:
        raise FileNotFoundError(f"No .xlsx found under input/{INPUT_PREFIX}")
    return max(blobs, key=lambda b: b.last_modified).name


def parse_request_message(body: str) -> dict:
    """Queue message -> run options.

    JSON {"mode": "report|apply", "dry_run": bool, "allow_mass_removal": bool, "excel": "file.xlsx"}
    or just the word report / apply. Anything else = safe default (report). An invalid mode raises:
    a retry cannot fix it, and apply must never be guessed.
    """
    opts = {"mode": "report", "dry_run": False, "allow_mass_removal": False, "excel": None}
    text = body.strip()
    try:
        d = json.loads(text)
    except ValueError:
        d = {"mode": text.lower()} if text.lower() in MODES else {}
    if isinstance(d, dict):
        if "mode" in d:
            mode = str(d["mode"]).lower()
            if mode not in MODES:
                raise ValueError(f"invalid mode {d['mode']!r}: use one of {MODES}")
            opts["mode"] = mode
        opts["dry_run"] = bool(d.get("dry_run"))
        opts["allow_mass_removal"] = bool(d.get("allow_mass_removal"))
        if d.get("excel"):
            opts["excel"] = INPUT_PREFIX + str(d["excel"])
    return opts


def timer_mode() -> str:
    """Mode for the scheduled run. FRESHNESS_TIMER_MODE, default report (apply must be switched on deliberately)."""
    mode = os.environ.get("FRESHNESS_TIMER_MODE", "report").strip().lower()
    if mode not in MODES:
        raise ValueError(f"FRESHNESS_TIMER_MODE={mode!r} invalid: use one of {MODES}")
    return mode


# ── script call (faked in tests) ──────────────────────────────
def force_utf8_stdio() -> None:
    """The script prints emoji. A Windows host (local func start) gives the worker a cp1252
    stdout, where print() raises UnicodeEncodeError and kills the run. Azure Linux is already UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # not a reconfigurable text stream: nothing to fix


def default_run(*, mode: str, excel_path: str, dry_run: bool, allow_mass_removal: bool) -> dict:
    force_utf8_stdio()
    # Index name is required explicitly: the script's own default is a legacy index.
    need = ["AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_NAME"]
    if mode == "apply" and not dry_run:
        need.append("AZURE_OPENAI_ENDPOINT")
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing required app settings: {', '.join(missing)}")
    from src import content_freshness_httpV1 as m  # lazy: heavy imports, reads env at import
    log.info("freshness config: mode=%s dry_run=%s index=%s search=%s work_dir=%s version=%s",
             mode, dry_run, m.INDEX_NAME, m.SEARCH_ENDPOINT, WORK_DIR, m.FRESHNESS_JOB_VERSION)
    return m.run_freshness_job(mode=mode, excel_path=excel_path, dry_run=dry_run,
                               allow_mass_removal=allow_mass_removal)


# ── outputs ───────────────────────────────────────────────────
def _snapshot() -> set:
    return {p for p in WORK_DIR.rglob("*") if p.is_file()} if WORK_DIR.exists() else set()


def upload_outputs(container_out, before: set) -> list:
    """Upload every file this run created. Returns the failed file names (never raises)."""
    failed = []
    for p in sorted(_snapshot() - before):
        rel = p.relative_to(WORK_DIR)
        prefix = OUTPUT_PREFIXES.get(rel.parts[0])
        if prefix is None:
            log.warning("not uploaded (unknown output folder): %s", rel)
            continue
        blob = prefix + "/".join(rel.parts[1:])
        try:
            with open(p, "rb") as f:
                container_out.upload_blob(name=blob, data=f, overwrite=True)
            log.info("uploaded %s (%d bytes)", blob, p.stat().st_size)
        except Exception as e:  # noqa: BLE001 - keep uploading the rest
            log.error("FRESHNESS_UPLOAD_FAILED %s: %s", blob, e)
            failed.append(blob)
    return failed


# ── run ───────────────────────────────────────────────────────
def run_freshness(container_in, container_out, *, mode: str = "report", excel: str | None = None,
                  dry_run: bool = False, allow_mass_removal: bool = False, run=default_run) -> dict:
    """One Freshness run. Raises when the script fails or reports errors, after uploading outputs."""
    if mode not in MODES:
        raise ValueError(f"invalid mode {mode!r}")
    log.info("freshness job waiting for lock: mode=%s", mode)
    with _JOB_LOCK, tempfile.TemporaryDirectory(prefix="freshness_in_") as tmp:
        blob_name = excel or latest_excel(container_in)
        path = Path(tmp) / Path(blob_name).name
        path.write_bytes(container_in.download_blob(blob_name).readall())
        log.info("downloaded %s -> %s", blob_name, path)

        before = _snapshot()
        error = None
        result = None
        try:
            result = run(mode=mode, excel_path=str(path), dry_run=dry_run, allow_mass_removal=allow_mass_removal)
        except Exception as e:  # noqa: BLE001 - outputs must be uploaded first
            error = e
        failed_uploads = upload_outputs(container_out, before)

        if error is not None:
            log.error("FRESHNESS_FAILED mode=%s: %s", mode, error)
            raise error
        if failed_uploads:
            raise RuntimeError(f"run finished but {len(failed_uploads)} output file(s) were not uploaded "
                               f"(rollback archive may be missing): {failed_uploads}")
        s = result.get("run_summary", {})
        if not result.get("success"):
            log.error("FRESHNESS_ERRORS mode=%s errors=%s", mode, result.get("errors"))
            raise RuntimeError(f"freshness run reported errors: {result.get('errors')}")
        log.info("FRESHNESS_DONE mode=%s dry_run=%s unchanged=%s changed=%s new=%s scrape_failed=%s "
                 "chunks_added=%s chunks_deleted=%s elapsed=%ss", mode, dry_run, s.get("live_unchanged"),
                 s.get("changed"), s.get("new"), s.get("scrape_failed"), s.get("chunks_added"),
                 s.get("chunks_deleted"), result.get("elapsed_seconds"))
        return result