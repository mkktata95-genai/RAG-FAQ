"""
audit_duplicates.py — read-only duplicate-content audit. v2
Pages through ALL docs in the target index and reports genuine
chunk-level content duplication.

CHANGELOG
---------
v2 — Fixed a false-positive bug found during v5 rollout QA.
      v1 grouped chunks by the stored `content_hash` field alone.
      That field is PAGE-level by design (chunk_and_index_hqaV5.py /
      content_freshnessV1.py deliberately stamp the SAME content_hash
      on every chunk belonging to one page — it exists so the
      freshness job can detect "did this page change", NOT to
      identify unique chunk content). Grouping by it made every
      multi-chunk page look like a "duplicate group" — a 297-page,
      ~8000-chunk index reported ~8000 "removable duplicates" when
      the actual chunk text was almost entirely unique. Confirmed:
      "groups within one URL" vastly outnumbering "groups across
      URLs" is the signature of this false positive, not real dup
      content — genuine cross-page duplication would show the
      opposite pattern.

      FIX: fetch the actual `content` field per chunk and hash THAT
      (chunk_content_hash, computed locally, never stored in the
      index) to find TRUE duplicates — chunks whose real text is
      identical. The old page-level content_hash is still fetched
      and reported separately as an informational "chunks per page"
      stat, clearly labelled as expected/by-design, not an error.

      Also: INDEX default updated v4 -> v5 (env var override still
      wins either way — this only affects the fallback default).
v1 — Original page-level-hash version (superseded — false positive
      on any multi-chunk page).
"""
import os
import hashlib
from collections import Counter, defaultdict

from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient

load_dotenv()

INDEX = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v5")
ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
API_KEY = os.getenv("AZURE_SEARCH_API_KEY") or os.getenv("AZURE_SEARCH_KEY")
credential = AzureKeyCredential(API_KEY) if API_KEY else DefaultAzureCredential()

client = SearchClient(ENDPOINT, INDEX, credential)

docs = []
skip = 0
PAGE = 1000
while True:
    batch = list(client.search(
        search_text="*",
        # v2: now also fetches "content" — the actual chunk text —
        # so real duplicates can be detected. content_hash is still
        # fetched too, but only for the separate informational stat.
        select=["chunk_id", "source_url", "content_hash", "content", "element_type"],
        top=PAGE,
        skip=skip,
        order_by=["indexed_at asc"],
    ))
    docs.extend(batch)
    if len(batch) < PAGE:
        break
    skip += PAGE

print(f"{INDEX}: fetched {len(docs)} docs")

# ── TRUE duplicate detection — hash the actual chunk content ──────────────
# This is the real signal: two chunks with byte-identical content text
# are genuine duplicates, regardless of what their stored content_hash
# (a page-level field) says.
by_content_hash = defaultdict(list)
for d in docs:
    text = d.get("content", "") or ""
    chunk_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    by_content_hash[chunk_hash].append(d)

true_dup_groups = {h: v for h, v in by_content_hash.items() if len(v) > 1}
true_dup_docs = sum(len(v) for v in true_dup_groups.values())

print("\n" + "=" * 70)
print("TRUE CONTENT DUPLICATES (hash of actual chunk text)")
print("=" * 70)
print(f"unique chunk contents  : {len(by_content_hash)}")
print(f"duplicated content groups: {len(true_dup_groups)}")
print(f"chunks involved in dups: {true_dup_docs} "
      f"({true_dup_docs - len(true_dup_groups)} removable)")

same_url = cross_url = 0
url_counter = Counter()
for h, v in true_dup_groups.items():
    urls = {d["source_url"] for d in v}
    if len(urls) == 1:
        same_url += 1
    else:
        cross_url += 1
    for u in urls:
        url_counter[u] += 1

print(f"groups within one URL  : {same_url}")
print(f"groups across URLs     : {cross_url}")

if true_dup_groups:
    print("\nTop 15 URLs involved in TRUE duplicate content:")
    for u, n in url_counter.most_common(15):
        print(f"  {n:3d}  {u}")

    print("\nSample duplicate groups (first 5, showing content preview):")
    for h, v in list(true_dup_groups.items())[:5]:
        preview = (v[0].get("content", "") or "")[:100].replace("\n", " ")
        print(f"\n  {len(v)}x identical | element_type={v[0].get('element_type','?')}")
        print(f"    preview: {preview!r}")
        for d in v[:3]:
            print(f"    - {d.get('source_url','')}")
else:
    print("\n✅ No true content duplicates found — every chunk's text is unique.")

# ── Informational only — page-level content_hash stat ─────────────────────
# NOT an error signal. content_hash is deliberately identical across
# every chunk from the same page (used by content_freshnessV1.py for
# page-change detection). A page with N chunks will always show N
# entries sharing one content_hash — that's correct, expected behaviour,
# reported here purely as a "chunks per page" sanity check, not a
# duplicate-content warning.
by_page_hash = defaultdict(list)
for d in docs:
    by_page_hash[d.get("content_hash", "")].append(d)

multi_chunk_pages = {h: v for h, v in by_page_hash.items() if h and len(v) > 1}

print("\n" + "=" * 70)
print("INFORMATIONAL — chunks-per-page (via page-level content_hash)")
print("=" * 70)
print("NOTE: this is expected/by-design, NOT a duplication issue.")
print(f"pages with multiple chunks: {len(multi_chunk_pages)}")
if multi_chunk_pages:
    chunk_counts = sorted((len(v) for v in multi_chunk_pages.values()), reverse=True)
    print(f"max chunks on one page    : {chunk_counts[0]}")
    print(f"avg chunks per multi-chunk page: {sum(chunk_counts)/len(chunk_counts):.1f}")