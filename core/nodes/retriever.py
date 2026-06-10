"""
Retriever Node — hybrid search, reuses embedding from cache node.

Migration: AzureKeyCredential → DefaultAzureCredential (no API key)
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
SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "")
INDEX_NAME      = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index")
TOP_K           = int(os.getenv("MAX_RETRIEVED_CHUNKS", "3"))

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

        # Deduplicate by URL
        chunks    = []
        seen_urls = set()

        for result in results:
            if len(chunks) >= TOP_K:
                break
            url = result["source_url"]
            if url not in seen_urls:
                chunks.append(RetrievedChunk(
                    chunk_id=result["chunk_id"],
                    content=result["content"],
                    source_url=url,
                    section=result.get("section", ""),
                    title=result.get("title", ""),
                    score=result.get("@search.score", 0.0),
                ))
                seen_urls.add(url)

        state.retrieved_chunks = chunks
        latency                = (time.time() - start) * 1000
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