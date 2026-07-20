"""
Aria — Retrieval Diagnostic Script  V2
========================================
Debugs Azure AI Search retrieval for any query.
Shows EXACTLY what chunks are returned, their scores,
content previews, and diagnoses why the wrong chunks win.

CHANGELOG
---------
v2.2.0 (2026-07-20) - Mukesh Kund
    URL PRESENCE CHECK BUG FIX.
    Why: unquoted search.ismatch tokenized the URL fragment
    ('what-is-a-pension' → what/is/a/pension) and matched any URL
    sharing any token — the check returned 20 bereavement dropdown
    chunks and reported "Page in index? YES" incorrectly in every
    prior run (v1 and v2.x).
    - Fragment now phrase-quoted inside search.ismatch.
    - Client-side substring guard: only chunks whose source_url
      contains the fragment are returned/counted.
v2.1.0 (2026-07-20) - Mukesh Kund
    HQA FIELD INSPECTION IN URL CHECK.
    Why: v4 (HQA) ranked the expected page WORSE than v4-baseline
    for the pension-types query — suspected cause is
    deduplicate_questions_across_chunks() assigning the key
    question to a competing page. To confirm, the URL presence
    check now surfaces each chunk's HQA fields.
    - url_search() selects augmented_questions + title_questions
      (base-select fallback for non-v4 indexes).
    - URL-chunks output prints HQA Qs / TitleQs previews per chunk,
      "(empty)" when a chunk carries no augmented questions.
v2.0.0 (2026-07-20) - Mukesh Kund
    PRODUCTION-EQUIVALENT SEMANTIC MODE ADDED.
    Why: v1 only tested plain RRF hybrid / vector / BM25 —
    it did NOT apply the L2 semantic reranker, title_questions
    select, or rl-retrieval-profile that retriever.py uses in
    production. v1 diagnoses therefore understated v4 index
    quality and wrongly recommended re-chunking.
    - New search mode 4: semantic_search() — mirrors
      retriever.py exactly: query_type="semantic",
      semantic_configuration_name (default rlg-semantic-config),
      V4 select incl. title_questions with BASE fallback,
      optional --broad flag applying rl-retrieval-profile.
    - Reranker score (@search.rerankerScore, 0-4 scale) shown
      alongside hybrid score in semantic results.
    - Diagnosis rewritten: semantic mode is the authoritative
      verdict; plain-hybrid absence alone no longer triggers
      the "re-chunk to ~200 tokens" recommendation (removed —
      CHUNK_SIZE=1600 chars ≈ 400 tokens is intentional).
    - EMBEDDING_DIMENSIONS default 1024 → 1536 (matches v4).
v1.0.0 - Original three-mode diagnostic (hybrid/vector/BM25).

HOW TO RUN:
    # Default query (the pension types problem):
    python debug_retrievalV2.py

    # Any custom query:
    python debug_retrievalV2.py "your query here"

    # Simulate a BROAD query (applies rl-retrieval-profile):
    python debug_retrievalV2.py "your query" --broad

    # Also check if a specific URL is in the index:
    python debug_retrievalV2.py --check-url "https://www.royallondon.com/..."

REQUIRES:
    - .env file with AZURE_SEARCH_ENDPOINT and AZURE_OPENAI_ENDPOINT set
    - pip install azure-search-documents azure-identity openai python-dotenv

OUTPUT:
    1. Raw hybrid search results (all candidates, ranked by score)
    2. Vector-only search results (semantic similarity only)
    3. BM25-only search results (keyword match only)
    4. SEMANTIC search results (production-equivalent: L2 reranker
       + title_questions + optional rl-retrieval-profile)
    5. URL presence check — is the right page even in the index?
    6. Diagnosis — why the wrong chunks are winning
"""

import os
import sys
import textwrap
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from dotenv import load_dotenv

load_dotenv()

# ── Config (mirrors retriever.py + embeddings.py exactly) ─────
AZURE_OPENAI_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_SEARCH_ENDPOINT    = os.getenv("AZURE_SEARCH_ENDPOINT", "")
INDEX_NAME               = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index")
EMBEDDING_DEPLOYMENT     = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")
EMBEDDING_DIMENSIONS     = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1536"))
MIN_RELEVANCE_SCORE      = float(os.getenv("MIN_RELEVANCE_SCORE", "0.01"))
TOP_K                    = int(os.getenv("MAX_RETRIEVED_CHUNKS", "3"))
API_VERSION              = "2024-12-01-preview"
# v2.0.0 — mirrors retriever.py: "" disables semantic mode
SEMANTIC_CONFIG          = os.getenv("AZURE_SEARCH_SEMANTIC_CONFIG", "rlg-semantic-config")

