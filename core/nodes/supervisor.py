"""
Supervisor Node — entry point, validates state, routes graph.

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
         Mistral Small, API key auth, basic intent classification

v1.1.0 — Migration: Mistral-small → gpt-4o-mini
         - Auth: API key → DefaultAzureCredential + bearer token
         - P4: Query length validation (max 100 words)
         - P3: Request ID injection (UUID per request)
         - Added IRRELEVANT intent for off-topic queries
           (weather, sport, food, politics, entertainment etc)

v1.2.0 — June 2026 | Mukesh Kund
         Context-aware routing override

         is_contextual_follow_up() [NEW FUNCTION]:
         - Discovered bug: "Why you didnt answer my previous
           question?" was classified as CHITCHAT → returned
           generic greeting at 0ms, ignoring conversation history
         - Fix: after intent classification, if intent is non-
           INSURANCE, check is_contextual_follow_up() before
           early-exiting. If follow-up detected AND conversation
           history exists → override intent to INSURANCE → route
           through full pipeline where generator has history access
         - Override only fires when conversation_history non-empty
         - Covers 9 misrouting categories (A-I):
           A. Frustration & complaint references
           B. Continuation with ambiguous opener
           C. Clarification of misunderstood answer
           D. Thanks opener + follow-up question
           E. Emotional response needing continuation
           F. Negative response / disagreement
           G. Implicit short query (meaning only from history)
           H. Frustrated farewell
           I. Negative correction

v1.3.0 — June 2026 | Mukesh Kund
         Account lookup detection before cache_check

         is_account_lookup() [NEW FUNCTION]:
         - Detects queries where customer tries to get Aria to
           look up personal account/policy/pension details
         - MUST run before cache_check — cache_check's canonical
           rewrite was transforming "My NI number is AB123456C,
           can you look up my pension?" into "How do I find a
           lost pension?" — bypassing the Account Access Rule
           in the generator system prompt entirely
         - Triggers on 25 patterns:
             "look up my", "check my account", "my ni number is",
             "my policy number is", "how much is in my" etc.
         - On match: immediate refusal with 0345 600 0371
           Pipeline exits at supervisor. No API calls made.
           PII never sent to cache, embeddings, or LLM.
         - Generator Account Access Rule remains as second layer

         ACCOUNT_LOOKUP_REFUSAL [NEW CONSTANT]:
         - Single source of truth for the refusal message
         - Consistent with Account Access Rule in generator.py

v1.4.0 — June 2026 | Mukesh Kund
         Store override flags on state for downstream nodes

         supervisor_node() [MODIFIED]:
         - When context override fires, now stores two values
           on state.__dict__ so downstream nodes can read them:
             state.__dict__['_override_triggered'] = True
             state.__dict__['_override_reason']    = reason
           (Same pattern as _query_embedding — avoids breaking
           Pydantic AgentState schema)
         - cache_check reads _override_triggered to skip
           canonical rewrite on follow-up queries
         - generator reads _override_triggered to inject
           history summary note instead of UNKNOWN PRODUCT RULE
         - Fixes: "Why didn't you answer my previous question?"
           was returning UNKNOWN PRODUCT RULE because canonical
           rewrite transformed it to "What is my previous
           question?" — destroying the contextual meaning

v1.5.0 — June 2026 | Mukesh Kund
         Empathy & financial disclaimer detection moved here
         from generator.py — fixes cache bypass of empathy/
         handoff for sensitive disclosures

         ROOT CAUSE:
         - EMPATHY_TRIGGERS / needs_empathy() and
           FINANCIAL_DECISION_TRIGGERS / needs_disclaimer()
           previously lived in generator.py and were only
           evaluated inside generator_node() — the LAST node
           before cache_write.
         - cache_check runs BEFORE generator. On a cache hit
           (Stage 2 direct match, or Stage 3 canonical-rewrite
           match), the graph routes straight to END —
           generator_node() never runs, so needs_empathy() was
           never evaluated for cached responses.
         - Reproduced live:
             "I have been diagnosed with terminal cancer"
               → Stage 2 direct cache check: correct MISS
                 (best_similarity=0.3546)
               → Stage 3 canonical_rewrite (gpt-4o-mini):
                 canonical="What is critical illness cover?"
               → cache_hit similarity=1.0 against an earlier
                 cached FAQ answer — empathy/disclaimer/handoff
                 never added.
             "My wife passed away last week"
               → canonical "How do I make a claim?"
               → cache_hit similarity=1.0 → cached claims-
                 process answer, no bereavement empathy or
                 handoff number.

         FIX — empathy/disclaimer detection moved to supervisor:
         - EMPATHY_TRIGGERS, FINANCIAL_DECISION_TRIGGERS,
           needs_empathy(), needs_disclaimer() [MOVED HERE from
           generator.py, logic unchanged]
         - supervisor_node() [MODIFIED]:
           After expand_query(), now sets:
             state.needs_empathy    = needs_empathy(state.query)
             state.needs_disclaimer = needs_disclaimer(state.query)
             state.is_sensitive     = state.needs_empathy
           These are real AgentState/GraphState fields already
           (see schemas.py) — they now carry a meaningful value
           through cache_check, input_safety, retriever and
           generator instead of only being set inside generator.
         - supervisor_start log now includes needs_empathy and
           needs_disclaimer for observability.
         - cache_check.py [MODIFIED separately, v1.5.0] now
           checks state.needs_empathy and skips the semantic
           cache entirely (no Stage 2 lookup, no Stage 3
           canonical rewrite) when True — guaranteeing the full
           pipeline runs so generator's empathy/disclaimer/
           handoff logic always fires for sensitive disclosures.
         - cache_write.py [MODIFIED separately, v1.1.0] mirrors
           this — sensitive exchanges are never written to
           cache, so they cannot later be served cold to another
           customer.
         - generator.py [MODIFIED separately, v1.6.0] no longer
           defines or recomputes these — it reads
           state.needs_empathy / state.needs_disclaimer as set
           by supervisor.

v1.6.0 — June 2026 | Mukesh Kund
         Bereavement-specific handoff number detection

         BACKGROUND:
         - The technical design doc states the bereavement
           support number (0370 850 2179) is "injected
           separately by the user prompt builder when
           bereavement-specific terms are detected" — but this
           was never implemented. generator.py's
           build_user_prompt only ever had access to
           state.needs_empathy (a generic flag covering cancer,
           redundancy, divorce, mental health etc.) with no way
           to distinguish a genuine bereavement from those other
           categories, so only the general number 0345 600 0371
           was ever used.
         - Confirmed live (Seq 2): a bereavement query got
           correct empathy + content + citations, but the
           handoff number was 0345 600 0371 instead of the
           bereavement-specific 0370 850 2179.

         FIX:
         - BEREAVEMENT_TRIGGERS [NEW CONSTANT] / is_bereavement()
           [NEW FUNCTION]: a STRICT SUBSET of EMPATHY_TRIGGERS
           covering only genuine bereavement language ("died",
           "passed away", "bereavement", "losing someone",
           "loss of a loved"). Deliberately excludes the broader
           "death" trigger from EMPATHY_TRIGGERS, which also
           matches product-feature questions like "what is the
           death benefit on my policy" — those should keep their
           existing empathy framing but must NOT receive the
           bereavement support line.
         - This is a STRICT SUBSET BY DESIGN: _bereavement=True
           must always imply needs_empathy=True, so cache_check
           always skips the semantic cache (v1.5.0) and
           generator_node always runs to apply the bereavement
           note (v1.7.0). Any future addition to
           BEREAVEMENT_TRIGGERS must also be present in
           EMPATHY_TRIGGERS, or the bereavement note may never be
           reached.
         - supervisor_node() [MODIFIED]: alongside
           needs_empathy/needs_disclaimer in Step 8b, now sets
             state.__dict__["_bereavement"] = is_bereavement(state.query)
           using the same state.__dict__ pattern as
           _override_triggered/_override_reason — and, per
           graph.py v1.1.0, this now actually propagates to
           generator.py.
         - supervisor_start log now includes bereavement.
         - generator.py [MODIFIED separately, v1.7.0]:
           build_user_prompt() injects a note instructing the
           model to use 0370 850 2179 instead of 0345 600 0371
           for the human handoff in this response only, when
           state.__dict__["_bereavement"] is True.

v1.7.0 — July 2026 | Mukesh Kund
         Sprint 1 refactor — slim supervisor + dotenv fix

         FUNCTIONS MOVED TO classifier_node.py (core/nodes/):
         - OBVIOUS_GREETINGS, OBVIOUS_FAREWELLS, OBVIOUS_THANKS
         - GREETING_RESPONSES
         - INTENT_SYSTEM_PROMPT
         - classify_intent()
         - HISTORY_REFERENCE_WORDS, DISAGREEMENT_WORDS,
           CLARIFICATION_WORDS, FRUSTRATED_FAREWELL_WORDS,
           IMPLICIT_QUERY_WORDS, CONTINUATION_WORDS
         - is_contextual_follow_up()

         WHAT STAYS IN SUPERVISOR:
         - quick_intent_check() — rule-based pattern match for
           obvious one-word greetings/farewells/thanks. Kept here
           for cost efficiency: intercepts BEFORE classifier_node
           runs, saving an unnecessary gpt-4o-mini call for "hi",
           "thanks", "bye" etc. Sets state.final_response directly
           and routes to END via route_after_supervisor.
         - All validation: sanitise_input(), validate_query_length()
         - is_account_lookup() + ACCOUNT_LOOKUP_REFUSAL
         - expand_query()
         - needs_empathy(), needs_disclaimer(), is_bereavement()
         - EMPATHY_TRIGGERS, FINANCIAL_DECISION_TRIGGERS,
           BEREAVEMENT_TRIGGERS, BEREAVEMENT_SUPPORT_NUMBER
         - All route_after_* routing functions
         - response_formatter_node()
         - request_id generation

         PIPELINE CHANGE:
         - route_after_supervisor now routes to "classifier"
           (not "cache_check" as before). classifier_node is
           the new second node in the pipeline.
         - route_after_classifier (in graph.py) routes to
           "cache_check" — same downstream behaviour.

         DOTENV FIX:

v1.8.0 — July 2026 | Mukesh Kund
         Expanded OBVIOUS_GREETINGS / FAREWELLS / THANKS sets

         PROBLEM: "How are you?" and other common chitchat
         phrases were falling through quick_intent_check() (no
         match) → reaching classifier_node → burning a
         gpt-4o-mini LLM call just to return CHITCHAT → 30-second
         latency for a phrase that needs zero intelligence.

         FIX — OBVIOUS_GREETINGS expanded from 8 to 26 phrases:
         - Added chitchat openers: "how are you", "how are you
           doing", "how are you today", "how r u", "how r you",
           "hru", "how is it going", "how's it going",
           "hows it going", "how are things", "how are things
           going", "what's up", "whats up", "wassup", "wazzup",
           "how do you do", "how have you been", "you alright",
           "you ok", "you okay", "are you there",
           "is anyone there", "anyone there"
         - Added single-word variations: "heya", "yo", "sup",
           "greetings", "salutations", "alright"
         - OBVIOUS_FAREWELLS expanded: added "ciao", "cheerio",
           "toodles", "ttyl", "talk later", "see you later",
           "night", "nite", "all done", "that's all",
           "no more questions", "nothing else", "i'm done" etc.
         - OBVIOUS_THANKS expanded: added "ta", "thanks so much",
           "much appreciated", "thanks for your help",
           "that's helpful", "that helped", "brilliant" etc.

         DESIGN NOTE: Hardcoding is intentional for this set.
         These are sub-5-word, unambiguous social phrases that
         carry no insurance intent under any interpretation.
         The LLM classifier is reserved for ambiguous cases
         where intent is genuinely unclear. Any phrase NOT in
         these sets still falls through to the classifier — the
         rule is exact-match only (after lowercase + strip punct).

         Sets kept in sync between supervisor.py (quick path)
         and classifier_node.py (secondary check) — both updated.

v1.10.0 — July 2026 | Mukesh Kund
         Tier 1 batch 2 — routing fixes (Bugs #1, #2, #24).

         BUG #24 — Safety checks moved earlier in the pipeline:
         - ROOT CAUSE: input_safety_node ran 4th (Supervisor →
           Classifier → Cache Check → Input Safety → ...), so
           every harmful/weapons/jailbreak query paid for a
           classifier LLM call AND a canonical-rewrite LLM call
           — both rejected by Azure OpenAI's own built-in content
           filter (HTTP 400) — before our own Layers 1-5 in
           safety.py ever got a chance to block it for free.
           Confirmed live: "How to make bomb" took ~26s and two
           wasted API calls (intent_classification_failed,
           canonical_rewrite_failed) before weapons_content_
           detected (v1.1.0, safety.py) finally fired in 7ms.
         - input_safety_node depends only on state.query — nothing
           classifier_node or cache_check_node produce — so moving
           it is safe. Confirmed via direct read of both files.
         - FIX: route_after_supervisor now targets "input_safety"
           (was "classifier"). route_after_input_safety now
           targets "classifier" (was "retriever"). route_after_
           classifier still leads to cache_check on success.
           route_after_cache now targets "retriever" (was
           "input_safety"). New pipeline order: Supervisor →
           Input Safety → Classifier → Cache Check → Retriever →
           Prompt Builder → Generator → Output Safety → Formatter
           → Cache Write.
         - Companion change: graph.py v1.3.0 — edge target map
           updates to match.

         BUG #1/#2 — classifier_node non-INSURANCE short-circuit
         never actually took effect:
         - ROOT CAUSE: classifier_node.py's Step 3 set
           state.final_response for non-INSURANCE intents but
           never set state.refusal_triggered. route_after_
           classifier ignored final_response entirely and always
           returned "cache_check" unconditionally — so a query
           classifier had already correctly identified as
           IRRELEVANT still ran the full pipeline (embedding,
           canonical rewrite, retrieval, generation) anyway.
           Confirmed live: "Who is the PM of UK?" was classified
           IRRELEVANT, then proceeded through 5 more nodes and
           ~19s before generating an answer using RLG pension
           documents to (badly) address an unrelated political
           question.
         - input_safety_node's existing pattern (set both flags
           together) was already correct — this brings
           classifier_node.py in line with it. See classifier_
           node.py v1.3.0 for that half of the fix.
         - FIX (this file): route_after_classifier now checks
           `state.refusal_triggered or state.final_response`
           before falling through to cache_check — this is what
           actually makes classifier_node's flag take effect.
         - DEFENCE IN DEPTH: route_after_cache and route_after_
           retriever also now check `state.final_response` in
           addition to `refusal_triggered`, so a future node that
           sets final_response without also setting
           refusal_triggered (the exact bug just fixed) can never
           again cause a silent, expensive fallthrough anywhere
           else in the pipeline.

         ROLLBACK:
         - Revert route_after_supervisor target to "classifier".
         - Revert route_after_input_safety target to "retriever".
         - Revert route_after_classifier to unconditional
           `return "cache_check"`.
         - Revert route_after_cache target to "input_safety".
         - Remove `or state.final_response` from route_after_cache
           and route_after_retriever.

v1.9.0 — July 2026 | Mukesh Kund
         RECOMMENDATION_TRIGGERS expanded

         PROBLEM (sprint 1 testing, 13 July — Image 2):
         - "Will my pension be enough to retire on at 65?" went
           through the full pipeline and got a substantive answer
           instead of hitting RECOMMENDATION_RESPONSE. This is a
           personal financial sufficiency assessment — under FCA
           Consumer Duty rules Aria must not answer it, the same as
           "which pension is better for me?".
         - Root cause: RECOMMENDATION_TRIGGERS list was phrase-exact
           and only covered explicit recommendation requests
           ("recommend", "which is better for me" etc). It had no
           coverage for sufficiency/adequacy, projection, or
           comparison questions.

         FIX — RECOMMENDATION_TRIGGERS expanded from 18 → 33 phrases:
         - Sufficiency/adequacy: "enough to retire", "will my pension
           be enough", "is my pension enough", "am i saving enough",
           "will i have enough", "do i have enough",
           "how much do i need to retire", "can i afford to retire",
           "when can i retire", "am i on track",
           "on track for retirement", "will i be okay",
           "will i be able to retire"
         - Comparison/superiority: "is it worth", "better to",
           "is it better", "which is better", "better option",
           "is royal london better", "better than",
           "worth switching", "worth transferring"
         - Personal projection: "how much will i get",
           "how much will i have", "how much will my pension be",
           "what will my pension be worth", "will i get enough",
           "will my savings be enough"
         - NOTE: "is it worth" and "better to" were already in
           FINANCIAL_DECISION_TRIGGERS (sets needs_disclaimer) but
           NOT in RECOMMENDATION_TRIGGERS (sets _skip_cache +
           RECOMMENDATION_RESPONSE). Now added to both — a query
           triggering needs_disclaimer should also trigger the
           full recommendation refusal, not just add a disclaimer.

═══════════════════════════════════════════════════════════════
"""

