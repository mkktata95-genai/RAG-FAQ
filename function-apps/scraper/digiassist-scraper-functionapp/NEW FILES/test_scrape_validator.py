import datetime as dt
import json
import types
from pathlib import Path

import pytest

from src import scraper_job as job
from src import scrape_validator as sv

BODY = "Body text about pensions and tax. " * 20


def page(url, content, title="T", **kw):
    return {"source_url": url, "title": title, "content": content, **kw}


def keyed(pages):
    return {(p["source_url"], p.get("dropdown_state", "")): p for p in pages}


def sev(issues, s):
    return [i for i in issues if i[0] == s]


# ---- validator ------------------------------------------------------------

def test_clean_pages_pass():
    assert sv.validate_pages(keyed([page("a", "# A\n" + BODY)])) == []


def test_missing_h1_is_error():
    issues = sv.validate_pages(keyed([page("a", "[Trustpilot](x)\n## Products\n" + BODY, title="Insurance")]))
    assert len(sev(issues, "ERROR")) == 1 and "no H1" in issues[0][3]


def test_title_in_content_counts_as_heading():
    assert sv.validate_pages(keyed([page("a", "About Insurance\n" + BODY, title="Insurance")])) == []


def test_short_page_error_unless_short_in_prev_too():
    new = keyed([page("calc", "x" * 90)])
    assert len(sev(sv.validate_pages(new), "ERROR")) == 1
    prev = keyed([page("calc", "x" * 90)])
    issues = sv.validate_pages(new, prev)
    assert not sev(issues, "ERROR") and len(sev(issues, "WARN")) == 1


def test_shrink_and_removed_page():
    prev = keyed([page("a", "# A\n" + BODY * 3), page("gone", "# G\n" + BODY)])
    new = keyed([page("a", "# A\n" + BODY)])
    issues = sv.validate_pages(new, prev)
    assert any("shrank" in i[3] for i in sev(issues, "ERROR"))
    assert any(i[1] == "gone" for i in sev(issues, "WARN"))


def test_boilerplate_and_duplicate_are_warnings():
    new = keyed([page("a", "# A\n" + BODY + "Skip to main content"), page("b", "# B\n" + BODY), page("c", "# B\n" + BODY)])
    issues = sv.validate_pages(new)
    assert not sev(issues, "ERROR") and len(sev(issues, "WARN")) == 2


# ---- hosting-layer integration (fake containers, injected scrape) ----------

class FakeIn:
    def download_blob(self, name):
        return types.SimpleNamespace(readall=lambda: b"xlsx")


class FakeOut:
    def __init__(self, existing=None):
        self.existing = existing or {}  # name -> (bytes, last_modified)
        self.uploaded = {}

    def list_blobs(self, name_starts_with=""):
        return [types.SimpleNamespace(name=n, last_modified=t)
                for n, (_, t) in self.existing.items() if n.startswith(name_starts_with)]

    def download_blob(self, name):
        data = self.existing[name][0]
        return types.SimpleNamespace(readall=lambda: data)

    def upload_blob(self, name, data, overwrite=False):
        self.uploaded[name] = data.read()


def fake_scrape(pages):
    def _scrape(excel_path):
        Path("scraped.json").write_text(json.dumps(pages), encoding="utf-8")
        return {"success": True, "output_path": "scraped.json", "failures_path": "",
                "pages_scraped": len(pages), "pages_failed": 0, "elapsed_seconds": 0}
    return _scrape


GOOD = [page("a", "# A\n" + BODY)]
BAD = [page("a", "[Trustpilot](x)\n## Products\n" + BODY, title="Insurance")]  # H1 dropped


def run(pages, mode, out=None):
    job.VALIDATION_MODE = mode
    out = out or FakeOut()
    res = job.process_excel(FakeIn(), out, "approved-urls/x.xlsx", scrape=fake_scrape(pages))
    return res, out


@pytest.fixture(autouse=True)
def restore_mode():
    old = job.VALIDATION_MODE
    yield
    job.VALIDATION_MODE = old


def test_warn_mode_never_blocks():
    _, out = run(BAD, "warn")
    assert list(out.uploaded) == ["scraped-data/scraped.json"]


def test_block_mode_routes_bad_scrape_to_rejected():
    job.VALIDATION_MODE = "block"
    out = FakeOut()
    with pytest.raises(RuntimeError, match="content validation failed"):
        job.process_excel(FakeIn(), out, "approved-urls/x.xlsx", scrape=fake_scrape(BAD))
    assert list(out.uploaded) == ["scraper-rejected/scraped.json"]


def test_block_mode_clean_scrape_is_accepted():
    _, out = run(GOOD, "block")
    assert list(out.uploaded) == ["scraped-data/scraped.json"]


def test_block_mode_uses_previous_accepted_scrape_as_baseline():
    t = dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)
    prev = [page("a", "# A\n" + BODY * 3)]
    existing = {"scraped-data/old.json": (json.dumps(prev).encode(), t),
                "scraped-data/old_failures.json": (b"[]", t + dt.timedelta(days=1))}  # must be ignored
    job.VALIDATION_MODE = "block"
    out = FakeOut(existing)
    with pytest.raises(RuntimeError, match="shrank"):
        job.process_excel(FakeIn(), out, "approved-urls/x.xlsx", scrape=fake_scrape(GOOD))


def test_off_mode_skips_validation():
    _, out = run(BAD, "off")
    assert list(out.uploaded) == ["scraped-data/scraped.json"]


def test_baseline_lookup_failure_skips_comparison_but_still_validates():
    class BrokenOut(FakeOut):
        def list_blobs(self, name_starts_with=""):
            raise RuntimeError("storage down")

    job.VALIDATION_MODE = "block"
    out = BrokenOut()
    job.process_excel(FakeIn(), out, "approved-urls/x.xlsx", scrape=fake_scrape(GOOD))  # accepted
    assert list(out.uploaded) == ["scraped-data/scraped.json"]
    out2 = BrokenOut()
    with pytest.raises(RuntimeError, match="content validation failed"):  # absolute checks still run
        job.process_excel(FakeIn(), out2, "approved-urls/x.xlsx", scrape=fake_scrape(BAD))


def test_validator_crash_never_fails_the_scrape(monkeypatch):
    def boom(*a, **k):
        raise ValueError("validator bug")
    monkeypatch.setattr(sv, "validate_pages", boom)
    job.VALIDATION_MODE = "block"
    out = FakeOut()
    job.process_excel(FakeIn(), out, "approved-urls/x.xlsx", scrape=fake_scrape(BAD))
    assert list(out.uploaded) == ["scraped-data/scraped.json"]


def test_load_pages_accepts_url_key_pages_wrapper_and_skips_urlless(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"pages": [{"url": "u", "content": "x"}, {"content": "no url"}]}), encoding="utf-8")
    assert list(sv.load_pages(f)) == [("u", "")]


def test_cli_exit_codes_and_csv(tmp_path, monkeypatch, capsys):
    good, bad, csv_out = tmp_path / "g.json", tmp_path / "b.json", tmp_path / "i.csv"
    good.write_text(json.dumps(GOOD), encoding="utf-8")
    bad.write_text(json.dumps(BAD), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["v", str(good)])
    sv.main()
    assert "RESULT: PASS" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["v", str(bad), "--prev", str(good), "--csv", str(csv_out)])
    with pytest.raises(SystemExit) as e:
        sv.main()
    assert e.value.code == 1 and csv_out.read_text(encoding="utf-8").startswith("severity,url")
