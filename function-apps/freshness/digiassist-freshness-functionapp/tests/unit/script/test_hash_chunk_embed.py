"""Hash parity, chunking (incl. the v1.1.3 scraped_at fallback), embedding (incl. settings), upload."""
import importlib.util
import os
import pathlib

import pytest

from src import content_freshness_httpV1 as m
from tests.fakes import FakeSearch

BASE = "https://www.royallondon.com"
LONG = ("Royal London pensions explained in plain English. " * 6).strip()


def page(**kw):
    p = {"source_url": f"{BASE}/pensions/a", "dropdown_url": "", "title": "Pensions", "content": LONG,
         "section": "Pensions", "audience": "customer", "scraped_at": "2026-01-01T00:00:00+00:00"}
    p.update(kw)
    return p


# ── hashes ────────────────────────────────────────────────────
def test_hash_chain_matches_manual_steps(page_html):
    html = page_html()
    md = m.html_fragment_to_markdown(m.extract_main_html(html))
    expected = m.compute_content_hash(m.clean_content(m.clean_scraped_content(md)))
    assert m.hash_from_raw_html(html) == expected and len(expected) == 64


def test_hash_ignores_boilerplate_outside_main(page_html):
    a = page_html()
    b = a.replace("Site header", "A completely different header")
    assert m.hash_from_raw_html(a) == m.hash_from_raw_html(b)


def test_hash_changes_when_content_changes(page_html):
    assert m.hash_from_raw_html(page_html()) != m.hash_from_raw_html(page_html(body_text=LONG + " More."))


def test_dropdown_panel_hash_uses_single_clean_pass():
    t = "Call us https://www.royallondon.com/x today about your policy please."
    assert m.hash_from_dropdown_panel_text(t) == m.compute_content_hash(m.clean_content(t))


# ── url helpers ───────────────────────────────────────────────
def test_url_helpers():
    assert m.normalise_url("  HTTPS://X.com/A/ ") == "https://x.com/a"
    assert m.normalise_url_path("https://x.com/A/?q=1#f") == "https://x.com/a"
    assert m.is_dropdown_url("https://x/a#state=uk") and m.is_dropdown_url("https://x/a#policy=1") and not m.is_dropdown_url("https://x/a")
    assert m.get_base_url("https://x/a#state=uk") == "https://x/a"


# ── chunk_pages ───────────────────────────────────────────────
def test_chunk_ids_are_deterministic():
    a = m.chunk_pages([page()], refresh_run_id="r1")
    b = m.chunk_pages([page()], refresh_run_id="r2")
    assert [c["chunk_id"] for c in a] == [c["chunk_id"] for c in b] and a


def test_scraped_at_kept_when_present():
    assert m.chunk_pages([page()])[0]["scraped_at"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize("value", ["", None])
def test_scraped_at_falls_back_to_index_time(value):
    """DateTimeOffset index fields reject '' - v1.1.3 falls back to the index time."""
    c = m.chunk_pages([page(scraped_at=value)])[0]
    assert c["scraped_at"] and c["scraped_at"] == c["indexed_at"]


def test_scraped_at_missing_key_falls_back():
    p = page()
    del p["scraped_at"]
    assert m.chunk_pages([p])[0]["scraped_at"]


def test_short_and_empty_pages_are_skipped():
    assert m.chunk_pages([page(content="short")]) == [] and m.chunk_pages([page(content="")]) == []


def test_duplicate_urls_are_chunked_once():
    out = m.chunk_pages([page(), page()])
    assert len({c["source_url"] for c in out}) == 1 and [c["chunk_index"] for c in out] == list(range(len(out)))


def test_dropdown_state_is_one_atomic_chunk_keyed_by_dropdown_url():
    st = page(dropdown_url=f"{BASE}/pensions/a#state=uk", dropdown_title="UK", content=LONG)
    base = page()
    cs = m.chunk_pages([base, st])
    d = [c for c in cs if c["element_type"] == "dropdown_state"]
    assert len(d) == 1 and d[0]["source_url"] == base["source_url"] and d[0]["total_chunks"] == 1
    assert d[0]["chunk_id"] != next(c for c in cs if c["element_type"] != "dropdown_state")["chunk_id"]


def test_chunk_carries_refresh_count_and_run_id():
    c = m.chunk_pages([page(refresh_count=3)], refresh_run_id="run-9")[0]
    assert c["refresh_count"] == 3 and c["index_run_id"] == "run-9"


def test_long_content_splits_into_several_chunks():
    cs = m.chunk_pages([page(content="\n\n".join(f"Paragraph {i}. " + "word " * 80 for i in range(12)))])
    assert len(cs) > 1 and all(c["total_chunks"] == len(cs) for c in cs)


def test_preflight_ok_and_failure_ratio():
    assert m.validate_chunking_preflight([])["ok"]
    good = m.validate_chunking_preflight([page()])
    assert good["ok"] and good["failed_pages"] == []


def test_preflight_flags_zero_chunks_for_real_content(mocker):
    mocker.patch.object(m, "chunk_pages", return_value=[])
    r = m.validate_chunking_preflight([page()])
    assert not r["ok"] and r["failed_pages"][0]["reason"] == "zero_chunks_for_nontrivial_content"


def test_preflight_flags_exception(mocker):
    mocker.patch.object(m, "chunk_pages", side_effect=ValueError("bad"))
    assert m.validate_chunking_preflight([page()])["failed_pages"][0]["reason"].startswith("chunking_exception")


# ── embedding settings (v1.1.2) ───────────────────────────────
def test_embedding_defaults():
    assert m.EMBEDDING_DEPLOYMENT == "text-embedding-3-large" and m.EMBEDDING_DIMS == 1536


def test_embedding_settings_come_from_app_settings(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "emb-x")
    monkeypatch.setenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1024")
    spec = importlib.util.spec_from_file_location("fresh_copy", pathlib.Path(m.__file__))
    copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy)
    assert copy.EMBEDDING_DEPLOYMENT == "emb-x" and copy.EMBEDDING_DIMS == 1024