import re
import os
import structlog
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv, find_dotenv
from core.schemas import AgentState, MAX_QUERY_WORDS
from core.middleware import generate_request_id, mask_pii_for_logging

_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path, override=True)
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")

# ── Quick Intent Patterns ─────────────────────────────────────
# Rule-based only — no LLM. Kept in supervisor for cost
# efficiency: intercepts obvious greetings BEFORE classifier_node
# runs, saving an unnecessary gpt-4o-mini call.
# Classifier_node handles all LLM-based classification.
OBVIOUS_GREETINGS = {
    # One-word
    "hi", "hello", "hey", "hiya", "howdy", "heya", "yo",
    "sup", "greetings", "salutations",
    # Time-of-day
    "good morning", "good afternoon", "good evening",
    "good night", "good day",
    # Chitchat openers (no LLM needed — standard deflect)
    "how are you", "how are you doing", "how are you today",
    "how r u", "how r you", "hru",
    "how is it going", "how's it going", "hows it going",
    "how are things", "how are things going",
    "what's up", "whats up", "wassup", "wazzup",
    "how do you do", "how have you been",
    "you alright", "you ok", "you okay",
    "alright", "alright then",
    "are you there", "is anyone there", "is there anyone",
    "anyone there",
}
OBVIOUS_FAREWELLS = {
    "bye", "goodbye", "good bye", "see you", "see ya",
    "take care", "farewell", "ciao", "cheerio", "toodles",
    "ttyl", "talk later", "talk to you later",
    "see you later", "see you soon", "later", "later on",
    "have a good day", "have a great day", "have a nice day",
    "good night", "night", "nite",
    "all done", "that's all", "thats all", "that will be all",
    "no more questions", "nothing else", "nothing more",
    "i'm done", "im done", "done for now",
}
OBVIOUS_THANKS = {
    "thanks", "thank you", "cheers", "ta",
    "thank you so much", "thanks a lot", "thanks so much",
    "many thanks", "appreciated", "much appreciated",
    "thank you very much", "thanks very much",
    "thanks for your help", "thank you for your help",
    "that's helpful", "thats helpful", "very helpful",
    "that helped", "that was helpful",
    "great help", "brilliant", "perfect", "lovely",
}

