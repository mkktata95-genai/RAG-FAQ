"""Offline check: python test_indexer_job.py  (no Azure, no network)."""
import datetime as dt
from types import SimpleNamespace as NS
from src import indexer_job as j


class Box:
    def __init__(s, files=None): s.f = dict(files or {})
    def list_blobs(s, name_starts_with=""): return [NS(name=k, last_modified=v[1]) for k, v in s.f.items() if k.startswith(name_starts_with)]
    def download_blob(s, n): return NS(readall=lambda: s.f[n][0])


class Ops:
    def __init__(s, idx, alias=None, counts=None): s.idx, s.alias, s.counts, s.deleted = list(idx), alias, counts or {}, []
    def list_indexes(s): return list(s.idx)
    def count(s, i): return s.counts[i]
    def get_alias(s): return s.alias
    def set_alias(s, i): s.alias = i
    def delete(s, i): s.deleted.append(i); s.idx.remove(i)


t = lambda d: dt.datetime(2026, 1, d)
P = "royal_london_faq_approved_httpV1_"
# names / filtering
assert j.is_main_json("scraped-data/" + P + "20260101_000000.json")
assert not j.is_main_json("scraped-data/" + P + "20260101_000000_failures.json")
assert not j.is_main_json("scraped-data/other.json") and not j.is_main_json("scraped-data/" + P + "x.xlsx")
box = Box({"scraped-data/" + P + "a.json": (b"A", t(1)), "scraped-data/" + P + "b.json": (b"B", t(2)),
           "scraped-data/" + P + "b_failures.json": (b"F", t(3))})  # newest, must be ignored
assert j.latest_scraped(box) == "scraped-data/" + P + "b.json"
assert j.parse_build_message('{"file":"x.json"}') == "scraped-data/x.json" and j.parse_build_message("run") is None
assert j.parse_promote_message("promote") == (None, False) and j.parse_promote_message("promote FORCE") == (None, True)
assert j.parse_promote_message('{"index":"rlg-faq-index-20260101-000000","force":true}') == ("rlg-faq-index-20260101-000000", True)
assert j.INDEX_RE.match(j.new_index_name()) and not j.INDEX_RE.match("rlg-faq-index-v5")

# build ok: unique new name, file present while running, nothing deleted
seen = {}
def ok_run(name, path):
    seen.update(name=name, data=open(path, "rb").read()); return dict(success=True, pages_indexed=3, chunks_uploaded=9, elapsed_seconds=1)
ops = Ops([])
r = j.build_index(box, "scraped-data/" + P + "b.json", run=ok_run, ops_factory=lambda: ops)
assert seen["data"] == b"B" and j.INDEX_RE.match(seen["name"]) and r["index_name"] == seen["name"] and not ops.deleted

# build failure: partial index deleted, error raised
ops = Ops([])
def bad_run(name, path): ops.idx.append(name); return dict(success=False, error="boom")
try:
    j.build_index(box, "scraped-data/" + P + "b.json", run=bad_run, ops_factory=lambda: ops); raise SystemExit("should raise")
except RuntimeError as e:
    assert "boom" in str(e) and ops.deleted and ops.idx == []

# promote: bootstrap (no alias), newest by default; legacy index never listed/deleted
n = lambda d: f"rlg-faq-index-2026010{d}-000000"
ops = Ops([n(1), n(2), n(3), "rlg-faq-index-v5"])
assert j.promote(ops)["promoted"] and ops.alias == n(3) and ops.deleted == []
# retention keeps newest 3 + live + previous live; deletes only older built ones
j.KEEP_INDEXES = 2
ops = Ops([n(1), n(2), n(3), n(4), "rlg-faq-index-v5"], alias=n(2), counts={n(2): 100, n(4): 90})
assert j.promote(ops, n(4))["promoted"] and ops.alias == n(4)
assert ops.deleted == [n(1)] and "rlg-faq-index-v5" in ops.idx and n(2) in ops.idx  # n2 = previous live, kept for rollback
# shrink gate refuses, then force overrides
ops = Ops([n(1), n(2)], alias=n(1), counts={n(1): 100, n(2): 10})
assert not j.promote(ops, n(2))["promoted"] and ops.alias == n(1)
assert j.promote(ops, n(2), force=True)["promoted"] and ops.alias == n(2)
# unknown index / already live
assert not j.promote(Ops([n(1)]), "rlg-faq-index-v5")["promoted"]
assert j.promote(Ops([n(1)], alias=n(1)))["reason"] == "already live"
print("OK")
