"""
Cache Check Node — checks semantic cache before hitting LLM.
Hybrid approach:
  Step 1:  Lemmatization + stop words (free)
  Step 1b: Skip cache entirely if state.needs_empathy (v1.5.0) —
           sensitive disclosures always go to the full pipeline
  Step 2:  Cache check with threshold 0.87
  Step 3:  LLM canonical rewrite (gpt-4o-mini)
  Step 4:  Cache check again with canonical form

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.8.0 — July 2026 | Mukesh Kund
         BUG #25 FIX — "vs" mis-lemmatized to "v"

         PROBLEM: WordNetLemmatizer doesn't recognise "vs" and
         guesses it's a plural noun, stripping the trailing "s".
         "pensions vs isa" normalized to "pension v isa" instead
         of "pension vs isa" — confirmed live in logs. Corrupts
         the cache key and canonical-rewrite embedding on every
         comparison-style query, reducing cache-hit precision.

         FIX: "vs" passed through unchanged instead of going
         through lemmatizer.lemmatize().

         ROLLBACK: revert to v1.7.0 — call lemmatizer.lemmatize()
         unconditionally on every non-stopword token.

v1.0.0 — Initial version
         Mistral Small, API key auth, basic cache check

v1.1.0 — Migration: Mistral-small → gpt-4o-mini
         Auth: API key → DefaultAzureCredential + bearer token

v1.3.0 — June 2026 | Mukesh Kund
         Normalisation pipeline bug fixes (5 bugs)

         DOMAIN_SYNONYMS [REDESIGNED]:
         - Sorted longest phrases first to prevent partial matches
           "individual savings account" now matched before "savings account"
           "critical illness cover" matched before "critical illness"
         - Removed duplicate-injecting synonyms:
             "lost pension" → "find lost pension" REMOVED
             (was causing "find find lost pension" when query
             already contained "find")
             "missing pension" → "lost pension" (just standardise)
             "trace pension"   → "lost pension" (just standardise)
         - Removed single-word synonyms from here — moved to
           SINGLE_WORD_SYNONYMS for safe word-boundary matching

         SINGLE_WORD_SYNONYMS [NEW]:
         - New dict for single-word synonyms that need word-boundary
           regex matching to prevent substring collisions
         - "call" → "contact" now uses \\bcall\\b regex
           Previously: "recall" → "recontact" (substring corruption)
           Now: "recall" preserved correctly
         - "email", "telephone", "isas", "drawdown", "transfer",
           "complain", "unhappy", "dispute" all moved here

         apply_domain_synonyms() [REWRITTEN]:
         - Two-stage approach:
           Stage 1: multi-word phrase dict replacement (longest first)
           Stage 2: single-word re.sub with word boundaries
         - Post-processing: re.sub removes consecutive duplicate words
           Catches any remaining duplicates from any replacement:
           "find find lost pension" → "find lost pension"
           "critical illness cover cover" → "critical illness cover"

         BUG SUMMARY — all 5 fixed:
           Bug 1: duplicate word injection (find/cover)        ✅
           Bug 2: substring collision (recall→recontact)       ✅
           Bug 3: context-free single-word synonyms            ✅
           Bug 4: ordering issue (longer phrases now first)    ✅
           Bug 5: post-replacement duplicate cleanup           ✅

v1.4.0 — June 2026 | Mukesh Kund
         Skip canonical rewrite on context override queries

         cache_check_node() [MODIFIED]:
         - When supervisor sets _override_triggered=True on state
           (meaning the query is a contextual follow-up like
           "Why didn't you answer my previous question?"),
           the canonical rewrite (Step 3) is now skipped entirely
         - WHY: canonical rewrite was destroying the meaning of
           follow-up queries. Example:
             "Why didn't you answer my previous question?"
             → canonical: "What is my previous question?"
           This nonsensical rewrite caused the retriever to fetch
           irrelevant chunks (score ~0.031) and the generator to
           fire the UNKNOWN PRODUCT RULE — completely wrong.
         - For override queries: Step 1 normalisation still runs
           (for cache lookup accuracy), Step 3 canonical rewrite
           is skipped (meaning preserved for downstream nodes)
         - The embedding generated in Step 1 is still stored on
           state._query_embedding for retriever reuse

v1.7.0 — July 2026 | Mukesh Kund
         BUG #4 FIX — get_canonical_form() 30-token GPT-5 truncation

         PROBLEM: max_tokens=30 (now max_completion_tokens=30 for
         GPT-5 via _build_create_kwargs) was sized for GPT-4's
         direct token-to-output mapping. GPT-5 reasoning models
         spend part of that budget on internal reasoning tokens
         before emitting visible content — at 30 tokens the budget
         was exhausted by reasoning alone, so
         response.choices[0].message.content came back None.
         .strip() on None raised, caught by the broad except, and
         canonical_rewrite_failed logged silently on every call —
         Stage 3/4 of cache_check_node (canonical-form cache hit)
         never actually ran, artificially inflating cache misses.

         Same failure signature already confirmed and fixed in
         chunk_and_index_hqaV4.py v1.5.3 (finish_reason=length,
         content='' on GPT-5-mini at low token budgets).

         FIX:
         - Token budget 30 → 300 → 1000 (v1.7.1 follow-up: 300 still
           hit finish_reason=length on some queries — e.g. "Difference
           between ISA and Income Protection" — GPT-5-mini's reasoning
           overhead is query-dependent and unpredictable, so a fixed
           small ceiling will always be able to fail on some input.
           1000 gives enough headroom; still a cheap, short call).
         - Explicit None/empty check on response content before
           .strip(), with a distinct canonical_rewrite_empty log
           (includes finish_reason) instead of falling through to
           the generic exception path — makes future truncation
           failures visible instead of silent.

         ROLLBACK: revert to v1.6.0 — restore max_tokens 30,
         remove the empty-content guard.

v1.6.0 — July 2026 | Mukesh Kund
         GPT-5 compatibility + model-agnostic API call helper.

         WHAT CHANGED:
         - Added _build_create_kwargs(model, max_tokens, temperature)
           helper (same pattern as classifier_node.py v1.2.0).
         - canonical_rewrite() now uses **_build_create_kwargs(...)
           replacing hardcoded max_tokens=30 / temperature=0.0.
         - GPT-4 family → max_tokens + temperature.
           GPT-5 family → max_completion_tokens, no temperature.
         - Backwards compatible across model families via env var.

         ROLLBACK: revert to v1.5.0 — restore max_tokens=30,
         temperature=0.0 directly in the completions.create() call.

v1.5.0 — June 2026 | Mukesh Kund
         Skip semantic cache entirely for sensitive disclosures
         (needs_empathy queries)

         ROOT CAUSE — REPRODUCED LIVE:
         "I have been diagnosed with terminal cancer"
           → Stage 2 direct cache check: correct MISS
             (best_similarity=0.3546)
           → Stage 3 canonical_rewrite (gpt-4o-mini):
             canonical="What is critical illness cover?"
           → embedding of canonical form → cache_hit
             similarity=1.0 against an earlier cached FAQ
             answer for "What is critical illness cover?"
           → full pipeline skipped, generator never runs,
             empathy / disclaimer / human handoff never added

         Same pattern: "My wife passed away last week"
           → canonical "How do I make a claim?" → cache_hit
             similarity=1.0 → cached claims-process answer,
             no bereavement empathy or handoff number.

         The CANONICAL_SYSTEM_PROMPT few-shot examples
         ("Critical illness explained" → "What is critical
         illness cover?", "I need to claim" → "How do I make a
         claim?") generalise correctly for topic-matching, but
         strip the emotional content that generator.py's
         EMPATHY RULE depends on.

         FIX:
         - supervisor.py [MODIFIED separately, v1.5.0] now
           computes state.needs_empathy BEFORE cache_check runs
           (moved from generator.py).
         - cache_check_node() [MODIFIED]:
           Immediately after Step 1 (normalisation + embedding,
           which the retriever still needs via
           state._query_embedding), if state.needs_empathy is
           True:
             - cache_hit is set to False
             - Stage 2 (direct cache.get) is skipped
             - Stage 3 (canonical rewrite) is skipped entirely
             - function returns early
           This guarantees sensitive disclosures always reach
           generator.py, where empathy/disclaimer/handoff logic
           runs.
         - cache_write.py [MODIFIED separately, v1.1.0] mirrors
           this — sensitive exchanges are never written to cache.

═══════════════════════════════════════════════════════════════
"""