# ── Empathy & Financial Disclaimer Detection ─────────────────
#
# MOVED HERE in v1.5.0 — was generator.py EMPATHY_TRIGGERS /
# FINANCIAL_DECISION_TRIGGERS / needs_empathy() / needs_disclaimer().
#
# WHY THIS MUST RUN IN SUPERVISOR (not generator):
# cache_check runs BEFORE generator. A cache hit (Stage 2 direct
# match or Stage 3 canonical rewrite) routes straight to END, so
# generator_node() — and therefore needs_empathy() — never ran for
# cached responses. By computing these flags here in supervisor
# (which always runs first), cache_check and cache_write can read
# state.needs_empathy / state.needs_disclaimer and skip the
# semantic cache for sensitive disclosures, guaranteeing
# generator's empathy/handoff/disclaimer logic always fires.
#
# IMPORTANT: Only include triggers that represent GENUINE distress
# or sensitive personal circumstances — NOT administrative tasks.
#
# REMOVED in generator.py v1.3.0 (were causing false positives,
# kept removed here too):
#   'claim', 'make a claim' → standard FAQ, not sensitive
#   'lost pension'          → administrative task, not distress
#   'illness'               → too broad, matches 'critical illness cover'
#   'condition'             → too broad, matches product descriptions
#   'injury'                → too broad
#   'accident'              → too broad
#   'hospital'              → too broad
#
EMPATHY_TRIGGERS = [
    # Terminal / life-threatening illness
    "cancer", "terminal", "critically ill", "serious illness",
    "life-limiting", "life limiting",
    # Bereavement
    "died", "death", "bereavement", "passed away",
    "losing someone", "loss of a loved",
    # Disability
    "disability", "disabled",
    # Employment / financial hardship
    "redundan", "unemployed", "losing my job", "lost my job",
    "financial difficulty", "financial hardship",
    "can't afford", "cannot afford", "struggling to pay",
    # Relationship breakdown
    "divorce", "separation", "separating",
    # Mental health
    "mental health", "anxiety", "depression",
    # Diagnosis
    "diagnosed",
]