# ── Target URL — the page that SHOULD be retrieved ────────────
EXPECTED_URL_FRAGMENT = "what-is-a-pension"

# ── Query ─────────────────────────────────────────────────────
BROAD_MODE = "--broad" in sys.argv          # v2.0.0
if "--check-url" in sys.argv:
    idx = sys.argv.index("--check-url")
    CHECK_URL = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
    QUERY = " ".join(a for a in sys.argv[1:] if not a.startswith("--") and a != CHECK_URL) or \
            "What types of pensions does Royal London offer?"
else:
    CHECK_URL = None
    QUERY = " ".join(a for a in sys.argv[1:] if not a.startswith("--")) or \
            "What types of pensions does Royal London offer?"


# ── Helpers ───────────────────────────────────────────────────
def divider(char="─", width=72):
    print(char * width)

def header(title):
    print()
    divider("═")
    print(f"  {title}")
    divider("═")

def section(title):
    print()
    divider()
    print(f"  {title}")
    divider()

def preview(text: str, chars: int = 300) -> str:
    """First N chars of chunk content, cleaned up."""
    clean = " ".join(text.split())
    return clean[:chars] + ("..." if len(clean) > chars else "")

def score_bar(score: float, max_score: float) -> str:
    """Visual bar showing relative score."""
    if max_score == 0:
        return "░" * 20
    filled = int((score / max_score) * 20)
    return "█" * filled + "░" * (20 - filled)


# ── Clients ───────────────────────────────────────────────────
def get_embedding_client() -> AzureOpenAI:
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    )
    return AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_ad_token_provider=token_provider,
        api_version=API_VERSION,
    )

def get_search_client() -> SearchClient:
    return SearchClient(
        endpoint=AZURE_SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=DefaultAzureCredential(),
    )

def get_query_embedding(query: str) -> list[float]:
    client = get_embedding_client()
    response = client.embeddings.create(
        input=[query],
        model=EMBEDDING_DEPLOYMENT,
        dimensions=EMBEDDING_DIMENSIONS,
    )
    return response.data[0].embedding


# ── Search functions ──────────────────────────────────────────
def hybrid_search(client: SearchClient, query: str, embedding: list[float], top: int = 10) -> list[dict]:
    """Hybrid BM25 + vector search — exactly as retriever.py does it."""
    vector_query = VectorizedQuery(
        vector=embedding,
        k_nearest_neighbors=top,
        fields="embedding",
    )
    results = client.search(
        search_text=query,
        vector_queries=[vector_query],
        select=["chunk_id", "content", "source_url", "section", "title"],
        top=top,
    )
    return [
        {
            "chunk_id":   r["chunk_id"],
            "content":    r["content"],
            "source_url": r["source_url"],
            "section":    r.get("section", ""),
            "title":      r.get("title", ""),
            "score":      r.get("@search.score", 0.0),
            "mode":       "hybrid",
        }
        for r in results
    ]

def vector_only_search(client: SearchClient, embedding: list[float], top: int = 10) -> list[dict]:
    """Pure vector search — semantic similarity only, no keyword boost."""
    vector_query = VectorizedQuery(
        vector=embedding,
        k_nearest_neighbors=top,
        fields="embedding",
    )
    results = client.search(
        search_text=None,
        vector_queries=[vector_query],
        select=["chunk_id", "content", "source_url", "section", "title"],
        top=top,
    )
    return [
        {
            "chunk_id":   r["chunk_id"],
            "content":    r["content"],
            "source_url": r["source_url"],
            "section":    r.get("section", ""),
            "title":      r.get("title", ""),
            "score":      r.get("@search.score", 0.0),
            "mode":       "vector",
        }
        for r in results
    ]

