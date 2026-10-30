import pytest


def table(rows):
    lines = ["| Name | Value |", "| --- | --- |"] + [f"| item{i} | {i} |" for i in range(rows)]
    return "\n".join(lines)


def test_parse_elements_recognises_each_type(script):
    content = "## Heading\n\nSome prose line\nsecond line\n| A | B |\n| - | - |\n| 1 | 2 |"
    types = [e["type"] for e in script._ea_parse_elements(content)]
    assert types == ["header", "blank", "prose", "table"]


def test_parse_table_block_splits_header_and_rows(script):
    parsed = script._ea_parse_table_block(["| A | B |", "| --- | --- |", "| 1 | 2 |", "| 3 | 4 |"])
    assert parsed["header_row"] == ["A", "B"]
    assert parsed["data_rows"] == [["1", "2"], ["3", "4"]]


def test_small_table_is_one_atomic_chunk_with_its_header(script):
    chunks = script.chunk_content_element_aware(table(5))
    assert len(chunks) == 1 and chunks[0]["element_type"] == "table"
    assert "| Name | Value |" in chunks[0]["text"]


def test_large_table_is_split_by_rows_and_keeps_header_in_each_chunk(script):
    chunks = script.chunk_content_element_aware(table(35))
    assert [c["element_type"] for c in chunks] == ["table", "table"]
    assert all(c["text"].startswith("| Name | Value |") for c in chunks)
    assert chunks[0]["text"].count("item") == 30 and chunks[1]["text"].count("item") == 5


def test_table_without_data_rows_is_kept_as_is(script):
    chunks = script.chunk_content_element_aware("| A | B |\n| - | - |")
    assert len(chunks) == 1 and "| A | B |" in chunks[0]["text"]


def test_render_table_chunk_without_header_renders_rows_only(script):
    assert script._ea_render_table_chunk(None, [["1", "2"]]) == "| 1 | 2 |"


def test_h2_headers_are_hard_section_boundaries(script):
    chunks = script.chunk_content_element_aware("## Alpha\nalpha text here\n## Beta\nbeta text here")
    texts = [c["text"] for c in chunks]
    assert len(texts) == 2
    assert texts[0].startswith("### Alpha") and "beta" not in texts[0]
    assert texts[1].startswith("### Beta") and "alpha" not in texts[1]


def test_content_without_tables_or_headers_is_plain_prose(script):
    chunks = script.chunk_content_element_aware("Plain paragraph about pensions. " * 10)
    assert chunks and all(c["element_type"] == "prose" for c in chunks)


def test_long_prose_is_split_within_chunk_size(script):
    chunks = script.chunk_content_element_aware("Sentence about pensions. " * 300, chunk_size=500, chunk_overlap=50)
    assert len(chunks) > 1 and all(len(c["text"]) <= 500 for c in chunks)


def test_orphaned_section_header_is_merged_into_next_chunk(script):
    assert script._ea_merge_orphaned_header(["### H", "body"], "H") == ["### H\nbody"]
    assert script._ea_merge_orphaned_header(["text", "body"], "H") == ["text", "body"]
    assert script._ea_merge_orphaned_header(["### H"], "H") == ["### H"]


def test_blank_input_gives_no_chunks(script):
    assert script.chunk_content_element_aware("") == []


@pytest.mark.parametrize("raw,expected", [
    ("See [our guide](https://example.com/x) now", "See our guide now"),
    ("Keep [ours](https://www.royallondon.com/a) link", "Keep [ours](https://www.royallondon.com/a) link"),
    ("Visit https://other.org/page today", "Visit today"),
    ("Keep https://www.royallondon.com/pensions here", "Keep https://www.royallondon.com/pensions here"),
])
def test_clean_content_removes_only_external_urls(script, raw, expected):
    assert script.clean_content(raw) == expected


def test_content_hash_is_stable_and_content_sensitive(script):
    assert script.compute_content_hash("a") == script.compute_content_hash("a")
    assert script.compute_content_hash("a") != script.compute_content_hash("b")
