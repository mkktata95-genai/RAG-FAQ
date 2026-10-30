BODY = "Pension information for customers. " * 10


def page(url, **extra):
    return {"source_url": url, "title": "T", "content": BODY, **extra}


def test_dropdown_page_becomes_one_atomic_chunk_keyed_by_dropdown_url(script):
    chunks = script.chunk_pages([page("https://x.com/a", dropdown_title="Option", dropdown_url="https://x.com/a#opt")])
    assert len(chunks) == 1
    assert chunks[0]["element_type"] == "dropdown_state"
    assert chunks[0]["chunk_id"] == script.compute_chunk_id("https://x.com/a#opt", 0, chunks[0]["content"])


def test_two_dropdown_states_under_one_page_are_both_kept(script):
    chunks = script.chunk_pages([
        page("https://x.com/a", dropdown_title="One", dropdown_url="https://x.com/a#one"),
        page("https://x.com/a", dropdown_title="Two", dropdown_url="https://x.com/a#two"),
    ])
    assert len(chunks) == 2 and len({c["chunk_id"] for c in chunks}) == 2


def test_duplicate_url_is_chunked_once(script):
    chunks = script.chunk_pages([page("https://x.com/a"), page("https://x.com/a")])
    assert {c["source_url"] for c in chunks} == {"https://x.com/a"}
    assert len([c for c in chunks if c["chunk_index"] == 0]) == 1


def test_very_short_pages_are_skipped(script):
    assert script.chunk_pages([{"source_url": "https://x.com/s", "title": "T", "content": "too short"}]) == []


def test_chunk_indexes_and_totals_are_consistent(script):
    pg = page("https://x.com/long")
    pg["content"] = "Long pension text about allowances. " * 200
    chunks = script.chunk_pages([pg])
    assert len(chunks) > 1
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(c["total_chunks"] == len(chunks) for c in chunks)


def test_chunks_carry_pipeline_version_and_run_id(script):
    chunks = script.chunk_pages([page("https://x.com/a"), page("https://x.com/b")])
    assert {c["pipeline_version"] for c in chunks} == {script.PIPELINE_VERSION}
    assert len({c["index_run_id"] for c in chunks}) == 1