# ── Financial Decision Detection ──────────────────────────────
# MOVED HERE in v1.5.0 — was generator.py FINANCIAL_DECISION_TRIGGERS.
FINANCIAL_DECISION_TRIGGERS = [
    "should i", "should i invest", "which pension",
    "best option", "recommend", "advice",
    "what should", "is it worth", "better to",
    "choose", "decide", "switch", "transfer",
    "how much should", "when should",
    "tax", "return", "growth", "performance",
]


def needs_empathy(query: str) -> bool:
    """
    MOVED HERE in v1.5.0 — was generator.py needs_empathy().
    Logic unchanged. Now called from supervisor_node() before
    cache_check runs, so the result is available on
    state.needs_empathy for cache_check/cache_write to read.
    """
    query_lower = query.lower()
    return any(t in query_lower for t in EMPATHY_TRIGGERS)


def needs_disclaimer(query: str) -> bool:
    """
    MOVED HERE in v1.5.0 — was generator.py needs_disclaimer().
    Logic unchanged.
    """
    query_lower = query.lower()
    return any(
        t in query_lower for t in FINANCIAL_DECISION_TRIGGERS
    )


# ── Bereavement Detection ─────────────────────────────────────
# NEW in v1.6.0 — see CHANGE LOG for full rationale.
#
# STRICT SUBSET OF EMPATHY_TRIGGERS BY DESIGN. Every term below
# must also appear in EMPATHY_TRIGGERS, so that
# _bereavement=True always implies needs_empathy=True. This
# guarantees:
#   - cache_check.py (v1.5.0) skips the semantic cache, so
#   - generator_node() always runs and can apply the
#     bereavement-specific handoff number (generator.py v1.7.0).
#
# Deliberately narrower than EMPATHY_TRIGGERS's "death"/"died"
# pair — "death" alone also matches product-feature questions
# such as "what is the death benefit on my policy", which should
# keep empathy framing but must NOT receive the bereavement
# support line (0370 850 2179) instead of the general number.
BEREAVEMENT_TRIGGERS = [
    "died", "passed away", "bereavement",
    "losing someone", "loss of a loved",
]


