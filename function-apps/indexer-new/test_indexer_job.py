"""Offline check: python test_indexer_job.py  (no Azure, no network)."""
import datetime as dt
from types import SimpleNamespace as NS
from src import indexer_job as j


class NotFound(Exception):
    pass
NotFound.__name__ = "ResourceNotFoundError"


class Box:
    def __init__(s, files=None): s.f = dict(files or {})
    def list_blobs(s, name_starts_with=""): return [NS(name=k, last_modified=v[1]) for k, v in s.f.items() if k.startswith(name_starts_with)]
    def download_blob(s, n):
        if n not in s.f: raise NotFound(n)
        return NS(readall=lambda: s.f[n][0])


t = lambda d: dt.datetime(2026, 1, d)
P = "royal_london_faq_approved_httpV1_"
assert j.is_main_json("scraped-data/" + P + "20260101_000000.json")
assert not j.is_main_json("scraped-data/" + P + "20260101_000000_failures.json")
assert not j.is_main_json("scraped-data/other.json") and not j.is_main_json("scraped-data/" + P + "x.xlsx")
box = Box({"scraped-data/" + P + "a.json": (b"A", t(1)), "scraped-data/" + P + "b.json": (b"B", t(2)),
           "scraped-data/" + P + "b_failures.json": (b"F", t(3))})  # newest, must be ignored
assert j.latest_scraped(box) == "scraped-data/" + P + "b.json"
assert j.parse_build_message('{"file":"x.json"}') == ("scraped-data/x.json", False)
assert j.parse_build_message('{"file":"x.json","allow_mass_removal":true}') == ("scraped-data/x.json", True)
assert j.parse_build_message("run") == (None, False) and j.parse_build_message("allow_mass_removal") == (None, True)

# sync with sidecar: both files present side by side while the script runs
seen = {}
def ok_run(path, allow):
    from pathlib import Path
    p = Path(path); seen.update(main=p.read_bytes(), side=(p.with_name(p.stem + "_failures.json")).read_bytes(), allow=allow)
    return dict(success=True, index_name="idx", pages_indexed=3, chunks_uploaded=9, chunks_deleted=1, elapsed_seconds=1)
j.sync_index(box, "scraped-data/" + P + "b.json", True, run=ok_run)
assert seen == dict(main=b"B", side=b"F", allow=True)

# no sidecar = clean run, still works
seen.clear()
def run2(path, allow): seen["ok"] = True; return dict(success=True)
j.sync_index(box, "scraped-data/" + P + "a.json", run=run2); assert seen["ok"]

# failure raises
try:
    j.sync_index(box, "scraped-data/" + P + "a.json", run=lambda p, a: dict(success=False, error="boom")); raise SystemExit("should raise")
except RuntimeError as e:
    assert "boom" in str(e)

# missing settings refuse
import os
for k in j.REQUIRED_ENV: os.environ.pop(k, None)
try:
    j.default_run("x.json"); raise SystemExit("should refuse")
except RuntimeError as e:
    assert "AZURE_SEARCH_INDEX_NAME" in str(e)
print("OK")
