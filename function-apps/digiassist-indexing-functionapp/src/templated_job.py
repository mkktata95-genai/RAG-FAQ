"""Templated-questions Function App core: Excel -> validated rows -> sync into the platform-owned index.

The index is created and owned by the platform team (Terraform). This app has data-plane access only
(Search Index Data Contributor): it upserts documents into the fixed index named by
AZURE_SEARCH_TEMPLATED_INDEX_NAME and NEVER creates, deletes or aliases an index.

The Excel is the full source of truth: after a run the index holds exactly the active rows of that file.
Rows that are new or changed are upserted (same id = overwrite), ids no longer in the file are deleted
AFTER the upload succeeded. Rollback = upload the previous Excel again.

The query side (agent) parses response_json with LLMResponse (extra="forbid") and treats a parse failure
as a SILENT cache miss, so validate() mirrors those rules and a single bad row refuses the whole run
before anything is written.

Plain functions only (no azure.functions import) so it is testable offline.
"""
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("templated_job")

# ── settings (all overridable by app settings) ────────────────
INPUT_PREFIX = os.environ.get("TEMPLATED_INPUT_PREFIX", "templated-questions/")
# Agent rules (config.yaml prompt.answer + src/core/constant.py). Keep in sync.
ANSWER_HARD_MAX = int(os.environ.get("TEMPLATED_ANSWER_MAX_CHARS", "250"))  # char_hard_limit 200 + char_buffer 50
ANSWER_SOFT = int(os.environ.get("TEMPLATED_ANSWER_SOFT_CHARS", "180"))     # char_soft_limit (warn only)
CITATION_MIN, CITATION_MAX = int(os.environ.get("TEMPLATED_CITATION_MIN", "0")), int(os.environ.get("TEMPLATED_CITATION_MAX", "3"))
READMORE_MIN, READMORE_MAX = int(os.environ.get("TEMPLATED_READMORE_MIN", "0")), int(os.environ.get("TEMPLATED_READMORE_MAX", "3"))
INTENTS = {"blocked", "direct", "grounded"}
SCENARIOS = {"not_applicable", "standard", "advice_seeking", "distress", "escalation_and_handoff", "account_action"}
ALLOWED_HOSTS = tuple(h.strip().lower() for h in os.environ.get("TEMPLATED_ALLOWED_HOSTS", "royallondon.com").split(",") if h.strip())
ID_RE = re.compile(r"^[A-Za-z0-9\-][A-Za-z0-9_=\-]*$")  # same as scache ingest

# Circuit breaker: refuse a run that would delete this many ids AND more than this share of the index
# (protects against an empty / truncated / wrong Excel). Override per run with allow_mass_removal.
MIN_REMOVALS_FOR_GATE = 3
MAX_REMOVAL_RATIO = float(os.environ.get("TEMPLATED_MAX_REMOVAL_RATIO", "0.5"))

RETRY_ATTEMPTS = int(os.environ.get("TEMPLATED_RETRY_ATTEMPTS", "4"))
RETRY_BASE_SECONDS = float(os.environ.get("TEMPLATED_RETRY_BASE_SECONDS", "2"))
RETRY_MAX_SECONDS = 60.0
VERIFY_ATTEMPTS = int(os.environ.get("TEMPLATED_VERIFY_ATTEMPTS", "6"))   # doc count is eventually consistent
VERIFY_WAIT_SECONDS = float(os.environ.get("TEMPLATED_VERIFY_WAIT_SECONDS", "5"))
TRANSIENT_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
SCAN_PAGE = 1000  # Azure AI Search returns at most 1000 results per request

