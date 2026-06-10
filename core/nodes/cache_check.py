"""
Cache Check Node — checks semantic cache before hitting LLM.
Hybrid approach:
  Step 1: Lemmatization + stop words (free)
  Step 2: Cache check with threshold 0.87
  Step 3: LLM canonical rewrite (gpt-4o-mini)
  Step 4: Cache check again with canonical form

Migration: Mistral-small → gpt-4o-mini
Auth:       DefaultAzureCredential + bearer token (no API key)
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
DOMAIN_SYNONYMS = {
    "life cover":               "life insurance",
    "life assurance":           "life insurance",
    "retirement fund":          "pension",
    "retirement savings":       "pension",
    "retirement pot":           "pension",
    "pension pot":              "pension",
    "pension fund":             "pension",
    "isas":                     "isa",
    "individual savings account": "isa",
    "savings account":          "isa",
    "critical illness":         "critical illness cover",
    "serious illness":          "critical illness cover",
    "income cover":             "income protection",
    "sick pay":                 "income protection",
    "equity release":           "equity release",
    "drawdown":                 "pension drawdown",
    "flexible access":          "pension drawdown",
    "phone number":             "contact",
    "telephone":                "contact",
    "call":                     "contact",
    "email":                    "contact",
    "get in touch":             "contact",
    "complain":                 "complaint",
    "unhappy":                  "complaint",
    "dispute":                  "complaint",
    "transfer":                 "pension transfer",
    "move pension":             "pension transfer",
    "lost pension":             "find lost pension",
    "missing pension":          "find lost pension",
    "trace pension":            "find lost pension",
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
    """Replace domain synonyms with standard terms."""
    text_lower = text.lower()
    for phrase, replacement in DOMAIN_SYNONYMS.items():
        if phrase in text_lower:
            text_lower = text_lower.replace(phrase, replacement)
    return text_lower


def normalize_query(text: str) -> str:
    """
    Full normalisation pipeline:
    1. Lowercase + strip punctuation
    2. Domain synonym replacement
    3. Stop word removal
    4. Lemmatization
    """
    text = text.lower().strip()
    text = apply_domain_synonyms(text)
    text = re.sub(r'[^\w\s]', '', text)

    words              = text.split()
    normalized_words   = []
    for word in words:
        if word not in FINAL_STOP_WORDS:
            lemma = lemmatizer.lemmatize(word)
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
    Use gpt-4o-mini to rewrite query into
    canonical insurance domain form.
    Returns canonical query or None on failure.
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
            max_tokens=30,
            temperature=0.0,
        )
        canonical = response.choices[0].message.content.strip()

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