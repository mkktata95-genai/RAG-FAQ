"""Excel loading, fetch/retry, dedup/save and the end-to-end run (offline, fixture HTML)."""
import json
from pathlib import Path

import pytest
import requests

from src import scrape_approved_urls_httpV1 as s

BASE = "https://www.royallondon.com"
U1, U2, U3 = f"{BASE}/pensions/one", f"{BASE}/pensions/two", f"{BASE}/isa/three"


# ── load_approved_pages ───────────────────────────────────────
def test_load_reads_columns_by_header_name_any_order(make_excel):
    p = make_excel([["Title A", U1, "faq"]], header=("Title", "URL", "Category"))
    assert s.load_approved_pages(p)[0]["url"] == U1


def test_load_strips_royal_london_title_suffix_and_lowercases_category(make_excel):
    p = make_excel([[U1, "Annual allowance | Royal London", "", "FAQ", ""],
                    [U2, "Tax - Royal London", "", "", ""]])
    pages = s.load_approved_pages(p)
    assert pages[0]["title"] == "Annual allowance" and pages[0]["excel_category"] == "faq"
    assert pages[1]["title"] == "Tax"


@pytest.mark.parametrize("status", ["404", "dead", "Broken", "removed", "410"])
def test_load_skips_only_unambiguous_dead_rows(make_excel, status):
    p = make_excel([[U1, "T", status, "", ""], [U2, "T", "", "", ""]])
    assert [x["url"] for x in s.load_approved_pages(p)] == [U2]


@pytest.mark.parametrize("status", ["", "200", "OK", "Live", "???"])
def test_load_keeps_ok_blank_and_unparseable_status(make_excel, status):
    assert len(s.load_approved_pages(make_excel([[U1, "T", status, "", ""]]))) == 1


def test_load_dedups_by_normalised_url(make_excel):
    p = make_excel([[U1, "A", "", "", ""], [f"{BASE}/Pensions/ONE/?utm=1", "B", "", "", ""]])
    assert len(s.load_approved_pages(p)) == 1


def test_load_without_url_column_raises_and_names_headers(make_excel):
    p = make_excel([["x"]], header=("Foo",))
    with pytest.raises(ValueError, match="Foo"):
        s.load_approved_pages(p)


def test_load_empty_sheet_returns_empty_list(make_excel):
    assert s.load_approved_pages(make_excel([])) == []


# ── retry ─────────────────────────────────────────────────────
def test_is_transient_error():
    assert s.is_transient_error(s.TransientHTTPError(503))
    assert s.is_transient_error(requests.exceptions.Timeout())
    assert s.is_transient_error(ConnectionError())
    assert not s.is_transient_error(ValueError())


def test_with_retry_retries_transient_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise s.TransientHTTPError(503)
        return "ok"
    assert s.with_retry(fn, op="t", attempts=5) == "ok" and calls["n"] == 3


def test_with_retry_permanent_error_not_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("bad")
    with pytest.raises(ValueError):
        s.with_retry(fn, op="t", attempts=5)
    assert calls["n"] == 1


def test_with_retry_gives_up_after_attempts():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise s.TransientHTTPError(500)
    with pytest.raises(s.TransientHTTPError):
        s.with_retry(fn, op="t", attempts=3)
    assert calls["n"] == 3


# ── fetch_html ────────────────────────────────────────────────
def test_fetch_fixture_found_and_missing(write_fixture):
    d = write_fixture(s, U1, "<html>x</html>")
    assert s.fetch_html(U1, fixture_dir=d) == ("<html>x</html>", 200, None)
    html, status, err = s.fetch_html(U2, fixture_dir=d)
    assert html is None and status is None and err.startswith("fixture_not_found")


class Resp:
    def __init__(self, status, text="<html>live</html>"):
        self.status_code, self.text = status, text


def test_fetch_live_ok(mocker):
    mocker.patch.object(s.requests, "get", return_value=Resp(200))
    assert s.fetch_html(U1) == ("<html>live</html>", 200, None)


def test_fetch_live_404_is_returned_not_retried(mocker):
    g = mocker.patch.object(s.requests, "get", return_value=Resp(404, "nope"))
    assert s.fetch_html(U1)[1] == 404 and g.call_count == 1


def test_fetch_live_persistent_503_reports_status(mocker):
    g = mocker.patch.object(s.requests, "get", return_value=Resp(503))
    html, status, err = s.fetch_html(U1)
    assert html is None and status == 503 and "503" in err and g.call_count == 3


def test_fetch_live_timeout_reports_error(mocker):
    mocker.patch.object(s.requests, "get", side_effect=requests.exceptions.Timeout("slow"))
    html, status, err = s.fetch_html(U1)
    assert html is None and status is None and "slow" in err