import re
import os
import time
import structlog
import nltk
from nltk.corpus import stopwords
from nltk.stem import WordNetLemmatizer
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv

from core.cache import get_cache
from core.embeddings import get_embedding
from core.schemas import AgentState

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
DEPLOYMENT_FAST       = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4o-mini")


# ── Model-agnostic API kwargs helper ─────────────────────────
def _build_create_kwargs(
    model: str,
    max_tokens: int,
    temperature: float | None = None,
) -> dict:
    """Return OpenAI completions.create() kwargs compatible with both
    GPT-4 and GPT-5 model families.

    GPT-4 family (gpt-4*): supports max_tokens and temperature.
    GPT-5 family (gpt-5*, o*, etc.): uses max_completion_tokens;
        temperature is not supported and must be omitted.
    """
    is_gpt4 = "gpt-4" in model.lower()
    kwargs: dict = {}
    if is_gpt4:
        kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
    else:
        kwargs["max_completion_tokens"] = max_tokens
    return kwargs


# ── Singleton client ──────────────────────────────────────────
_credential:    DefaultAzureCredential | None = None
_openai_client: AzureOpenAI | None            = None


def get_credential() -> DefaultAzureCredential:
    """Get or create singleton DefaultAzureCredential."""
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def get_openai_client() -> AzureOpenAI:
    """
    Get or create singleton AzureOpenAI client.
    Reused across all cache_check calls.
    """
    global _openai_client
    if _openai_client is None:
        if not AZURE_OPENAI_ENDPOINT:
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT is not set in .env"
            )
        token_provider = get_bearer_token_provider(
            get_credential(),
            "https://cognitiveservices.azure.com/.default",
        )
        _openai_client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            azure_ad_token_provider=token_provider,
            api_version="2024-12-01-preview",
        )
        log.info(
            "cache_check_client_created",
            endpoint=AZURE_OPENAI_ENDPOINT,
        )
    return _openai_client