def is_bereavement(query: str) -> bool:
    """
    Detects genuine bereavement language.
    Used in build_user_prompt() to direct customer to the
    bereavement page URL (v1.8.0: changed from phone number
    to URL — bereavement numbers vary by policy type).
    """
    query_lower = query.lower()
    return any(t in query_lower for t in BEREAVEMENT_TRIGGERS)


# ── Recommendation Detection ──────────────────────────────────
# Queries asking for personal financial recommendations MUST
# bypass the semantic cache. Without this, the cache may serve
# a cached factual response (e.g. pension types) instead of the
# required RECOMMENDATION_RESPONSE refusal — an FCA Consumer
# Duty compliance failure.
# _skip_cache=True is read by cache_check_node v1.6.0 which
# skips all cache stages (direct lookup + canonical rewrite).
RECOMMENDATION_TRIGGERS = [
    # Explicit recommendation requests
    "recommend", "recommendation", "recommendations",
    "suggest", "suggestion",
    "what should i", "which should i",
    "which is better for me", "what would you",
    "which one should", "what do you think i should",
    "advise me", "best for me", "best option for me",
    "what do you recommend", "which do you recommend",
    "should i choose", "help me decide", "help me choose",
    "which is best", "what is best for me",
    # Retirement sufficiency / adequacy questions
    # (Image 2: "Will my pension be enough to retire on at 65?"
    #  was not caught — these are personal financial assessments,
    #  not factual FAQ queries, and must be refused under FCA rules)
    "enough to retire", "will my pension be enough",
    "is my pension enough", "am i saving enough",
    "will i have enough", "do i have enough",
    "how much do i need to retire", "how much do i need for retirement",
    "can i afford to retire", "when can i retire",
    "am i on track", "on track for retirement",
    "will i be okay", "will i be able to retire",
    # Comparison / superiority questions (competitor or product)
    "is it worth", "better to", "is it better",
    "which is better", "better option",
    "is royal london better", "better than",
    "worth switching", "worth transferring",
    # Personal projection questions
    "how much will i get", "how much will i have",
    "how much will my pension be", "what will my pension be worth",
    "will i get enough", "will my savings be enough",
]


