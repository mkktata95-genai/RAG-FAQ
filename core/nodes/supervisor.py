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

═══════════════════════════════════════════════════════════════
"""

import re
import os
import json
import structlog
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
from core.schemas import AgentState, MAX_QUERY_WORDS
from core.middleware import generate_request_id, mask_pii_for_logging

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
DEPLOYMENT_FAST       = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4o-mini")

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
    """Get or create singleton AzureOpenAI client."""
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
            "supervisor_client_created",
            endpoint=AZURE_OPENAI_ENDPOINT,
        )
    return _openai_client


# ── Quick Intent Patterns ─────────────────────────────────────
OBVIOUS_GREETINGS = {
    "hi", "hello", "hey", "hiya", "howdy",
    "good morning", "good afternoon",
    "good evening", "good night",
}
OBVIOUS_FAREWELLS = {
    "bye", "goodbye", "good bye", "see you",
    "see ya", "take care", "farewell",
}
OBVIOUS_THANKS = {
    "thanks", "thank you", "cheers",
    "thank you so much", "thanks a lot",
    "many thanks", "appreciated",
}

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

# ── Context-Aware Routing Override ───────────────────────────
#
# These word groups detect when a query that LOOKS like CHITCHAT,
# THANKS, IRRELEVANT, or FAREWELL is actually a contextual follow-up
# that REQUIRES conversation history to answer correctly.
#
# If ANY word/phrase from these groups matches AND conversation
# history exists → override intent → route through full pipeline.
#
# CATEGORY A: Frustration & complaint references
# "Why didn't you answer?" / "That's not what I asked" etc.
HISTORY_REFERENCE_WORDS = [
    "previous", "earlier", "before", "last time",
    "you said", "didn't answer", "didnt answer",
    "you told", "you mentioned", "already asked",
    "already told", "not what i asked", "not what i said",
    "not what i meant", "misunderstood", "you keep",
    "repeating", "same thing", "try again", "asked you",
    "my question", "my previous", "what i asked",
]

# CATEGORY F: Negative responses / disagreement
# "No that's not right" / "Are you sure?" etc.
DISAGREEMENT_WORDS = [
    "that's not right", "thats not right",
    "that's wrong", "thats wrong", "that's incorrect",
    "thats incorrect", "that's not correct", "thats not correct",
    "are you sure", "i don't think so", "i dont think so",
    "i've heard differently", "ive heard differently",
    "the website says", "that contradicts",
    "that's not accurate", "thats not accurate",
]

# CATEGORY C: Clarification — user correcting a misunderstood answer
# "No I meant my pension" / "Let me rephrase" etc.
CLARIFICATION_WORDS = [
    "i meant", "i was asking about", "let me rephrase",
    "actually i meant", "no i meant", "not the",
    "i said", "what i want to know", "i wasn't asking",
    "i wasnt asking", "i meant to ask", "what i meant",
    "to clarify", "to be clear",
]

# CATEGORY H: Frustrated farewell
# "Whatever forget it" / "This is useless" / "Never mind" etc.
# These need an empathetic response, not the standard farewell.
FRUSTRATED_FAREWELL_WORDS = [
    "whatever", "forget it", "never mind", "nevermind",
    "this is useless", "this isn't helpful", "this isnt helpful",
    "fine i'll", "fine ill", "i'll just call", "ill just call",
    "not helpful", "waste of time", "pointless",
]

# CATEGORY G: Implicit short queries — meaningful ONLY with history
# "Is that covered?" / "What are the fees?" / "Can I do it online?"
# These are ≤5 words and contain a context-dependent word.
IMPLICIT_QUERY_WORDS = [
    "covered", "fees", "cost", "costs", "charges",
    "how long", "how much", "take", "online",
    "affect", "happen", "after", "change",
    "different", "instead", "alternatively",
    "what if", "what about", "same for",
]

# CATEGORY D & E: Continuation openers + emotional follow-up
# "OK so what happens next?" / "I'm confused by that"
CONTINUATION_WORDS = [
    "but one more", "but what about", "but what if",
    "now what about", "and what about", "so what does",
    "what does that mean", "but how", "but when",
    "can you simplify", "i'm confused", "im confused",
    "confused by that", "confused by your",
    "that sounds", "that seems expensive",
    "that seems complicated",
    # Category B: thanks + continuation
    "thanks but", "thank you but", "cheers but",
    "thanks and", "thank you and", "ok thanks but",
    "ok thank you but", "great and", "ok and",
    "got it but", "got it and", "understood but",
]


def is_contextual_follow_up(
    query: str,
    history: list[dict],
) -> tuple[bool, str]:
    """
    Detect if a query that looks like non-INSURANCE is actually
    a contextual follow-up that requires conversation history.

    Returns:
        (True, reason_category) if it should override to INSURANCE
        (False, '') if it is genuinely non-insurance

    Only activates if conversation_history is non-empty.
    A first-message query with no history cannot be a follow-up.
    """
    if not history:
        return False, ""

    q = query.lower().strip()

    # Category A: Explicit references to previous conversation
    for phrase in HISTORY_REFERENCE_WORDS:
        if phrase in q:
            return True, "history_reference"

    # Category F: Disagreement / correction
    for phrase in DISAGREEMENT_WORDS:
        if phrase in q:
            return True, "disagreement"

    # Category C: Clarification of previous answer
    for phrase in CLARIFICATION_WORDS:
        if phrase in q:
            return True, "clarification"

    # Category H: Frustrated farewell needs empathetic routing
    for phrase in FRUSTRATED_FAREWELL_WORDS:
        if phrase in q:
            return True, "frustrated_farewell"

    # Category D/E: Continuation opener
    for phrase in CONTINUATION_WORDS:
        if phrase in q:
            return True, "continuation"

    # Category G: Short implicit query (≤6 words) with context-dependent word
    # Only applies if history exists (already checked above)
    words = q.split()
    if len(words) <= 6:
        for word in IMPLICIT_QUERY_WORDS:
            if word in q:
                return True, "implicit_short_query"

    return False, ""


# ── Intent Classifier System Prompt ──────────────────────────
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
"Who won last football worldcup?" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"What's for dinner?" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"Tell me a joke" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"What is the capital of France?" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"Who is the prime minister?" -> {"intent": "IRRELEVANT", "confidence": 1.0}
"Best restaurants near me" -> {"intent": "IRRELEVANT", "confidence": 1.0}"""

