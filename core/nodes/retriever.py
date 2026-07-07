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

v1.3.0 — July 2026 | Mukesh Kund
         Sprint 1 enhancements: rerank_chunks(), title_questions
         select field, scoring profile, dotenv fix.

         rerank_chunks() [NEW]:
         - Post-retrieval fuzzy title boost using rapidfuzz.
         - Only fires when state.query_type == "BROAD" (set by
           classifier_node). SPECIFIC queries skip it entirely
           — 5ms overhead only paid when there's a real signal.
         - Logic: for each chunk, compute fuzz.partial_ratio
           between the query and the chunk's title. If ratio
           > 0.7 (70% match), add up to 2.0 to the reranker
           score. Resort by adjusted score.
         - WHY this is needed alongside the index-level
           title_questions boost:
           Azure AI Search's scoring profile weights affect BM25
           scoring. The semantic reranker and vector scores are
           independent. Post-retrieval re-ranking at the Python
           level applies a uniform title signal across all three
           retrieval legs, ensuring that an overview page with
           title "What is a Pension" ranks above a keyword-rich
           workplace pension page for "What types of pensions
           does Royal London offer?" even after the Azure ranker
           has scored them.

         title_questions field added to select:
         - Retrieved alongside content, source_url, section,
           title in the Azure AI Search call.
         - Stored on RetrievedChunk (currently unused at query
           time — the field is in the index for retrieval
           ranking, not for display). Kept in select for
           observability / debugging.

         scoring_profile added to search call:
         - Passes "rl-retrieval-profile" (created in
           chunk_and_index_hqaV3.py at index build time) only
           when state.query_type == "BROAD". Specific queries
           use Azure's default scoring — the title_questions
           boost would distort specific-query results.

         dotenv fix:
         - was: load_dotenv() — no args, no override
         - now: load_dotenv(find_dotenv(usecwd=False), override=True)