class FakeEmb:
    def __init__(self, dims, short=False, wrong_dims=False, shuffle=True):
        self.calls, self.dims, self.short, self.wrong, self.shuffle = [], dims, short, wrong_dims, shuffle
        self.embeddings = self

    def create(self, input, model, dimensions):
        from types import SimpleNamespace as NS
        self.calls.append((len(input), model, dimensions))
        n = len(input) - (1 if self.short else 0)
        data = [NS(index=i, embedding=[float(i)] * (self.dims - (1 if self.wrong else 0))) for i in range(n)]
        return NS(data=list(reversed(data)) if self.shuffle else data)


def chunks(n):
    return [{"content": f"c{i}"} for i in range(n)]


def test_embed_batches_sorted_and_uses_configured_model(mocker):
    fake = FakeEmb(m.EMBEDDING_DIMS)
    mocker.patch.object(m, "get_openai_client", return_value=fake)
    out = m.embed_chunks(chunks(m.EMBEDDING_BATCH_SIZE + 5))
    assert len(out) == m.EMBEDDING_BATCH_SIZE + 5 and out[0][0] == 0.0 and out[1][0] == 1.0     # restored to input order
    assert [c[0] for c in fake.calls] == [m.EMBEDDING_BATCH_SIZE, 5]
    assert all(c[1:] == (m.EMBEDDING_DEPLOYMENT, m.EMBEDDING_DIMS) for c in fake.calls)


def test_embed_empty_makes_no_call(mocker):
    g = mocker.patch.object(m, "get_openai_client")
    assert m.embed_chunks([]) == [] and not g.called


def test_embed_wrong_vector_count_raises(mocker):
    mocker.patch.object(m, "get_openai_client", return_value=FakeEmb(m.EMBEDDING_DIMS, short=True))
    with pytest.raises(RuntimeError, match="expected 3 vectors"):
        m.embed_chunks(chunks(3))


def test_embed_wrong_dims_raises(mocker):
    mocker.patch.object(m, "get_openai_client", return_value=FakeEmb(m.EMBEDDING_DIMS, wrong_dims=True))
    with pytest.raises(RuntimeError, match="dims"):
        m.embed_chunks(chunks(2))


def test_openai_client_needs_endpoint(monkeypatch):
    monkeypatch.setattr(m, "_openai_client", None)
    monkeypatch.setattr(m, "AZURE_OPENAI_ENDPOINT", "")
    with pytest.raises(ValueError):
        m.get_openai_client()


# ── upload ────────────────────────────────────────────────────
def docs(n):
    return [{"chunk_id": f"id{i}", "content": f"c{i}"} for i in range(n)]


def test_upload_success(mocker):
    f = FakeSearch()
    mocker.patch.object(m, "get_search_client", return_value=f)
    assert m.upload_chunks(docs(3), [[0.1]] * 3) == 3 and len(f.uploaded) == 3 and "embedding" in f.uploaded[0]


def test_upload_retries_failed_documents(mocker):
    f = FakeSearch(fail_upload=1)
    mocker.patch.object(m, "get_search_client", return_value=f)
    assert m.upload_chunks(docs(3), [[0.1]] * 3) == 3 and len(f.docs) == 3


def test_upload_gives_up_and_raises(mocker):
    f = FakeSearch(fail_upload=99)
    mocker.patch.object(m, "get_search_client", return_value=f)
    with pytest.raises(RuntimeError, match="failed to upload"):
        m.upload_chunks(docs(2), [[0.1]] * 2)


def test_upload_length_mismatch(mocker):
    with pytest.raises(RuntimeError, match="embeddings"):
        m.upload_chunks(docs(2), [[0.1]])


def test_upload_batches_of_100(mocker):
    f = FakeSearch()
    spy = mocker.spy(f, "upload_documents")
    mocker.patch.object(m, "get_search_client", return_value=f)
    m.upload_chunks(docs(250), [[0.1]] * 250)
    assert spy.call_count == 3
