"""Wrapper core: blob/queue helpers and process_excel (download -> scrape in temp cwd -> upload). Scraper is faked."""
import json
import os

import pytest

from src import scraper_job as j


# ── names / messages ──────────────────────────────────────────
@pytest.mark.parametrize("name, expected", [
    ("approved-urls/a.xlsx", True), ("approved-urls/A.XLSX", True),
    ("approved-urls/~$a.xlsx", False), ("approved-urls/a.csv", False), ("approved-urls/_failures.json", False),
])
def test_is_excel(name, expected):
    assert j.is_excel(name) is expected


def test_latest_excel_picks_newest_and_skips_temp_files(fake_container, day):
    box = fake_container({"approved-urls/a.xlsx": (b"A", day(1)), "approved-urls/b.xlsx": (b"B", day(2)),
                          "approved-urls/~$b.xlsx": (b"x", day(3)), "other/z.xlsx": (b"z", day(9))})
    assert j.latest_excel(box) == "approved-urls/b.xlsx"


def test_latest_excel_none_found(fake_container, day):
    with pytest.raises(FileNotFoundError, match="No .xlsx found"):
        j.latest_excel(fake_container({"approved-urls/readme.txt": (b"", day(1))}))


@pytest.mark.parametrize("body, expected", [
    ('{"excel": "a.xlsx"}', "approved-urls/a.xlsx"),
    ('{"excel": null}', None), ("{}", None), ("run", None), ("[1]", None), ("", None),
])
def test_parse_queue_message(body, expected):
    assert j.parse_queue_message(body) == expected


# ── process_excel ─────────────────────────────────────────────
def fake_scraper(failed=0, ok=True, write=True, pages=9):
    """Stands in for the script: writes output files into the CURRENT directory like the real one."""
    def run(path):
        assert os.path.exists(path), "the Excel must be downloaded before the scraper runs"
        os.makedirs("scraper/data")
        r = dict(success=ok, pages_scraped=pages, pages_failed=failed, output_path="", failures_path="",
                 elapsed_seconds=1.5, error="" if ok else "too many failures")
        if write:
            with open("scraper/data/x_1.json", "w") as f:
                json.dump([1], f)
            r["output_path"] = "scraper/data/x_1.json"
        if failed:
            with open("scraper/data/x_1_failures.json", "w") as f:
                f.write("[]")
            r["failures_path"] = "scraper/data/x_1_failures.json"
        return r
    return run


@pytest.fixture
def inbox(fake_container, day):
    return fake_container({"approved-urls/b.xlsx": (b"B", day(2))})


def test_clean_run_uploads_json_to_scraped_data(inbox, fake_container):
    out = fake_container()
    r = j.process_excel(inbox, out, "approved-urls/b.xlsx", fake_scraper())
    assert list(out.uploaded) == ["scraped-data/x_1.json"] and r["pages_scraped"] == 9


def test_partial_run_uploads_failures_first_then_main_json(inbox, fake_container, caplog):
    """The main JSON upload is what triggers the indexer, so the failures sidecar must already be there."""
    out = fake_container()
    with caplog.at_level("ERROR", logger="scraper_job"):
        j.process_excel(inbox, out, "approved-urls/b.xlsx", fake_scraper(failed=2))
    assert list(out.uploaded) == ["scraped-data/x_1_failures.json", "scraped-data/x_1.json"]
    assert "SCRAPE_PARTIAL pages_failed=2" in caplog.text


def test_rejected_run_goes_to_rejected_folder_and_raises(inbox, fake_container):
    """Over the failure limit: kept for diagnosis but OUT of scraped-data/, so no index is built from it."""
    out = fake_container()
    with pytest.raises(RuntimeError, match="scrape rejected, uploaded to scraper-rejected/"):
        j.process_excel(inbox, out, "approved-urls/b.xlsx", fake_scraper(failed=50, ok=False))
    assert list(out.uploaded) == ["scraper-rejected/x_1_failures.json", "scraper-rejected/x_1.json"]
    assert not any(n.startswith("scraped-data/") for n in out.uploaded)


def test_no_output_file_raises(inbox, fake_container):
    out = fake_container()
    with pytest.raises(RuntimeError, match="scrape produced no output: too many failures"):
        j.process_excel(inbox, out, "approved-urls/b.xlsx", fake_scraper(failed=1, ok=False, write=False))
    assert out.uploaded == {}


def test_working_directory_is_restored_even_when_the_scraper_crashes(inbox, fake_container):
    here = os.getcwd()

    def boom(path):
        raise ValueError("scraper blew up")
    with pytest.raises(ValueError, match="blew up"):
        j.process_excel(inbox, fake_container(), "approved-urls/b.xlsx", boom)
    assert os.getcwd() == here


def test_working_directory_is_restored_after_success(inbox, fake_container):
    here = os.getcwd()
    j.process_excel(inbox, fake_container(), "approved-urls/b.xlsx", fake_scraper())
    assert os.getcwd() == here


def test_missing_input_blob_raises(fake_container):
    with pytest.raises(KeyError):
        j.process_excel(fake_container(), fake_container(), "approved-urls/nope.xlsx", fake_scraper())


def test_default_scrape_calls_the_script(monkeypatch):
    from src import scrape_approved_urls_httpV1 as script
    seen = {}
    monkeypatch.setattr(script, "run_scraper", lambda excel_path: seen.setdefault("p", excel_path) and {"ok": 1})
    assert j.default_scrape("/tmp/a.xlsx") == {"ok": 1} and seen["p"] == "/tmp/a.xlsx"