# ── NLTK Setup ────────────────────────────────────────────────
try:
    STOP_WORDS = set(stopwords.words('english'))
except LookupError:
    nltk.download('stopwords', quiet=True)
    STOP_WORDS = set(stopwords.words('english'))

try:
    lemmatizer = WordNetLemmatizer()
    lemmatizer.lemmatize("test")
except LookupError:
    nltk.download('wordnet', quiet=True)
    nltk.download('omw-1.4', quiet=True)
    lemmatizer = WordNetLemmatizer()

# Keep important words even if in stopwords
KEEP_WORDS = {
    'not', 'no', 'nor', 'against', 'before', 'after',
    'between', 'through', 'during', 'without', 'within',
    'when', 'where', 'how', 'what', 'why', 'which', 'who',
}

FINAL_STOP_WORDS = STOP_WORDS - KEEP_WORDS

# ── Insurance Domain Synonyms ─────────────────────────────────
#
# DESIGN RULES (read before modifying):
#
# 1. LONGER PHRASES FIRST — prevents partial matches.
#    "individual savings account" must appear before "savings account".
#    "critical illness cover" must appear before "critical illness".
#
# 2. NO DUPLICATE INJECTION — replacement must not add words already
#    in the query. "lost pension" must NOT map to "find lost pension"
#    because "How do I FIND a lost pension?" becomes
#    "how do i find a find lost pension" (duplicate "find").
#    Map to the standard noun form only.
#
# 3. NO SINGLE-WORD SYNONYMS HERE — words like "call", "email",
#    "transfer" match as substrings inside other words with str.replace().
#    "call" → "contact" corrupts "recall" → "recontact".
#    Single-word synonyms live in SINGLE_WORD_SYNONYMS below and
#    use word-boundary regex matching.
#
# 4. CONTEXT-FREE SYNONYMS ARE DANGEROUS — "transfer" → "pension transfer"
#    would corrupt "bank transfer". Only map in multi-word context.
#
DOMAIN_SYNONYMS = {
    # ── Multi-word phrases — longest first ────────────────────────────
    "individual savings account": "isa",
    "life assurance":             "life insurance",
    "life cover":                 "life insurance",
    "retirement savings":         "pension",
    "retirement fund":            "pension",
    "retirement pot":             "pension",
    "pension fund":               "pension",
    "pension pot":                "pension",
    "serious illness cover":      "critical illness cover",
    "critical illness cover":     "critical illness cover",  # already standard
    "serious illness":            "critical illness cover",
    "critical illness":           "critical illness cover",
    "income cover":               "income protection",
    "flexible access":            "pension drawdown",
    "savings account":            "isa",
    "get in touch":               "contact",
    "phone number":               "contact",
    "move pension":               "pension transfer",
    # FIX: map to standard noun only — do NOT add "find" (causes duplicates)
    # "How do I find a lost pension?" was becoming "how find find lost pension"
    "missing pension":            "lost pension",
    "trace pension":              "lost pension",
    "sick pay":                   "income protection",
    # NOTE: "lost pension" already standard — no mapping needed
    # NOTE: "transfer" alone is in SINGLE_WORD_SYNONYMS (word-boundary safe)
    # NOTE: "drawdown" alone is in SINGLE_WORD_SYNONYMS (word-boundary safe)
}