def is_recommendation_query(query: str) -> bool:
    """
    v1.8.0 — Detects queries asking for personal financial
    recommendations. Sets state.__dict__['_skip_cache'] = True
    so cache_check_node bypasses the semantic cache entirely.

    FCA Consumer Duty: Aria must never make personal financial
    recommendations. Cache contamination where a semantically
    similar factual query hits the cache and returns pension
    information instead of the RECOMMENDATION_RESPONSE refusal
    is an FCA compliance breach.
    """
    q = query.lower()
    return any(trigger in q for trigger in RECOMMENDATION_TRIGGERS)


# ── Account Lookup Detection ──────────────────────────────────
#
# Detects queries where a customer is trying to get Aria to
# look up their personal account, policy, or pension details.
#
# WHY THIS IS IN SUPERVISOR (not just generator):
# The cache_check node runs a canonical rewrite using gpt-4o-mini
# which can transform "My NI number is AB123456C, can you look
# up my pension?" into "How do I find a lost pension?" — a
# completely different query that bypasses the Account Access Rule
# in the generator system prompt.
#
# By detecting account lookups HERE in the supervisor — before
# cache_check ever runs — we:
#   1. Exit immediately with the correct refusal
#   2. Never call the embedding API on PII-containing queries
#   3. Never store PII-containing queries in the cache
#   4. Never pass PII to the LLM
#
# The generator Account Access Rule remains as a second layer
# of defence for any edge cases that slip through.
#
ACCOUNT_LOOKUP_TRIGGERS = [
    # Direct lookup requests
    "look up my",
    "look up my pension",
    "look up my policy",
    "check my account",
    "check my policy",
    "check my pension",
    "access my account",
    "access my policy",
    "find my policy",
    "find my pension",
    "retrieve my",
    "pull up my",
    "search my",
    # PII-first patterns (customer provides data then asks for lookup)
    "my ni number is",
    "my national insurance number is",
    "my national insurance is",
    "my policy number is",
    "my date of birth is",
    "my dob is",
    "my account number is",
    # What is my... patterns
    "what is my policy",
    "what is my pension",
    "what is my balance",
    "what is my surrender value",
    "how much is in my",
    "what's in my",
    "whats in my",
]

ACCOUNT_LOOKUP_REFUSAL = (
    "I'm not able to access account information directly. "
    "For your account details please call us on "
    "0345 600 0371 Monday to Friday 8am to 6pm."
)


def is_account_lookup(query: str) -> bool:
    """
    Detect if query is attempting to get Aria to look up
    personal account/policy/pension details.

    Must run BEFORE cache_check to prevent canonical rewriting
    from transforming PII-containing queries into generic ones
    that would bypass the Account Access Rule in the generator.
    """
    q = query.lower().strip()
    return any(trigger in q for trigger in ACCOUNT_LOOKUP_TRIGGERS)

# ── Greeting responses ────────────────────────────────────────
# Kept in supervisor for the quick_intent_check() short-circuit
# path (obvious one-word greetings handled before classifier runs).
# The full GREETING_RESPONSES dict lives in classifier_node.py
# for LLM-classified intents — this is just the subset supervisor
# needs for its own path.
QUICK_GREETING_RESPONSES = {
    "GREETING": (
        "Hello! I'm Aria, RLG's AI Assistant. "
        "I'm here to help you with questions about "
        "Royal London insurance, pensions, ISAs and "
        "other financial products. How can I help you today?"
    ),
    "THANKS": (
        "You're welcome! I'm glad I could help. "
        "Is there anything else you'd like to know about "
        "Royal London's products or services?"
    ),
    "FAREWELL": (
        "Goodbye! Thank you for using RLG's AI Assistant. "
        "Feel free to return if you have any questions "
        "about your Royal London products. Take care!"
    ),
}


def quick_intent_check(query: str) -> str | None:
    """
    Rule-based fast-path for obvious one-word greetings.
    No LLM call — runs in supervisor BEFORE classifier_node.

    Intercepts:
      "hi", "hello", "thanks", "bye" etc. → supervisor handles
      directly, sets state.final_response, routes to END.

    Anything not in these exact sets falls through to
    classifier_node for LLM-based classification.

    NOTE: Multi-word phrases like "good morning", "thank you so
    much" etc. are in the sets — these are still cheap to check
    and save a gpt-4o-mini call. The key is EXACT match only.
    "Why didn't you answer my previous question?" will NOT match.
    """
    q = query.lower().strip()
    q = re.sub(r'[^\w\s]', '', q).strip()

    if q in OBVIOUS_GREETINGS:
        return "GREETING"
    if q in OBVIOUS_FAREWELLS:
        return "FAREWELL"
    if q in OBVIOUS_THANKS:
        return "THANKS"

    return None


