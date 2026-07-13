"""
Classifier Node — intent classification and query type detection.

Runs AFTER supervisor (validation, sanitisation, quick greeting
check) and BEFORE cache_check. Sets state.intent and
state.query_type so all downstream nodes have consistent,
pre-computed signals without re-classifying.

Pipeline position:
    Supervisor → [Classifier] → Cache Check → Input Safety
    → Retriever → Prompt Builder → Generator → Output Safety
    → Formatter → Cache Write

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — July 2026 | Mukesh Kund
         New node — extracted from supervisor.py (v1.6.0) as
         part of Sprint 1 pipeline refactor.

         WHAT MOVED HERE FROM supervisor.py:
         - OBVIOUS_GREETINGS, OBVIOUS_FAREWELLS, OBVIOUS_THANKS
           (rule-based pattern sets — kept here for completeness
           but the supervisor uses quick_intent_check() to
           handle obvious greetings before this node runs)
         - GREETING_RESPONSES (templates for non-INSURANCE intents)
         - INTENT_SYSTEM_PROMPT (LLM classifier prompt)
         - classify_intent() (gpt-4o-mini LLM classification)
         - All context-override detection word sets:
           HISTORY_REFERENCE_WORDS, DISAGREEMENT_WORDS,
           CLARIFICATION_WORDS, FRUSTRATED_FAREWELL_WORDS,
           IMPLICIT_QUERY_WORDS, CONTINUATION_WORDS
         - is_contextual_follow_up() (override detection logic)

         WHAT STAYS IN supervisor.py:
         - quick_intent_check() — rule-based only, no LLM.
           Intercepts obvious greetings ("hi", "thanks", "bye")
           BEFORE this node runs, saving unnecessary LLM calls.
           Supervisor sets state.final_response directly for
           these and routes to END via route_after_supervisor.

         WHY CLASSIFY_INTENT USES A SEPARATE DEPLOYMENT VAR:
         - DEPLOYMENT_CLASSIFICATION reads
           AZURE_OPENAI_DEPLOYMENT_CLASSIFICATION (gpt-4o-mini).
         - Deliberately separate from DEPLOYMENT_FAST (now gpt-4o)
           — classification is a cheap structured task (97%
           accuracy at gpt-4o-mini cost, per evaluation data).
         - Changing DEPLOYMENT_FAST to a better generation model
           must NEVER silently upgrade classification cost.

         WHY CONTEXTUAL OVERRIDE MOVES HERE:
         - is_contextual_follow_up() needs to run AFTER
           initial intent classification (it only fires when
           intent != INSURANCE) but BEFORE cache_check (which
           needs _override_triggered to skip canonical rewrite
           for follow-up queries). Classifier sits between
           supervisor and cache_check — the correct position.
         - Sets state.__dict__["_override_triggered"] and
           state.__dict__["_override_reason"] following the same
           pattern as graph.py v1.1.0 _DICT_EXTRA_KEYS which
           ensures these propagate through all node boundaries.

         NEW — classify_query_type():
         - Rule-based (no LLM) — cheap and fast.
         - Returns "BROAD" or "SPECIFIC".
         - BROAD signals: entry-point questions covering multiple
           product types or asking for overviews. These trigger:
           (a) retriever applies title_questions scoring boost
           (b) rerank_chunks() fuzzy title matching in retriever
           (c) is_simple_query() upgrades BROAD to gpt-4o minimum
               regardless of word count (richer answers needed)
         - SPECIFIC: all other queries — existing routing logic
           unchanged.

         DEPLOYMENT:
         - AZURE_OPENAI_DEPLOYMENT_CLASSIFICATION = gpt-4o-mini
           (separate env var, separate from DEPLOYMENT_FAST)

v1.1.0 — July 2026 | Mukesh Kund
         Expanded OBVIOUS_GREETINGS / FAREWELLS / THANKS sets
         (companion change to supervisor.py v1.8.0)

         Sets kept in sync with supervisor.py. See supervisor.py
         v1.8.0 CHANGE LOG for full rationale and phrase list.

═══════════════════════════════════════════════════════════════
"""

import os
import json
import time
import structlog
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv, find_dotenv

from core.schemas import AgentState

_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path, override=True)
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")

