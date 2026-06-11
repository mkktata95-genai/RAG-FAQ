"""
Retriever Node — hybrid search, reuses embedding from cache node.

Migration: AzureKeyCredential → DefaultAzureCredential (no API key)
Fix:        Added relevance score threshold check — if best chunk
            scores below MIN_RELEVANCE_SCORE, treat as no results
            to prevent hallucination on irrelevant context
"""

import os
import time
import structlog
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from dotenv import load_dotenv

from core.embeddings import get_embedding
from core.schemas import AgentState, RetrievedChunk

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
SEARCH_ENDPOINT     = os.getenv("AZURE_SEARCH_ENDPOINT", "")
INDEX_NAME          = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index")
TOP_K               = int(os.getenv("MAX_RETRIEVED_CHUNKS", "3"))

# Minimum relevance score for retrieved chunks.
# Hybrid search scores vary — chunks below this threshold
# are considered irrelevant to the query and discarded.
# This prevents GPT hallucinating answers when context
# doesn't actually match the query (e.g. credit card query
# returning pension chunks with low similarity).
# Tune this value if legitimate queries get blocked:
#   Too high (e.g. 0.03) → blocks valid queries
#   Too low  (e.g. 0.005) → allows irrelevant context through
MIN_RELEVANCE_SCORE = float(
    os.getenv("MIN_RELEVANCE_SCORE", "0.01")
)

# ── Singleton client ──────────────────────────────────────────
_credential:     DefaultAzureCredential | None = None
_search_client:  SearchClient | None           = None


def get_credential() -> DefaultAzureCredential:
    """Get or create singleton DefaultAzureCredential."""
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def get_search_client() -> SearchClient:
    """Get or create singleton SearchClient."""
    global _search_client
    if _search_client is None:
        if not SEARCH_ENDPOINT:
            raise ValueError(
                "AZURE_SEARCH_ENDPOINT is not set in .env"
            )
        _search_client = SearchClient(
            endpoint=SEARCH_ENDPOINT,
            index_name=INDEX_NAME,
            credential=get_credential(),
        )
        log.info(
            "search_client_created",
            endpoint=SEARCH_ENDPOINT,
            index=INDEX_NAME,
        )
    return _search_client


# ── Main node ─────────────────────────────────────────────────
def retriever_node(state: AgentState) -> AgentState:
    """Hybrid search using cached embedding from cache_check node."""
    start = time.time()

    try:
        # Reuse embedding from cache_check node if available
        embedding = state.__dict__.get("_query_embedding")
        if not embedding:
            log.warning("embedding_not_cached_regenerating")
            embedding = get_embedding(
                state.query, input_type="query"
            )

        search_client = get_search_client()

        vector_query = VectorizedQuery(
            vector=embedding,
            k_nearest_neighbors=TOP_K * 3,
            fields="embedding",
        )

        results = search_client.search(
            search_text=state.query,
            vector_queries=[vector_query],
            select=[
                "chunk_id", "content", "source_url",
                "section", "title",
            ],
            top=TOP_K * 3,
        )

        # Deduplicate by URL + collect all candidates
        candidates = []
        seen_urls  = set()

        for result in results:
            if len(candidates) >= TOP_K * 3:
                break
            url   = result["source_url"]
            score = result.get("@search.score", 0.0)
            if url not in seen_urls:
                candidates.append(RetrievedChunk(
                    chunk_id=result["chunk_id"],
                    content=result["content"],
                    source_url=url,
                    section=result.get("section", ""),
                    title=result.get("title", ""),
                    score=score,
                ))
                seen_urls.add(url)

        # ── Relevance score filter ────────────────────────────
        # Check if best chunk meets minimum relevance threshold.
        # If even the top result scores below MIN_RELEVANCE_SCORE,
        # the query has no relevant content in our index — treat
        # as no results to prevent hallucination.
        if candidates:
            best_score = max(c.score for c in candidates)
            log.info(
                "retrieval_scores",
                best_score=round(best_score, 4),
                min_threshold=MIN_RELEVANCE_SCORE,
                candidates=len(candidates),
            )

            if best_score < MIN_RELEVANCE_SCORE:
                log.warning(
                    "low_relevance_scores",
                    best_score=round(best_score, 4),
                    threshold=MIN_RELEVANCE_SCORE,
                    query=state.query[:50],
                )
                candidates = []

        # Take top K from filtered candidates
        chunks = candidates[:TOP_K]

        state.retrieved_chunks        = chunks
        latency                       = (time.time() - start) * 1000
        state.latency_ms["retriever"] = latency

        log.info(
            "retrieval_complete",
            chunks_found=len(chunks),
            latency_ms=round(latency),
        )

        if not chunks:
            from core.refusal import get_refusal, RefusalReason
            state.refusal_triggered = True
            state.final_response    = get_refusal(
                RefusalReason.NO_RESULTS
            )
            log.warning("no_chunks_retrieved")

    except Exception as e:
        log.error("retriever_error", error=str(e))
        from core.refusal import get_refusal, RefusalReason
        state.refusal_triggered = True
        state.final_response    = get_refusal(
            RefusalReason.GENERAL
        )

    return state