def bm25_only_search(client: SearchClient, query: str, top: int = 10) -> list[dict]:
    """Pure BM25 keyword search — no vector component."""
    results = client.search(
        search_text=query,
        select=["chunk_id", "content", "source_url", "section", "title"],
        top=top,
    )
    return [
        {
            "chunk_id":   r["chunk_id"],
            "content":    r["content"],
            "source_url": r["source_url"],
            "section":    r.get("section", ""),
            "title":      r.get("title", ""),
            "score":      r.get("@search.score", 0.0),
            "mode":       "bm25",
        }
        for r in results
    ]

def semantic_search(client: SearchClient, query: str, embedding: list[float],
                    top: int = 10, broad: bool = False) -> list[dict]:
    """
    v2.0.0 — PRODUCTION-EQUIVALENT search. Mirrors retriever.py:
      - hybrid (BM25 + vector) base
      - query_type="semantic" + SEMANTIC_CONFIG (L2 reranker)
      - V4 select incl. title_questions, BASE fallback for old indexes
      - broad=True applies rl-retrieval-profile scoring profile
    Returns rerankerScore (0-4 scale) as primary score.
    """
    vector_query = VectorizedQuery(
        vector=embedding,
        k_nearest_neighbors=top,
        fields="embedding",
    )
    BASE_SELECT = ["chunk_id", "content", "source_url", "section", "title"]
    V4_SELECT   = BASE_SELECT + ["title_questions"]

    kwargs = dict(
        search_text=query,
        vector_queries=[vector_query],
        select=V4_SELECT,
        top=top,
    )
    if SEMANTIC_CONFIG:
        kwargs["query_type"] = "semantic"
        kwargs["semantic_configuration_name"] = SEMANTIC_CONFIG
    if broad and SEMANTIC_CONFIG:
        kwargs["scoring_profile"] = "rl-retrieval-profile"

    try:
        results = list(client.search(**kwargs))
    except Exception as err:
        # Fallback for indexes without v4 fields / profile
        if "title_questions" in str(err) or "rl-retrieval-profile" in str(err):
            kwargs["select"] = BASE_SELECT
            kwargs.pop("scoring_profile", None)
            results = list(client.search(**kwargs))
        else:
            raise

    out = []
    for r in results:
        reranker = r.get("@search.rerankerScore")
        out.append({
            "chunk_id":        r["chunk_id"],
            "content":         r["content"],
            "source_url":      r["source_url"],
            "section":         r.get("section", ""),
            "title":           r.get("title", ""),
            "title_questions": r.get("title_questions", ""),
            # rerankerScore (0-4) is authoritative when semantic is on
            "score":           reranker if reranker is not None else r.get("@search.score", 0.0),
            "hybrid_score":    r.get("@search.score", 0.0),
            "reranker_score":  reranker,
            "mode":            "semantic",
        })
    return out


def url_search(client: SearchClient, url_fragment: str, top: int = 20) -> list[dict]:
    """
    Check if any chunks from a specific URL exist in the index.
    v2.1.0: also selects augmented_questions + title_questions so the
    HQA coverage for the expected page can be inspected directly —
    diagnoses HQA gaps (e.g. dedup stripping a key question) vs
    chunk-size issues. Falls back to base select on non-v4 indexes.
    """
    base_select = ["chunk_id", "content", "source_url", "section", "title"]
    hqa_select  = base_select + ["augmented_questions", "title_questions"]
    # v2.2.0: phrase-quoted match. Unquoted ismatch tokenizes the
    # fragment ('what-is-a-pension' → what/is/a/pension) and matches
    # ANY url sharing ANY token — returned bereavement pages and made
    # "Page in index? YES (N chunks)" wrong in every prior run.
    kwargs = dict(
        search_text="*",
        filter=f"search.ismatch('\"{url_fragment}\"', 'source_url')",
        select=hqa_select,
        top=top,
    )
    try:
        results = list(client.search(**kwargs))
    except Exception as err:
        if "augmented_questions" in str(err) or "title_questions" in str(err):
            kwargs["select"] = base_select
            results = list(client.search(**kwargs))
        else:
            raise
    # v2.2.0: client-side substring guard — belt and braces even
    # with the phrase query, only chunks whose source_url actually
    # contains the fragment are counted.
    return [
        {
            "chunk_id":            r["chunk_id"],
            "content":             r["content"],
            "source_url":          r["source_url"],
            "section":             r.get("section", ""),
            "title":               r.get("title", ""),
            "augmented_questions": r.get("augmented_questions", ""),
            "title_questions":     r.get("title_questions", ""),
            "score":               r.get("@search.score", 0.0),
        }
        for r in results
        if url_fragment in r["source_url"]
    ]


