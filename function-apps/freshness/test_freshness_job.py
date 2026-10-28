"""Offline check: python test_freshness_job.py  (no Azure, no network)."""
import datetime as dt
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

# point the work dir at a throwaway folder BEFORE importing the job
_tmp = tempfile.mkdtemp(prefix="fresh_test_")
os.environ["FRESHNESS_WORK_DIR"] = _tmp
from src import freshness_job as j  # noqa: E402


class Box:
    def __init__(s, files=None): s.f = dict(files or {}); s.up = {}
    def list_blobs(s, name_starts_with=""): return [NS(name=k, last_modified=v[1]) for k, v in s.f.items() if k.startswith(name_starts_with)]
    def download_blob(s, n): return NS(readall=lambda: s.f[n][0])
    def upload_blob(s, name, data, overwrite=False): s.up[name] = data.read()


t = lambda d: dt.datetime(2026, 1, d)
# excel selection: newest .xlsx under approved-urls/, lock files and other prefixes ignored
box = Box({"approved-urls/a.xlsx": (b"A", t(1)), "approved-urls/b.xlsx": (b"B", t(2)),
           "approved-urls/~$b.xlsx": (b"L", t(5)), "approved-urls/notes.txt": (b"N", t(6)),
           "other/c.xlsx": (b"C", t(7))})
assert j.latest_excel(box) == "approved-urls/b.xlsx"
assert j.is_excel("approved-urls/X.XLSX") and not j.is_excel("approved-urls/~$x.xlsx") and not j.is_excel("a.csv")
try:
    j.latest_excel(Box()); raise SystemExit("should raise")
except FileNotFoundError:
    pass

# message parsing: safe default is report; apply only when explicit; bad mode raises
assert j.parse_request_message("run") == {"mode": "report", "dry_run": False, "allow_mass_removal": False, "excel": None}
assert j.parse_request_message("APPLY")["mode"] == "apply" and j.parse_request_message("report")["mode"] == "report"
o = j.parse_request_message('{"mode":"apply","dry_run":true,"allow_mass_removal":true,"excel":"x.xlsx"}')
assert o == {"mode": "apply", "dry_run": True, "allow_mass_removal": True, "excel": "approved-urls/x.xlsx"}
assert j.parse_request_message("{}")["mode"] == "report"
try:
    j.parse_request_message('{"mode":"delete"}'); raise SystemExit("should raise")
except ValueError:
    pass
os.environ.pop("FRESHNESS_TIMER_MODE", None)
assert j.timer_mode() == "report"
os.environ["FRESHNESS_TIMER_MODE"] = "Apply"; assert j.timer_mode() == "apply"
os.environ["FRESHNESS_TIMER_MODE"] = "nope"
try:
    j.timer_mode(); raise SystemExit("should raise")
except ValueError:
    pass

# run: Excel handed over, outputs uploaded to the right prefixes, only NEW files uploaded
W = Path(_tmp)
for d in j.OUTPUT_PREFIXES:
    (W / d).mkdir(exist_ok=True)
(W / "freshness_reports" / "old_report.xlsx").write_bytes(b"old")  # pre-existing: must not be re-uploaded
seen = {}
def ok_run(**kw):
    seen.update(kw, excel_bytes=open(kw["excel_path"], "rb").read())
    (W / "freshness_reports" / "freshness_report_apply_1.xlsx").write_bytes(b"R")
    (W / "freshness_manifests" / "run_manifest_1.json").write_bytes(b"M")
    (W / "freshness_archive" / "deleted_abc_1.json").write_bytes(b"A")
    return dict(success=True, errors=[], elapsed_seconds=1, run_summary=dict(live_unchanged=5, changed=1, new=0, scrape_failed=0, chunks_added=3, chunks_deleted=3))