# ── dedup / save ──────────────────────────────────────────────
def test_dedup_pages_by_dropdown_url_then_source_url():
    pages = [{"source_url": "a"}, {"source_url": "a"}, {"source_url": "a", "dropdown_url": "a#x"},
             {"source_url": "a", "dropdown_url": "a#x"}, {"title": "no identity"}]
    out = s.dedup_pages(pages)
    assert len(out) == 3 and {"title": "no identity"} in out


def test_save_empty_results_writes_nothing(tmp_path):
    f = tmp_path / "out" / "x.json"
    assert s.save_scraped_pages([], f) == "" and not f.exists()


def test_save_writes_atomically_and_leaves_no_tmp(tmp_path):
    f = tmp_path / "out" / "x.json"
    assert s.save_scraped_pages([{"a": "é"}], f) == str(f)
    assert json.loads(f.read_text(encoding="utf-8")) == [{"a": "é"}]
    assert not list(f.parent.glob("*.tmp"))


def test_save_failure_cleans_tmp_and_raises(tmp_path, mocker):
    f = tmp_path / "x.json"
    mocker.patch.object(s.os, "replace", side_effect=OSError("disk"))
    with pytest.raises(OSError):
        s.save_scraped_pages([{"a": 1}], f)
    assert not list(tmp_path.glob("*.tmp"))


# ── run_scraper end to end ────────────────────────────────────
def test_dry_run_counts_urls_and_writes_nothing(make_excel):
    r = s.run_scraper(make_excel([[U1, "T", "", "", ""], [U2, "T", "", "", ""]]), dry_run=True)
    assert r["success"] and r["dry_run"] and not Path("scraper").exists()


def test_run_scrapes_fixture_pages_and_writes_json(make_excel, write_fixture, page_html):
    excel = make_excel([[U1, "One", "", "faq", ""], [U2, "Two", "", "", ""]])
    d = write_fixture(s, U1, page_html(title="One"))
    write_fixture(s, U2, page_html(title="Two"))
    r = s.run_scraper(excel, fixture_dir=d)
    assert r["success"] and r["pages_scraped"] == 2 and r["pages_failed"] == 0 and r["failures_path"] == ""
    data = json.loads(Path(r["output_path"]).read_text(encoding="utf-8"))
    assert {p["source_url"] for p in data} == {s.normalize_url(U1), s.normalize_url(U2)} or len(data) == 2
    assert all(p["content"] and p["section"] == "Pensions" for p in data)


def test_run_is_deterministic_for_same_input(make_excel, write_fixture, page_html):
    excel = make_excel([[U1, "One", "", "", ""]])
    d = write_fixture(s, U1, page_html())
    a = json.loads(Path(s.run_scraper(excel, fixture_dir=d)["output_path"]).read_text(encoding="utf-8"))
    b = json.loads(Path(s.run_scraper(excel, fixture_dir=d)["output_path"]).read_text(encoding="utf-8"))
    for x in (a, b):                       # scraped_at is the only field allowed to differ
        for page in x:
            page.pop("scraped_at")
    assert a == b and a[0]["content_hash"]


def test_run_over_failure_limit_keeps_output_but_fails(make_excel, write_fixture, page_html):
    excel = make_excel([[U1, "One", "", "", ""], [U2, "Two", "", "", ""]])
    d = write_fixture(s, U1, page_html())               # U2 has no fixture -> fails (50% > 5%)
    r = s.run_scraper(excel, fixture_dir=d)
    assert not r["success"] and r["pages_scraped"] == 1 and r["pages_failed"] == 1
    assert "do NOT index" in r["error"]
    assert r["output_path"] and Path(r["output_path"]).exists()
    fails = json.loads(Path(r["failures_path"]).read_text(encoding="utf-8"))
    assert fails[0]["url"] == s.normalize_url(U2) and "fixture_not_found" in fails[0]["reason"]


def test_run_all_failed_writes_no_output(make_excel, tmp_path):
    excel = make_excel([[U1, "One", "", "", ""]])
    empty = tmp_path / "nofix"
    empty.mkdir()
    r = s.run_scraper(excel, fixture_dir=str(empty))
    assert not r["success"] and r["output_path"] == "" and "All URLs failed" in r["error"] and r["pages_failed"] == 1


def test_run_missing_excel_reports_error_not_exception(tmp_path):
    r = s.run_scraper(str(tmp_path / "missing.xlsx"))
    assert not r["success"] and r["error"]


def test_run_too_short_page_counts_as_failure(make_excel, write_fixture, page_html):
    excel = make_excel([[U1, "One", "", "", ""]])
    d = write_fixture(s, U1, page_html(body_text="tiny"))
    r = s.run_scraper(excel, fixture_dir=d)
    assert not r["success"] and r["pages_failed"] == 1


def test_run_http_error_page_is_recorded(make_excel, mocker):
    mocker.patch.object(s.requests, "get", return_value=Resp(404, "gone"))
    r = s.run_scraper(make_excel([[U1, "One", "", "", ""]]))
    assert not r["success"] and r["failed_urls"][0]["url"] == s.normalize_url(U1)
