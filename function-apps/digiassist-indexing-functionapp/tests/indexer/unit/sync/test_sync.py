import pytest


def url(i):
    return f"https://www.royallondon.com/p{i}"


def stale_doc(script, source_url, text="old text", dropdown_url=""):
    return {"chunk_id": script.compute_chunk_id(dropdown_url or source_url, 0, text),
            "source_url": source_url, "dropdown_url": dropdown_url}


def test_unchanged_scrape_deletes_nothing(script, index, pages, write_scrape):
    before = set(index.docs)
    result = script.run_pipeline("sync", write_scrape(pages))
    assert result["success"] and result["chunks_deleted"] == 0
    assert set(index.docs) == before


def test_changed_page_replaces_its_old_chunks(script, index, pages, write_scrape):
    changed = [dict(p) for p in pages]
    changed[0]["content"] = "Totally new text. " * 100
    result = script.run_pipeline("sync", write_scrape(changed))
    assert result["success"] and result["chunks_deleted"] > 0
    assert set(index.docs) == {c["chunk_id"] for c in script.chunk_pages(changed)}


def test_single_removed_url_is_deleted(script, index, pages, write_scrape):
    result = script.run_pipeline("sync", write_scrape(pages[1:]))
    assert result["success"] and result["urls_removed"] == 1
    assert url(0) not in {d["source_url"] for d in index.docs.values()}


def test_mass_removal_is_refused_before_any_write(script, index, pages, write_scrape):
    before = set(index.docs)
    result = script.run_pipeline("sync", write_scrape(pages[3:]))  # 3 of 5 URLs vanish
    assert not result["success"] and "ABORTED" in result["error"]
    assert set(index.docs) == before


def test_mass_removal_allowed_with_override(script, index, pages, write_scrape):
    result = script.run_pipeline("sync", write_scrape(pages[3:]), allow_mass_removal=True)
    assert result["success"] and result["urls_removed"] == 3
    assert len(index.docs) == len(script.chunk_pages(pages[3:]))


def test_failed_urls_keep_their_chunks(script, index, pages, write_scrape):
    failures = [{"url": url(i), "reason": "timeout"} for i in range(3)]
    result = script.run_pipeline("sync", write_scrape(pages[3:], failures=failures))
    assert result["success"] and result["urls_removed"] == 0 and result["chunks_retained_failed"] > 0
    assert len(index.docs) == len(script.chunk_pages(pages))


def test_unreadable_failures_sidecar_stops_the_run(script, index, pages, write_scrape):
    path = write_scrape(pages)
    with open(path.replace(".json", "_failures.json"), "w") as f:
        f.write("not json")
    before = set(index.docs)
    result = script.run_pipeline("sync", path)
    assert not result["success"]
    assert set(index.docs) == before


def test_orphan_dropdown_chunk_is_removed_base_page_kept(script, index, pages, write_scrape):
    orphan = stale_doc(script, url(0), dropdown_url=url(0) + "#tab1")
    index.docs[orphan["chunk_id"]] = orphan
    result = script.run_pipeline("sync", write_scrape(pages))
    assert result["success"] and result["chunks_deleted"] == 1
    assert orphan["chunk_id"] not in index.docs


def test_failed_delete_fails_the_run(script, index, pages, write_scrape):
    orphan = stale_doc(script, url(0), dropdown_url=url(0) + "#tab1")
    index.docs[orphan["chunk_id"]] = orphan
    index.fail_delete = {orphan["chunk_id"]}
    result = script.run_pipeline("sync", write_scrape(pages))
    assert not result["success"] and "could not be deleted" in result["error"]


def test_new_only_adds_new_pages_and_never_deletes(script, index, pages, write_scrape):
    orphan = stale_doc(script, url(0), dropdown_url=url(0) + "#tab1")
    index.docs[orphan["chunk_id"]] = orphan
    extra = pages + [{"source_url": "https://www.royallondon.com/new", "title": "New", "content": "Brand new page. " * 60}]
    result = script.run_pipeline("new-only", write_scrape(extra))
    assert result["success"] and result["pages_indexed"] == 1 and result["chunks_deleted"] == 0
    assert orphan["chunk_id"] in index.docs


def test_empty_scrape_is_refused(script, index, write_scrape):
    assert not script.run_pipeline("sync", write_scrape([]))["success"]


def test_dry_run_makes_no_azure_calls(script, index, pages, write_scrape):
    result = script.run_pipeline("sync", write_scrape(pages), dry_run=True)
    assert result["success"] and result["chunks_uploaded"] == 0
    script.verify_azure_access.assert_not_called()


def test_unset_index_name_is_refused(mocker, script, pages, write_scrape):
    mocker.patch.object(script, "INDEX_NAME", "")
    result = script.run_pipeline("sync", write_scrape(pages))
    assert not result["success"] and "AZURE_SEARCH_INDEX_NAME" in result["error"]


def test_invalid_mode_is_refused(script, pages, write_scrape):
    assert not script.run_pipeline("full", write_scrape(pages))["success"]