# ── Greeting Responses ────────────────────────────────────────
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


# ── Helper functions ──────────────────────────────────────────
def quick_intent_check(query: str) -> str | None:
    """
    Fast pattern check for obvious greetings.
    No API call needed — saves ~1-2s latency.
    Returns intent string or None if uncertain.

    NOTE: This only matches EXACT short phrases.
    "Why didn't you answer my previous question?" will NOT
    match here — it falls through to classify_intent() and
    then the context override check.
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


def classify_intent(query: str) -> tuple[str, float]:
    """
    Use gpt-4o-mini to classify query intent.
    Returns (intent, confidence).
    Falls back to INSURANCE on any error.
    """
    try:
        client   = get_openai_client()
        response = client.chat.completions.create(
            model=DEPLOYMENT_FAST,
            messages=[
                {
                    "role":    "system",
                    "content": INTENT_SYSTEM_PROMPT,
                },
                {
                    "role":    "user",
                    "content": query,
                },
            ],
            max_tokens=50,
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()

        # Strip markdown code fences if present
        clean = raw.strip()
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
        )
        return intent, confidence

    except Exception as e:
        log.warning("intent_classification_failed", error=str(e))
        return "INSURANCE", 1.0


def sanitize_input(text: str) -> str:
    """P8 - Input sanitization."""
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
    intent     = quick_intent_check(state.query)
    confidence = 1.0
    _used_llm  = False

    # ── Step 5: LLM classifier only if uncertain ──────────
    if intent is None:
        intent, confidence = classify_intent(state.query)
        _used_llm = True

    # ── Step 6: Context-aware routing override ────────────
    #
    # BEFORE early-exiting on a non-INSURANCE intent, check whether
    # this query is actually a contextual follow-up that requires
    # history to answer correctly.
    #
    # Examples that would early-exit WITHOUT this check:
    #   "Why you didnt answer my previous question?" → CHITCHAT → wrong
    #   "That's not what I asked"                   → CHITCHAT → wrong
    #   "Are you sure about that?"                  → CHITCHAT → wrong
    #   "Is that covered?"                          → IRRELEVANT → wrong
    #   "Never mind, forget it"                     → FAREWELL → wrong
    #   "Thanks but what does that mean for me?"    → THANKS → wrong
    #
    # With the override, all of these route through the full pipeline
    # where the generator has access to conversation_history.
    #
    _override_triggered = False
    _override_reason    = ""

    if intent != "INSURANCE" and confidence >= 0.85:
        is_follow_up, reason = is_contextual_follow_up(
            state.query,
            state.conversation_history,
        )
        if is_follow_up:
            _override_triggered = True
            _override_reason    = reason
            intent              = "INSURANCE"  # Force full pipeline
            log.info(
                "context_override_triggered",
                original_intent=intent,
                override_reason=reason,
                query=state.query[:60],
                history_turns=len(state.conversation_history),
                request_id=state.request_id,
            )

    # ── Step 7: Handle genuine non-insurance intents ──────
    if intent != "INSURANCE" and confidence >= 0.85:
        state.final_response    = GREETING_RESPONSES.get(
            intent,
            GREETING_RESPONSES["IRRELEVANT"],
        )
        state.refusal_triggered = False
        log.info(
            "non_insurance_intent_handled",
            intent=intent,
            confidence=confidence,
            query=state.query[:50],
            request_id=state.request_id,
            used_llm=_used_llm,
        )
        return state

    # ── Step 8: Insurance query — continue pipeline ───────
    if len(state.conversation_history) > 10:
        state.conversation_history = (
            state.conversation_history[-10:]
        )

    # Expand ambiguous multi-turn queries
    state.query = expand_query(
        state.query,
        state.conversation_history,
    )

    # Log with PII masked
    masked_query = mask_pii_for_logging(state.query)
    log.info(
        "supervisor_start",
        request_id=state.request_id,
        query=masked_query[:50],
        history_turns=len(state.conversation_history),
        intent=intent,
        confidence=confidence,
        used_llm=_used_llm,
        override_triggered=_override_triggered,
        override_reason=_override_reason,
    )

    return state


# ── Router functions ──────────────────────────────────────────
def route_after_supervisor(state: AgentState) -> str:
    if state.refusal_triggered or state.final_response:
        return "end"
    return "cache_check"


def route_after_cache(state: AgentState) -> str:
    if state.cache_hit:
        return "end"
    return "input_safety"


def route_after_input_safety(state: AgentState) -> str:
    if state.refusal_triggered:
        return "end"
    return "retriever"


def route_after_retriever(state: AgentState) -> str:
    if state.refusal_triggered:
        return "end"
    return "generator"


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