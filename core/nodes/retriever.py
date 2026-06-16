"""
Retriever Node — hybrid search, reuses embedding from cache node.

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
         Migration: AzureKeyCredential → DefaultAzureCredential
         (no API key).
         Added relevance score threshold check — if best chunk
         scores below MIN_RELEVANCE_SCORE, treat as no results
         to prevent hallucination on irrelevant context.

v1.1.0 — June 2026 | Mukesh Kund
         Skip Azure Search entirely for context-override queries

v1.2.0 — June 2026 | Mukesh Kund
         Enable Azure AI Search semantic ranker (L2 reranker)

         ROOT CAUSE:
         - Hybrid search (BM25 + vector) scores chunks by keyword
           frequency and embedding cosine similarity. Both are
           shallow signals that don’t understand what the query
           is ASKING FOR vs what a chunk is ANSWERING.
         - "What types of pensions does Royal London offer?"
           matched workplace-pensions chunks (high BM25, repeated
           keywords) instead of the what-is-a-pension chunk
           (correct answer, weaker keyword density).
         - AGM/press-release chunks won on BM25 because they
           contain "pension", "Royal London" repeatedly —
           irrelevant but keyword-rich.

         FIX:
         - query_type="semantic" activates Azure’s L2 semantic
           reranker — a transformer model that re-scores top-50
           hybrid candidates by reading actual query intent and
           chunk meaning, not just keyword counts.
         - semantic_configuration_name="rlg-semantic-config"
           tells the ranker which fields to use:
             title    → primary signal
             content  → main scoring body
             section  → keyword boost
         - TOP_K raised 3 → 5: more candidates, richer context.
         - SEMANTIC_MIN_SCORE replaces MIN_RELEVANCE_SCORE for
           semantic mode — semantic scores use a 0–4 scale,
           not the hybrid scale. Default 0.5.
         - NO re-indexing required.

         ROOT CAUSE / LIVE REPRO:
         - When _override_triggered=True (set by supervisor.py
           v1.4.0+ for contextual follow-up queries such as "Why
           didn't you answer my previous question?"),
           cache_check.py (v1.5.0+) correctly skips canonical
           rewrite and returns state with cache_hit=False, so
           the full pipeline runs including the retriever.
         - The retriever ran with the raw normalised query
           "why didnt answer previous question" — which has no
           meaningful match in the Royal London index. Azure AI
           Search returned its best-scoring chunks regardless
           (best_score=0.0311, candidates=5) — in this case,
           AGM Resolutions pages (2022/2021/2024).
         - generator.py's override_note correctly pushed the
           model to use conversation history instead of the
           retrieved context, so the response content was right
           — but extract_citations() still found [1][2][3]
           markers placed by the model against those irrelevant
           chunks, causing "2022 AGM Resolutions / 2021 AGM
           Resolutions / Our 2024 AGM" citation chips to appear
           below an ISA recap. Confusing and unprofessional for
           a customer-facing FCA-regulated assistant.

         FIX:
         - retriever_node() checks _override_triggered at the
           top, BEFORE the embedding lookup or search call.
         - If True: set state.retrieved_chunks=[], log
           retrieval_skipped (reason=context_override), and
           return state immediately. No Azure Search call is
           made, no irrelevant chunks can be retrieved, and
           extract_citations() will find no [n] markers
           (generator.py v1.9.0+ build_context() returns ""
           for empty chunks, and build_user_prompt() omits
           the context block entirely when chunks are empty
           and override is active, using conversation history
           only).
         - The no_chunks_retrieved / refusal path that normally
           fires when retrieval returns empty IS NOT triggered
           here — the override path returns early before that
           block, so the override query proceeds to generator
           with empty chunks but no refusal flag set.

═══════════════════════════════════════════════════════════════
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
SEARCH_ENDPOINT          = os.getenv("AZURE_SEARCH_ENDPOINT", "")
INDEX_NAME               = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index")
TOP_K                    = int(os.getenv("MAX_RETRIEVED_CHUNKS", "5"))   # v1.2.0: 3→5
# v1.2.0 — Semantic ranker configuration.
# Name must match the configuration created in Azure Portal
# (Indexes → rlg-faq-index → Semantic configurations).
# Set AZURE_SEARCH_SEMANTIC_CONFIG="" in .env to disable
# and fall back to pure hybrid search.
SEMANTIC_CONFIG          = os.getenv(
    "AZURE_SEARCH_SEMANTIC_CONFIG", "rlg-semantic-config"
)
# Semantic reranker scores on a 0-4 scale (not the hybrid
# 0-35 scale). 0.5 filters genuinely irrelevant results
# while keeping borderline-relevant ones.
SEMANTIC_MIN_SCORE       = float(
    os.getenv("SEMANTIC_MIN_SCORE", "0.5")
)

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

    # v1.1.0 — skip search entirely for context-override queries.
    # The generator uses conversation history (via override_note)
    # for these queries, not retrieved chunks. Running the search
    # with a query like "why didnt answer previous question"
    # returns irrelevant chunks (AGM pages, score ~0.03) which
    # produce meaningless citation chips in the UI even though
    # the model correctly ignores them. Returning empty chunks
    # here prevents that — see CHANGE LOG v1.1.0 above.
    if state.__dict__.get("_override_triggered"):
        state.retrieved_chunks = []
        state.latency_ms["retriever"] = 0
        log.info(
            "retrieval_skipped",
            reason="context_override",
            query=state.query[:50],
        )
        return state

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

        # v1.2.0 — Use semantic ranker if configured.
        # Semantic ranking is a two-stage process:
        #   Stage 1: hybrid BM25+vector retrieves top-50
        #   Stage 2: L2 transformer reranks those 50 by
        #            actual query-answer relevance
        # This fixes the "wrong chunk wins" problem where
        # keyword-dense but irrelevant chunks (AGM pages,
        # workplace pensions) outscored the correct answer.
        use_semantic = bool(SEMANTIC_CONFIG)
        search_kwargs = dict(
            search_text=state.query,
            vector_queries=[vector_query],
            select=[
                "chunk_id", "content", "source_url",
                "section", "title",
            ],
            top=TOP_K * 3,
        )
        if use_semantic:
            search_kwargs["query_type"] = "semantic"
            search_kwargs["semantic_configuration_name"] = SEMANTIC_CONFIG

        results = search_client.search(**search_kwargs)

        # Deduplicate by URL + collect all candidates
        candidates = []
        seen_urls  = set()

        for result in results:
            if len(candidates) >= TOP_K * 3:
                break
            url   = result["source_url"]
            # v1.2.0 — prefer @search.rerankerScore (semantic)
            # over @search.score (hybrid) when available.
            # rerankerScore is on a 0-4 scale and is a much
            # better signal of true relevance.
            score = (
                result.get("@search.rerankerScore")
                or result.get("@search.score", 0.0)
            )
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
        # v1.2.0: Use SEMANTIC_MIN_SCORE when semantic ranker
        # is active (scores 0-4 scale), otherwise fall back to
        # MIN_RELEVANCE_SCORE for hybrid scores (0-35 scale).
        # This prevents hallucination when no relevant content
        # exists for the query in our index.
        if candidates:
            best_score     = max(c.score for c in candidates)
            threshold      = (
                SEMANTIC_MIN_SCORE
                if use_semantic
                else MIN_RELEVANCE_SCORE
            )
            log.info(
                "retrieval_scores",
                best_score=round(best_score, 4),
                min_threshold=threshold,
                candidates=len(candidates),
                semantic=use_semantic,
            )

            if best_score < threshold:
                log.warning(
                    "low_relevance_scores",
                    best_score=round(best_score, 4),
                    threshold=threshold,
                    semantic=use_semantic,
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