# Dedicated classification deployment — gpt-4o-mini.
# DELIBERATELY separate from DEPLOYMENT_FAST (now gpt-4o).
# See CHANGE LOG v1.0.0 for rationale.
DEPLOYMENT_CLASSIFICATION = os.getenv(
    "AZURE_OPENAI_DEPLOYMENT_CLASSIFICATION",
    "gpt-4o-mini",
)

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
    """Get or create singleton AzureOpenAI client for classification."""
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
            "classifier_client_created",
            endpoint=AZURE_OPENAI_ENDPOINT,
            deployment=DEPLOYMENT_CLASSIFICATION,
        )
    return _openai_client


# ── Quick pattern sets (moved from supervisor.py) ─────────────
# These are kept here for completeness and used in classifier_node
# as a secondary fast-path before hitting the LLM.
# NOTE: supervisor.py also has quick_intent_check() which runs
# BEFORE classifier_node for obvious one-word greetings. If
# supervisor already handled it (state.final_response set), this
# node is never reached.
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

# ── Greeting responses (moved from supervisor.py) ─────────────
# Used by classifier_node to generate direct responses for
# non-INSURANCE intents that reach this node (edge case: multi-
# word phrases that supervisor's quick_intent_check missed).
GREETING_RESPONSES = {
    "GREETING": (
        "Hello! I'm Aria, RLG's AI Assistant. "
        "I'm here to help you with questions about "
        "Royal London insurance, pensions, ISAs and "
        "other financial products. How can I help you today?"
    ),
    "CHITCHAT": (
        "I'm doing great, thank you for asking! "
        "I'm here and ready to help you with any questions "
        "about Royal London insurance, pensions, ISAs or "
        "other financial products. What can I help you with today?"
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
    "CAPABILITY": (
        "I'm Aria, RLG's AI Assistant. I can help you with:\n"
        "- Making a claim on your policy\n"
        "- Understanding pension options and drawdown\n"
        "- ISA information and allowances\n"
        "- Critical illness and life insurance\n"
        "- Contacting Royal London\n"
        "- Finding a lost pension\n"
        "- And much more!\n\n"
        "What would you like to know today?"
    ),
    "IRRELEVANT": (
        "I can only help with questions about Royal London "
        "insurance, pensions, ISAs and related financial "
        "products. Is there anything in those areas "
        "I can help you with today?"
    ),
}

# ── Context-override detection (moved from supervisor.py) ─────
# Detects queries that LOOK like non-INSURANCE but are actually
# contextual follow-ups requiring conversation history.
# See supervisor.py v1.2.0 / v1.4.0 for original rationale.

# Category A: Explicit references to previous conversation
HISTORY_REFERENCE_WORDS = [
    "previous", "earlier", "before", "last time",
    "you said", "didn't answer", "didnt answer",
    "you told", "you mentioned", "already asked",
    "already told", "not what i asked", "not what i said",
    "not what i meant", "misunderstood", "you keep",
    "repeating", "same thing", "try again", "asked you",
    "my question", "my previous", "what i asked",
]

# Category F: Negative responses / disagreement
DISAGREEMENT_WORDS = [
    "that's not right", "thats not right",
    "that's wrong", "thats wrong", "that's incorrect",
    "thats incorrect", "that's not correct", "thats not correct",
    "are you sure", "i don't think so", "i dont think so",
    "i've heard differently", "ive heard differently",
    "the website says", "that contradicts",
    "that's not accurate", "thats not accurate",
]

# Category C: Clarification of misunderstood answer
CLARIFICATION_WORDS = [
    "i meant", "i was asking about", "let me rephrase",
    "actually i meant", "no i meant", "not the",
    "i said", "what i want to know", "i wasn't asking",
    "i wasnt asking", "i meant to ask", "what i meant",
    "to clarify", "to be clear",
]

# Category H: Frustrated farewell — needs empathetic routing
FRUSTRATED_FAREWELL_WORDS = [
    "whatever", "forget it", "never mind", "nevermind",
    "this is useless", "this isn't helpful", "this isnt helpful",
    "fine i'll", "fine ill", "i'll just call", "ill just call",
    "not helpful", "waste of time", "pointless",
]

# Category G: Short implicit queries — meaningful only with history
IMPLICIT_QUERY_WORDS = [
    "covered", "fees", "cost", "costs", "charges",
    "how long", "how much", "take", "online",
    "affect", "happen", "after", "change",
    "different", "instead", "alternatively",
    "what if", "what about", "same for",
]

# Category D/E: Continuation openers + emotional follow-up
CONTINUATION_WORDS = [
    "but one more", "but what about", "but what if",
    "now what about", "and what about", "so what does",
    "what does that mean", "but how", "but when",
    "can you simplify", "i'm confused", "im confused",
    "confused by that", "confused by your",
    "that sounds", "that seems expensive",
    "that seems complicated",
    "thanks but", "thank you but", "cheers but",
    "thanks and", "thank you and", "ok thanks but",
    "ok thank you but", "great and", "ok and",
    "got it but", "got it and", "understood but",
]


# ── Intent classifier system prompt ──────────────────────────
INTENT_SYSTEM_PROMPT = """You are an intent classifier for
an insurance and pensions AI assistant called Aria.

Classify the user query into exactly ONE of these intents:

INSURANCE  - Questions about insurance, pensions, ISA,
             claims, Royal London products, financial products,
             bereavement, policy details, contact info etc.

GREETING   - Hello, hi, hey, good morning, good evening etc.

CHITCHAT   - How are you, how's it going, you okay,
             casual conversation not related to insurance

THANKS     - Thank you, thanks, cheers, appreciated, helpful etc.

FAREWELL   - Bye, goodbye, see you, take care etc.

CAPABILITY - What can you do, who are you, how can you help,
             what are you, tell me about yourself etc.

IRRELEVANT - Questions completely unrelated to insurance or
             pensions: weather, sport, food, politics,
             entertainment, technology, travel, gaming,
             celebrity news, recipes, football results etc.

Respond with JSON only:
{"intent": "<INTENT>", "confidence": <0.0-1.0>}

Examples:
"How do I make a claim?" -> {"intent": "INSURANCE", "confidence": 1.0}
"Hi there!" -> {"intent": "GREETING", "confidence": 1.0}
"How are you doing today?" -> {"intent": "CHITCHAT", "confidence": 0.95}
"Thanks that helped" -> {"intent": "THANKS", "confidence": 1.0}
"Bye!" -> {"intent": "FAREWELL", "confidence": 1.0}
"What can you help with?" -> {"intent": "CAPABILITY", "confidence": 1.0}
"lol ok bye then" -> {"intent": "FAREWELL", "confidence": 0.9}
"u ok mate" -> {"intent": "CHITCHAT", "confidence": 0.95}
"morning!" -> {"intent": "GREETING", "confidence": 1.0}
"What is the weather today?" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"Who won the football?" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"What's for dinner?" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"Tell me a joke" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"What is the capital of France?" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"Who is the prime minister?" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"Best restaurants near me" -> {"intent": "IRRELEVANT", "confidence": 1.0}"""


# ── BROAD query signals ───────────────────────────────────────
# Used by classify_query_type() — no LLM needed.
# A query matching ANY of these is classified as BROAD.
BROAD_SIGNALS = [
    # Overview / types questions
    "what types", "what type of", "what are the",
    "what kinds", "what kind of",
    "what are", "what is a", "what is an", "what is the",
    # Explanation / comparison
    "overview", "explain", "tell me",
    "difference between", "compare", "comparison",
    "versus", "vs",
    # How questions (process / mechanism — not specific lookups)
    "how does", "how do", "how can",
    # Royal London specific overviews
    "does royal london offer", "does royal london provide",
    "does royal london have", "what does royal london offer",
    "what does royal london provide",
    # Range / options
    "options available", "available options",
    "range of", "types available",
    "what products", "which products",
    "all the", "list of",
]

# Negative signals — these phrases make a query SPECIFIC even
# if it contains a BROAD signal word. E.g. "how does MY pension
# work" is specific (the customer is asking about THEIR pension).
SPECIFIC_OVERRIDES = [
    "my pension", "my policy", "my account", "my isa",
    "my plan", "my coverage", "my claim", "my premium",
    "my options", "my situation", "my case", "my money",
    "i have", "i've got", "i currently", "my existing",
    "can i", "am i eligible", "do i qualify",
    "how much is", "how much am i", "how much will i",
]


def classify_query_type(query: str) -> str:
    """
    Classify query as BROAD or SPECIFIC. Rule-based, no LLM.

    BROAD:  Entry-point overview questions covering multiple
            product types or asking for general explanations.
            These trigger the title_questions retrieval boost
            in retriever.py and upgrade model routing so
            comprehensive answers get gpt-4o minimum.

    SPECIFIC: Targeted questions about a particular product,
              feature, or personal situation. Existing
              retrieval and routing logic applies unchanged.

    Logic:
    1. Check SPECIFIC_OVERRIDES first — personalised queries
       are always SPECIFIC regardless of other signals.
    2. Check BROAD_SIGNALS — any match → BROAD.
    3. Default → SPECIFIC (safe default for retrieval).

    Short queries (≤4 words) default to SPECIFIC unless they
    contain an explicit broad signal — short queries are
    almost always targeted lookups ("what is the MPAA?").
    """
    query_lower = query.lower().strip()

    # Step 1: personalised queries are always SPECIFIC
    if any(sig in query_lower for sig in SPECIFIC_OVERRIDES):
        return "SPECIFIC"

    # Step 2: check broad signals FIRST before short-query default.
    # Important: do this BEFORE the short-query default so that
    # "Explain drawdown" (2 words, "explain" is a broad signal)
    # is correctly classified as BROAD.
    if any(sig in query_lower for sig in BROAD_SIGNALS):
        return "BROAD"

    # Step 3: short queries (≤4 words) default to SPECIFIC.
    # These are almost always targeted lookups. We already
    # checked broad signals above, so if we reach here the
    # short query has no broad signal — it's SPECIFIC.
    # v1.0.0 BUG FIX: word_count was used here but never defined.
    # Every call to classify_query_type() would raise NameError
    # on any query that reached this branch (i.e. any query that
    # was not caught by SPECIFIC_OVERRIDES or BROAD_SIGNALS).
    word_count = len(query_lower.split())
    if word_count <= 4:
        return "SPECIFIC"

    return "SPECIFIC"


def classify_intent(query: str) -> tuple[str, float]:
    """
    Use gpt-4o-mini to classify query intent.
    Returns (intent, confidence).
    Falls back to INSURANCE on any error — fail-safe default
    (better to run the full pipeline than to refuse a valid query).

    Uses DEPLOYMENT_CLASSIFICATION (gpt-4o-mini) — separate from
    DEPLOYMENT_FAST (gpt-4o) to prevent classification cost from
    silently escalating when generation model improves.
    """
    try:
        client   = get_openai_client()
        response = client.chat.completions.create(
            model=DEPLOYMENT_CLASSIFICATION,
            messages=[
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {"role": "user",   "content": query},
            ],
            max_tokens=50,
            temperature=0.0,
        )
        raw   = response.choices[0].message.content.strip()
        clean = raw
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]

        result     = json.loads(clean.strip())
        intent     = result.get("intent", "INSURANCE").upper()
        confidence = float(result.get("confidence", 1.0))

        log.info(
            "intent_classified",
            query=query[:50],
            intent=intent,
            confidence=confidence,
            deployment=DEPLOYMENT_CLASSIFICATION,
        )
        return intent, confidence

    except Exception as e:
        log.warning(
            "intent_classification_failed",
            error=str(e),
            fallback="INSURANCE",
        )
        return "INSURANCE", 1.0