def sanitize_input(text: str) -> str:
    """
    P8 — Input sanitization.
    Strips HTML tags, control characters, and normalises whitespace.

    NOTE: This was accidentally embedded as dead code inside
    quick_intent_check() after its return None statement in the
    Sprint 1 refactor — making it unreachable and causing every
    request to crash with NameError when supervisor_node() called
    sanitize_input(). Fixed in v1.7.0 as a standalone function.
    """
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = " ".join(text.split())
    return text.strip()


def validate_query_length(query: str) -> tuple[bool, str]:
    """Validate query is appropriate length."""
    words = query.split()

    if not query.strip():
        return False, (
            "I am unable to process this request. "
            "Please provide a question."
        )

    if len(words) > MAX_QUERY_WORDS:
        return False, (
            f"Your query is too long. Please limit your "
            f"question to {MAX_QUERY_WORDS} words or fewer."
        )

    return True, ""


def expand_query(
    query: str,
    history: list[dict],
) -> str:
    """Expand ambiguous queries using conversation history."""
    ambiguous_words = [
        "that", "it", "this", "those", "them",
        "there", "for that", "about that",
    ]
    query_lower = query.lower()

    has_ambiguity = any(
        word in query_lower.split()
        for word in ambiguous_words
    )

    if not has_ambiguity or not history:
        return query

    last_user = next(
        (m["content"] for m in reversed(history)
         if m["role"] == "user"), ""
    )

    if last_user and len(query.split()) < 8:
        expanded = f"{query} (in context of: {last_user[:60]})"
        return expanded

    return query


# ── Main node ─────────────────────────────────────────────────
def supervisor_node(state: AgentState) -> AgentState:
    """
    Entry point for the graph.
    Validates, sanitizes, classifies intent and prepares state.

    Routing logic (in order):
    1. Validate input (length, empty)
    2. Quick pattern check (GREETING/FAREWELL/THANKS exact match)
    3. LLM intent classification (gpt-4o-mini)
    4. Context-aware override check — if query looks non-INSURANCE
       BUT references conversation history → override to INSURANCE
    5. Handle genuine non-insurance intents (early exit)
    6. Continue pipeline for INSURANCE queries
    """
    # ── Step 1: Assign request ID ─────────────────────────
    if not state.request_id:
        state.request_id = generate_request_id()

    # ── Step 2: Sanitize input ────────────────────────────
    state.query = sanitize_input(state.query)

    # ── Step 3: Validate length ───────────────────────────
    valid, message = validate_query_length(state.query)
    if not valid:
        state.refusal_triggered = True
        state.final_response    = message
        log.warning(
            "query_validation_failed",
            request_id=state.request_id,
            reason=message[:50],
        )
        return state

    # ── Step 3b: Account lookup detection ────────────────
    # MUST run before intent classification and cache_check.
    # cache_check's canonical rewrite can transform a PII-
    # containing account lookup into a generic query (e.g.
    # "My NI is AB123456C, look up my pension" becomes
    # "How do I find a lost pension?") which bypasses the
    # Account Access Rule in the generator entirely.
    # Detecting here ensures:
    #   - Immediate refusal before any API call
    #   - PII never sent to cache, embeddings, or LLM
    #   - Generator Account Access Rule is second layer only
    if is_account_lookup(state.query):
        state.final_response = ACCOUNT_LOOKUP_REFUSAL
        log.info(
            "account_lookup_blocked",
            request_id=state.request_id,
            query=mask_pii_for_logging(state.query)[:60],
        )
        return state

    # ── Step 4: Quick pattern check (no API call) ─────────
    # Only catches exact short phrases ("hi", "thanks", "bye").
    # Longer / ambiguous queries fall through to classifier_node.
    intent = quick_intent_check(state.query)

    if intent is not None:
        # Obvious greeting/farewell/thanks — handle directly
        # without an LLM call. Route to END via
        # route_after_supervisor (state.final_response is set).
        state.final_response = QUICK_GREETING_RESPONSES.get(
            intent,
            QUICK_GREETING_RESPONSES["GREETING"],
        )
        log.info(
            "quick_intent_handled",
            intent=intent,
            query=state.query[:50],
            request_id=state.request_id,
        )
        return state

    # ── Step 5: Insurance query path ──────────────────────
    # All other queries proceed to classifier_node (via
    # route_after_supervisor → "classifier"). classifier_node
    # sets state.intent and state.query_type.
    # Supervisor does NOT call classify_intent() or
    # is_contextual_follow_up() — those are classifier's job.
    if len(state.conversation_history) > 10:
        state.conversation_history = (
            state.conversation_history[-10:]
        )

    # Expand ambiguous multi-turn queries using history
    state.query = expand_query(
        state.query,
        state.conversation_history,
    )

    # ── Step 5b: Empathy & financial disclaimer detection ─
    # Computed HERE (not in generator.py) — see v1.5.0 changelog.
    # cache_check (next node after classifier) reads
    # state.needs_empathy to decide whether to skip the semantic
    # cache entirely for sensitive disclosures.
    state.needs_empathy    = needs_empathy(state.query)
    state.needs_disclaimer = needs_disclaimer(state.query)
    state.is_sensitive     = state.needs_empathy

    # v1.6.0: Bereavement-specific handoff number detection.
    _bereavement = is_bereavement(state.query)
    state.__dict__["_bereavement"] = _bereavement

    # v1.8.0: recommendation queries must bypass the semantic
    # cache entirely — see is_recommendation_query() above.
    _skip_cache = is_recommendation_query(state.query)
    state.__dict__["_skip_cache"] = _skip_cache

    masked_query = mask_pii_for_logging(state.query)
    log.info(
        "supervisor_complete",
        request_id=state.request_id,
        query=masked_query[:50],
        history_turns=len(state.conversation_history),
        needs_empathy=state.needs_empathy,
        needs_disclaimer=state.needs_disclaimer,
        bereavement=_bereavement,
        next_node="classifier",
    )

    return state


