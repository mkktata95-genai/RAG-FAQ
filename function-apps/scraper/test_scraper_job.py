"""Offline check: python test_scraper_job.py  (no Azure, no network)."""
import json, os, datetime as dt
from types import SimpleNamespace as NS
from src import scraper_job as j

class Box:
    def __init__(s, files=None): s.f = dict(files or {}); s.up = {}
    def list_blobs(s, name_starts_with=""): return [NS(name=k, last_modified=v[1]) for k, v in s.f.items() if k.startswith(name_starts_with)]
    def download_blob(s, n): return NS(readall=lambda: s.f[n][0])
    def upload_blob(s, name, data, overwrite): s.up[name] = data.read()

t = lambda d: dt.datetime(2026, 1, d)
cin = Box({"approved-urls/a.xlsx": (b"A", t(1)), "approved-urls/b.xlsx": (b"B", t(2)), "approved-urls/~$b.xlsx": (b"x", t(3)), "approved-urls/_failures.json": (b"x", t(4))})
assert j.latest_excel(cin) == "approved-urls/b.xlsx"
assert j.parse_queue_message('{"excel":"a.xlsx"}') == "approved-urls/a.xlsx" and j.parse_queue_message("run") is None

def fake(failed=0, ok=True, write=True):
    def run(path):
        assert os.path.exists(path)
        os.makedirs("scraper/data"); r = dict(success=ok, pages_scraped=9, pages_failed=failed, output_path="", failures_path="", elapsed_seconds=1, error="" if ok else "too many")
        if write:
            open("scraper/data/x_1.json", "w").write(json.dumps([1])); r["output_path"] = "scraper/data/x_1.json"
        if failed:
            open("scraper/data/x_1_failures.json", "w").write("[]"); r["failures_path"] = "scraper/data/x_1_failures.json"
        return r
    return run

cwd = os.getcwd()
o = Box(); j.process_excel(cin, o, "approved-urls/b.xlsx", fake()); assert list(o.up) == ["scraped-data/x_1.json"]
o = Box(); j.process_excel(cin, o, "approved-urls/b.xlsx", fake(failed=2)); assert list(o.up) == ["scraped-data/x_1_failures.json", "scraped-data/x_1.json"]  # failures first
o = Box()
try: j.process_excel(cin, o, "approved-urls/b.xlsx", fake(failed=50, ok=False)); assert 0
except RuntimeError: assert list(o.up) == ["scraper-rejected/x_1_failures.json", "scraper-rejected/x_1.json"]
try: j.process_excel(cin, Box(), "approved-urls/b.xlsx", fake(failed=1, ok=False, write=False)); assert 0
except RuntimeError: pass
assert os.getcwd() == cwd
print("OK")