# ── Display ───────────────────────────────────────────────────
def print_results(results: list[dict], label: str, highlight_url: str = ""):
    section(label)
    if not results:
        print("  ⚠️  No results returned.")
        return

    max_score = max(r["score"] for r in results)

    for i, r in enumerate(results, 1):
        is_expected = highlight_url and highlight_url in r["source_url"]
        is_sent_to_llm = i <= TOP_K
        below_threshold = r["score"] < MIN_RELEVANCE_SCORE

        flag = ""
        if is_expected:
            flag = "  ✅ THIS IS THE PAGE WE WANT"
        if below_threshold:
            flag += "  ⛔ BELOW MIN_RELEVANCE_SCORE — filtered out"
        if is_sent_to_llm and not below_threshold:
            flag += "  → SENT TO LLM"

        print(f"\n  #{i}  {score_bar(r['score'], max_score)}  score={r['score']:.4f}{flag}")
        print(f"       Title : {r['title'] or '(no title)'}")
        print(f"       URL   : {r['source_url']}")
        print(f"       Chunk : {r['chunk_id']}")
        # Wrap content preview
        content_preview = preview(r["content"], 250)
        wrapped = textwrap.wrap(content_preview, width=65)
        print(f"       Content preview:")
        for line in wrapped:
            print(f"         {line}")


