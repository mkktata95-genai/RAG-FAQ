"""Whole run_freshness_job offline: Azure, health checks and OpenAI are faked; pages come from fixture HTML."""
import asyncio
import json

import pytest
from openpyxl import load_workbook

from src import content_freshness_httpV1 as m
from tests.fakes import FakeSearch, doc

BASE = "https://www.royallondon.com"
U1, U2, U3 = f"{BASE}/pensions/one", f"{BASE}/pensions/two", f"{BASE}/isa/three"


@pytest.fixture
def env(mocker, make_excel, write_fixture, page_html, work):
    class Env:
        pass
    e = Env()
    e.health = {}                                                      # url -> status (default live)
    e.excel = lambda urls: make_excel([[u, "T", "", "", ""] for u in urls])
    e.fixtures = lambda pages: [write_fixture(m, u, page_html(title=t, body_text=b)) for u, t, b in pages][-1]

    async def fake_health(entries):
        return [{"url": x["url"], "status": e.health.get(x["url"], "live"), "status_code": 200, "redirect_note": ""}
                for x in entries]
    mocker.patch.object(m, "verify_azure_access", return_value=0)
    mocker.patch.object(m, "check_all_urls_health", side_effect=fake_health)
    mocker.patch.object(m, "embed_chunks", side_effect=lambda cs: [[0.0] * m.EMBEDDING_DIMS for _ in cs])
    e.index = lambda docs, **kw: mocker.patch.object(m, "get_search_client", return_value=(f := FakeSearch(docs, **kw))) and f
    e.work = work
    return e


def stored_hash(page_html, body):
    return m.hash_from_raw_html(page_html(title="X", body_text=body))


def run(env, urls, docs, mode="report", **kw):
    f = env.index(docs, **kw.pop("search", {}))
    fixture_dir = kw.pop("fixture_dir")
    r = m.run_freshness_job(mode=mode, excel_path=env.excel(urls), fixture_dir=fixture_dir, **kw)
    return r, f


TEXT_A = "Annual allowance explained. " * 12
TEXT_B = "Tax free cash explained. " * 12


def test_unchanged_page_is_left_alone(env, page_html):
    fx = env.fixtures([(U1, "One", TEXT_A)])
    h = m.hash_from_raw_html(page_html(title="One", body_text=TEXT_A))
    r, f = run(env, [U1], [doc(U1, "c1", h)], fixture_dir=fx)
    assert r["success"] and r["run_summary"]["live_unchanged"] == 1 and r["run_summary"]["changed"] == 0 and f.deleted == []


def test_report_mode_flags_change_but_writes_nothing(env):
    fx = env.fixtures([(U1, "One", TEXT_A)])
    r, f = run(env, [U1], [doc(U1, "c1", "old-hash")], fixture_dir=fx)
    assert r["run_summary"]["changed"] == 1 and f.deleted == [] and f.uploaded == []
    assert (env.work / "freshness_reports").glob("freshness_report_report_*.xlsx")
    assert load_workbook(r["report_path"]) is not None and json.load(open(r["manifest_path"]))["run_summary"]["mode"] == "report"


def test_apply_replaces_changed_page_and_keeps_rollback_copy(env):
    fx = env.fixtures([(U1, "One", TEXT_A)])
    r, f = run(env, [U1], [doc(U1, "old1", "old-hash", refresh_count=2), doc(U1, "old2", "old-hash", refresh_count=2)],
               mode="apply", fixture_dir=fx)
    s = r["run_summary"]
    assert r["success"] and s["changed"] == 1 and s["chunks_deleted"] == 2 and s["chunks_added"] >= 1
    assert sorted(f.deleted) == ["old1", "old2"] and f.uploaded
    assert all(d["refresh_count"] == 3 for d in f.uploaded)                          # incremented, not reset
    assert all(d["scraped_at"] for d in f.uploaded)                                  # never empty (DateTimeOffset)
    archives = list((env.work / "freshness_archive").glob("deleted_*.json"))
    assert len(archives) == 1 and len(json.loads(archives[0].read_text(encoding="utf-8"))) == 2


def test_apply_adds_new_page(env):
    fx = env.fixtures([(U1, "One", TEXT_A), (U2, "Two", TEXT_B)])
    h = m.hash_from_raw_html(page_html_for("One", TEXT_A))
    r, f = run(env, [U1, U2], [doc(U1, "c1", h)], mode="apply", fixture_dir=fx)
    assert r["run_summary"]["new"] == 1 and r["run_summary"]["chunks_added"] >= 1 and f.deleted == []
    assert {d["source_url"] for d in f.uploaded} == {m.normalize_url(U2)}
    assert all(d["refresh_count"] == 0 for d in f.uploaded)


