"""
Multi-layer safety system for Royal London FAQ chatbot.
Layer 1: Relevance check
Layer 2: Crime/fraud detection
Layer 3: Prompt injection detection
Layer 4: Jailbreak detection
Layer 5: Azure Content Safety (violence/hate/sexual/self-harm)

Migration: AzureKeyCredential → DefaultAzureCredential
Auth:       No API key required

# ─────────────────────────────────────────────────────────────
# TODO: PRODUCTION READINESS
# Before go-live replace/enhance the following:
#
# Layer 1 - Relevance Check:
#      Current  → keyword matching (may over-block edge cases)
#      Enhance  → Use gpt-4o-mini for smarter relevance scoring
#
# Layer 2-4 - Crime/Fraud/Injection/Jailbreak:
#      Current  → regex patterns (good but not exhaustive)
#      Enhance  → Azure AI Content Safety Prompt Shield API
#                 Specifically designed for prompt injection
#                 and jailbreak detection
#                 POST /contentsafety/text:shieldPrompt
#
# Layer 5 - Azure Content Safety:
#      Current  → DefaultAzureCredential (production ready ✅)
#      Ensure   → CONTENT_SAFETY_ENDPOINT is provisioned in
#                 the same RLG Azure subscription
#                 Verify BLOCK_THRESHOLD=2 with RLG compliance team
#
# Overall:
#      Add audit logging of all safety blocks to
#      Azure Application Insights for FCA compliance reporting
# ─────────────────────────────────────────────────────────────
"""

import os
import re
import concurrent.futures
from azure.ai.contentsafety import ContentSafetyClient
from azure.ai.contentsafety.models import (
    AnalyzeTextOptions,
    TextCategory,
)
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import HttpResponseError
from dotenv import load_dotenv
import structlog

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
SAFETY_ENDPOINT = os.getenv("CONTENT_SAFETY_ENDPOINT", "")
BLOCK_THRESHOLD = 2

# ── Singleton Client ──────────────────────────────────────────
_credential:     DefaultAzureCredential | None = None
_safety_client:  ContentSafetyClient | None    = None


def get_credential() -> DefaultAzureCredential:
    """Get or create singleton DefaultAzureCredential."""
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def get_safety_client() -> ContentSafetyClient:
    """
    Get or create singleton Content Safety client.
    Uses DefaultAzureCredential — no API key required.
    """
    global _safety_client
    if _safety_client is None:
        if not SAFETY_ENDPOINT:
            raise ValueError(
                "CONTENT_SAFETY_ENDPOINT is not set in .env"
            )
        _safety_client = ContentSafetyClient(
            endpoint=SAFETY_ENDPOINT,
            credential=get_credential(),
        )
        log.info(
            "safety_client_created",
            endpoint=SAFETY_ENDPOINT,
        )
    return _safety_client


# ── Layer 1: Relevance ────────────────────────────────────────
RELEVANT_KEYWORDS = [
    "insurance", "pension", "policy", "claim", "premium",
    "life cover", "isa", "investment", "retirement", "annuity",
    "drawdown", "beneficiary", "royal london", "coverage",
    "endowment", "funeral", "critical illness", "income protection",
    "mortgage", "equity release", "surrender", "maturity",
    "contribution", "transfer", "dividend", "fund", "savings",
    "life insurance", "whole of life", "term insurance",
    "complaint", "bereavement", "probate", "trust",
    "workplace pension", "financial advice",
    "personal pension", "state pension",
    "unit trust", "bond", "financial adviser", "protection",
    "cancer", "terminal illness", "serious illness",
    "critical illness", "disability", "bereavement",
    "redundancy", "divorce", "separation", "diagnosis",
    "hospital", "mental health", "financial hardship",
    "passed away", "deceased", "probate", "executor",
]

IRRELEVANT_KEYWORDS = [
    "weather", "forecast", "recipe", "cook", "food",
    "sport", "football", "cricket", "rugby", "tennis",
    "movie", "film", "music", "song", "concert",
    "politics", "election", "party", "government",
    "celebrity", "gossip", "entertainment",
    "dating", "relationship", "romance",
    "travel", "holiday", "visa", "passport",
    "gaming", "video game", "console",
    "fashion", "clothes", "shopping",
    "joke", "funny", "meme",
    "capital of", "prime minister", "president",
    "cryptocurrency", "bitcoin", "nft",
    "smartphone", "phone", "laptop", "technology",
    "pasta", "pizza", "restaurant",
    "premier league", "champions league",
]