# ── Diagnosis ─────────────────────────────────────────────────
def diagnose(hybrid: list[dict], vector: list[dict], bm25: list[dict],
             semantic: list[dict], url_chunks: list[dict], query: str):
    header("DIAGNOSIS")

    expected_in_hybrid = any(EXPECTED_URL_FRAGMENT in r["source_url"] for r in hybrid)
    expected_in_vector = any(EXPECTED_URL_FRAGMENT in r["source_url"] for r in vector)
    expected_in_bm25   = any(EXPECTED_URL_FRAGMENT in r["source_url"] for r in bm25)
    expected_in_sem    = any(EXPECTED_URL_FRAGMENT in r["source_url"] for r in semantic)
    expected_in_index  = len(url_chunks) > 0

    print(f"\n  Query: \"{query}\"")
    print(f"  Expected page fragment: '{EXPECTED_URL_FRAGMENT}'")
    print()
    print(f"  Page in index at all?        {'✅ YES (' + str(len(url_chunks)) + ' chunks)' if expected_in_index else '❌ NO — PAGE NOT INDEXED'}")
    print(f"  Appears in hybrid search?    {'✅ YES' if expected_in_hybrid else '❌ NO'}")
    print(f"  Appears in vector search?    {'✅ YES' if expected_in_vector else '❌ NO'}")
    print(f"  Appears in BM25 search?      {'✅ YES' if expected_in_bm25 else '❌ NO'}")
    print(f"  Appears in SEMANTIC search?  {'✅ YES' if expected_in_sem else '❌ NO'}  ← production-equivalent")
    print()

    # Root cause
    print("  ROOT CAUSE:")
    if not expected_in_index:
        print("  ❌ CRITICAL: The page was never indexed.")
        print("     Fix: Re-run your scraper/indexer for this URL and re-index.")

    elif expected_in_sem:
        rank = next(i+1 for i, r in enumerate(semantic) if EXPECTED_URL_FRAGMENT in r["source_url"])
        print(f"  ✅ Page ranks #{rank} in SEMANTIC (production-equivalent) search.")
        print("     Production retrieval is healthy — plain hybrid/vector/BM25")
        print("     absence is expected; the L2 reranker is doing its job.")
        print("     No re-chunking needed.")

    elif not expected_in_hybrid and not expected_in_vector and not expected_in_bm25 and not expected_in_sem:
        print("  ❌ Page IS in index but absent from ALL modes incl. SEMANTIC.")
        print("     Before considering re-chunking, verify:")
        print("       1. Is the top result actually a BETTER answer? (Check")
        print("          expected-fragment mapping in the test sheet first.)")
        print("       2. Do this page's augmented_questions/title_questions")
        print("          cover this query phrasing? (HQA gap, not chunk size.)")
        print("     Re-chunking is a LAST resort — CHUNK_SIZE=1600 is intentional.")

    elif not expected_in_hybrid and expected_in_vector:
        print("  ⚠️  Page appears in vector search but NOT hybrid.")
        print("     BM25 keyword matching is HURTING this page's rank.")
        print("     Likely cause: The chunk doesn't contain enough exact")
        print("     query keywords ('types', 'pensions', 'Royal London').")
        print("     Fix: Improve chunk title/section metadata to boost BM25.")

    elif not expected_in_hybrid and expected_in_bm25:
        print("  ⚠️  Page appears in BM25 but NOT hybrid.")
        print("     Vector similarity is HURTING this page's rank.")
        print("     Likely cause: The embedding of this chunk is not close")
        print("     enough to the query embedding — chunk may be too generic.")
        print("     Fix: Split the chunk so the key sentence is more prominent.")

    elif expected_in_hybrid:
        # Find its rank
        rank = next(i+1 for i, r in enumerate(hybrid) if EXPECTED_URL_FRAGMENT in r["source_url"])
        score = next(r["score"] for r in hybrid if EXPECTED_URL_FRAGMENT in r["source_url"])
        if rank <= TOP_K and score >= MIN_RELEVANCE_SCORE:
            print(f"  ✅ Page IS being retrieved (rank #{rank}, score={score:.4f}).")
            print("     But check: is the content in that chunk the right content?")
            print("     The model may be answering correctly from your index —")
            print("     re-read the chunk content above to confirm.")
        elif score < MIN_RELEVANCE_SCORE:
            print(f"  ⛔ Page found at rank #{rank} but score={score:.4f} is below")
            print(f"     MIN_RELEVANCE_SCORE={MIN_RELEVANCE_SCORE} — filtered out.")
            print(f"     Fix: Lower MIN_RELEVANCE_SCORE to {score - 0.001:.3f} or")
            print(f"     improve chunk quality so it scores higher.")
        else:
            print(f"  ⚠️  Page found but at rank #{rank} — below TOP_K={TOP_K} cutoff.")
            print(f"     Score={score:.4f}. It's retrieved but not sent to the LLM.")
            print(f"     Fix: Increase TOP_K or improve chunk quality.")

    print()
    print("  AGM / IRRELEVANT CHUNKS IN RESULTS:")
    agm_in_hybrid = [r for r in hybrid[:TOP_K] if "agm" in r["source_url"].lower()
                     or "agm" in r["title"].lower()
                     or "agm" in r["content"].lower()[:100]]
    if agm_in_hybrid:
        print(f"  ⚠️  {len(agm_in_hybrid)} AGM chunk(s) making it into top-{TOP_K} sent to LLM.")
        print(f"     Current MIN_RELEVANCE_SCORE={MIN_RELEVANCE_SCORE}")
        print(f"     These chunks score: {[round(r['score'],4) for r in agm_in_hybrid]}")
        print(f"     Fix: Raise MIN_RELEVANCE_SCORE above those scores,")
        print(f"     or exclude AGM pages during indexing.")
    else:
        print(f"  ✅ No AGM chunks in top-{TOP_K}.")

    print()
    divider("═")
    print()