═══════════════════════════════════════════════════════════════
"""

import os
import time
import structlog
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from dotenv import load_dotenv, find_dotenv

from core.embeddings import get_embedding
from core.schemas import AgentState, RetrievedChunk

try:
    from rapidfuzz import fuzz as _fuzz
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False
    # rerank_chunks() will fall back to returning chunks unchanged
    # if rapidfuzz is not installed. Install with: pip install rapidfuzz

_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path, override=True)
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


def rerank_chunks(
    chunks: list[RetrievedChunk],
    query: str,
    query_type: str,
) -> list[RetrievedChunk]:
    """
    v1.3.0 — Post-retrieval fuzzy title boost for BROAD queries.

    Azure AI Search's scoring profile and semantic ranker improve
    BM25 and cross-field scoring, but the Python-level re-ranking
    applies a uniform title signal across all three retrieval legs
    (BM25 + vector + semantic), ensuring overview pages with
    strongly matching titles rank above keyword-dense product pages
    even after the Azure ranker has scored them.

    Only fires when query_type == "BROAD" — SPECIFIC queries are
    not affected. 5ms overhead (no API call, pure Python).

    Logic:
    - For each chunk, compute rapidfuzz.partial_ratio between the
      normalised query and the chunk title.
    - If ratio > 0.70 (70% match threshold), add up to 2.0 boost
      to the reranker score (proportional to match strength).
    - Resort by adjusted score descending.
    - If rapidfuzz is not installed: return chunks unchanged
      (warning logged — install rapidfuzz to enable this feature).

    The 2.0 boost is calibrated to move a strongly-title-matching
    chunk from position 3-4 to position 1-2, without completely
    overriding the Azure ranker's relevance signal. Adjust
    TITLE_BOOST_WEIGHT if needed after A/B comparison.
    """
    TITLE_FUZZY_THRESHOLD = 0.70  # 70% match to trigger boost
    TITLE_BOOST_WEIGHT    = 2.0   # max boost added to reranker score

    if not _RAPIDFUZZ_AVAILABLE:
        log.warning(
            "rerank_chunks_skipped",
            reason="rapidfuzz not installed",
            note="pip install rapidfuzz to enable title re-ranking",
        )
        return chunks

    query_lower = query.lower().strip()
    scored      = []

    for chunk in chunks:
        title      = (chunk.title or "").lower().strip()
        base_score = chunk.score

        if title:
            ratio = _fuzz.partial_ratio(query_lower, title) / 100
            boost = ratio * TITLE_BOOST_WEIGHT if ratio > TITLE_FUZZY_THRESHOLD else 0
        else:
            boost = 0

        scored.append((base_score + boost, chunk))

    scored.sort(key=lambda x: -x[0])
    reranked = [c for _, c in scored]

    if reranked and reranked[0].chunk_id != chunks[0].chunk_id:
        log.info(
            "rerank_applied",
            query_type=query_type,
            original_top=chunks[0].title[:40] if chunks[0].title else chunks[0].source_url,
            reranked_top=reranked[0].title[:40] if reranked[0].title else reranked[0].source_url,
        )

    return reranked


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
                "title_questions",  # v1.3.0 — for observability
            ],
            top=TOP_K * 3,
        )
        if use_semantic:
            search_kwargs["query_type"] = "semantic"
            search_kwargs["semantic_configuration_name"] = SEMANTIC_CONFIG

        # v1.3.0: apply scoring profile only for BROAD queries.
        # "rl-retrieval-profile" boosts title_questions (weight 5.0)
        # which were generated specifically to match broad/overview
        # entry-point queries. For SPECIFIC queries, the default
        # Azure scoring is better — title_questions boost would
        # distort specific-product results.
        query_type = state.query_type or "SPECIFIC"
        if query_type == "BROAD" and SEMANTIC_CONFIG:
            search_kwargs["scoring_profile"] = "rl-retrieval-profile"
            log.info(
                "scoring_profile_applied",
                profile="rl-retrieval-profile",
                query_type=query_type,
            )

        # v1.4.0: Enable semantic answer extraction.
        # Azure extracts the best answer passage independently of
        # chunk ranking — this is what Search Explorer shows as
        # @search.answers with a confidence score (e.g. 0.97).
        # We were ignoring these and relying on chunk ranking,
        # which can be BM25-dominated by keyword-dense chunks.
        # Using extracted answers as priority context means the
        # generator sees the best passage FIRST — before any
        # keyword-dense but lower-relevance chunks.
        if use_semantic:
            search_kwargs["query_answer"]           = "extractive"
            search_kwargs["query_answer_count"]     = 3
            search_kwargs["query_answer_threshold"] = 0.7

        # v1.3.1: graceful fallback for v3-only features.
        # title_questions field and rl-retrieval-profile only
        # exist on v3+ indexes. On v2, remove both and retry.
        def _run_search(kwargs: dict) -> list:
            return list(search_client.search(**kwargs))

        try:
            results = _run_search(search_kwargs)
        except Exception as e:
            err      = str(e)
            v3_error = (
                "title_questions"         in err
                or "UnknownScoringProfile" in err
                or "scoringProfile"        in err
            )
            if v3_error:
                search_kwargs["select"] = BASE_SELECT
                search_kwargs.pop("scoring_profile", None)
                log.warning(
                    "v3_features_not_available",
                    index=INDEX_NAME,
                    error=err[:120],
                    note=(
                        "title_questions and rl-retrieval-profile "
                        "only exist on v3+ indexes. Removed both — "
                        "retrying with v2-compatible select and default scoring."
                    ),
                )
                results = _run_search(search_kwargs)
            else:
                raise

        # ── Build chunk_id lookup for answer matching ─────────
        # Option 1: match @search.answers back to their source
        # chunk by chunk_id to get correct source_url for citations.
        chunk_by_key: dict[str, RetrievedChunk] = {}
        for r in results:
            cid = r.get("chunk_id", "")
            if cid and cid not in chunk_by_key:
                s = (
                    r.get("@search.rerankerScore")
                    or r.get("@search.score", 0.0)
                )
                chunk_by_key[cid] = RetrievedChunk(
                    chunk_id=cid,
                    content=r["content"],
                    source_url=r["source_url"],
                    section=r.get("section", ""),
                    title=r.get("title", ""),
                    score=s,
                )

        # ── Extract @search.answers ───────────────────────────
        semantic_answer_chunks: list[RetrievedChunk] = []
        if use_semantic and results:
            # Answers are on the first result's attributes in
            # the Azure SDK when using list() conversion.
            raw_answers = []
            try:
                first = results[0] if results else {}
                raw_answers = first.get("@search.answers", []) or []
            except Exception:
                raw_answers = []

            for answer in raw_answers:
                try:
                    key   = getattr(answer, "key", None)
                    score = float(getattr(answer, "score", 0.0))
                    text  = getattr(answer, "text", "")

                    if not key or not text or score < 0.7:
                        continue

                    if key in chunk_by_key:
                        base = chunk_by_key[key]
                        # Boost score so answer chunks sort before
                        # regular chunks. Score * 10 ensures a
                        # 0.97 answer outranks any 0-4 reranker score.
                        semantic_answer_chunks.append(RetrievedChunk(
                            chunk_id=key,
                            content=text,
                            source_url=base.source_url,
                            section=base.section,
                            title=base.title,
                            score=score * 10,
                        ))
                        log.info(
                            "semantic_answer_matched",
                            score=round(score, 3),
                            title=base.title[:40],
                            url=base.source_url[:60],
                        )
                except Exception as ae:
                    log.warning(
                        "semantic_answer_parse_error",
                        error=str(ae)[:60],
                    )

        # ── Build candidate chunks from results ───────────────
        # Deduplicate by URL. semantic_answer_chunks are prepended
        # so they appear as positions 1-N before BM25/reranker chunks.
        # Seed seen_urls with answer URLs first to prevent duplicates.
        candidates = []
        seen_urls  = set()
        for ac in semantic_answer_chunks:
            seen_urls.add(ac.source_url)

        for result in results:
            if len(candidates) >= TOP_K * 3:
                break
            url   = result["source_url"]
            # v1.2.0 — prefer @search.rerankerScore (semantic)
            # over @search.score (hybrid) when available.
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

        # Take top K from filtered candidates.
        # Prepend semantic answer chunks (score*10, so always rank
        # first) then fill remaining slots with regular chunks.
        # Cap total at TOP_K so generator doesn't get too much context.
        regular_chunks = candidates[:TOP_K]
        if chunks and query_type == "BROAD":
            regular_chunks = rerank_chunks(
                regular_chunks, state.query, query_type
            )

        # Merge: answers first, then regular (deduplicated by URL
        # already — seen_urls was seeded with answer URLs above).
        # Limit answer chunks to 2 max to leave room for context.
        answer_slots  = semantic_answer_chunks[:2]
        context_slots = regular_chunks[: max(1, TOP_K - len(answer_slots))]
        chunks        = answer_slots + context_slots

        if answer_slots:
            log.info(
                "semantic_answers_prepended",
                answer_count=len(answer_slots),
                context_count=len(context_slots),
                total=len(chunks),
            )

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