def is_contextual_follow_up(
    query: str,
    history: list[dict],
) -> tuple[bool, str]:
    """
    Detect if a query that looks like non-INSURANCE is actually
    a contextual follow-up requiring conversation history.

    Moved from supervisor.py v1.2.0. Logic unchanged.

    Returns:
        (True, reason_category) → override intent to INSURANCE,
                                   set _override_triggered on state
        (False, "")            → genuinely non-insurance

    Only activates when conversation_history is non-empty.
    A first-message query with no history cannot be a follow-up.
    """
    if not history:
        return False, ""

    q = query.lower().strip()

    for phrase in HISTORY_REFERENCE_WORDS:
        if phrase in q:
            return True, "history_reference"

    for phrase in DISAGREEMENT_WORDS:
        if phrase in q:
            return True, "disagreement"

    for phrase in CLARIFICATION_WORDS:
        if phrase in q:
            return True, "clarification"

    for phrase in FRUSTRATED_FAREWELL_WORDS:
        if phrase in q:
            return True, "frustrated_farewell"

    for phrase in CONTINUATION_WORDS:
        if phrase in q:
            return True, "continuation"

    # Short implicit queries (≤6 words) meaningful only with history
    words = q.split()
    if len(words) <= 6:
        for word in IMPLICIT_QUERY_WORDS:
            if word in q:
                return True, "implicit_short_query"

    return False, ""


