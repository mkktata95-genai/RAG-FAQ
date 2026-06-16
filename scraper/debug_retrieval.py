"""
Aria — Retrieval Diagnostic Script
====================================
Debugs Azure AI Search retrieval for any query.
Shows EXACTLY what chunks are returned, their scores,
content previews, and diagnoses why the wrong chunks win.

HOW TO RUN:
    # Default query (the pension types problem):
    python debug_retrieval.py

    # Any custom query:
    python debug_retrieval.py "your query here"

    # Also check if a specific URL is in the index:
    python debug_retrieval.py --check-url "https://www.royallondon.com/..."

REQUIRES:
    - .env file with AZURE_SEARCH_ENDPOINT and AZURE_OPENAI_ENDPOINT set
    - pip install azure-search-documents azure-identity openai python-dotenv

OUTPUT:
    1. Raw hybrid search results (all candidates, ranked by score)
    2. Vector-only search results (semantic similarity only)
    3. BM25-only search results (keyword match only)
    4. URL presence check — is the right page even in the index?
    5. Diagnosis — why the wrong chunks are winning
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
EMBEDDING_DIMENSIONS     = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1024"))
MIN_RELEVANCE_SCORE      = float(os.getenv("MIN_RELEVANCE_SCORE", "0.01"))
TOP_K                    = int(os.getenv("MAX_RETRIEVED_CHUNKS", "3"))
API_VERSION              = "2024-12-01-preview"

# ── Target URL — the page that SHOULD be retrieved ────────────
EXPECTED_URL_FRAGMENT = "what-is-a-pension"

# ── Query ─────────────────────────────────────────────────────
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

def url_search(client: SearchClient, url_fragment: str, top: int = 20) -> list[dict]:
    """Check if any chunks from a specific URL exist in the index."""
    results = client.search(
        search_text="*",
        filter=f"search.ismatch('{url_fragment}', 'source_url')",
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
        }
        for r in results
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
             url_chunks: list[dict], query: str):
    header("DIAGNOSIS")

    expected_in_hybrid = any(EXPECTED_URL_FRAGMENT in r["source_url"] for r in hybrid)
    expected_in_vector = any(EXPECTED_URL_FRAGMENT in r["source_url"] for r in vector)
    expected_in_bm25   = any(EXPECTED_URL_FRAGMENT in r["source_url"] for r in bm25)
    expected_in_index  = len(url_chunks) > 0

    print(f"\n  Query: \"{query}\"")
    print(f"  Expected page fragment: '{EXPECTED_URL_FRAGMENT}'")
    print()
    print(f"  Page in index at all?        {'✅ YES (' + str(len(url_chunks)) + ' chunks)' if expected_in_index else '❌ NO — PAGE NOT INDEXED'}")
    print(f"  Appears in hybrid search?    {'✅ YES' if expected_in_hybrid else '❌ NO'}")
    print(f"  Appears in vector search?    {'✅ YES' if expected_in_vector else '❌ NO'}")
    print(f"  Appears in BM25 search?      {'✅ YES' if expected_in_bm25 else '❌ NO'}")
    print()

    # Root cause
    print("  ROOT CAUSE:")
    if not expected_in_index:
        print("  ❌ CRITICAL: The page was never indexed.")
        print("     Fix: Re-run your scraper/indexer for this URL and re-index.")

    elif not expected_in_hybrid and not expected_in_vector and not expected_in_bm25:
        print("  ❌ Page IS in index but scores too low to appear in top-10 results.")
        print("     Likely cause: Chunks are too large — the key sentence")
        print("     'three main kinds of pension' is buried in a large block.")
        print("     Fix: Re-chunk this page into smaller, focused chunks (~200 tokens).")

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

    # 4. URL presence check
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
    else:
        section(f"CHUNKS FROM '{EXPECTED_URL_FRAGMENT}' IN INDEX")
        print(f"\n  ❌ NONE FOUND — this page is NOT in your index.")
        print(f"  The URL https://www.royallondon.com/guides-tools/pension-guides/")
        print(f"  pension-basics/what-is-a-pension/ was either not scraped")
        print(f"  or excluded during indexing.")

    # Diagnosis
    diagnose(hybrid, vector, bm25, url_chunks, QUERY)


if __name__ == "__main__":
    main()