# ── Router functions ──────────────────────────────────────────
def route_after_supervisor(state: AgentState) -> str:
    """
    Route after supervisor:
    - Greeting/farewell/thanks handled → END
    - Account lookup blocked → END
    - All other queries → input_safety

    v1.9.0 (Bug #24): target changed from "classifier" to
    "input_safety" — safety checks now run before any LLM call.
    Previously, a harmful/weapons/jailbreak query burned a
    classifier LLM call AND a canonical-rewrite LLM call — both
    rejected by Azure's own content filter — before our own
    Layers 1-5 in safety.py ever got a chance to block it for
    free. Confirmed live: "How to make bomb" cost ~26s and two
    wasted API calls before weapons_content_detected fired.
    input_safety_node only depends on state.query — nothing
    classifier or cache_check produce — so moving it earlier is
    safe and has no other side effects.
    """
    if state.refusal_triggered or state.final_response:
        return "end"
    return "input_safety"


def route_after_input_safety(state: AgentState) -> str:
    """
    Route after input_safety (v1.9.0 — moved earlier in the
    pipeline; now runs immediately after supervisor, before
    classifier and cache_check. See route_after_supervisor for
    rationale — Bug #24).
    - Unsafe (harmful/weapons/jailbreak/irrelevant) → END
    - Safe → classifier
    """
    if state.refusal_triggered or state.final_response:
        return "end"
    return "classifier"


def route_after_classifier(state: AgentState) -> str:
    """
    Route after classifier:
    - Non-INSURANCE intent (IRRELEVANT/GREETING/CHITCHAT etc,
      confidence >= 0.85) → END directly.

    v1.9.0 FIX (Bug #1/#2): classifier_node now sets
    refusal_triggered=True alongside final_response for
    non-INSURANCE intents — this check is what makes that
    actually take effect. Previously this function always
    returned "cache_check" unconditionally, regardless of what
    classifier set on state, so a query classifier had already
    correctly identified as IRRELEVANT still ran the full
    expensive pipeline anyway (cache embedding generation,
    canonical rewrite LLM call, retrieval, generation) — wasting
    latency and cost, and in one confirmed case producing a
    visibly wrong answer when retrieved insurance content was
    used to (badly) answer an unrelated political question.
    The docstring here previously claimed "cache_check detects
    this and short-circuits" — that logic never existed anywhere
    in cache_check_node; this fix implements what was always
    intended instead of just describing it.
    - INSURANCE intent → cache_check
    """
    if state.refusal_triggered or state.final_response:
        return "end"
    return "cache_check"


def route_after_cache(state: AgentState) -> str:
    """
    Route after cache_check.

    v1.9.0: target changed from "input_safety" to "retriever" —
    input_safety now runs earlier in the pipeline (see
    route_after_supervisor, Bug #24), so cache_check's next stop
    on a miss is retrieval directly.
    """
    if state.cache_hit or state.final_response:
        return "end"
    return "retriever"


def route_after_retriever(state: AgentState) -> str:
    """
    Route after retriever:
    - No results / refusal → END
    - Retrieved chunks → prompt_builder (v1.7.0+)
      prompt_builder assembles state.built_prompt before
      generator_node runs. Was "generator" in 8-node pipeline.

    v1.9.0: added `or state.final_response` alongside the
    existing refusal_triggered check — defence in depth, so a
    node that sets final_response without also setting
    refusal_triggered (the exact class of bug fixed in
    classifier_node.py this round) can never again cause a
    silent, expensive pipeline fallthrough.
    """
    if state.refusal_triggered or state.final_response:
        return "end"
    return "prompt_builder"


def route_after_generator(state: AgentState) -> str:
    if state.refusal_triggered:
        return "end"
    return "output_safety"


def route_after_output_safety(state: AgentState) -> str:
    if state.refusal_triggered:
        return "end"
    return "response_formatter"


def response_formatter_node(state: AgentState) -> AgentState:
    """Final formatting — enforces answer length limits."""
    if not state.refusal_triggered and state.raw_response:
        response = state.raw_response

        words = response.split()
        if len(words) > 400:
            response = " ".join(words[:400]) + "..."
            log.info(
                "response_truncated",
                original_words=len(words),
                request_id=state.request_id,
            )

        state.final_response = response
        log.info(
            "response_formatted",
            citations=len(state.citations),
            response_length=len(state.final_response),
            request_id=state.request_id,
        )

    return state