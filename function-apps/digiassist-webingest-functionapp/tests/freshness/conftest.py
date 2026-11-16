"""Shared test setup. Unit tests never touch the network or Azure:
- FRESHNESS_WORK_DIR points at a throwaway folder BEFORE anything is imported (the script creates folders at import)
- app settings are fake values; retry tuning is pinned
- DefaultAzureCredential is replaced with one that raises; time.sleep is mocked
"""
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest import mock

import pytest
from openpyxl import Workbook

WORK = tempfile.mkdtemp(prefix="fresh_unit_")
for _k in [k for k in os.environ if k.startswith(("FRESHNESS_", "PIPELINE_", "AZURE_SEARCH", "AZURE_OPENAI", "CHUNK_"))]:
    del os.environ[_k]
os.environ.update({
    "FRESHNESS_WORK_DIR": WORK,
    "PIPELINE_RETRY_BASE_SECONDS": "0",
    "AZURE_SEARCH_ENDPOINT": "https://fake-search.search.windows.net",
    "AZURE_SEARCH_INDEX_NAME": "fake-index",
    "AZURE_OPENAI_ENDPOINT": "https://fake-openai.openai.azure.com",
})


class FakeCredential:
    def __init__(self, *args, **kwargs):
        pass

    def get_token(self, *args, **kwargs):
        raise RuntimeError("credentials are disabled in tests")


mock.patch("azure.identity.DefaultAzureCredential", FakeCredential).start()


@pytest.fixture(autouse=True)
def isolated(mocker, tmp_path, monkeypatch):
    """No sleeping; empty work folders for every test."""
    mocker.patch("time.sleep")
    import shutil
    from pathlib import Path
    for d in ("freshness_reports", "freshness_manifests", "freshness_archive"):
        shutil.rmtree(Path(WORK) / d, ignore_errors=True)
    monkeypatch.chdir(tmp_path)


LONG_TEXT = ("Royal London pensions explained in plain English. " * 6).strip()


@pytest.fixture
def make_excel(tmp_path):
    """make_excel(rows, header=("URL","Title","Status","Category","Page Purpose")) -> path of a real .xlsx."""
    counter = iter(range(1000))

    def _make(rows, header=("URL", "Title", "Status", "Category", "Page Purpose")):
        wb = Workbook()
        ws = wb.active
        ws.append(list(header))
        for r in rows:
            ws.append(list(r))
        p = tmp_path / f"approved_{next(counter)}.xlsx"
        wb.save(p)
        return str(p)
    return _make


@pytest.fixture
def page_html():
    """page_html(body_text=LONG_TEXT, title="Pensions", description="", extra="") -> a realistic page."""
    def _make(body_text=LONG_TEXT, title="Pensions", description="", extra="", main_tag="main"):
        meta = f'<meta name="description" content="{description}">' if description else ""
        return (f"<html><head><title>{title} | Royal London</title>{meta}</head><body>"
                f"<nav>Menu Home Products</nav><header>Site header</header>"
                f"<{main_tag}><h1>{title}</h1><p>{body_text}</p>{extra}</{main_tag}>"
                f"<footer>Copyright Royal London</footer><script>var x = 1;</script></body></html>")
    return _make


@pytest.fixture
def write_fixture(tmp_path):
    """write_fixture(script_module, url, html) -> fixture dir containing the file fetch_html(fixture_dir=...) expects."""
    import re

    def _write(script, url, html):
        d = tmp_path / "fixtures"
        d.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", script.normalize_url(url)).strip("_")
        (d / f"{slug}.html").write_text(html, encoding="utf-8")
        return str(d)
    return _write


@pytest.fixture
def work():
    from pathlib import Path
    root = Path(WORK)
    for d in ("freshness_reports", "freshness_manifests", "freshness_archive"):
        (root / d).mkdir(exist_ok=True)
    return root


class FakeContainer:
    """Minimal ContainerClient: blobs {name: (bytes, last_modified)} + upload recording."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.uploaded = {}

    def list_blobs(self, name_starts_with=""):
        return [NS(name=k, last_modified=v[1]) for k, v in self.files.items() if k.startswith(name_starts_with)]

    def download_blob(self, name):
        return NS(readall=lambda: self.files[name][0])

    def upload_blob(self, name, data, overwrite=False):
        self.uploaded[name] = data.read()


@pytest.fixture
def fake_container():
    return FakeContainer


@pytest.fixture
def day():
    return lambda d: datetime(2026, 1, d, tzinfo=timezone.utc)