# ── Main ──────────────────────────────────────────────────────
def main():
    header(f"ARIA RETRIEVAL DIAGNOSTIC")
    print(f"\n  Query        : {QUERY}")
    print(f"  Index        : {INDEX_NAME}")
    print(f"  Endpoint     : {AZURE_SEARCH_ENDPOINT}")
    print(f"  TOP_K        : {TOP_K}  (chunks sent to LLM)")
    print(f"  MIN_SCORE    : {MIN_RELEVANCE_SCORE}")
    print(f"  Embedding    : {EMBEDDING_DEPLOYMENT} ({EMBEDDING_DIMENSIONS}d)")
    print(f"  Semantic     : {SEMANTIC_CONFIG or 'DISABLED'}  |  BROAD mode: {'ON (rl-retrieval-profile)' if BROAD_MODE else 'off'}")

    # Validate config
    if not AZURE_SEARCH_ENDPOINT:
        print("\n  ❌ AZURE_SEARCH_ENDPOINT not set in .env — aborting.")
        sys.exit(1)
    if not AZURE_OPENAI_ENDPOINT:
        print("\n  ❌ AZURE_OPENAI_ENDPOINT not set in .env — aborting.")
        sys.exit(1)

    print("\n  Generating query embedding...", end="", flush=True)
    try:
        embedding = get_query_embedding(QUERY)
        print(f" done ({len(embedding)}d)")
    except Exception as e:
        print(f"\n  ❌ Embedding failed: {e}")
        sys.exit(1)

    search_client = get_search_client()

    # 1. Hybrid search (what the pipeline actually does)
    print("  Running hybrid search...", end="", flush=True)
    try:
        hybrid = hybrid_search(search_client, QUERY, embedding, top=10)
        print(f" done ({len(hybrid)} results)")
    except Exception as e:
        print(f"\n  ❌ Hybrid search failed: {e}")
        hybrid = []

    # 2. Vector-only search
    print("  Running vector-only search...", end="", flush=True)
    try:
        vector = vector_only_search(search_client, embedding, top=10)
        print(f" done ({len(vector)} results)")
    except Exception as e:
        print(f"\n  ❌ Vector search failed: {e}")
        vector = []

    # 3. BM25-only search
    print("  Running BM25-only search...", end="", flush=True)
    try:
        bm25 = bm25_only_search(search_client, QUERY, top=10)
        print(f" done ({len(bm25)} results)")
    except Exception as e:
        print(f"\n  ❌ BM25 search failed: {e}")
        bm25 = []

    # 4. SEMANTIC — production-equivalent (v2.0.0)
    print("  Running SEMANTIC search (production-equivalent)...", end="", flush=True)
    try:
        semantic = semantic_search(search_client, QUERY, embedding, top=10, broad=BROAD_MODE)
        print(f" done ({len(semantic)} results)")
    except Exception as e:
        print(f"\n  ❌ Semantic search failed: {e}")
        semantic = []

    # 5. URL presence check
    print(f"  Checking index for '{EXPECTED_URL_FRAGMENT}'...", end="", flush=True)
    try:
        url_chunks = url_search(search_client, EXPECTED_URL_FRAGMENT, top=20)
        print(f" done ({len(url_chunks)} chunks found)")
    except Exception as e:
        print(f"\n  ⚠️  URL check failed: {e}")
        url_chunks = []

    # Print results
    print_results(hybrid, f"HYBRID SEARCH — top 10 (pipeline uses top {TOP_K})",
                  highlight_url=EXPECTED_URL_FRAGMENT)
    print_results(vector, "VECTOR-ONLY SEARCH — top 10 (semantic similarity)",
                  highlight_url=EXPECTED_URL_FRAGMENT)
    print_results(bm25,   "BM25-ONLY SEARCH — top 10 (keyword match)",
                  highlight_url=EXPECTED_URL_FRAGMENT)
    print_results(semantic, "SEMANTIC SEARCH — top 10 (PRODUCTION-EQUIVALENT: "
                  "L2 reranker 0-4 scale)",
                  highlight_url=EXPECTED_URL_FRAGMENT)

    # URL chunks
    if url_chunks:
        section(f"CHUNKS FROM '{EXPECTED_URL_FRAGMENT}' IN INDEX ({len(url_chunks)} found)")
        for i, r in enumerate(url_chunks, 1):
            print(f"\n  Chunk {i}: {r['chunk_id']}")
            print(f"  URL    : {r['source_url']}")
            content_preview = preview(r["content"], 400)
            wrapped = textwrap.wrap(content_preview, width=65)
            print(f"  Content:")
            for line in wrapped:
                print(f"    {line}")
            # v2.1.0 — HQA field inspection
            if r.get("augmented_questions"):
                print(f"  HQA Qs :")
                for line in textwrap.wrap(preview(r["augmented_questions"], 300), width=65):
                    print(f"    {line}")
            else:
                print(f"  HQA Qs : (empty)")
            if r.get("title_questions"):
                print(f"  TitleQs:")
                for line in textwrap.wrap(preview(r["title_questions"], 300), width=65):
                    print(f"    {line}")
    else:
        section(f"CHUNKS FROM '{EXPECTED_URL_FRAGMENT}' IN INDEX")
        print(f"\n  ❌ NONE FOUND — this page is NOT in your index.")
        print(f"  The URL https://www.royallondon.com/guides-tools/pension-guides/")
        print(f"  pension-basics/what-is-a-pension/ was either not scraped")
        print(f"  or excluded during indexing.")

    # Diagnosis
    diagnose(hybrid, vector, bm25, semantic, url_chunks, QUERY)


if __name__ == "__main__":
    main()