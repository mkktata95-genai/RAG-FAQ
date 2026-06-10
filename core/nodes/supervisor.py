"""
Supervisor Node — entry point, validates state, routes graph.
P4 - Query length validation
P3 - Request ID injection

Migration: Mistral-small → gpt-4o-mini
Auth:       DefaultAzureCredential + bearer token (no API key)
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
    """
    Get or create singleton AzureOpenAI client.
    Reused across all supervisor calls.
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
"morning!" -> {"intent": "GREETING", "confidence": 1.0}"""

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
}


# ── Helper functions ──────────────────────────────────────────
def quick_intent_check(query: str) -> str | None:
    """
    Fast pattern check for obvious greetings.
    No API call needed — saves ~1-2s latency.
    Returns intent string or None if uncertain.
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
    """
    # P3: Assign request ID
    if not state.request_id:
        state.request_id = generate_request_id()

    # P8: Sanitize input
    state.query = sanitize_input(state.query)

    # Validate length
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

    # ── Step 1: Quick pattern check (no API call) ─────────
    intent     = quick_intent_check(state.query)
    confidence = 1.0
    _used_llm  = False

    # ── Step 2: LLM classifier only if uncertain ──────────
    if intent is None:
        intent, confidence = classify_intent(state.query)
        _used_llm = True

    # ── Step 3: Handle non-insurance intents ──────────────
    if intent != "INSURANCE" and confidence >= 0.85:
        state.final_response    = GREETING_RESPONSES.get(
            intent,
            GREETING_RESPONSES["CAPABILITY"],
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

    # ── Step 4: Insurance query — continue pipeline ───────
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
    )

    return state


# ── Router functions ──────────────────────────────────────────
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