# Single-word synonyms — use word-boundary regex to prevent substring
# collisions ("call" matching inside "recall", "cancel", "callback" etc)
SINGLE_WORD_SYNONYMS = {
    "isas":       "isa",
    "telephone":  "contact",
    "call":       "contact",
    "email":      "contact",
    "complain":   "complaint",
    "unhappy":    "complaint",
    "dispute":    "complaint",
    "drawdown":   "pension drawdown",
    "transfer":   "pension transfer",
}

# ── Canonical Rewrite Prompt ──────────────────────────────────
CANONICAL_SYSTEM_PROMPT = """You are a query normaliser for
a Royal London insurance and pensions AI assistant.

Rewrite the user query into the most standard canonical form
that captures the core intent in 10 words or less.

Rules:
- Keep it concise (max 10 words)
- Use standard insurance/pension terminology
- Remove filler words
- Standardise plurals to singular
- Map synonyms to standard terms:
  "life cover/assurance" → "life insurance"
  "retirement fund/savings/pot" → "pension"
  "ISAs" → "ISA"
  "sort out/manage" → "manage"
  "get in touch/call/email" → "contact Royal London"

Examples:
"Tell me about ISAs" → "What is an ISA?"
"How do ISAs work?" → "What is an ISA?"
"I want to know about ISA" → "What is an ISA?"
"How do I sort my pension out?" → "How do I manage my pension?"
"Pension explained" → "How does pension work?"
"Making a claim" → "How do I make a claim?"
"I need to claim" → "How do I make a claim?"
"Contact details" → "How do I contact Royal London?"
"Phone number for Royal London" → "How do I contact Royal London?"
"Critical illness explained" → "What is critical illness cover?"
"How do I find my old pension?" → "How do I find a lost pension?"
"Retirement options" → "What are my pension options?"
"Life cover options" → "What life insurance does Royal London offer?"

Respond with ONLY the canonical query.
No explanation, no quotes, just the query text."""


# ── Helper functions ──────────────────────────────────────────
def apply_domain_synonyms(text: str) -> str:
    """
    Replace domain synonyms with standard insurance terms.

    Two-stage approach:
    Stage 1: Multi-word phrase replacement (longest phrases first,
             as defined in DOMAIN_SYNONYMS ordering).
    Stage 2: Single-word replacement with word-boundary regex matching.
             Prevents substring collisions:
               "call" → "contact" without corrupting "recall"/"cancel"
               "transfer" → "pension transfer" without corrupting
               "bank transfer my funds" → "bank pension transfer funds"

    Post-processing: Remove consecutive duplicate words caused by any
    replacement. e.g. "find find lost pension" → "find lost pension"
    """
    text_lower = text.lower()

    # Stage 1: multi-word phrase replacement (longest first)
    for phrase, replacement in DOMAIN_SYNONYMS.items():
        if phrase in text_lower and replacement != phrase:
            text_lower = text_lower.replace(phrase, replacement)

    # Stage 2: single-word replacement with word boundaries
    for word, replacement in SINGLE_WORD_SYNONYMS.items():
        text_lower = re.sub(
            r'\b' + re.escape(word) + r'\b',
            replacement,
            text_lower,
        )

    # Post-processing: remove consecutive duplicate words
    # Catches any duplicates caused by synonym replacement
    # "find find lost pension" → "find lost pension"
    # "critical illness cover cover" → "critical illness cover"
    text_lower = re.sub(r'\b(\w+)\s+\1\b', r'\1', text_lower)

    return text_lower


