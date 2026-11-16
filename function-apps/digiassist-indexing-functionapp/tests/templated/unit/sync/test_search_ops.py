"""AzureSearchOps (data plane) and the retry helper, against a faked SearchClient."""
from types import SimpleNamespace as NS
from unittest import mock

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError

from src import templated_job as j


def http_error(status, message="msg"):
    resp = mock.Mock(status_code=status, reason="r", headers={}, content_type=None)
    return HttpResponseError(message=message, response=resp)


class FakeSearchClient:
    def __init__(self, docs=(), fail_count=None, fail_search=None):
        self.docs = list(docs)
        self.fail_count, self.fail_search = fail_count, fail_search
        self.uploaded, self.deleted, self.search_calls = [], [], []
        self.upload_results = None
        self.delete_results = None
        self.fail_once = None  # exception raised on the first upload call only

    def get_document_count(self):
        if self.fail_count:
            raise self.fail_count
        return len(self.docs)

    def search(self, search_text="*", select=None, top=None, skip=0, include_total_count=False):
        if self.fail_search:
            raise self.fail_search
        self.search_calls.append({"select": select, "top": top, "skip": skip})
        page = self.docs[skip:skip + top]

        class Result(list):
            def get_count(_self):
                return len(self.docs)
        return Result(page)

    def upload_documents(self, batch):
        if self.fail_once:
            e, self.fail_once = self.fail_once, None
            raise e
        self.uploaded.append(batch)
        return self.upload_results or [NS(key=d["id"], succeeded=True) for d in batch]

    def delete_documents(self, batch):
        self.deleted.append(batch)
        return self.delete_results or [NS(key=d["id"], succeeded=True) for d in batch]


@pytest.fixture
def make_ops(monkeypatch):
    def _make(**kw):
        fake = FakeSearchClient(**kw)
        monkeypatch.setattr("azure.search.documents.SearchClient", lambda *a, **k: fake)
        return j.AzureSearchOps(), fake
    return _make


# ── construction / preflight ──────────────────────────────────
def test_missing_settings_fail_fast(monkeypatch):
    monkeypatch.delenv("AZURE_SEARCH_TEMPLATED_INDEX_NAME")
    with pytest.raises(RuntimeError, match="must be set"):
        j.AzureSearchOps()


def test_probe_returns_doc_count_and_selects_required_fields(make_ops):
    ops, fake = make_ops(docs=[{"id": "a"}, {"id": "b"}])
    assert ops.probe() == 2
    assert fake.search_calls[0]["select"] == list(j.REQUIRED_FIELDS) and fake.search_calls[0]["top"] == 1


def test_probe_missing_index_says_platform_creates_it(make_ops):
    ops, _ = make_ops(fail_count=ResourceNotFoundError("nope"))
    with pytest.raises(RuntimeError, match="created by the platform"):
        ops.probe()


def test_probe_schema_mismatch_is_explained(make_ops):
    ops, _ = make_ops(fail_search=http_error(400, "Unknown field 'response_json'"))
    with pytest.raises(RuntimeError, match="does not match the expected schema"):
        ops.probe()


def test_probe_other_http_errors_propagate(make_ops):
    ops, _ = make_ops(fail_count=http_error(403, "forbidden"))
    with pytest.raises(HttpResponseError):
        ops.probe()


# ── scan ──────────────────────────────────────────────────────
def test_scan_ids_pages_through_the_whole_index(make_ops, monkeypatch):
    monkeypatch.setattr(j, "SCAN_PAGE", 2)
    ops, fake = make_ops(docs=[{"id": f"q{i}"} for i in range(5)])
    assert ops.scan_ids() == {f"q{i}" for i in range(5)}
    assert [c["skip"] for c in fake.search_calls] == [0, 2, 4]


def test_scan_ids_empty_index(make_ops):
    ops, _ = make_ops()
    assert ops.scan_ids() == set()


def test_incomplete_scan_raises_instead_of_returning_a_partial_set(make_ops):
    ops, fake = make_ops(docs=[{"id": "a"}, {"id": "b"}])
    fake.search = lambda **kw: type("R", (list,), {"get_count": lambda s: 5})([{"id": "a"}])   # claims 5, returns 1
    with pytest.raises(RuntimeError, match="scan incomplete: read 1 of 5"):
        ops.scan_ids()


# ── upload / delete ───────────────────────────────────────────
def test_upload_batches_of_100(make_ops):
    ops, fake = make_ops()
    ops.upload([{"id": str(i)} for i in range(250)])
    assert [len(b) for b in fake.uploaded] == [100, 100, 50]


def test_upload_failure_for_any_document_raises(make_ops):
    ops, fake = make_ops()
    fake.upload_results = [NS(key="a", succeeded=True), NS(key="b", succeeded=False)]
    with pytest.raises(RuntimeError, match=r"upload failed for 1 id\(s\): \['b'\]"):
        ops.upload([{"id": "a"}, {"id": "b"}])


def test_delete_sends_keys_only_in_batches(make_ops):
    ops, fake = make_ops()
    ops.delete([f"q{i}" for i in range(150)])
    assert [len(b) for b in fake.deleted] == [100, 50] and fake.deleted[0][0] == {"id": "q0"}


def test_delete_failure_raises(make_ops):
    ops, fake = make_ops()
    fake.delete_results = [NS(key="q1", succeeded=False)]
    with pytest.raises(RuntimeError, match="delete failed"):
        ops.delete(["q1"])


def test_count(make_ops):
    ops, _ = make_ops(docs=[{"id": "a"}])
    assert ops.count() == 1


def test_transient_upload_error_is_retried(make_ops):
    ops, fake = make_ops()
    fake.fail_once = ServiceRequestError("connection reset")
    ops.upload([{"id": "a"}])
    assert len(fake.uploaded) == 1


# ── retry helper ──────────────────────────────────────────────
class RateLimitError(Exception):          # same class NAME the openai SDK uses
    pass


@pytest.mark.parametrize("exc, expected", [
    (ServiceRequestError("x"), True),
    (RateLimitError("x"), True),
    (TimeoutError(), True),
    (ConnectionError(), True),
    (http_error(503), True),
    (http_error(429), True),
    (http_error(404), False),
    (http_error(403), False),
    (ValueError("x"), False),
])
def test_is_transient_error(exc, expected):
    assert j.is_transient_error(exc) is expected


def test_with_retry_recovers_after_transient_failures():
    calls = iter([ServiceRequestError("a"), ServiceRequestError("b"), "done"])

    def fn():
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v
    assert j.with_retry(fn, op="t", attempts=3, base_seconds=0) == "done"


def test_with_retry_raises_after_exhausting_attempts():
    fn = mock.Mock(side_effect=ServiceRequestError("down"))
    with pytest.raises(ServiceRequestError):
        j.with_retry(fn, op="t", attempts=3, base_seconds=0)
    assert fn.call_count == 3


def test_with_retry_does_not_retry_permanent_errors():
    fn = mock.Mock(side_effect=ValueError("bad input"))
    with pytest.raises(ValueError):
        j.with_retry(fn, op="t", attempts=5, base_seconds=0)
    assert fn.call_count == 1


def test_with_retry_logs_each_retry(caplog):
    fn = mock.Mock(side_effect=[ServiceRequestError("blip"), "ok"])
    with caplog.at_level("WARNING", logger="templated_job"):
        j.with_retry(fn, op="upload", attempts=2, base_seconds=0)
    assert "operation_retry op=upload" in caplog.text
