"""Shared test setup. Unit tests must never reach Azure or a real index:
- fake settings are ASSIGNED (not setdefault), so a developer's shell values can not leak in
- any TEMPLATED_* tuning variable from the shell is removed before the module is imported
- DefaultAzureCredential is replaced with one that raises, so any accidental real call fails loudly
"""
import os
from types import SimpleNamespace as NS
from unittest import mock

import pytest
from openpyxl import Workbook

for _k in [k for k in os.environ if k.startswith("TEMPLATED_")]:
    del os.environ[_k]

os.environ["AZURE_SEARCH_ENDPOINT"] = "https://search.test.invalid"
os.environ["AZURE_SEARCH_TEMPLATED_INDEX_NAME"] = "test-templated-index"
os.environ["AZURE_OPENAI_ENDPOINT"] = "https://openai.test.invalid"
os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"] = "test-embedding"
os.environ["AZURE_OPENAI_EMBEDDING_DIMENSIONS"] = "4"


class FakeCredential:
    def __init__(self, *args, **kwargs):
        pass

    def get_token(self, *args, **kwargs):
        raise RuntimeError("credentials are disabled in tests")


mock.patch("azure.identity.DefaultAzureCredential", FakeCredential).start()

OK_URL = "https://www.royallondon.com/x/"
HEADER = ["id", "question", "answer", "active", "intent", "scenario", "advice_boundary",
          "citation_1_title", "citation_1_url", "citation_2_title", "citation_2_url",
          "citation_3_title", "citation_3_url", "citation_4_title", "citation_4_url",
          "cta_1_label", "cta_1_url"]


@pytest.fixture(autouse=True)
def no_sleep(mocker):
    """Retry/backoff/verify waits must never slow the tests down."""
    mocker.patch("time.sleep")


@pytest.fixture
def ok_url():
    return OK_URL


@pytest.fixture
def make_row():
    """make_row(**overrides) -> list of cell values in HEADER order (one valid row by default)."""
    def _make(**kw):
        base = dict(id="", question="What is a pension?", answer="A pension is savings for retirement.", active="Y",
                    intent="", scenario="", advice_boundary="", citation_1_title="Pensions", citation_1_url=OK_URL,
                    citation_2_title="", citation_2_url="", citation_3_title="", citation_3_url="",
                    citation_4_title="", citation_4_url="", cta_1_label="Basics", cta_1_url=OK_URL + "b/")
        base.update(kw)
        return [base[k] for k in HEADER]
    return _make


@pytest.fixture
def make_xlsx(tmp_path):
    """make_xlsx(rows, header=None) -> path of a real .xlsx file."""
    counter = iter(range(1000))

    def _make(rows, header=None):
        wb = Workbook()
        ws = wb.active
        ws.append(header or HEADER)
        for r in rows:
            ws.append(r)
        p = tmp_path / f"t{next(counter)}.xlsx"
        wb.save(p)
        return str(p)
    return _make


@pytest.fixture
def entries():
    """entries(n) -> n validated-style entries with ids q1..qn."""
    def _make(n, start=1):
        return [{"id": f"q{i}", "question": f"Question {i}?", "response_json": "{}", "source_urls": [OK_URL]}
                for i in range(start, start + n)]
    return _make


class FakeOps:
    """In-memory stand-in for AzureSearchOps. Records the order of calls."""

    def __init__(self, existing=(), index_name="test-templated-index"):
        self.index_name = index_name
        self.docs = {i: {"id": i} for i in existing}
        self.calls = []
        self.fail_upload = False
        self.fail_probe = None
        self.count_lag = 0  # first N count() calls report a stale number

    def probe(self):
        self.calls.append("probe")
        if self.fail_probe:
            raise self.fail_probe
        return len(self.docs)

    def scan_ids(self):
        self.calls.append("scan")
        return set(self.docs)

    def upload(self, docs):
        self.calls.append("upload")
        if self.fail_upload:
            raise RuntimeError("boom")
        for d in docs:
            self.docs[d["id"]] = d

    def delete(self, ids):
        self.calls.append("delete")
        for i in ids:
            self.docs.pop(i, None)

    def count(self):
        self.calls.append("count")
        if self.count_lag > 0:
            self.count_lag -= 1
            return len(self.docs) + 1
        return len(self.docs)


@pytest.fixture
def fake_ops():
    return FakeOps


@pytest.fixture
def embed4():
    """Fake embedding: 4 dims (matches the conftest dimensions setting)."""
    calls = []

    def _embed(texts):
        calls.append(list(texts))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    _embed.calls = calls
    return _embed


class FakeContainer:
    """Minimal ContainerClient: one blob (bytes), plus a listing."""

    def __init__(self, blobs=None):
        self.blobs = blobs or {}  # name -> bytes

    def download_blob(self, name):
        return NS(readall=lambda: self.blobs[name])

    def list_blobs(self, name_starts_with=""):
        from datetime import datetime, timezone
        return [NS(name=n, last_modified=datetime(2026, 1, 1 + i, tzinfo=timezone.utc))
                for i, n in enumerate(self.blobs) if n.startswith(name_starts_with)]


@pytest.fixture
def fake_container():
    return FakeContainer
