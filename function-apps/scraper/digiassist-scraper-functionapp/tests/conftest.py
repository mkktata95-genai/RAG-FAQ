"""Shared test setup. Unit tests never touch the network or Azure:
- retry tuning / failure limits are pinned (a developer's shell values can not leak in)
- DefaultAzureCredential is replaced with one that raises
- time.sleep is mocked, and every test runs in its own temp working directory (the scraper writes ./scraper/data)
"""
import os
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest import mock

import pytest
from openpyxl import Workbook

for _k in [k for k in os.environ if k.startswith(("SCRAPER_", "PIPELINE_"))]:
    del os.environ[_k]
os.environ["PIPELINE_RETRY_BASE_SECONDS"] = "0"


class FakeCredential:
    def __init__(self, *args, **kwargs):
        pass

    def get_token(self, *args, **kwargs):
        raise RuntimeError("credentials are disabled in tests")


mock.patch("azure.identity.DefaultAzureCredential", FakeCredential).start()

LONG_TEXT = ("Royal London pensions explained in plain English. " * 6).strip()


@pytest.fixture(autouse=True)
def isolated(mocker, tmp_path, monkeypatch):
    """No sleeping, and a fresh working directory for every test."""
    mocker.patch("time.sleep")
    monkeypatch.chdir(tmp_path)


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


class FakeContainer:
    """Minimal ContainerClient for the wrapper: blobs {name: (bytes, last_modified)} + upload recording."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.uploaded = {}

    def list_blobs(self, name_starts_with=""):
        return [NS(name=k, last_modified=v[1]) for k, v in self.files.items() if k.startswith(name_starts_with)]

    def download_blob(self, name):
        return NS(readall=lambda: self.files[name][0])

    def upload_blob(self, name, data, overwrite):
        self.uploaded[name] = data.read()


@pytest.fixture
def fake_container():
    return FakeContainer


@pytest.fixture
def day():
    return lambda d: datetime(2026, 1, d, tzinfo=timezone.utc)