# ── Main node ─────────────────────────────────────────────────
def classifier_node(state: AgentState) -> AgentState:
    """
    Classify query intent and type. Sets state.intent and
    state.query_type for all downstream nodes to consume.

    Pipeline position: Supervisor → [Classifier] → Cache Check

    This node always routes to Cache Check — there are no
    early-exit branches here. Non-INSURANCE responses
    (greetings, chitchat, etc.) are generated HERE and stored
    on state.final_response, but the node still returns normally.
    route_after_classifier in graph.py always sends to cache_check;
    cache_check will see final_response already set and skip.

    Wait — actually route_after_supervisor handles non-insurance
    early exit. Let me clarify the flow:

    If supervisor's quick_intent_check() caught "hi"/"thanks"/
    "bye" → supervisor set state.final_response → route_after_
    supervisor returned "end" → this node never runs.

    If a longer greeting/chitchat/irrelevant query reached here:
    → classify_intent() returns GREETING/CHITCHAT/etc.
    → is_contextual_follow_up() checks if it's actually a
      follow-up (history context)
    → If not a follow-up: set state.intent, set
      state.final_response from GREETING_RESPONSES,
      state.query_type = "SPECIFIC"
    → If follow-up override: set state.intent = "INSURANCE",
      state._override_triggered = True, continue to pipeline
    → For INSURANCE queries: set state.intent, state.query_type

    In all cases, route_after_classifier → cache_check.
    cache_check and downstream nodes check state.final_response
    and state.refusal_triggered to decide whether to proceed.

    Execution steps:
    1. classify_intent() — LLM (gpt-4o-mini)
    2. is_contextual_follow_up() — rule-based override check
    3. classify_query_type() — rule-based, always runs
    4. Set state fields + log
    """
    start = time.time()

    try:
        # ── Step 1: LLM intent classification ────────────────
        intent, confidence = classify_intent(state.query)

        # ── Step 2: Context-aware override check ─────────────
        # If intent is non-INSURANCE but we have conversation
        # history, check if this is actually a follow-up.
        # Mirrors supervisor.py v1.2.0 / v1.4.0 logic, now
        # correctly positioned after classification.
        _override_triggered = False
        _override_reason    = ""

        if intent != "INSURANCE":
            is_follow_up, reason = is_contextual_follow_up(
                state.query,
                state.conversation_history,
            )
            if is_follow_up:
                _override_triggered = True
                _override_reason    = reason
                intent              = "INSURANCE"
                # Propagate via state.__dict__ — graph.py v1.1.0
                # _DICT_EXTRA_KEYS ensures these survive all node
                # boundary conversions.
                state.__dict__["_override_triggered"] = True
                state.__dict__["_override_reason"]    = reason
                log.info(
                    "context_override_triggered",
                    reason=reason,
                    query=state.query[:60],
                    history_turns=len(state.conversation_history),
                    request_id=state.request_id,
                )

        # ── Step 3: Handle non-INSURANCE intents ─────────────
        # Generate direct response — does NOT exit the pipeline.
        # route_after_classifier always goes to cache_check;
        # downstream nodes check state.final_response.
        if intent != "INSURANCE" and confidence >= 0.85:
            state.final_response = GREETING_RESPONSES.get(
                intent, GREETING_RESPONSES["IRRELEVANT"]
            )
            log.info(
                "non_insurance_handled",
                intent=intent,
                confidence=confidence,
                query=state.query[:50],
                request_id=state.request_id,
            )

        # ── Step 4: Query type classification ─────────────────
        # Always runs — even for non-INSURANCE queries — so
        # downstream nodes always have a valid query_type.
        # Rule-based, no LLM call.
        query_type = classify_query_type(state.query)

        # ── Step 5: Set state fields ──────────────────────────
        state.intent     = intent
        state.query_type = query_type

        latency = (time.time() - start) * 1000
        state.latency_ms["classifier"] = latency

        log.info(
            "classifier_complete",
            intent=intent,
            confidence=confidence,
            query_type=query_type,
            override_triggered=_override_triggered,
            override_reason=_override_reason,
            latency_ms=round(latency),
            request_id=state.request_id,
        )

    except Exception as e:
        # Fail-safe: INSURANCE + SPECIFIC means the full
        # pipeline runs. Better than a hard failure that
        # leaves the customer with no response.
        log.error(
            "classifier_error",
            error=str(e),
            request_id=state.request_id,
            fallback="INSURANCE/SPECIFIC",
        )
        state.intent     = "INSURANCE"
        state.query_type = "SPECIFIC"

    return state