# ── Layer 2: Crime/Fraud ──────────────────────────────────────
CRIME_FRAUD_PATTERNS = [
    r"\bfraud\b",
    r"\bscam\b",
    r"\bscamm",
    r"\bhack\b",
    r"\bhacking\b",
    r"\bsteal\b",
    r"\btheft\b",
    r"\bforge\b",
    r"\bforger",
    r"\bforgery\b",
    r"\blaunder\b",
    r"\bmoney laundering\b",
    r"\bidentity theft\b",
    r"\bimpersonat",
    r"\bcheat\b",
    r"\bdeceiv",
    r"\bdefraud",
    r"\bmanipulat",
    r"\bbribe\b",
    r"\bcorrupt",
    r"\billegal",
    r"\bcriminal",
    r"\bembezzl",
    r"\bextort",
    r"\bblackmail\b",
    r"\bpyramid scheme\b",
    r"\bponzi\b",
    r"\bphishing\b",
    r"\bransomware\b",
    r"\bmalware\b",
    r"\bguarantee\b.{0,30}(return|grow|profit|gain)",
    r"(list|give me|show).{0,20}(customer|account|personal).{0,20}(number|data|detail|info)",
    r"say (negative|bad|terrible).{0,20}(about|things)",
    r"(bankrupt|insolvent|collapse).{0,20}(never|won.t|will not)",
]

# ── Layer 3: Prompt Injection ─────────────────────────────────
INJECTION_PATTERNS = [
    r"ignore (all |previous |your )?(instructions|rules|guidelines|constraints|prompt)",
    r"forget (your |all |previous )?(instructions|rules|training|guidelines)",
    r"(new|updated|override) (system |)prompt",
    r"system\s*:\s*(new|ignore|override|forget)",
    r"you are now (a |an |)(different|new|unrestricted|free)",
    r"(disable|bypass|remove|override) (all |)(filters?|restrictions?|safety|guidelines)",
    r"(developer|debug|admin|root|god)\s*mode",
    r"end of prompt",
    r"(act|pretend|roleplay|behave|respond)\s+as\s+(if\s+)?(you (are|were)|an? )(different|unrestricted|free|uncensored)",
    r"without (any |)(restrictions?|limitations?|filters?|rules?|guidelines?)",
    r"jailbreak",
    r"dan\b.*\brestrictions?\b",
    r"do anything now",
    r"no (content |)(policy|filter|restriction|rule)",
    r"(reveal|show|tell me|expose|print|output|display)\s+(your\s+)?(system\s+)?(prompt|instructions|guidelines|rules|training)",
    r"</?(s|sys|system|prompt|inst)>",
    r"#\s*(system|override|prompt)",
    r"\[ignor",
    r"base64",
    r"roleplay as (a |an |)(ai|assistant|bot).{0,30}(answer|respond|comply)",
    r"(previous|another|last) (user|person|customer).{0,30}(okay|ok|said|told|allow)",
    r"(authorized|authorise|permission).{0,30}(bypass|ignore|disable)",
    r"token.{0,20}smug",
]

# ── Layer 4: Jailbreak ────────────────────────────────────────
JAILBREAK_PATTERNS = [
    r"\bDAN\b",
    r"do anything now",
    r"gpt.{0,10}(4|5|without|no).{0,20}(filter|restriction|policy)",
    r"(grandmother|bedtime story|fictional).{0,50}(hack|fraud|bomb|weapon|illegal)",
    r"hypothetical(ly)?.{0,50}(rules?.{0,10}don.t apply|without rules|no rules)",
    r"alternate universe.{0,30}(answer|respond|tell)",
    r"(security audit|penetration test|red team).{0,30}(disable|bypass|ignore)",
    r"i am (anthropic|microsoft|openai|developer|admin|engineer)",
    r"if you were (a |an |)(different|another|unrestricted)",
    r"without (your |any |)(safety |)(training|guidelines|restrictions)",
    r"(letter by letter|spell out|character by character).{0,30}(prompt|instruction|rule)",
    r"pr0mpt|syst3m|1nstruct",
]