def page_html_for(title, body):
    return ("<html><head><title>%s | Royal London</title></head><body><nav>Menu Home Products</nav><header>Site header</header>"
            "<main><h1>%s</h1><p>%s</p></main><footer>Copyright Royal London</footer><script>var x = 1;</script></body></html>"
            % (title, title, body))


def test_apply_removes_dead_page_and_delisted_page(env):
    fx = env.fixtures([(U1, "One", TEXT_A)])
    env.health[U1] = "dead_404"
    docs = [doc(U1, "c1", "h"), doc(U3, "c3", "h")]            # U3 is indexed but no longer in the Excel
    r, f = run(env, [U1], docs, mode="apply", fixture_dir=fx, allow_mass_removal=True)
    s = r["run_summary"]
    assert s["dead_404"] == 1 and s["delisted"] == 1 and sorted(f.deleted) == ["c1", "c3"] and f.docs == []


def test_ambiguous_health_never_deletes(env):
    fx = env.fixtures([(U1, "One", TEXT_A)])
    env.health[U1] = "error"
    r, f = run(env, [U1], [doc(U1, "c1", "h")], mode="apply", fixture_dir=fx)
    assert r["run_summary"]["scrape_failed"] == 1 and f.deleted == []


def test_mass_removal_breaker_aborts_before_any_write(env):
    urls = [f"{BASE}/pensions/p{i}" for i in range(10)]
    fx = env.fixtures([(u, "T", TEXT_A) for u in urls])
    docs = [doc(u, f"c{i}", "h") for i, u in enumerate(urls)]
    for u in urls[:5]:
        env.health[u] = "dead_404"
    with pytest.raises(RuntimeError, match="ABORTED before any index write"):
        run(env, urls, docs, mode="apply", fixture_dir=fx)
    assert len(env.index(docs).docs) == 10


def test_mass_removal_can_be_overridden(env):
    urls = [f"{BASE}/pensions/p{i}" for i in range(10)]
    fx = env.fixtures([(u, "T", TEXT_A) for u in urls])
    docs = [doc(u, f"c{i}", "h") for i, u in enumerate(urls)]
    for u in urls[:5]:
        env.health[u] = "dead_404"
    r, f = run(env, urls, docs, mode="apply", fixture_dir=fx, allow_mass_removal=True)
    assert r["run_summary"]["removal_breaker_tripped"] and {f"c{i}" for i in range(5)} <= set(f.deleted)


def test_breaker_only_warns_in_report_mode(env):
    urls = [f"{BASE}/pensions/p{i}" for i in range(10)]
    fx = env.fixtures([(u, "T", TEXT_A) for u in urls])
    for u in urls[:5]:
        env.health[u] = "dead_404"
    r, f = run(env, urls, [doc(u, f"c{i}", "h") for i, u in enumerate(urls)], fixture_dir=fx)
    assert r["run_summary"]["removal_breaker_tripped"] and f.deleted == []


def test_delete_failure_keeps_old_chunks_and_fails_run(env):
    fx = env.fixtures([(U1, "One", TEXT_A)])
    r, f = run(env, [U1], [doc(U1, "old1", "old-hash")], mode="apply", fixture_dir=fx, search={"fail_delete_keys": {"old1"}})
    assert not r["success"] and any("delete failed" in e for e in r["errors"]) and f.uploaded == []


def test_upload_failure_after_delete_is_reported(env):
    fx = env.fixtures([(U1, "One", TEXT_A)])
    r, f = run(env, [U1], [doc(U1, "old1", "old-hash")], mode="apply", fixture_dir=fx, search={"fail_upload": 99})
    assert not r["success"] and any("upload failed after delete" in e for e in r["errors"])


def test_apply_dry_run_writes_nothing(env):
    fx = env.fixtures([(U1, "One", TEXT_A)])
    r, f = run(env, [U1], [doc(U1, "old1", "old-hash")], mode="apply", dry_run=True, fixture_dir=fx)
    assert f.deleted == [] and f.uploaded == [] and r["run_summary"]["changed"] == 1


def test_missing_fixture_marks_scrape_failed_not_deleted(env, tmp_path):
    empty = tmp_path / "nofix"
    empty.mkdir()
    r, f = run(env, [U1], [doc(U1, "c1", "h")], mode="apply", fixture_dir=str(empty))
    assert r["run_summary"]["scrape_failed"] == 1 and f.deleted == []


def test_apply_needs_openai_endpoint(env, monkeypatch):
    monkeypatch.setattr(m, "AZURE_OPENAI_ENDPOINT", "")
    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
        m.run_freshness_job(mode="apply", excel_path="x.xlsx")


def test_search_endpoint_required(monkeypatch):
    monkeypatch.setattr(m, "SEARCH_ENDPOINT", "")
    with pytest.raises(ValueError, match="AZURE_SEARCH_ENDPOINT"):
        m.run_freshness_job(mode="report", excel_path="x.xlsx")