REQUIRED_ENV = ("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_TEMPLATED_INDEX_NAME", "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "AZURE_OPENAI_EMBEDDING_DIMENSIONS")
# Fields the platform index must expose (question_vector is not retrievable, so it cannot be probed).
REQUIRED_FIELDS = ("id", "question", "response_json", "source_urls", "created_at")

_JOB_LOCK = threading.Lock()  # one run at a time per process


# ── retry ─────────────────────────────────────────────────────
_TRANSIENT_NAMES = {
    "ServiceRequestError", "ServiceResponseError", "ClientAuthenticationError",  # azure-core (incl. token blips)
    "RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError",  # openai
    "TimeoutError", "ConnectionError",
}


def is_transient_error(exc: BaseException) -> bool:
    """True for errors worth retrying: network/timeout, throttling, 5xx, token-acquisition blips."""
    if any(c.__name__ in _TRANSIENT_NAMES for c in type(exc).__mro__):
        return True
    return getattr(exc, "status_code", None) in TRANSIENT_HTTP_STATUS


def with_retry(fn, *, op: str, attempts: int | None = None, base_seconds: float | None = None):
    """Call fn() retrying transient errors (exponential backoff + jitter). Every retry is logged at WARNING;
    a permanent error, or exhausted retries, is logged at ERROR and re-raised (never swallowed)."""
    attempts = attempts or RETRY_ATTEMPTS
    base = RETRY_BASE_SECONDS if base_seconds is None else base_seconds
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - classified below, always re-raised or retried
            transient = is_transient_error(e)
            if not transient or attempt >= attempts:
                log.error("operation_failed op=%s attempt=%s/%s transient=%s error=%s: %s",
                          op, attempt, attempts, transient, type(e).__name__, e)
                raise
            wait = min(RETRY_MAX_SECONDS, base * (2 ** (attempt - 1))) * random.uniform(0.75, 1.25)
            log.warning("operation_retry op=%s attempt=%s/%s wait=%.1fs error=%s: %s",
                        op, attempt, attempts, wait, type(e).__name__, e)
            time.sleep(wait)


# ── Excel -> rows ─────────────────────────────────────────────
def read_excel(path: str) -> list[dict]:
    """First sheet; header row = column names (case/space-insensitive). Blank rows skipped."""
    from openpyxl import load_workbook
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 - corrupt / not a real xlsx
        log.error("excel_unreadable path=%s error=%s: %s", path, type(e).__name__, e)
        raise ValueError(f"cannot read Excel file {Path(path).name}: {e}") from e
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [re.sub(r"\s+", "_", str(h).strip().lower()) if h is not None else "" for h in next(it, ())]
        rows = []
        for n, vals in enumerate(it, start=2):  # n = Excel row number
            row = {h: ("" if v is None else str(v).strip()) for h, v in zip(header, vals) if h}
            if any(row.values()):
                row["_row"] = n
                rows.append(row)
        return rows
    finally:
        wb.close()


# ── validation ────────────────────────────────────────────────
def norm_question(q: str) -> str:
    return re.sub(r"\s+", " ", q).strip().rstrip("?").strip().casefold()


def make_id(q: str) -> str:
    return "q-" + hashlib.sha1(norm_question(q).encode("utf-8")).hexdigest()[:10]


def _url_problem(url: str) -> str | None:
    p = urlparse(url)
    if p.scheme != "https" or not p.netloc:
        return "must be an absolute https URL"
    host = (p.hostname or "").lower()
    if ALLOWED_HOSTS and not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        return f"host '{host}' is not allowed (only {', '.join(ALLOWED_HOSTS)})"
    return None


def _pairs(row: dict, a: str, b: str, prefix: str, kind: str, lo: int, hi: int, errs: list):
    out = []
    for i in range(1, 10):
        x, y = row.get(f"{prefix}_{i}_{a}", ""), row.get(f"{prefix}_{i}_{b}", "")
        if not x and not y:
            continue
        if i > hi:
            errs.append(f"{kind} {i}: at most {hi} allowed")
            continue
        if not x or not y:
            errs.append(f"{kind} {i}: both {a} and {b} are required")
            continue
        bad = _url_problem(y)
        if bad:
            errs.append(f"{kind} {i} url {bad}")
            continue
        out.append({a: x, b: y})
    if len(out) < lo:
        errs.append(f"at least {lo} {kind} required")
    return out


def _bool(v: str, default=False):
    if v == "":
        return default
    if v.lower() in ("true", "yes", "y", "1"):
        return True
    if v.lower() in ("false", "no", "n", "0"):
        return False
    raise ValueError(v)


def validate(rows: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """-> (entries, errors, warnings). Any error means the run must be refused."""
    entries, errors, warnings, seen_q, seen_id = [], [], [], {}, {}
    for row in rows:
        n, errs = row["_row"], []
        if row.get("active", "y").lower() in ("n", "no", "false", "0"):
            continue  # retired row: not indexed (and removed from the index by the next sync)
        q, ans = row.get("question", ""), row.get("answer", "")
        if not q:
            errs.append("question is empty")
        if not ans:
            errs.append("answer is empty")
        elif len(ans) > ANSWER_HARD_MAX:
            errs.append(f"answer is {len(ans)} chars, over the {ANSWER_HARD_MAX} limit")
        elif len(ans) > ANSWER_SOFT:
            warnings.append(f"row {n}: answer is {len(ans)} chars (soft limit {ANSWER_SOFT})")
        intent, scenario = row.get("intent", "") or "grounded", row.get("scenario", "") or "standard"
        if intent not in INTENTS:
            errs.append(f"intent '{intent}' not in {sorted(INTENTS)}")
        if scenario not in SCENARIOS:
            errs.append(f"scenario '{scenario}' not in {sorted(SCENARIOS)}")
        try:
            boundary = _bool(row.get("advice_boundary", ""))
        except ValueError:
            errs.append("advice_boundary must be true/false")
            boundary = False
        cites = _pairs(row, "title", "url", "citation", "citation", CITATION_MIN, CITATION_MAX, errs)
        ctas = _pairs(row, "label", "url", "cta", "cta", READMORE_MIN, READMORE_MAX, errs)
        eid = row.get("id", "") or (make_id(q) if q else "")
        if row.get("id") and not ID_RE.match(eid):
            errs.append(f"id '{eid}' has invalid characters")
        if q:
            k = norm_question(q)
            if k in seen_q:
                errs.append(f"duplicate of row {seen_q[k]}")
            seen_q.setdefault(k, n)
        if eid:
            if eid in seen_id:
                errs.append(f"id '{eid}' already used in row {seen_id[eid]}")
            seen_id.setdefault(eid, n)
        if errs:
            errors += [f"row {n}: {e}" for e in errs]
            continue
        entries.append({
            "id": eid, "question": q,
            # key order == LLMResponse field order == model_dump_json order
            "response_json": json.dumps({"intent": intent, "scenario": scenario, "answer": ans,
                                         "advice_boundary": boundary, "citations": cites, "read_more": ctas},
                                        ensure_ascii=False, separators=(",", ":")),
            "source_urls": sorted({c["url"] for c in cites} | {c["url"] for c in ctas}),
        })
    if not entries and not errors:
        errors.append("no active rows found")
    return entries, errors, warnings


# ── embeddings ────────────────────────────────────────────────
def _openai_client():
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_ad_token_provider=get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"),
    )


def azure_embed(texts: list[str]) -> list[list[float]]:
    """Embed in batches of 16, each batch retried on transient errors. Output order == input order."""
    client = _openai_client()
    dims = int(os.environ["AZURE_OPENAI_EMBEDDING_DIMENSIONS"])
    deployment = os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]
    out: list[list[float]] = []
    for i in range(0, len(texts), 16):
        batch = texts[i:i + 16]
        r = with_retry(lambda b=batch: client.embeddings.create(model=deployment, input=b, dimensions=dims),
                       op="embed")
        out += [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
    return out


# ── Azure Search data plane (faked in tests) ──────────────────
class AzureSearchOps:
    """Documents only. No SearchIndexClient: this identity can not (and must not) manage indexes."""

    def __init__(self):
        from azure.identity import DefaultAzureCredential
        from azure.search.documents import SearchClient
        endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
        self.index_name = os.environ.get("AZURE_SEARCH_TEMPLATED_INDEX_NAME", "")
        if not endpoint or not self.index_name:
            raise RuntimeError("AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_TEMPLATED_INDEX_NAME must be set")
        self._client = SearchClient(endpoint, self.index_name, DefaultAzureCredential())

    def probe(self) -> int:
        """Preflight with data-plane calls only: the index exists and exposes the fields we write. Returns doc count."""
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
        try:
            count = with_retry(self._client.get_document_count, op="preflight_count")
            with_retry(lambda: list(self._client.search(search_text="*", select=list(REQUIRED_FIELDS), top=1)),
                       op="preflight_probe")
        except ResourceNotFoundError as e:
            raise RuntimeError(f"Search index '{self.index_name}' does not exist. It is created by the platform "
                               f"team (Terraform); this app never creates indexes.") from e
        except HttpResponseError as e:
            if getattr(e, "status_code", None) == 400:
                raise RuntimeError(f"Search index '{self.index_name}' does not match the expected schema "
                                   f"(needs {', '.join(REQUIRED_FIELDS)} + question_vector): {e.message}") from e
            raise
        return count

    def count(self) -> int:
        return with_retry(self._client.get_document_count, op="count")

    def scan_ids(self) -> set[str]:
        """Every id in the index. Raises if the scan is incomplete (a partial set would delete live entries)."""
        ids: set[str] = set()
        skip, page, total = 0, SCAN_PAGE, None
        while True:
            def _call(skip=skip):
                r = self._client.search(search_text="*", select=["id"], top=page, skip=skip, include_total_count=True)
                return list(r), r.get_count()
            batch, count = with_retry(_call, op="scan_ids")
            total = count if total is None else total
            ids.update(d["id"] for d in batch)
            if len(batch) < page:
                break
            skip += page
        if total is not None and len(ids) != total:
            raise RuntimeError(f"index scan incomplete: read {len(ids)} of {total} documents")
        return ids

    def upload(self, docs: list[dict]) -> None:
        for i in range(0, len(docs), 100):
            batch = docs[i:i + 100]
            res = with_retry(lambda b=batch: self._client.upload_documents(b), op="upload")  # upsert by key
            bad = [r.key for r in res if not r.succeeded]
            if bad:
                raise RuntimeError(f"upload failed for {len(bad)} id(s): {bad[:5]}")

    def delete(self, ids: list[str]) -> None:
        for i in range(0, len(ids), 100):
            batch = [{"id": x} for x in ids[i:i + 100]]
            res = with_retry(lambda b=batch: self._client.delete_documents(b), op="delete")
            bad = [r.key for r in res if not r.succeeded]
            if bad:
                raise RuntimeError(f"delete failed for {len(bad)} id(s): {bad[:5]}")


# ── sync ──────────────────────────────────────────────────────
def _verify_count(ops, expected: int) -> int:
    """Doc count is eventually consistent: poll a few times before declaring a mismatch."""
    got = -1
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        got = ops.count()
        if got == expected:
            return got
        log.warning("verify_count attempt=%s/%s expected=%s got=%s", attempt, VERIFY_ATTEMPTS, expected, got)
        if attempt < VERIFY_ATTEMPTS:
            time.sleep(VERIFY_WAIT_SECONDS)
    raise RuntimeError(f"index document count is {got}, expected {expected} after sync. Data is written; "
                       f"re-running the same file is safe (idempotent).")


def sync_entries(entries: list[dict], ops, embed=azure_embed, allow_mass_removal: bool = False,
                 dry_run: bool = False) -> dict:
    """Make the index hold exactly `entries`. Order: preflight -> read-only plan -> embed -> upsert -> delete stale -> verify."""
    t0, run_id = time.monotonic(), uuid.uuid4().hex[:8]
    dims = int(os.environ["AZURE_OPENAI_EMBEDDING_DIMENSIONS"])
    ops.probe()
    existing = ops.scan_ids()
    wanted = {e["id"] for e in entries}
    stale = sorted(existing - wanted)
    result = {"success": True, "run_id": run_id, "index_name": getattr(ops, "index_name", ""), "rows": len(entries),
              "added": len(wanted - existing), "updated": len(wanted & existing), "deleted": len(stale),
              "dry_run": dry_run}
    if (len(stale) >= MIN_REMOVALS_FOR_GATE and existing and len(stale) > MAX_REMOVAL_RATIO * len(existing)
            and not allow_mass_removal):
        msg = (f"refusing to delete {len(stale)} of {len(existing)} entries (limit {MAX_REMOVAL_RATIO:.0%}). "
               f"Wrong or truncated Excel? If intended, re-run with allow_mass_removal.")
        log.error("TEMPLATED_REFUSED run=%s %s", run_id, msg)
        raise RuntimeError(msg)
    if dry_run:
        log.warning("TEMPLATED_DRY_RUN run=%s add=%s update=%s delete=%s (nothing written)", run_id,
                    result["added"], result["updated"], result["deleted"])
        result["elapsed_seconds"] = round(time.monotonic() - t0, 1)
        return result

    vecs = embed([e["question"] for e in entries])
    if len(vecs) != len(entries) or any(len(v) != dims for v in vecs):
        raise RuntimeError(f"embedding result mismatch (expected {len(entries)} x {dims})")
    now = datetime.now(timezone.utc).isoformat()  # created_at is always set (DateTimeOffset)
    docs = [{"id": e["id"], "question": e["question"], "question_vector": v, "response_json": e["response_json"],
             "source_urls": e["source_urls"], "created_at": now} for e, v in zip(entries, vecs)]
    ops.upload(docs)
    if stale:
        ops.delete(stale)  # only after the upload succeeded: a failure here leaves extra entries, never fewer
    _verify_count(ops, len(entries))
    result["elapsed_seconds"] = round(time.monotonic() - t0, 1)
    return result


# ── blob / queue helpers ──────────────────────────────────────
def is_excel(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return base.lower().endswith(".xlsx") and not base.startswith(("~$", "."))  # skip Excel lock/temp files


def latest_excel(container) -> str:
    blobs = [b for b in container.list_blobs(name_starts_with=INPUT_PREFIX) if is_excel(b.name)]
    if not blobs:
        raise FileNotFoundError(f"No .xlsx found under input/{INPUT_PREFIX}")
    return max(blobs, key=lambda b: b.last_modified).name


def parse_build_message(body: str) -> tuple[str | None, bool, bool]:
    """Optional {"file": "x.xlsx", "allow_mass_removal": true, "dry_run": true}.
    Anything else = newest Excel, safe defaults. -> (blob name or None, allow_mass_removal, dry_run)"""
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            name = d.get("file")
            return ((INPUT_PREFIX + name) if name else None), bool(d.get("allow_mass_removal")), bool(d.get("dry_run"))
    except ValueError:
        pass
    low = body.lower()
    return None, "allow_mass_removal" in low, "dry_run" in low


def sync_workbook(container_in, blob_name: str, allow_mass_removal: bool = False, dry_run: bool = False,
                  embed=azure_embed, ops_factory=AzureSearchOps) -> dict:
    """Download Excel -> validate (no Azure calls yet) -> sync into the platform index. Raises on any failure."""
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing required app settings: {', '.join(missing)}")
    log.info("templated sync waiting for lock: %s", blob_name)
    with _JOB_LOCK, tempfile.TemporaryDirectory(prefix="templated_") as tmp:
        src = Path(tmp) / Path(blob_name).name
        src.write_bytes(container_in.download_blob(blob_name).readall())
        entries, errors, warnings = validate(read_excel(str(src)))
        for w in warnings:
            log.warning("TEMPLATED_WARN %s", w)
        if errors:
            for e in errors:
                log.error("TEMPLATED_REJECTED %s: %s", blob_name, e)
            raise ValueError(f"{blob_name} rejected, {len(errors)} problem(s); first: {errors[0]}")
        result = sync_entries(entries, ops_factory(), embed=embed, allow_mass_removal=allow_mass_removal, dry_run=dry_run)
        result["warnings"] = warnings
        if not dry_run:
            log.info("TEMPLATED_SYNCED index=%s source=%s rows=%s added=%s updated=%s deleted=%s elapsed=%ss",
                     result["index_name"], blob_name, result["rows"], result["added"], result["updated"],
                     result["deleted"], result["elapsed_seconds"])
        return result
