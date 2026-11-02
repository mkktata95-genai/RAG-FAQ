"""Index scan/snapshot/delete/archive, preflight and health classification (fake clients, no network)."""
import asyncio
import json

import pytest

from src import content_freshness_httpV1 as m
from tests.fakes import FakeSearch, doc

A, B = "https://www.royallondon.com/pensions/a", "https://www.royallondon.com/isa/b"


@pytest.fixture
def index(mocker):
    def _make(docs=None, **kw):
        f = FakeSearch(docs, **kw)
        mocker.patch.object(m, "get_search_client", return_value=f)
        return f
    return _make


# ── scan ──────────────────────────────────────────────────────
def test_scan_pages_through_whole_index(index):
    f = index([doc(A, f"c{i}") for i in range(2500)])
    seen = []
    assert m._scan_index(f, "t", ["*"], seen.append) == 2500 and len(seen) == 2500 and f.search_calls == 3


def test_scan_incomplete_raises(index):
    f = index([doc(A, "c1")], count_override=5)
    with pytest.raises(RuntimeError, match="incomplete"):
        m._scan_index(f, "t", ["*"], lambda d: None)


def test_scan_empty_index(index):
    assert m._scan_index(index([]), "t", ["*"], lambda d: None) == 0


def test_current_state_keys_dropdowns_by_dropdown_url(index):
    index([doc(A, "c1", "h1"), doc(A, "c2", "h1"), doc(A, "c3", "h2", dropdown_url=A + "#state=uk")])
    st = m.fetch_current_state_from_index()
    assert set(st) == {m.normalise_url(A), m.normalise_url(A + "#state=uk")}
    assert st[m.normalise_url(A + "#state=uk")]["is_dropdown"] and not st[m.normalise_url(A)]["is_dropdown"]
    assert st[m.normalise_url(A)]["source_url"] == A


def test_snapshot_groups_by_base_and_keeps_highest_refresh_count(index):
    index([doc(A, "c1", refresh_count=2), doc(A, "c2", refresh_count=5), doc(B, "c3")])
    snap = m.fetch_index_snapshot()
    assert snap["total_docs"] == 3 and len(snap["by_base"][m.normalise_url(A)]) == 2
    assert snap["refresh"][m.normalise_url(A)] == 5 and snap["refresh"][m.normalise_url(B)] == 0
    assert sorted(m.get_chunk_ids_for_url(A, snap)) == ["c1", "c2"] and m.get_chunk_ids_for_url("https://x/none", snap) == []


# ── delete ────────────────────────────────────────────────────
def snap_of(docs):
    return {"by_base": {m.normalise_url(A): [d for d in docs if d["source_url"] == A],
                        m.normalise_url(B): [d for d in docs if d["source_url"] == B]}, "refresh": {}, "total_docs": len(docs)}


def test_delete_removes_all_chunks_of_the_url(index):
    docs = [doc(A, "c1"), doc(A, "c2"), doc(B, "c3")]
    f = index(docs)
    summary, failed = m.delete_chunks_for_urls([A], snap_of(docs))
    assert summary == {A: 2} and failed == [] and sorted(f.deleted) == ["c1", "c2"] and [d["chunk_id"] for d in f.docs] == ["c3"]


def test_delete_dry_run_changes_nothing(index):
    docs = [doc(A, "c1")]
    f = index(docs)
    summary, failed = m.delete_chunks_for_urls([A], snap_of(docs), dry_run=True)
    assert summary == {A: 1} and f.deleted == []


def test_delete_url_without_chunks_is_skipped(index):
    index([])
    assert m.delete_chunks_for_urls([A], snap_of([])) == ({}, [])


def test_delete_failure_reported_not_counted(index):
    docs = [doc(A, "c1"), doc(B, "c2")]
    f = index(docs, fail_delete_keys={"c1"})
    summary, failed = m.delete_chunks_for_urls([A, B], snap_of(docs))
    assert failed == [A] and summary == {B: 1} and f.deleted == ["c2"]


# ── archive ───────────────────────────────────────────────────
def test_archive_writes_rollback_copy(work):
    docs = [doc(A, "c1"), doc(A, "c2")]
    path = m.archive_deleted_chunks_locally(A, "20260101_000000", snap_of(docs))
    assert path and path.startswith(str(work / "freshness_archive"))
    assert [d["chunk_id"] for d in json.loads(open(path, encoding="utf-8").read())] == ["c1", "c2"]


def test_archive_nothing_to_archive(work):
    assert m.archive_deleted_chunks_locally("https://x/none", "t", snap_of([])) is None


# ── preflight access check ────────────────────────────────────
def test_verify_access_reports_document_count(index, mocker):
    mocker.patch.object(m, "get_credential", return_value=mocker.Mock())
    index([doc(A, "c1")])
    assert m.verify_azure_access(need_openai=True) == 1
    scopes = [c.args[0] for c in m.get_credential().get_token.call_args_list]
    assert "https://cognitiveservices.azure.com/.default" in scopes


def test_verify_access_skips_openai_scope_when_not_needed(index, mocker):
    cred = mocker.Mock()
    mocker.patch.object(m, "get_credential", return_value=cred)
    index([])
    m.verify_azure_access(need_openai=False)
    assert [c.args[0] for c in cred.get_token.call_args_list] == ["https://search.azure.com/.default"]


# ── health classification ─────────────────────────────────────
def probe_returns(mocker, *answers):
    seq = list(answers)

    async def fake(session, url):
        a = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(a, Exception):
            raise a
        return a
    mocker.patch.object(m, "_probe_url", side_effect=fake)


def check(url=A):
    return asyncio.run(m.check_single_url(None, {"url": url}))


@pytest.mark.parametrize("answer, status", [
    ((200, A), "live"), ((200, A + "/"), "live"),
    ((200, "https://www.royallondon.com/other"), "internal_redirect"),
    ((404, A), "dead_404"), ((410, A), "dead_404"),
    ((403, A), "error"), ((429, A), "error"), ((500, A), "error"),
])
def test_check_single_url_classification(mocker, answer, status):
    probe_returns(mocker, answer)
    assert check()["status"] == status


def test_external_redirect(mocker):
    probe_returns(mocker, (200, "https://elsewhere.com/x"))
    assert check()["status"] == "external_redirect"


def test_transient_status_retried_then_live(mocker):
    probe_returns(mocker, (503, A), (200, A))
    assert check()["status"] == "live"


def test_timeout_and_network_errors_are_non_destructive(mocker):
    probe_returns(mocker, asyncio.TimeoutError())
    assert check()["status"] == "timeout"
    probe_returns(mocker, m.aiohttp.ClientError("down"))
    assert check()["status"] == "error"


def test_unexpected_exception_not_retried(mocker):
    probe_returns(mocker, ValueError("bug"))
    assert check()["status"] == "error"
    assert m._probe_url.call_count == 1


def test_all_urls_health_rechecks_dead_candidates(mocker):
    """A 404 must be confirmed by a second check before it can lead to deletion."""
    mocker.patch.object(m, "HEALTH_RECHECK_DELAY_SECONDS", 0)
    answers = {A: [(404, A), (200, A)], B: [(200, B)]}

    async def fake(session, url):
        seq = answers[url]
        return seq.pop(0) if len(seq) > 1 else seq[0]
    mocker.patch.object(m, "_probe_url", side_effect=fake)
    res = asyncio.run(m.check_all_urls_health([{"url": A}, {"url": B}]))
    assert {r["url"]: r["status"] for r in res} == {A: "live", B: "live"}
