"""Wrapper core: message parsing, Excel selection, outputs upload, error handling. The script is faked."""
import io
import sys

import pytest

from src import freshness_job as j


# ── names / messages ──────────────────────────────────────────
@pytest.mark.parametrize("name, expected", [
    ("approved-urls/a.xlsx", True), ("approved-urls/A.XLSX", True),
    ("approved-urls/~$a.xlsx", False), ("approved-urls/a.csv", False),
])
def test_is_excel(name, expected):
    assert j.is_excel(name) is expected


def test_latest_excel_picks_newest_and_ignores_other_prefixes(fake_container, day):
    box = fake_container({"approved-urls/a.xlsx": (b"A", day(1)), "approved-urls/b.xlsx": (b"B", day(2)),
                          "approved-urls/~$b.xlsx": (b"L", day(5)), "approved-urls/notes.txt": (b"N", day(6)),
                          "other/c.xlsx": (b"C", day(7))})
    assert j.latest_excel(box) == "approved-urls/b.xlsx"


def test_latest_excel_none_found(fake_container):
    with pytest.raises(FileNotFoundError, match="No .xlsx found"):
        j.latest_excel(fake_container())


DEFAULT = {"mode": "report", "dry_run": False, "allow_mass_removal": False, "excel": None}


@pytest.mark.parametrize("body", ["run", "", "{}", "[1]", "garbage"])
def test_message_default_is_report(body):
    assert j.parse_request_message(body) == DEFAULT


@pytest.mark.parametrize("body, mode", [("apply", "apply"), ("APPLY", "apply"), ("report", "report"), (' {"mode": "Apply"} ', "apply")])
def test_message_mode(body, mode):
    assert j.parse_request_message(body)["mode"] == mode


def test_message_full_options():
    o = j.parse_request_message('{"mode":"apply","dry_run":true,"allow_mass_removal":true,"excel":"x.xlsx"}')
    assert o == {"mode": "apply", "dry_run": True, "allow_mass_removal": True, "excel": "approved-urls/x.xlsx"}


def test_message_invalid_mode_raises():
    with pytest.raises(ValueError, match="invalid mode"):
        j.parse_request_message('{"mode":"delete"}')


def test_timer_mode(monkeypatch):
    monkeypatch.delenv("FRESHNESS_TIMER_MODE", raising=False)
    assert j.timer_mode() == "report"
    monkeypatch.setenv("FRESHNESS_TIMER_MODE", " Apply ")
    assert j.timer_mode() == "apply"
    monkeypatch.setenv("FRESHNESS_TIMER_MODE", "nope")
    with pytest.raises(ValueError):
        j.timer_mode()


# ── run_freshness ─────────────────────────────────────────────
SUMMARY = dict(live_unchanged=5, changed=1, new=0, scrape_failed=0, chunks_added=3, chunks_deleted=3)


@pytest.fixture
def box(fake_container, day):
    return fake_container({"approved-urls/a.xlsx": (b"A", day(1)), "approved-urls/b.xlsx": (b"B", day(2))})


def run_writing(work, success=True, errors=None, files=None, raises=None, seen=None):
    def _run(**kw):
        if seen is not None:
            seen.update(kw, excel_bytes=open(kw["excel_path"], "rb").read())
        for rel, data in (files or {}).items():
            (work / rel).write_bytes(data)
        if raises:
            raise raises
        return dict(success=success, errors=errors or [], elapsed_seconds=1, run_summary=SUMMARY if success else {})
    return _run


def test_run_hands_over_newest_excel_and_uploads_only_new_outputs(box, fake_container, work):
    (work / "freshness_reports" / "old.xlsx").write_bytes(b"old")           # existed before the run
    seen, out = {}, fake_container()
    files = {"freshness_reports/r1.xlsx": b"R", "freshness_manifests/m1.json": b"M", "freshness_archive/d1.json": b"A"}
    j.run_freshness(box, out, mode="apply", allow_mass_removal=True, run=run_writing(work, files=files, seen=seen))
    assert seen["excel_bytes"] == b"B" and seen["mode"] == "apply" and seen["allow_mass_removal"] and not seen["dry_run"]
    assert out.uploaded == {"freshness-reports/r1.xlsx": b"R", "freshness-manifests/m1.json": b"M", "freshness-archive/d1.json": b"A"}
    from pathlib import Path
    assert not Path(seen["excel_path"]).exists()                              # temp input cleaned up