# ── Layer functions ───────────────────────────────────────────
def check_relevance(text: str) -> tuple[bool, str | None]:
    """Layer 1: Check if query is relevant to Royal London."""
    text_lower = text.lower()

    for keyword in IRRELEVANT_KEYWORDS:
        if keyword in text_lower:
            if not any(r in text_lower for r in RELEVANT_KEYWORDS):
                return False, "irrelevant"

    return True, None


def check_crime_fraud(text: str) -> tuple[bool, str | None]:
    """Layer 2: Detect financial crime and fraud attempts."""
    text_lower = text.lower()

    for pattern in CRIME_FRAUD_PATTERNS:
        if re.search(pattern, text_lower):
            log.warning("crime_fraud_detected", pattern=pattern)
            return False, "harmful"

    return True, None


def check_prompt_injection(text: str) -> tuple[bool, str | None]:
    """Layer 3: Detect prompt injection attempts."""
    text_lower = text.lower()

    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            log.warning(
                "prompt_injection_detected", pattern=pattern
            )
            return False, "harmful"

    return True, None


def check_jailbreak(text: str) -> tuple[bool, str | None]:
    """Layer 4: Detect jailbreak attempts."""
    text_lower = text.lower()

    for pattern in JAILBREAK_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            log.warning("jailbreak_detected", pattern=pattern)
            return False, "harmful"

    return True, None


def check_azure_content_safety(
    text: str,
) -> tuple[bool, str | None]:
    """
    Layer 5: Azure Content Safety.
    Uses singleton client with DefaultAzureCredential.
    Fails open on error — never blocks user on API failure.

    Timeout: 10s hard cap — prevents 32s+ hangs when RBAC is
    misconfigured or endpoint is unreachable (confirmed via
    check_content_safety.py diagnostic).
    """
    # Guard: skip Layer 5 entirely if endpoint not configured
    if not SAFETY_ENDPOINT:
        log.debug("content_safety_skipped", reason="endpoint_not_configured")
        return True, None

    if not text or not text.strip():
        return True, None

    try:
        client  = get_safety_client()
        request = AnalyzeTextOptions(
            text=text[:10000],
            categories=[
                TextCategory.HATE,
                TextCategory.VIOLENCE,
                TextCategory.SEXUAL,
                TextCategory.SELF_HARM,
            ],
        )

        # Python-level hard timeout — SDK timeout params are ineffective
        # when Azure backend returns errors slowly (~30s). This guarantees
        # a 10s cap regardless of SDK or server behaviour.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.analyze_text, request)
            try:
                response = future.result(timeout=10)
            except concurrent.futures.TimeoutError:
                log.warning("content_safety_timeout", timeout_seconds=10)
                return True, None

        for result in response.categories_analysis:
            if result.severity >= BLOCK_THRESHOLD:
                log.warning(
                    "content_unsafe",
                    category=result.category,
                    severity=result.severity,
                )
                return False, "harmful"

        return True, None

    except HttpResponseError as e:
        log.warning("safety_api_error", error=str(e))
        return True, None

    except Exception as e:
        log.warning("safety_check_failed", error=str(e))
        return True, None


# ── Main check functions ──────────────────────────────────────
def check_input(text: str) -> tuple[bool, str | None]:
    """
    Full multi-layer input check.
    Returns (is_safe, reason) where reason is None if safe.
    """
    if not text or not text.strip():
        return False, "irrelevant"

    # Layer 1: Relevance (no API call)
    safe, reason = check_relevance(text)
    if not safe:
        return False, reason

    # Layer 2: Crime/Fraud (no API call)
    safe, reason = check_crime_fraud(text)
    if not safe:
        return False, reason

    # Layer 3: Prompt Injection (no API call)
    safe, reason = check_prompt_injection(text)
    if not safe:
        return False, reason

    # Layer 4: Jailbreak (no API call)
    safe, reason = check_jailbreak(text)
    if not safe:
        return False, reason

    # Layer 5: Azure Content Safety (API call — singleton client)
    safe, reason = check_azure_content_safety(text)
    if not safe:
        return False, reason

    return True, None


def check_output(text: str) -> tuple[bool, str | None]:
    """Output safety — Azure Content Safety only."""
    return check_azure_content_safety(text)


def is_relevant_query(text: str) -> bool:
    """Backward compatibility."""
    safe, _ = check_relevance(text)
    return safe