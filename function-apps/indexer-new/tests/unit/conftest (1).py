import json
import os
import tempfile

import pytest


class Result:
    def __init__(self, key, ok=True):
        self.key, self.succeeded, self.error_message, self.status_code = key, ok, "boom", 500


class FakeSearchClient:
    """In-memory stand-in for the Azure Search index (documents keyed by chunk_id)."""

    def __init__(self, docs=()):
        self.docs = {d["chunk_id"]: dict(d) for d in docs}
        self.fail_delete = set()

    def get_document_count(self):
        return len(self.docs)

    def upload_documents(self, documents):
        for d in documents:
            self.docs[d["chunk_id"]] = d
        return [Result(d["chunk_id"]) for d in documents]

    def delete_documents(self, documents):
        out = []
        for d in documents:
            key = d["chunk_id"]
            if key in self.fail_delete:
                out.append(Result(key, False))
            else:
                self.docs.pop(key, None)
                out.append(Result(key))
        return out


def make_pages(n=5):
    return [{"source_url": f"https://www.royallondon.com/p{i}", "title": f"P{i}",
             "content": f"Pension text {i}. " * 120, "scraped_at": "2026-10-01T10:00:00+00:00"} for i in range(n)]


@pytest.fixture
def pages():
    return make_pages()


@pytest.fixture
def write_scrape():
    """write_scrape(pages, failures=None) -> path of a scraper-style JSON (+ optional failures sidecar)."""
    def _write(pgs, failures=None):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "royal_london_faq_approved_httpV1_x.json")
        with open(path, "w") as f:
            json.dump(pgs, f)
        if failures is not None:
            with open(path.replace(".json", "_failures.json"), "w") as f:
                json.dump(failures, f)
        return path
    return _write


@pytest.fixture
def index(mocker, script, pages):
    """An index already holding the chunks of `pages`, wired into the script; Azure calls stubbed out."""
    client = FakeSearchClient({"chunk_id": c["chunk_id"], "source_url": c["source_url"],
                               "dropdown_url": c.get("dropdown_url", "")} for c in script.chunk_pages(pages))
    mocker.patch.object(script, "get_search_client", return_value=client)
    mocker.patch.object(script, "_scan_index",
                        side_effect=lambda c, op, sel, on_doc: [on_doc(dict(d)) for d in list(c.docs.values())] and len(c.docs))
    mocker.patch.object(script, "embed_chunks", side_effect=lambda chunks: [[0.0] for _ in chunks])
    mocker.patch.object(script, "verify_azure_access", return_value=len(client.docs))
    mocker.patch.object(script, "verify_index", return_value=True)
    mocker.patch.object(script.time, "sleep")
    mocker.patch.object(script, "get_indexed_urls",
                        side_effect=lambda: {d["source_url"] for d in client.docs.values()})
    return client