def test_run_uses_named_excel(box, fake_container, work):
    seen = {}
    j.run_freshness(box, fake_container(), excel="approved-urls/a.xlsx", run=run_writing(work, seen=seen))
    assert seen["excel_bytes"] == b"A" and seen["mode"] == "report"


def test_script_exception_still_uploads_outputs_then_raises(box, fake_container, work):
    out = fake_container()
    run = run_writing(work, files={"freshness_reports/r2.xlsx": b"R2"}, raises=RuntimeError("ABORTED before any index write"))
    with pytest.raises(RuntimeError, match="ABORTED"):
        j.run_freshness(box, out, mode="apply", run=run)
    assert "freshness-reports/r2.xlsx" in out.uploaded


def test_script_reported_errors_raise_after_upload(box, fake_container, work):
    out = fake_container()
    run = run_writing(work, success=False, errors=["delete failed for 1 URL(s)"], files={"freshness_archive/d3.json": b"A3"})
    with pytest.raises(RuntimeError, match="delete failed"):
        j.run_freshness(box, out, mode="apply", run=run)
    assert "freshness-archive/d3.json" in out.uploaded


def test_archive_upload_failure_is_fatal_even_on_script_success(box, fake_container, work):
    class Bad(fake_container):
        def upload_blob(self, name, data, overwrite=False):
            if "archive" in name:
                raise OSError("blob down")
            super().upload_blob(name, data, overwrite)
    with pytest.raises(RuntimeError, match="not uploaded"):
        j.run_freshness(box, Bad(), mode="apply", run=run_writing(work, files={"freshness_archive/d4.json": b"A4"}))


def test_unknown_output_folder_is_not_uploaded(box, fake_container, work):
    (work / "stray").mkdir(exist_ok=True)
    out = fake_container()
    j.run_freshness(box, out, run=run_writing(work, files={"stray/x.txt": b"x"}))
    assert out.uploaded == {}


def test_invalid_mode_rejected_before_download(fake_container):
    with pytest.raises(ValueError):
        j.run_freshness(fake_container(), fake_container(), mode="delete", run=lambda **k: {})


# ── default_run ───────────────────────────────────────────────
def test_default_run_requires_search_settings(monkeypatch):
    for k in ("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_NAME", "AZURE_OPENAI_ENDPOINT"):
        monkeypatch.delenv(k)
    with pytest.raises(RuntimeError) as e:
        j.default_run(mode="report", excel_path="x", dry_run=False, allow_mass_removal=False)
    assert "AZURE_SEARCH_ENDPOINT" in str(e.value) and "AZURE_SEARCH_INDEX_NAME" in str(e.value) and "OPENAI" not in str(e.value)


def test_default_run_apply_also_requires_openai(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
        j.default_run(mode="apply", excel_path="x", dry_run=False, allow_mass_removal=False)


def test_default_run_calls_script_with_options(mocker):
    from src import content_freshness_httpV1 as m
    run = mocker.patch.object(m, "run_freshness_job", return_value={"success": True})
    r = j.default_run(mode="report", excel_path="x.xlsx", dry_run=True, allow_mass_removal=False)
    assert r == {"success": True}
    run.assert_called_once_with(mode="report", excel_path="x.xlsx", dry_run=True, allow_mass_removal=False)


# ── stdout ────────────────────────────────────────────────────
def test_force_utf8_stdio_survives_cp1252_stdout(monkeypatch):
    raw = io.BytesIO()
    fake = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setattr(sys, "stderr", fake)
    j.force_utf8_stdio()
    print("\U0001f510 Step 0")
    fake.flush()
    assert "\U0001f510".encode("utf-8") in raw.getvalue()


def test_force_utf8_stdio_ignores_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", object())
    j.force_utf8_stdio()
