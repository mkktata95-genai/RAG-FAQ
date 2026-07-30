"""
Multi-layer safety system for Royal London FAQ chatbot.
Layer 1:  Relevance check
Layer 2:  Crime/fraud detection
Layer 2B: Weapons/explosives detection
Layer 3A: Prompt Shields (Azure ML — jailbreak + injection)
Layer 3B: Prompt injection detection (regex fallback)
Layer 4:  Jailbreak detection (regex fallback)
Layer 5:  Azure Content Safety (violence/hate/sexual/self-harm)

Migration: AzureKeyCredential → DefaultAzureCredential
Auth:       No API key required

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
         5-layer safety system. Layers 1-4 regex-based, no API
         cost. Layer 5 Azure Content Safety via DefaultAzureCredential.

v1.1.0 — July 2026 | Mukesh Kund
         Tier 1 safety hardening — three changes.

         FIX 1 — Layer 2B: weapons/explosives detection [NEW]:
         - ROOT CAUSE: Layers 1-4 had zero coverage for weapons,
           explosives, or CBRN-adjacent content. A direct query
           like "how to make bomb" passed all four regex layers
           untouched — the only backstops were Azure OpenAI's own
           built-in content filter (opaque, not under our control)
           and a coincidental retrieval-relevance failure (chunks=[]
           on an insurance-only index). Neither is a real, auditable
           application-level control.
         - WEAPONS_PATTERNS [NEW CONSTANT] + check_weapons()
           [NEW FUNCTION]: same shape as check_crime_fraud(),
           covers explosives, firearms manufacturing, chemical/
           biological weapon construction language. Zero API cost,
           always active regardless of Content Safety reachability.
         - Wired into check_input() as Layer 2B, immediately after
           Layer 2 (crime/fraud) — same category of always-on,
           no-cost, no-dependency control.

         FIX 2 — Layer 3A: Prompt Shields integration [NEW]:
         - Azure Content Safety's dedicated jailbreak + prompt
           injection detection API (POST /contentsafety/text:
           shieldPrompt) — ML-based, catches novel phrasing our
           regex inevitably misses. Was TODO since v1.0.0.
         - Uses the Foundry multi-service endpoint (confirmed
           working for both analyze_text and shieldPrompt via
           test2.py / test3.py verification — see CLAUDE.md).
         - check_prompt_shields() [NEW FUNCTION]: Bearer token via
           DefaultAzureCredential (get_token, cognitiveservices
           audience) — no API key. Python-level ThreadPoolExecutor
           timeout (10s), matching the pattern already proven in
           check_azure_content_safety(). Three return states:
             (False, "harmful") → attack detected, block immediately
             (True, None)       → ML confirmed clean, skip regex
                                   Layers 3B+4 entirely
             (None, None)       → endpoint unreachable/error, fall
                                   through to regex Layers 3B+4
         - Regex Layers 3B (injection) and 4 (jailbreak) are NOT
           removed — they remain the always-on fallback per the
           agreed design ("AI-detection with regex fallback",
           same pattern already used for Content Safety vs regex
           elsewhere in this file). This also means Prompt Shields
           unreachability (e.g. from VDI without the Foundry
           endpoint configured) degrades gracefully to exactly
           today's behaviour — no regression risk.

         FIX 3 — ThreadPoolExecutor shutdown bug in
         check_azure_content_safety() [BUG FIX]:
         - ROOT CAUSE: `with concurrent.futures.ThreadPoolExecutor(...)
           as executor:` calls executor.shutdown(wait=True) on
           context exit — which BLOCKS until the background thread
           finishes, even after future.result(timeout=10) has
           already raised TimeoutError and been caught. Confirmed
           live: content_safety_timeout logged at ~15s but
           input_safe/output_safe latency still showed ~30-129s —
           the "hard 10s cap" was never actually a hard cap because
           the with-block silently waited for the abandoned thread
           anyway.
         - FIX: replaced `with ... as executor:` with manual
           executor = ThreadPoolExecutor(...); executor.shutdown(
           wait=False) after both the success and timeout paths —
           abandons the background thread immediately instead of
           blocking on it. Same fix applied to the new
           check_prompt_shields() function from the start.

         ROLLBACK:
         - Remove WEAPONS_PATTERNS, check_weapons(), and its call
           in check_input().
         - Remove check_prompt_shields() and its call in
           check_input(); Layers 3B+4 alone are safe to run as
           before (this was existing behaviour).
         - Revert check_azure_content_safety()'s executor block to
           `with concurrent.futures.ThreadPoolExecutor(...) as
           executor:` (re-introduces the shutdown-blocking bug —
           not recommended).

# ─────────────────────────────────────────────────────────────
# TODO: PRODUCTION READINESS
# Before go-live replace/enhance the following:
#
# Layer 1 - Relevance Check:
#      Current  → keyword matching (may over-block edge cases)
#      Enhance  → Use gpt-4o-mini for smarter relevance scoring
#
# Layer 2B - Weapons/Explosives:
#      Current  → regex patterns (v1.1.0 — good but not exhaustive)
#      Enhance  → Consider Azure Content Safety custom categories
#                 if the regex list shows gaps in production.
#
# Layer 3A - Prompt Shields:
#      Current  → LIVE as of v1.1.0, using Foundry multi-service
#                 endpoint. Regex Layers 3B+4 remain as fallback.
#      Ensure   → Foundry endpoint RBAC (Cognitive Services User)
#                 stays granted; monitor prompt_shields_timeout /
#                 prompt_shields_failed logs for reachability drift.
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
import requests
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

# Prompt Shields uses the same Content Safety resource/endpoint
# as Layer 5 (analyze_text) — different API path on the same
# multi-service Foundry endpoint. Confirmed working via test3.py.
PROMPT_SHIELDS_API_VERSION = "2024-09-01"

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

# ── Layer 2B: Weapons/Explosives (v1.1.0 — NEW) ───────────────
# Covers explosives, firearms manufacturing, and chemical/
# biological weapon construction language. Deliberately broad
# on "how to make/build X" + weapon-noun combinations — false
# positives here (blocking a legitimate weapons question, which
# has no business being asked of an insurance chatbot anyway)
# are far cheaper than false negatives on this category.
WEAPONS_PATTERNS = [
    r"how (to|do i|can i) (make|build|create|construct).{0,30}"
    r"(bomb|explosive|detonat|grenade|molotov)",
    r"\bbomb\b.{0,20}(instruction|recipe|make|build|how to)",
    r"(make|build|create|construct).{0,20}"
    r"(explosive|detonator|ied|pipe bomb)",
    r"\b(tnt|c4|semtex|nitroglycerin)\b",
    r"how (to|do i|can i) (make|build|3d print|convert).{0,30}"
    r"(gun|firearm|rifle|pistol).{0,20}(untraceable|illegal|silencer|automatic)",
    r"(build|make|create).{0,20}(chemical|biological|nerve).{0,20}"
    r"(weapon|agent|toxin)",
    r"how (to|do i).{0,20}(synthesi[sz]e|make).{0,20}"
    r"(sarin|ricin|anthrax|nerve gas)",
    r"(untraceable|homemade|improvised).{0,20}"
    r"(weapon|firearm|explosive|bomb)",
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


def check_weapons(text: str) -> tuple[bool, str | None]:
    """
    Layer 2B (v1.1.0): Detect weapons/explosives construction
    language. No API call — regex only, always active regardless
    of Content Safety/Prompt Shields reachability.
    """
    text_lower = text.lower()

    for pattern in WEAPONS_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            log.warning("weapons_content_detected", pattern=pattern)
            return False, "harmful"

    return True, None


def check_prompt_injection(text: str) -> tuple[bool, str | None]:
    """Layer 3B: Detect prompt injection attempts (regex fallback)."""
    text_lower = text.lower()

    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            log.warning(
                "prompt_injection_detected", pattern=pattern
            )
            return False, "harmful"

    return True, None


def check_jailbreak(text: str) -> tuple[bool, str | None]:
    """Layer 4: Detect jailbreak attempts (regex fallback)."""
    text_lower = text.lower()

    for pattern in JAILBREAK_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            log.warning("jailbreak_detected", pattern=pattern)
            return False, "harmful"

    return True, None


def check_prompt_shields(text: str) -> tuple[bool | None, str | None]:
    """
    Layer 3A (v1.1.0): Azure Prompt Shields — ML-based jailbreak
    and prompt injection detection. Covers what Layers 3B+4 regex
    inevitably miss (novel phrasing, no keyword match).

    Uses the same Foundry multi-service endpoint as Layer 5
    (analyze_text) — different API path (shieldPrompt), confirmed
    working via test3.py verification.

    Returns THREE possible states (note: bool | None, not bool):
        (False, "harmful") → attack detected — block immediately
        (True, None)       → ML confirmed clean — caller should
                              skip regex Layers 3B+4 entirely
        (None, None)       → endpoint not configured, unreachable,
                              or errored — caller falls through to
                              regex Layers 3B+4 as normal fallback

    Fails open to (None, None) on any error — never blocks a
    user due to Prompt Shields being unavailable; regex layers
    provide the safety net.
    """
    if not SAFETY_ENDPOINT:
        return None, None

    if not text or not text.strip():
        return True, None

    try:
        credential = get_credential()
        token = credential.get_token(
            "https://cognitiveservices.azure.com/.default"
        ).token

        url = (
            f"{SAFETY_ENDPOINT.rstrip('/')}"
            f"/contentsafety/text:shieldPrompt"
            f"?api-version={PROMPT_SHIELDS_API_VERSION}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {
            "userPrompt": text[:10000],
            "documents": [],
        }

        # Same Python-level hard timeout pattern as Layer 5 — and
        # the SAME fix (shutdown(wait=False)) applied from the
        # start, unlike the original check_azure_content_safety()
        # which had this bug until v1.1.0.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            requests.post, url, headers=headers, json=body, timeout=10
        )
        try:
            response = future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            log.warning("prompt_shields_timeout", timeout_seconds=10)
            return None, None
        executor.shutdown(wait=False)

        if response.status_code != 200:
            log.warning(
                "prompt_shields_error",
                status=response.status_code,
            )
            return None, None

        result = response.json()
        attack = result.get("userPromptAnalysis", {}).get(
            "attackDetected", False
        )
        if attack:
            log.warning("prompt_shields_attack_detected")
            return False, "harmful"

        return True, None

    except Exception as e:
        log.warning("prompt_shields_failed", error=str(e))
        return None, None


def check_azure_content_safety(
    text: str,
) -> tuple[bool, str | None]:
    """
    Layer 5: Azure Content Safety.
    Uses singleton client with DefaultAzureCredential.
    Fails open on error — never blocks user on API failure.

    Timeout: 10s hard cap (v1.1.0 — fixed: executor.shutdown(
    wait=False) after both success and timeout paths, replacing
    the `with ... as executor:` pattern whose __exit__ was
    silently blocking on the abandoned thread and making the
    "hard cap" not actually hard — confirmed live at 15s logged
    timeout but 30-129s actual latency before this fix).
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

        # Python-level hard timeout — SDK timeout params are
        # ineffective when Azure backend returns errors slowly
        # (~30s). shutdown(wait=False) — not a `with` block — is
        # what actually enforces the 10s cap (v1.1.0 fix).
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(client.analyze_text, request)
        try:
            response = future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            log.warning("content_safety_timeout", timeout_seconds=10)
            return True, None
        executor.shutdown(wait=False)

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

    Layer order (v1.1.0):
    1.  Relevance (regex, no API)
    2.  Crime/Fraud (regex, no API)
    2B. Weapons/Explosives (regex, no API)
    3A. Prompt Shields (Azure ML) — skips 3B+4 if it returns a
        decisive True; falls through to 3B+4 if unreachable/error
    3B. Prompt Injection (regex fallback)
    4.  Jailbreak (regex fallback)
    5.  Azure Content Safety (API call — singleton client)
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

    # Layer 2B: Weapons/Explosives (no API call) — v1.1.0
    safe, reason = check_weapons(text)
    if not safe:
        return False, reason

    # Layer 3A: Prompt Shields (ML-based, covers 3B+4) — v1.1.0
    shields_safe, shields_reason = check_prompt_shields(text)
    if shields_safe is False:
        return False, shields_reason  # ML-confirmed attack — block
    if shields_safe is True:
        # ML confirmed clean — skip regex Layers 3B+4 entirely
        pass
    else:
        # shields_safe is None — endpoint unreachable/error,
        # fall through to regex Layers 3B+4 as normal
        # Layer 3B: Prompt Injection (no API call)
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