out = Box()
r = j.run_freshness(box, out, mode="apply", allow_mass_removal=True, run=ok_run)
assert seen["excel_bytes"] == b"B" and seen["mode"] == "apply" and seen["allow_mass_removal"] and not seen["dry_run"]
assert out.up == {"freshness-reports/freshness_report_apply_1.xlsx": b"R", "freshness-manifests/run_manifest_1.json": b"M",
                  "freshness-archive/deleted_abc_1.json": b"A"}, out.up
assert not Path(seen["excel_path"]).exists()  # temp input cleaned up

# explicit excel name is used instead of the newest
seen.clear(); j.run_freshness(box, Box(), excel="approved-urls/a.xlsx", run=ok_run)
assert seen["excel_bytes"] == b"A" and seen["mode"] == "report"

# script raises (e.g. mass-removal breaker): outputs still uploaded, error re-raised
def breaker_run(**kw):
    (W / "freshness_reports" / "freshness_report_apply_2.xlsx").write_bytes(b"R2")
    raise RuntimeError("ABORTED before any index write")
out = Box()
try:
    j.run_freshness(box, out, mode="apply", run=breaker_run); raise SystemExit("should raise")
except RuntimeError as e:
    assert "ABORTED" in str(e) and "freshness-reports/freshness_report_apply_2.xlsx" in out.up

# script reports errors (success False): raises after upload
def err_run(**kw):
    (W / "freshness_archive" / "deleted_def_3.json").write_bytes(b"A3")
    return dict(success=False, errors=["delete failed for 1 URL(s)"], run_summary={})
out = Box()
try:
    j.run_freshness(box, out, mode="apply", run=err_run); raise SystemExit("should raise")
except RuntimeError as e:
    assert "delete failed" in str(e) and "freshness-archive/deleted_def_3.json" in out.up

# archive upload failure is fatal even when the script says success (rollback copy would be lost)
class BadBox(Box):
    def upload_blob(s, name, data, overwrite=False):
        if "archive" in name: raise OSError("blob down")
        super().upload_blob(name, data, overwrite)
def arch_run(**kw):
    (W / "freshness_archive" / "deleted_ghi_4.json").write_bytes(b"A4")
    return dict(success=True, errors=[], run_summary={})
try:
    j.run_freshness(box, BadBox(), mode="apply", run=arch_run); raise SystemExit("should raise")
except RuntimeError as e:
    assert "not uploaded" in str(e)

# invalid mode rejected before anything is downloaded
try:
    j.run_freshness(box, Box(), mode="delete", run=ok_run); raise SystemExit("should raise")
except ValueError:
    pass

# default_run refuses to run without the index name (script default is a legacy index)
for k in ("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_NAME", "AZURE_OPENAI_ENDPOINT"):
    os.environ.pop(k, None)
try:
    j.default_run(mode="report", excel_path="x", dry_run=False, allow_mass_removal=False); raise SystemExit("should raise")
except RuntimeError as e:
    assert "AZURE_SEARCH_ENDPOINT" in str(e) and "AZURE_SEARCH_INDEX_NAME" in str(e) and "OPENAI" not in str(e)
try:
    j.default_run(mode="apply", excel_path="x", dry_run=False, allow_mass_removal=False); raise SystemExit("should raise")
except RuntimeError as e:
    assert "AZURE_OPENAI_ENDPOINT" in str(e)
# force_utf8_stdio: cp1252 stdout would crash on emoji; after the call it must not
import io, sys  # noqa: E402
raw = io.BytesIO()
fake = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
saved = sys.stdout, sys.stderr
sys.stdout = sys.stderr = fake
try:
    j.force_utf8_stdio()
    print("\U0001f510 Step 0")  # raised UnicodeEncodeError before the fix
    fake.flush()
finally:
    sys.stdout, sys.stderr = saved
assert "\U0001f510".encode("utf-8") in raw.getvalue()
class NoReconf:  # a stream without reconfigure() is left alone, no crash
    pass
sys.stdout = NoReconf()
try:
    j.force_utf8_stdio()
finally:
    sys.stdout = saved[0]
print("OK")