def normalize_query(text: str) -> str:
    """
    Full normalisation pipeline:
    1. Lowercase + strip punctuation
    2. Domain synonym replacement
    3. Stop word removal
    4. Lemmatization

    BUG #25 FIX (July 2026, Mukesh Kund): WordNetLemmatizer doesn't
    recognise "vs" as a real word and guesses it's a plural, stripping
    the trailing "s" -> "v" (e.g. "pensions vs isa" -> "pension v isa").
    This corrupts the normalized cache key and canonical-rewrite
    embedding on every comparison-style query ("X vs Y"), reducing
    cache-hit precision. "vs" is passed through the lemmatizer
    unchanged now, same treatment as any other exempt token.
    """
    text = text.lower().strip()
    text = apply_domain_synonyms(text)
    text = re.sub(r'[^\w\s]', '', text)

    words              = text.split()
    normalized_words   = []
    for word in words:
        if word not in FINAL_STOP_WORDS:
            lemma = word if word == "vs" else lemmatizer.lemmatize(word)
            normalized_words.append(lemma)

    result = ' '.join(normalized_words)
    log.debug(
        "query_normalized",
        original=text[:50],
        normalized=result[:50],
    )
    return result


def get_canonical_form(query: str) -> str | None:
    """
    Use the fast deployment to rewrite query into
    canonical insurance domain form.
    Returns canonical query or None on failure.

    v1.x BUG FIX (#4): token budget was 30 — fine for GPT-4 family
    (no reasoning tokens), but GPT-5 reasoning models consume part
    of max_completion_tokens on internal reasoning before emitting
    any visible content. At 30 tokens the reasoning overhead alone
    exhausted the budget, so response.choices[0].message.content
    came back empty/None — canonical.strip() then threw, caught by
    the except block, and the rewrite silently failed every time
    (logged as canonical_rewrite_failed). Net effect: cache miss
    rate stayed artificially high because the canonical-form cache
    lookup (Step 4) never ran with a real rewritten query. Bumped
    to 300 — same pattern already proven in chunk_and_index_hqaV4.py
    v1.5.3 (finish_reason=length, content='' on GPT-5-mini at low
    token budgets). Cheap either way; this call uses DEPLOYMENT_FAST.
    """
    try:
        client   = get_openai_client()
        response = client.chat.completions.create(
            model=DEPLOYMENT_FAST,
            messages=[
                {
                    "role":    "system",
                    "content": CANONICAL_SYSTEM_PROMPT,
                },
                {
                    "role":    "user",
                    "content": query,
                },
            ],
            **_build_create_kwargs(DEPLOYMENT_FAST, 1000, 0.0),
        )
        canonical = response.choices[0].message.content
        if not canonical:
            log.warning(
                "canonical_rewrite_empty",
                model=DEPLOYMENT_FAST,
                finish_reason=getattr(
                    response.choices[0], "finish_reason", None
                ),
            )
            return None
        canonical = canonical.strip()

        # Clean up any quotes or extra formatting
        canonical = canonical.strip('"\'')

        # Only use if meaningfully different from original
        if canonical.lower() == query.lower():
            return None

        log.info(
            "canonical_rewrite",
            original=query[:50],
            canonical=canonical[:50],
        )
        return canonical

    except Exception as e:
        log.warning("canonical_rewrite_failed", error=str(e))
        return None


