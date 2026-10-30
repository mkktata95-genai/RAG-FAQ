"""Plants ONE junk chunk in the index to prove the sync's stale-delete works on real Azure.
Run from the repo root with the same $env: settings and venv as the sync. Then re-run the sync."""
import os, sys
sys.path.insert(0, "src")
import chunk_embed_index_funcV1 as m

c = m.get_search_client()
before = c.get_document_count()
doc = {"chunk_id": "zz-stale-test-1", "content": "STALE TEST - safe to delete", "title": "stale test",
       "source_url": "https://www.royallondon.com/zz-stale-test", "chunk_index": 0, "total_chunks": 1,
       "scraped_at": "2026-10-30T00:00:00+00:00", "indexed_at": "2026-10-30T00:00:00+00:00",
       "embedding": [0.0] * m.EMBEDDING_DIMS}
r = c.upload_documents([doc])
print("uploaded:", [x.succeeded for x in r], "| count before:", before)
print("now re-run the sync; expect stale_chunks=1 urls_removed=1 and the count back to", before)