from datetime import datetime

from tests.unit.sync.conftest import make_pages


def test_chunk_id_is_deterministic_for_same_input(script):
    assert script.compute_chunk_id("https://x/a", 0, "text") == script.compute_chunk_id("https://x/a", 0, "text")


def test_chunk_id_changes_when_content_url_or_position_changes(script):
    base = script.compute_chunk_id("https://x/a", 0, "text")
    assert base != script.compute_chunk_id("https://x/a", 0, "other")
    assert base != script.compute_chunk_id("https://x/b", 0, "text")
    assert base != script.compute_chunk_id("https://x/a", 1, "text")


def test_chunking_same_pages_twice_gives_same_ids(script):
    pages = make_pages()
    assert [c["chunk_id"] for c in script.chunk_pages(pages)] == [c["chunk_id"] for c in script.chunk_pages(pages)]


def test_every_page_produces_chunks_with_required_fields(script):
    chunks = script.chunk_pages(make_pages())
    assert {c["source_url"] for c in chunks} == {p["source_url"] for p in make_pages()}
    for chunk in chunks:
        assert chunk["content"] and chunk["chunk_id"] and chunk["content_hash"]


def test_scraped_at_falls_back_to_a_valid_timestamp(script):
    page = {"source_url": "https://www.royallondon.com/nodate", "title": "N", "content": "No date here. " * 50}
    for chunk in script.chunk_pages([page]):
        assert chunk["scraped_at"]
        datetime.fromisoformat(chunk["scraped_at"])  # real timestamp, never ""


def test_scraped_at_is_kept_when_present(script):
    chunks = script.chunk_pages(make_pages(1))
    assert all(c["scraped_at"] == "2026-10-01T10:00:00+00:00" for c in chunks)