# ── Main node ─────────────────────────────────────────────────
def cache_check_node(state: AgentState) -> AgentState:
    """
    Hybrid cache check:
    Step 1: Normalize with lemmatization + synonyms
    Step 2: Check cache (threshold 0.87)
    Step 3: If miss → LLM canonical rewrite
    Step 4: Check cache again with canonical form
    """
    start = time.time()

    try:
        cache = get_cache()

        # ── Step 1: Normalize query ───────────────────────
        normalized = normalize_query(state.query)
        log.debug(
            "cache_query_normalized",
            original=state.query[:50],
            normalized=normalized[:50],
        )

        # Generate embedding on normalized query
        embedding = get_embedding(normalized, input_type="query")

        # Always store embedding for retriever reuse
        state.__dict__["_query_embedding"] = embedding

        # ── Step 1b: Sensitive-disclosure check (v1.5.0) ──
        # state.needs_empathy is set in supervisor.py BEFORE
        # cache_check runs. If the customer's query contains a
        # genuine-distress trigger (terminal illness, bereavement,
        # redundancy, financial hardship etc — see
        # EMPATHY_TRIGGERS in supervisor.py), skip the semantic
        # cache entirely:
        #   - Stage 2 (direct cache.get) is skipped
        #   - Stage 3 (canonical rewrite) is skipped — this is
        #     the step that was collapsing sensitive disclosures
        #     into generic FAQ phrasing and producing false
        #     1.0-similarity cache hits (see v1.5.0 changelog)
        # The normalised embedding above is still stored on
        # state._query_embedding so the retriever can reuse it.
        if state.needs_empathy:
            latency = (time.time() - start) * 1000
            state.latency_ms["cache_check"] = latency
            state.cache_hit = False
            log.info(
                "cache_check_skipped",
                reason="needs_empathy",
                query=state.query[:50],
            )
            return state

        # v1.6.0: skip cache for recommendation queries.
        # supervisor.py sets _skip_cache=True when the query
        # matches RECOMMENDATION_TRIGGERS. Without this, a cached
        # factual response (e.g. pension types) could be served
        # instead of the RECOMMENDATION_RESPONSE refusal — an FCA
        # Consumer Duty compliance failure. All cache stages
        # (direct lookup + canonical rewrite) are bypassed.
        if state.__dict__.get("_skip_cache", False):
            latency = (time.time() - start) * 1000
            state.latency_ms["cache_check"] = latency
            state.cache_hit = False
            log.info(
                "cache_check_skipped",
                reason="recommendation_query_fca_bypass",
                query=state.query[:50],
            )
            return state

        # ── Step 2: First cache check ─────────────────────
        cached = cache.get(embedding)

        if cached:
            latency                = (time.time() - start) * 1000
            state.latency_ms["cache_check"] = latency
            state.cache_hit        = True
            state.cached_response  = cached.answer
            state.citations        = cached.citations
            state.final_response   = cached.answer
            log.info(
                "cache_hit",
                step=1,
                latency_ms=round(latency),
                query=state.query[:50],
            )
            return state

        # ── Step 3: LLM canonical rewrite ─────────────────
        # SKIP for context override queries.
        # When supervisor sets _override_triggered=True it means
        # the query is a contextual follow-up (frustration,
        # disagreement, clarification, implicit follow-up etc).
        # Canonical rewriting destroys the meaning of these queries:
        #   "Why didn't you answer my previous question?"
        #   → "What is my previous question?"  (nonsensical)
        # This caused retrieval to fail and UNKNOWN PRODUCT RULE
        # to fire — completely wrong for a follow-up query.
        # The original query meaning must be preserved intact
        # so the generator can use conversation_history correctly.
        is_override = state.__dict__.get("_override_triggered", False)

        if is_override:
            log.info(
                "canonical_rewrite_skipped",
                reason="context_override_query",
                query=state.query[:50],
            )
            latency          = (time.time() - start) * 1000
            state.latency_ms["cache_check"] = latency
            state.cache_hit  = False
            return state

        log.info(
            "cache_miss_step1",
            query=state.query[:50],
            trying_canonical=True,
        )

        canonical = get_canonical_form(state.query)

        if canonical:
            # Normalize canonical form too
            canonical_normalized = normalize_query(canonical)
            canonical_embedding  = get_embedding(
                canonical_normalized, input_type="query"
            )

            # ── Step 4: Second cache check ────────────────
            cached = cache.get(canonical_embedding)

            if cached:
                # Update embedding to canonical for better
                # future cache hits
                state.__dict__["_query_embedding"] = (
                    canonical_embedding
                )
                latency                = (time.time() - start) * 1000
                state.latency_ms["cache_check"] = latency
                state.cache_hit        = True
                state.cached_response  = cached.answer
                state.citations        = cached.citations
                state.final_response   = cached.answer
                log.info(
                    "cache_hit",
                    step=2,
                    canonical=canonical[:50],
                    latency_ms=round(latency),
                    query=state.query[:50],
                )
                return state

            log.info(
                "cache_miss_step2",
                canonical=canonical[:50],
            )

        # ── Both steps missed → full pipeline ────────────
        latency          = (time.time() - start) * 1000
        state.latency_ms["cache_check"] = latency
        state.cache_hit  = False
        log.info(
            "cache_miss_final",
            latency_ms=round(latency),
            had_canonical=canonical is not None,
        )

    except Exception as e:
        log.error("cache_check_error", error=str(e))
        state.cache_hit = False

    return state