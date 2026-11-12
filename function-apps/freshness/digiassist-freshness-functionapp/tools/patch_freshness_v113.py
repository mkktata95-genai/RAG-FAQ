"""One-off patch: Freshness script v1.1.2 (hero-banner version) -> v1.1.3.

  1. FRESHNESS_WORK_DIR: report/manifest/archive folders go to a writable folder (Function App package is read-only)
  2. scraped_at falls back to the index time when the page has none (DateTimeOffset field, "" is invalid)
  3. embedding deployment and dimensions come from the app settings (current values stay the defaults)

Usage (repo root):  python tools\\patch_freshness_v113.py src\\content_freshness_httpV1.py
Safe: every edit must match exactly once, otherwise nothing is written. Keeps the file's line endings.
"""
import hashlib
import sys

EDITS = [
    ('FRESHNESS_JOB_VERSION = "1.1.2"',
     '# v1.1.3 - (1) FRESHNESS_WORK_DIR override: archive/manifest/report folders live on a writable disk;\n'
     '#          (2) scraped_at falls back to the index time (the index field is DateTimeOffset, "" is invalid);\n'
     '#          (3) embedding deployment/dimensions read from app settings, defaults unchanged.\n'
     '#          ROLLBACK: restore v1.1.2 file.\n'
     'FRESHNESS_JOB_VERSION = "1.1.3"'),
    ('EMBEDDING_DEPLOYMENT = "text-embedding-3-large"\nEMBEDDING_DIMS = 1536',
     'EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")\n'
     'EMBEDDING_DIMS = int(os.environ.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1536"))'),
    ('BASE_DIR = Path(__file__).resolve().parent\n',
     'BASE_DIR = Path(os.environ.get("FRESHNESS_WORK_DIR") or Path(__file__).resolve().parent)\n'),
    ('"scraped_at": page.get("scraped_at", ""),',
     '"scraped_at": page.get("scraped_at") or _indexed_at,'),
]

path = sys.argv[1] if len(sys.argv) > 1 else "src/content_freshness_httpV1.py"
with open(path, encoding="utf-8", newline="") as f:
    text = f.read()
crlf = "\r\n" in text
work = text.replace("\r\n", "\n")
for old, new in EDITS:
    n = work.count(old)
    if n != 1:
        sys.exit(f"STOP: expected exactly 1 match, found {n} for: {old[:60]!r}. Nothing written.")
    work = work.replace(old, new)
out = work.replace("\n", "\r\n") if crlf else work
with open(path, "w", encoding="utf-8", newline="") as f:
    f.write(out)
print("patched", path, "| line endings:", "CRLF" if crlf else "LF")
print("sha256 (LF-normalised):", hashlib.sha256(work.encode("utf-8")).hexdigest()[:16])