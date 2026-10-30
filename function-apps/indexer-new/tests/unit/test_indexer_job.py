import datetime as dt
from types import SimpleNamespace as NS

import pytest

from src import indexer_job as job

P = "royal_london_faq_approved_httpV1_"


class NotFound(Exception):
    pass


NotFound.__name__ = "ResourceNotFoundError"


class Box:
    """Fake blob container: {blob name: (bytes, last_modified)}."""

    def __init__(self, files=None):
        self.f = dict(files or {})

    def list_blobs(self, name_starts_with=""):
        return [NS(name=k, last_modified=v[1]) for k, v in self.f.items() if k.startswith(name_starts_with)]

    def download_blob(self, name):
        if name not in self.f:
            raise NotFound(name)
        return NS(readall=lambda: self.f[name][0])


def t(day):
    return dt.datetime(2026, 1, day)


@pytest.mark.parametrize("name,expected", [
    ("scraped-data/" + P + "20260101_000000.json", True),
    ("scraped-data/" + P + "20260101_000000_failures.json", False),
    ("scraped-data/other.json", False),
    ("scraped-data/" + P + "x.xlsx", False),
])
def test_is_main_json(name, expected):
    assert job.is_main_json(name) is expected


def test_latest_scraped_ignores_failures_sidecar():
    box = Box({"scraped-data/" + P + "a.json": (b"A", t(1)), "scraped-data/" + P + "b.json": (b"B", t(2)),
               "scraped-data/" + P + "b_failures.json": (b"F", t(3))})
    assert job.latest_scraped(box) == "scraped-data/" + P + "b.json"


def test_latest_scraped_raises_when_empty():
    with pytest.raises(FileNotFoundError):
        job.latest_scraped(Box())


@pytest.mark.parametrize("body,expected", [
    ('{"file":"x.json"}', ("scraped-data/x.json", False)),
    ('{"file":"x.json","allow_mass_removal":true}', ("scraped-data/x.json", True)),
    ("run", (None, False)),
    ("allow_mass_removal", (None, True)),
])
def test_parse_build_message(body, expected):
    assert job.parse_build_message(body) == expected


def test_sync_index_passes_main_file_and_sidecar_together():
    box = Box({"scraped-data/" + P + "b.json": (b"B", t(1)), "scraped-data/" + P + "b_failures.json": (b"F", t(1))})
    seen = {}

    def run(path, allow):
        from pathlib import Path
        p = Path(path)
        seen.update(main=p.read_bytes(), side=p.with_name(p.stem + "_failures.json").read_bytes(), allow=allow)
        return {"success": True}

    job.sync_index(box, "scraped-data/" + P + "b.json", True, run=run)
    assert seen == {"main": b"B", "side": b"F", "allow": True}


def test_sync_index_works_without_sidecar():
    box = Box({"scraped-data/" + P + "a.json": (b"A", t(1))})
    assert job.sync_index(box, "scraped-data/" + P + "a.json", run=lambda p, a: {"success": True})["success"]


def test_sync_index_raises_on_failed_run():
    box = Box({"scraped-data/" + P + "a.json": (b"A", t(1))})
    with pytest.raises(RuntimeError, match="boom"):
        job.sync_index(box, "scraped-data/" + P + "a.json", run=lambda p, a: {"success": False, "error": "boom"})


def test_sync_index_propagates_unexpected_sidecar_error():
    class Broken(Box):
        def download_blob(self, name):
            if name.endswith("_failures.json"):
                raise PermissionError("denied")
            return super().download_blob(name)

    box = Broken({"scraped-data/" + P + "a.json": (b"A", t(1))})
    with pytest.raises(PermissionError):
        job.sync_index(box, "scraped-data/" + P + "a.json", run=lambda p, a: {"success": True})


def test_default_run_refuses_when_settings_missing(monkeypatch):
    for key in job.REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="AZURE_SEARCH_INDEX_NAME"):
        job.default_run("x.json")
