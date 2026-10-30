import json
import os
import sys
from types import SimpleNamespace as NS

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError

from tests.unit.sync.conftest import FakeSearchClient, Result, make_pages


def http_error(status):
    err = HttpResponseError(message="x")
    err.status_code = status
    return err


# ---------- retry ----------
@pytest.mark.parametrize("exc,expected", [
    (TimeoutError(), True), (ConnectionError(), True), (ServiceRequestError("x"), True),
    (http_error(503), True), (http_error(429), True), (http_error(404), False), (ValueError("x"), False),
])
def test_is_transient_error(script, exc, expected):
    assert script.is_transient_error(exc) is expected


def test_with_retry_recovers_from_transient_errors(script):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ServiceRequestError("blip")
        return "ok"

    assert script.with_retry(flaky, op="t", attempts=5) == "ok" and len(calls) == 3


def test_with_retry_does_not_retry_permanent_errors(script):
    calls = []

    def bad():
        calls.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        script.with_retry(bad, op="t", attempts=5)
    assert len(calls) == 1


def test_with_retry_gives_up_after_attempts(script):
    calls = []

    def always():
        calls.append(1)
        raise ServiceRequestError("down")

    with pytest.raises(ServiceRequestError):
        script.with_retry(always, op="t", attempts=3)
    assert len(calls) == 3


# ---------- upload / delete ----------
def test_upload_rejects_chunk_embedding_mismatch(script):
    with pytest.raises(RuntimeError, match="embeddings"):
        script.upload_chunks([{"chunk_id": "a"}], [])


def test_upload_adds_embedding_to_each_document(mocker, script):
    client = FakeSearchClient()
    mocker.patch.object(script, "get_search_client", return_value=client)
    assert script.upload_chunks([{"chunk_id": "a"}, {"chunk_id": "b"}], [[1.0], [2.0]]) == 2
    assert client.docs["b"]["embedding"] == [2.0]


def test_upload_retries_only_the_failed_documents(mocker, script):
    mocker.patch.object(script, "RETRY_ATTEMPTS", 3)

    class Flaky(FakeSearchClient):
        first = True

        def upload_documents(self, documents):
            if self.first:
                self.first = False
                super().upload_documents([d for d in documents if d["chunk_id"] != "b"])
                return [Result(d["chunk_id"], d["chunk_id"] != "b") for d in documents]
            return super().upload_documents(documents)

    client = Flaky()
    mocker.patch.object(script, "get_search_client", return_value=client)
    assert script.upload_chunks([{"chunk_id": "a"}, {"chunk_id": "b"}], [[1.0], [2.0]]) == 2
    assert set(client.docs) == {"a", "b"}


def test_upload_raises_when_documents_keep_failing(mocker, script):
    mocker.patch.object(script, "RETRY_ATTEMPTS", 2)

    class Down(FakeSearchClient):
        def upload_documents(self, documents):
            return [Result(d["chunk_id"], False) for d in documents]

    mocker.patch.object(script, "get_search_client", return_value=Down())
    with pytest.raises(RuntimeError, match="failed to upload"):
        script.upload_chunks([{"chunk_id": "a"}], [[1.0]])


def test_delete_chunks_batches_large_lists(mocker, script):
    client = FakeSearchClient({"chunk_id": f"id{i}"} for i in range(250))
    mocker.patch.object(script, "get_search_client", return_value=client)
    assert script.delete_chunks([f"id{i}" for i in range(250)]) == 250 and client.docs == {}


def test_delete_chunks_with_nothing_to_delete_does_nothing(script):
    assert script.delete_chunks([]) == 0


# ---------- verification ----------
def test_document_count_verifies_when_it_matches_eventually(mocker, script):
    counts = iter([5, 5, 7])
    client = NS(get_document_count=lambda: next(counts))
    mocker.patch.object(script, "get_search_client", return_value=client)
    assert script.verify_document_count(7) == 7


def test_document_count_mismatch_raises(mocker, script):
    mocker.patch.object(script, "get_search_client", return_value=NS(get_document_count=lambda: 1))
    with pytest.raises(RuntimeError, match="expected 9"):
        script.verify_document_count(9, attempts=2)


def test_verify_index_true_when_query_returns_results(mocker, script):
    mocker.patch.object(script, "embed_chunks", return_value=[[0.0]])
    mocker.patch.object(script, "get_search_client", return_value=NS(search=lambda **kw: [{"source_url": "u"}]))
    assert script.verify_index() is True


def test_verify_index_false_when_no_results(mocker, script):
    mocker.patch.object(script, "embed_chunks", return_value=[[0.0]])
    mocker.patch.object(script, "get_search_client", return_value=NS(search=lambda **kw: []))
    assert script.verify_index() is False


def test_verify_index_false_when_query_fails(mocker, script):
    mocker.patch.object(script, "embed_chunks", side_effect=RuntimeError("down"))
    assert script.verify_index() is False


# ---------- preflight ----------
@pytest.fixture
def credentials(mocker, script):
    mocker.patch.object(script, "get_credential", return_value=NS(get_token=lambda scope: "token"))


def test_preflight_skips_index_check_when_not_requested(credentials, script):
    assert script.verify_azure_access(check_index=False) is None


def test_preflight_returns_document_count(credentials, mocker, script):
    client = NS(get_document_count=lambda: 42, search=lambda **kw: [])
    mocker.patch.object(script, "get_search_client", return_value=client)
    assert script.verify_azure_access(check_index=True) == 42


