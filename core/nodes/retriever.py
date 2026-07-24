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
           chunk_and_index_hqaV4.py at index build time) only
           when state.query_type == "BROAD". Specific queries
           use Azure's default scoring — the title_questions
           boost would distort specific-query results.

         dotenv fix:
         - was: load_dotenv() — no args, no override
         - now: load_dotenv(find_dotenv(usecwd=False), override=True)

v1.5.0 — July 2026 | Mukesh Kund
    FIX — parent_url-aware citation URL.
    Dropdown state pages are indexed with source_url=#state=... fragment.
    retriever.py was passing this fragment directly into RetrievedChunk.source_url
    → citation chips showed dead links to customers.
    - V4_SELECT now includes "parent_url".
    - Every RetrievedChunk construction uses:
        source_url = r.get("parent_url") or r["source_url"]
      so dropdown chunks cite the real navigable page; standard chunks
      are unaffected (parent_url is "" → falls back to source_url).
    - BASE_SELECT fallback does not include parent_url (old indexes
      won't have the field) — safe, falls back to source_url as before.
v1.4.0 — July 2026 | Mukesh Kund
         V3_SELECT renamed to V4_SELECT. All stale v3 references
         updated to v4. Comments corrected to reflect that the
         retriever now queries rlg-faq-index-v4 exclusively.

         RENAMED:
           V3_SELECT → V4_SELECT (variable name in retriever_node)
           "v3+" → "v4+" in all inline comments
           "v2 and v3" → "v2, v3, or v4" in fallback comment
           scoring_profile comment: "v3+" → "v4+"
           fallback error comment: "v3+ indexes" → "v4 indexes"
           error note string: "v3+ indexes" → "v4 indexes"
           changelog v1.3.0 scoring_profile reference updated

         NO logic changes — retrieval behaviour identical.

v1.6.0 — July 2026 | Mukesh Kund
         Selective semantic reranker gate — should_use_semantic().

         PROBLEM:
         - use_semantic = bool(SEMANTIC_CONFIG) fired on every query.
         - Free tier (1,000 queries/month) exhausted immediately.
         - Simple factual queries ("What is a Stocks and Shares ISA?")
           were classified BROAD (due to "what is a" signal) and
           passed through the semantic reranker unnecessarily.

         FIX:
         - Added should_use_semantic(state) function with 7-rule
           priority chain. Semantic ON for: safety-critical queries,
           classifier failures, conversational follow-ups, complex/
           comparative signals, low confidence (<0.85). Semantic OFF
           for short simple SPECIFIC queries (≤8 words). Default ON.
         - Replaces use_semantic = bool(SEMANTIC_CONFIG) (always on).
         - Expected ~60-70% reduction in semantic reranker calls.

         RELATED CHANGES:
         - schemas.py: Added confidence: float = 1.0 to AgentState.
         - classifier_node.py: state.confidence set on both normal
           path and exception fallback (0.0 → semantic defaults ON).

         ROLLBACK:
         - Revert use_semantic line to: use_semantic = bool(SEMANTIC_CONFIG)

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


def should_use_semantic(state) -> bool:
    """
    v1.6.0 — Selective semantic reranker gate.

    Azure semantic reranker (L2) is expensive and quota-limited.
    Only activate it when hybrid search genuinely needs help:
    complex, ambiguous, or multi-concept queries.

    Priority order (first match wins):
    1. Safety-critical queries → always ON (hard invariant).
    2. Classifier failed (confidence=0.0) → ON (safe default).
    3. Conversation follow-up (history present) → ON (needs context).
    4. Complex/comparative signals in query text → ON.
    5. Low classifier confidence (<0.85) → ON (ambiguous intent).
    6. Short simple queries (≤8 words, SPECIFIC) → OFF.
    7. Default → ON (safe fallback for anything uncategorised).
    """
    if not SEMANTIC_CONFIG:
        return False

    # 1. Safety-critical — always use semantic for best retrieval
    if state.needs_disclaimer or getattr(state, "_bereavement", False):
        return True

    # 2. Classifier failed — unknown complexity, default to ON
    if getattr(state, "confidence", 1.0) == 0.0:
        return True

    # 3. Conversational follow-up — context-dependent, needs semantic
    if state.conversation_history:
        return True

    # 4. Complex/comparative/conditional signals
    COMPLEX_SIGNALS = [
        "difference between", "compare", "versus", " vs ",
        "what happens if", "what would", "if i ", "when i ",
        "and also", "as well as", "both", "all types",
        "better", "should i", "which is best", "which is better",
        "how does", "why does", "why would",
    ]
    query_lower = state.query.lower()
    if any(sig in query_lower for sig in COMPLEX_SIGNALS):
        return True

    # 5. Low confidence — ambiguous intent, semantic adds value
    if getattr(state, "confidence", 1.0) < 0.85:
        return True

    # 6. Short simple queries — hybrid alone is sufficient
    word_count = len(query_lower.split())
    if word_count <= 8 and (state.query_type or "SPECIFIC") == "SPECIFIC":
        return False

    # 7. Default — ON
    return True


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

        use_semantic = should_use_semantic(state)   # v1.6.0 — selective gate
        query_type   = state.query_type or "SPECIFIC"

        # ── Select fields ─────────────────────────────────────
        # BASE_SELECT: core fields present on all index versions.
        # V4_SELECT: adds title_questions (v4 indexes only).
        # The except block falls back to BASE_SELECT if v4
        # fields cause an InvalidRequestParameter error
        # (e.g. when querying an older index during migration).
        BASE_SELECT = [
            "chunk_id", "content", "source_url", "section", "title",
        ]
        V4_SELECT = BASE_SELECT + ["title_questions", "parent_url"]

        search_kwargs = dict(
            search_text=state.query,
            vector_queries=[vector_query],
            select=V4_SELECT,
            top=TOP_K * 3,
        )
        if use_semantic:
            search_kwargs["query_type"] = "semantic"
            search_kwargs["semantic_configuration_name"] = SEMANTIC_CONFIG

        # v1.3.0: scoring profile for BROAD queries only.
        # rl-retrieval-profile boosts title_questions on v4+.
        if query_type == "BROAD" and SEMANTIC_CONFIG:
            search_kwargs["scoring_profile"] = "rl-retrieval-profile"
            log.info(
                "scoring_profile_applied",
                profile="rl-retrieval-profile",
                query_type=query_type,
            )

        # v1.4.0: semantic answer extraction.
        # Azure extracts the best answer passage independently of
        # chunk ranking — same as @search.answers in Search Explorer.
        # Only fires with semantic ranker enabled.
        if use_semantic:
            search_kwargs["query_answer"]           = "extractive"
            search_kwargs["query_answer_count"]     = 3
            search_kwargs["query_answer_threshold"] = 0.7

        # ── Execute search with v4 feature fallback ───────────
        # title_questions field and rl-retrieval-profile only
        # exist on v4 indexes. On older indexes, remove both
        # and retry once. Both removed in same catch to avoid
        # a second failure.
        # ── Execute search with v4 feature fallback ───────────
        # get_answers() MUST be called on the SearchItemPaged object
        # BEFORE converting to list() — the Azure SDK iterator is
        # exhausted by list() and answers are no longer accessible
        # afterward. This was the root cause of semantic answer
        # extraction never working: list() was called first.
        def _execute_search(kwargs: dict) -> tuple[list, list]:
            """
            Execute search and extract answers in one call.
            Returns (results_list, raw_answers).
            Must get answers before list() exhausts the iterator.
            """
            paged   = search_client.search(**kwargs)
            answers = []
            if kwargs.get("query_answer"):
                try:
                    answers = paged.get_answers() or []
                except Exception:
                    answers = []
            return list(paged), answers

        try:
            results, raw_answers = _execute_search(search_kwargs)
        except Exception as e:
            err      = str(e)
            v3_error = (
                "title_questions"          in err
                or "UnknownScoringProfile" in err
                or "scoringProfile"        in err
            )
            if v3_error:
                search_kwargs["select"] = BASE_SELECT
                search_kwargs.pop("scoring_profile", None)
                log.warning(
                    "v4_features_not_available",
                    index=INDEX_NAME,
                    error=err[:120],
                    note=(
                        "title_questions and rl-retrieval-profile "
                        "only exist on v4 indexes. Removed both — "
                        "retrying with base select and default scoring."
                    ),
                )
                results, raw_answers = _execute_search(search_kwargs)
            else:
                raise

        # ── Build chunk_id lookup for semantic answer matching ─
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
                    # v1.5.0: prefer parent_url for dropdown state chunks
                    # so citation chips never show dead #state= fragments.
                    source_url=r.get("parent_url") or r["source_url"],
                    section=r.get("section", ""),
                    title=r.get("title", ""),
                    score=s,
                )

        # ── Process @search.answers ───────────────────────────
        # raw_answers already retrieved BEFORE list() above.
        semantic_answer_chunks: list[RetrievedChunk] = []
        if use_semantic and raw_answers:

            for answer in raw_answers:
                try:
                    key   = getattr(answer, "key", None)
                    score = float(getattr(answer, "score", 0.0))
                    text  = getattr(answer, "text", "")
                    if not key or not text or score < 0.7:
                        continue
                    if key in chunk_by_key:
                        base = chunk_by_key[key]
                        semantic_answer_chunks.append(RetrievedChunk(
                            chunk_id=key,
                            content=text,
                            source_url=base.source_url,
                            section=base.section,
                            title=base.title,
                            score=score * 10,  # ensure answers rank first
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

        # ── Deduplicate and build candidate chunks ────────────
        # Seed seen_urls with answer chunk URLs first to prevent
        # duplicates when regular chunks overlap with answers.
        candidates = []
        seen_urls  = set()
        for ac in semantic_answer_chunks:
            seen_urls.add(ac.source_url)

        for result in results:
            if len(candidates) >= TOP_K * 3:
                break
            url   = result["source_url"]
            score = (
                result.get("@search.rerankerScore")
                or result.get("@search.score", 0.0)
            )
            # v1.5.0: use parent_url if set (dropdown state chunks)
            citation_url = result.get("parent_url") or url
            if citation_url not in seen_urls:
                candidates.append(RetrievedChunk(
                    chunk_id=result["chunk_id"],
                    content=result["content"],
                    source_url=citation_url,
                    section=result.get("section", ""),
                    title=result.get("title", ""),
                    score=score,
                ))
                seen_urls.add(citation_url)

        # ── Relevance score filter ────────────────────────────
        if candidates:
            best_score = max(c.score for c in candidates)
            threshold  = (
                SEMANTIC_MIN_SCORE if use_semantic
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

        # ── Merge semantic answers + regular chunks ───────────
        # Answers (score*10) always rank before regular chunks.
        # Cap answers at 2 to leave room for supporting context.
        regular_chunks = candidates[:TOP_K]
        if regular_chunks and query_type == "BROAD":
            regular_chunks = rerank_chunks(
                regular_chunks, state.query, query_type
            )

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
            from core.nodes.prompt_builder_node import CONTACT_CITATION
            from core.schemas import Citation
            state.refusal_triggered = True
            state.final_response    = get_refusal(
                RefusalReason.NO_RESULTS
            )
            state.citations = [Citation(
                index=1,
                url=CONTACT_CITATION["url"],
                title=CONTACT_CITATION["title"],
                section=CONTACT_CITATION["section"],
            )]
            log.warning("no_chunks_retrieved")

    except Exception as e:
        log.error("retriever_error", error=str(e))
        from core.refusal import get_refusal, RefusalReason
        from core.nodes.prompt_builder_node import CONTACT_CITATION
        from core.schemas import Citation
        state.refusal_triggered = True
        state.final_response    = get_refusal(
            RefusalReason.GENERAL
        )
        state.citations = [Citation(
            index=1,
            url=CONTACT_CITATION["url"],
            title=CONTACT_CITATION["title"],
            section=CONTACT_CITATION["section"],
        )]

    return state