def test_preflight_explains_missing_index(credentials, mocker, script):
    def missing():
        raise ResourceNotFoundError("nope")

    mocker.patch.object(script, "get_search_client", return_value=NS(get_document_count=missing))
    with pytest.raises(RuntimeError, match="created by the platform"):
        script.verify_azure_access(check_index=True)


def test_preflight_detects_missing_field(credentials, mocker, script):
    def search(**kw):
        raise HttpResponseError(message="Unknown field 'dropdown_url'")

    client = NS(get_document_count=lambda: 1, search=search)
    mocker.patch.object(script, "get_search_client", return_value=client)
    with pytest.raises(RuntimeError, match="expected schema"):
        script.verify_azure_access(check_index=True)


# ---------- index scan ----------
class _Result(list):
    def __init__(self, items, total):
        super().__init__(items)
        self._total = total

    def get_count(self):
        return self._total


class PagedSearch:
    """Fake client.search that serves `docs` page by page and reports `total`."""

    def __init__(self, docs, total=None):
        self.docs, self.total = docs, len(docs) if total is None else total

    def search(self, include_total_count, search_text, select, top, skip):
        page = self.docs[skip:skip + top]
        return _Result(page, self.total)


def test_scan_index_reads_every_page(script):
    seen = []
    client = PagedSearch([{"chunk_id": str(i)} for i in range(2500)])
    assert script._scan_index(client, "t", ["chunk_id"], seen.append) == 2500 and len(seen) == 2500


def test_scan_index_raises_when_scan_is_incomplete(script):
    client = PagedSearch([{"chunk_id": "1"}, {"chunk_id": "2"}], total=5)
    with pytest.raises(RuntimeError, match="incomplete"):
        script._scan_index(client, "t", ["chunk_id"], lambda d: None)


def test_get_indexed_urls_returns_source_urls(mocker, script):
    mocker.patch.object(script, "get_search_client", return_value=PagedSearch(
        [{"source_url": "https://a"}, {"source_url": "https://b"}, {"source_url": ""}]))
    assert script.get_indexed_urls() == {"https://a", "https://b"}


def test_get_indexed_urls_treats_missing_index_as_empty(mocker, script):
    mocker.patch.object(script, "_scan_index", side_effect=ResourceNotFoundError("nope"))
    assert script.get_indexed_urls() == set()


# ---------- file loading ----------
def test_load_pages_missing_file(script, tmp_path):
    with pytest.raises(FileNotFoundError):
        script.load_pages(str(tmp_path / "nope.json"))


@pytest.mark.parametrize("content", [[], {"a": 1}, [{"url": "https://x", "reason": "timeout"}], ["text"]])
def test_load_pages_refuses_empty_or_malformed_files(script, tmp_path, content):
    path = tmp_path / "f.json"
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        script.load_pages(str(path))


def test_load_pages_reads_valid_file(script, tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps(make_pages(2)))
    assert len(script.load_pages(str(path))) == 2


def test_failed_urls_sidecar_is_read_and_normalised(script, tmp_path):
    main = tmp_path / "royal_london_faq_approved_httpV1_x.json"
    main.write_text("[]")
    (tmp_path / "royal_london_faq_approved_httpV1_x_failures.json").write_text(
        json.dumps([{"url": "https://a/", "reason": "t"}, {"nourl": 1}]))
    assert script.load_failed_urls(str(main)) == {"https://a"}


def test_failed_urls_without_sidecar_is_empty(script, tmp_path):
    assert script.load_failed_urls(str(tmp_path / "royal_london_faq_approved_httpV1_x.json")) == set()


def test_find_latest_scraped_file_picks_newest_and_ignores_failures(script, tmp_path, monkeypatch):
    data = tmp_path / "scraper" / "data"
    data.mkdir(parents=True)
    for name, mtime in [("royal_london_faq_approved_a.json", 100), ("royal_london_faq_approved_b.json", 200),
                        ("royal_london_faq_approved_b_failures.json", 300)]:
        f = data / name
        f.write_text("[]")
        os.utime(f, (mtime, mtime))
    monkeypatch.chdir(tmp_path)
    assert script.find_latest_scraped_file().endswith("royal_london_faq_approved_b.json")


def test_find_latest_scraped_file_falls_back_when_nothing_found(script, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert script.find_latest_scraped_file() == script.SCRAPED_FILE
    (tmp_path / "scraper" / "data").mkdir(parents=True)
    assert script.find_latest_scraped_file() == script.SCRAPED_FILE


# ---------- command line ----------
OK = {"success": True, "pages_indexed": 1, "chunks_created": 2, "chunks_uploaded": 2, "chunks_deleted": 0,
      "urls_removed": 0, "chunks_retained_failed": 0, "dry_run": False, "index_name": "i", "verified": True,
      "elapsed_seconds": 1}


def test_main_success_prints_summary(mocker, script, capsys, monkeypatch):
    run = mocker.patch.object(script, "run_pipeline", return_value=OK)
    monkeypatch.setattr(sys, "argv", ["x", "--file", "f.json", "--new-only", "--allow-mass-removal"])
    script.main()
    run.assert_called_once_with(mode="new-only", scraped_file="f.json", dry_run=False, allow_mass_removal=True)
    assert "Chunks uploaded" in capsys.readouterr().out


def test_main_failure_exits_with_error_code(mocker, script, monkeypatch):
    mocker.patch.object(script, "run_pipeline", return_value={**OK, "success": False, "error": "boom"})
    monkeypatch.setattr(sys, "argv", ["x", "--file", "f.json"])
    with pytest.raises(SystemExit) as exit_info:
        script.main()
    assert exit_info.value.code == 1
