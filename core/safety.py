"""
Multi-layer safety system for Royal London FAQ chatbot.
Layer 1:  Relevance check
Layer 1B: PII detection (regex + Presidio NER) — v1.2.0
Layer 2:  Crime/fraud detection
Layer 2B: Weapons/explosives detection
Layer 3A: Prompt Shields (Azure ML — jailbreak + injection)
Layer 3B: Prompt injection detection (regex fallback)
Layer 4:  Jailbreak detection (regex fallback)
Layer 5:  Azure Content Safety (violence/hate/sexual/self-harm)

Migration: AzureKeyCredential → DefaultAzureCredential
Auth:       No API key required

BUG #6/#7 FIX (July 2026, Mukesh Kund):
- #6 JAILBREAK_PATTERNS "i am admin" (no \b) matched as a substring
  inside "i am administrator" — false-positive on legitimate scheme
  administrators. Added trailing \b.
- #7 INJECTION_PATTERNS reveal/show/tell-me pattern included "rules"
  and "guidelines" as unqualified targets — matched real product
  queries like "tell me your rules on ISA transfers". Split into two
  patterns: prompt/instructions/training stay broadly matched
  (unambiguous meta-request), rules/guidelines now require an
  explicit "your system" qualifier to fire.
ROLLBACK: restore the single combined pattern with unqualified
rules/guidelines, and drop the \b on the "i am admin" alternation.

BUG #26 FIX (July 2026, Mukesh Kund): "prime minister" in
IRRELEVANT_KEYWORDS didn't catch the "PM" abbreviation; same gap
for "MP". Added IRRELEVANT_PATTERNS (regex, not plain substring)
for both — "pm" collides with time-of-day ("3pm"), "mp" collides
with "employment"/"unemployment"/"self-employment" (all common in
this domain). Also broadened IRRELEVANT_KEYWORDS with common
abbreviations/slang for existing categories (sports leagues/
abbreviations, streaming services, social media, crypto, consoles,
etc.) — each checked individually against domain phrases
("premium", "gig economy income protection", "self-employment",
"contact method", "3pm"/"2pm") before inclusion; none collide.
This is a performance optimisation (avoids paying for the gpt-5-nano
classify_intent() call on common cases), not a coverage fix —
gate 2 (classify_intent) already catches anything gate 1 misses.
ROLLBACK: remove IRRELEVANT_PATTERNS and its check_relevance()
call site; revert IRRELEVANT_KEYWORDS to the pre-v1.x list.

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

v1.2.0 — August 2026 | Mukesh Kund
         Layer 1B: PII Detection [NEW] — blocks queries containing
         customer PII before they reach cache_check (which
         generates an embedding unconditionally at its Step 1,
         before even the needs_empathy skip check — the only
         layer position that guarantees PII never reaches
         embeddings, cache, or the LLM).
         - PII_PATTERNS/PII_REPLACEMENTS [MOVED from middleware.py]:
           single source of truth here now; middleware.py imports
           them for log-masking instead of duplicating.
         - detect_pii() [MOVED from middleware.py, was dead code
           there — zero callers]: regex detection, all 8 types.
         - detect_presidio_entities() [NEW]: Presidio NER for
           contextual PII (names, addresses) regex structurally
           cannot catch. Optional dependency — fails open to
           regex-only if presidio-analyzer isn't installed (no
           crash, same posture as Prompt Shields/Content Safety).
         - check_pii() [NEW]: Layer 1B. BLOCK_PII_TYPES restricts
           hard-blocking to high-confidence regex types (policy#,
           NI#, email, phone, sort code) — account_number (any
           8-digit number) and date_of_birth stay mask-only, too
           broad to block on without false-positive risk.
         - Wired into check_input() as Layer 1B, immediately after
           Layer 1 (relevance) — earliest possible position, so a
           block also skips Prompt Shields (3A)/Content Safety (5)
           API cost.
         - refusal.py: new RefusalReason.PII_DETECTED, no phone
           number (matches file convention).
         - input_safety.py: new elif branch, logs query_pii_detected
           with no query text at all (nothing to leak).
         ROLLBACK: remove check_pii(), detect_pii(),
         detect_presidio_entities(), PII_PATTERNS, PII_REPLACEMENTS,
         BLOCK_PII_TYPES from this file and its call in
         check_input(); restore local PII_PATTERNS/PII_REPLACEMENTS/
         detect_pii() in middleware.py; remove PII_DETECTED from
         refusal.py; remove the elif branch in input_safety.py.

v1.3.0 — August 2026 | Mukesh Kund
         BUG FIX — Presidio LOCATION false positive on "London":
         - ROOT CAUSE: PRESIDIO_ENTITIES included LOCATION at
           launch. Confirmed live (VDI testing, test_presidio_
           standalone.py) that Presidio flags "London" as LOCATION
           at both 0.6 AND 0.85 confidence regardless of context —
           "what does the London stock exchange do" blocked at
           both thresholds. Not a threshold-tuning problem; a
           category problem. Unusable as a blocking signal for an
           insurer named Royal London — would false-positive on a
           large share of ordinary traffic.
         - FIX: PRESIDIO_ENTITIES reduced to ["PERSON"] only.
           PERSON alone still covers every contextual-PII case
           regex couldn't (names in free text) — confirmed via all
           4 name-detection test cases still passing.
         - Structured addresses (the coverage LOCATION was
           partially providing) now caught via the existing
           postcode regex pattern, added to BLOCK_PII_TYPES.
         - PRESIDIO_SCORE_THRESHOLD raised 0.6 → 0.85 (kept from
           the failed tuning attempt — doesn't hurt PERSON-only
           accuracy, no reason to revert).
         ROLLBACK: PRESIDIO_ENTITIES = ["PERSON", "LOCATION"];
         remove "postcode" from BLOCK_PII_TYPES; PRESIDIO_SCORE_
         THRESHOLD = 0.6 (not recommended — reintroduces the
         false-positive).

v1.4.0 — August 2026 | Mukesh Kund
         Production-hardening pass on Layer 1B (PII), found via
         strict edge-case review before go-live sign-off. Four
         changes, all in this file only:

         FIX 1 — Presidio timeout [GAP]:
         - ROOT CAUSE: check_prompt_shields() and
           check_azure_content_safety() both wrap their calls in
           ThreadPoolExecutor with a hard 10s timeout (see v1.1.0
           FIX 3). detect_presidio_entities() had none — a
           synchronous call with nothing bounding it. Since Layer
           1B now runs first (see FIX 3 below), an unbounded call
           here would stall every request that reaches it, not
           just PII cases.
         - FIX: same ThreadPoolExecutor pattern, PRESIDIO_TIMEOUT_
           SECONDS = 10, executor.shutdown(wait=False) on both
           success and timeout paths (same FIX-3 shutdown-blocking
           avoidance as v1.1.0).

         FIX 2 — Singleton race under concurrency [GAP]:
         - ROOT CAUSE: get_presidio_analyzer()'s lazy init had no
           lock. Fine under VDI single-threaded testing; under real
           ACA production concurrency, simultaneous first-requests
           could each construct their own AnalyzerEngine() before
           the global was set — not corrupting (GIL-safe
           assignment) but wasteful (duplicate multi-second model
           loads under load).
         - FIX: double-checked locking with threading.Lock().
           Written for production concurrency assumptions, not
           validated only against single-user VDI testing.

         FIX 3 — Execution order: PII before Relevance [DECISION]:
         - ROOT CAUSE: Layer 1 (Relevance) ran before Layer 1B
           (PII). A message containing PII that ALSO trips an
           irrelevant-keyword match (e.g. PII plus an off-topic
           aside in the same message) was blocked and logged with
           reason "irrelevant" — Layer 1B never ran. Still safe
           (blocked before embeddings either way) but undercounts
           true PII incidents in the FCA audit trail.
         - DECISION: PII is a security/compliance event and must
           be the logged reason whenever present, regardless of
           what else accompanies it. check_pii() now runs before
           check_relevance() in check_input(). Layer label kept as
           "1B" (not renumbered) — only call order changed, to
           minimise diff against existing comments/tests
           referencing "Layer 1B".

         NOTED, not yet fixed — needs dedicated test coverage
         before production sign-off, tracked here rather than only
         in chat history:
         - Presidio PERSON entity risk: words that are simultaneously
           common domain vocabulary and person names — "Will" (legal
           document, core to probate/beneficiary domain), "May"
           (calendar month), "Grace" ("grace period" is an actual
           insurance term). spaCy weighs capitalization heavily;
           sentence-initial capitalized instances are genuinely
           ambiguous. None of these were in the original test set.
         - postcode regex (BLOCK_PII_TYPES, v1.3.0) was validated
           for true positives only, not stress-tested for false
           positives against fund codes/product references — lower
           risk than LOCATION was (bounded match length via \\b
           anchors) but not yet confirmed clean.

         ROLLBACK:
         - FIX 1: remove ThreadPoolExecutor wrapper in
           detect_presidio_entities(), revert to direct
           analyzer.analyze() call (not recommended).
         - FIX 2: remove _presidio_lock and the `with` block in
           get_presidio_analyzer(), revert to unlocked lazy init
           (not recommended for production).
         - FIX 3: swap check_pii()/check_relevance() call order
           back in check_input() (not recommended — reintroduces
           audit undercounting).

v1.5.0 — August 2026 | Mukesh Kund
         VALIDATION — Presidio live on VDI (presidio-analyzer +
         en_core_web_lg installed), full run via
         test_presidio_standalone.py:
         - Hard cases (regex + Presidio true/false positives,
           mixed, edge cases): 15/15 passed.
         - Known-risk cases (v1.4.0 NOTED item — "Will"/"May"/
           "Grace" domain-vocabulary ambiguity): 0/6 triggered a
           block at current settings (PRESIDIO_ENTITIES=["PERSON"],
           PRESIDIO_SCORE_THRESHOLD=0.85). Closes that open item —
           not a theoretical risk anymore, tested and clean as of
           this run. Re-run this test if PRESIDIO_SCORE_THRESHOLD
           changes, the spaCy model version changes, or query
           patterns drift meaningfully from what's in the test set.
         - postcode regex false-positive stress testing (the other
           v1.4.0 NOTED item — fund codes/product references) was
           NOT part of this run — still outstanding.
         No code changes in this entry — validation record only.

# ─────────────────────────────────────────────────────────────
# TODO: PRODUCTION READINESS
# Before go-live replace/enhance the following:
#
# Layer 1 - Relevance Check:
#      Current  → keyword matching (may over-block edge cases)
#      Enhance  → Use gpt-4o-mini for smarter relevance scoring
#
# Layer 1B - PII Detection:
#      Current  → LIVE as of v1.2.0/v1.3.0. Regex (policy#, NI#,
#                 email, phone, sort code, postcode) always on,
#                 no dependency. Presidio NER (PERSON only, since
#                 v1.3.0) is an OPTIONAL dependency — fails open
#                 to regex-only if presidio-analyzer isn't
#                 installed, meaning name-in-free-text PII is NOT
#                 caught until this is resolved.
#      BLOCKER  → Presidio's NER model (en_core_web_lg) normally
#                 downloads from spaCy's CDN at runtime — conflicts
#                 with RLG's no-external-pull, Azure-contained
#                 posture (same constraint as Playwright Chromium).
#                 Needs the model vendored/baked into the container
#                 image at build time, not pulled in ACA at runtime.
#                 Get RLG security sign-off before relying on
#                 Presidio coverage in production; until then only
#                 the regex half is a guaranteed control.
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
import threading
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


# ── Layer 1B: PII Detection (v1.2.0 — NEW) ────────────────────
# Blocks queries containing customer PII BEFORE cache_check —
# cache_check_node generates an embedding unconditionally at its
# Step 1 (before even the needs_empathy skip check), so this is
# the only layer position that guarantees PII never reaches
# embeddings, cache, or the LLM. Runs early (Layer 1B) so a block
# also skips the cost of Prompt Shields (3A) and Content Safety
# (5) API calls.
#
# Single source of truth for PII patterns — middleware.py imports
# PII_PATTERNS/PII_REPLACEMENTS from here for log-masking. Two
# consumers, one detector:
#   - check_pii() (below)            → blocks the request
#   - mask_pii_for_logging() (middleware.py) → masks for logs only,
#     defense-in-depth for anything that still reaches a log line
#
# Full pattern set (all 8 types) is used for MASKING. Only a
# subset is used for BLOCKING — account_number (\d{8}) and
# date_of_birth are too broad as hard-block triggers (any 8-digit
# number, any date-shaped string) and would false-positive on
# ordinary questions. Masking them in logs is still correct;
# blocking on them is not.
PII_PATTERNS = {
    "policy_number":  r"\b(RL|rl)\d{6,10}\b",
    "ni_number":      r"\b[A-Z]{2}\d{6}[A-D]\b",
    "date_of_birth":  r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
    "email":          r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    "phone":          r"\b(\+44|0)\d{9,10}\b",
    "sort_code":      r"\b\d{2}-\d{2}-\d{2}\b",
    "account_number": r"\b\d{8}\b",
    "postcode":       r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b",
}

PII_REPLACEMENTS = {
    "policy_number":  "[POLICY_NUMBER]",
    "ni_number":      "[NI_NUMBER]",
    "date_of_birth":  "[DATE]",
    "email":          "[EMAIL]",
    "phone":          "[PHONE]",
    "sort_code":      "[SORT_CODE]",
    "account_number": "[ACCOUNT_NUMBER]",
    "postcode":       "[POSTCODE]",
}

# High-confidence types only — safe to hard-block on.
# v1.3.0: postcode added — replaces LOCATION as the address
# signal (see PRESIDIO_ENTITIES note below).
BLOCK_PII_TYPES = {
    "policy_number", "ni_number", "email", "phone", "sort_code",
    "postcode",
}

# Presidio NER — catches contextual PII regex structurally cannot
# (names in free text). Optional dependency: if presidio-analyzer
# isn't installed (e.g. pending security review for offline model
# hosting), fails open to regex-only — never crashes the pipeline.
_presidio_analyzer = None
_presidio_lock = threading.Lock()
_PRESIDIO_AVAILABLE = True
try:
    from presidio_analyzer import AnalyzerEngine
except ImportError:
    _PRESIDIO_AVAILABLE = False

# v1.3.0: LOCATION dropped — confirmed live that Presidio flags
# "London" as LOCATION at >=0.85 confidence regardless of context
# ("London stock exchange" blocked). Not a threshold problem, a
# category problem: unusable for an insurer named Royal London.
# PERSON alone still covers every contextual-PII gap regex can't
# (names in free text). Addresses now caught via postcode regex
# (BLOCK_PII_TYPES above) instead of NER.
PRESIDIO_ENTITIES = ["PERSON"]
PRESIDIO_SCORE_THRESHOLD = 0.85

# v1.4.0: hard timeout for Presidio inference — same 10s cap as
# Prompt Shields (3A) / Content Safety (5). Layer 1B runs earliest
# (before those two), so an unbounded call here would have stalled
# every request that reaches it, not just PII cases.
PRESIDIO_TIMEOUT_SECONDS = 10


def get_presidio_analyzer():
    """
    Singleton AnalyzerEngine — model loaded once, not per-request.

    v1.4.0: double-checked locking. Original lazy-init had a race
    under concurrent requests (fine on single-threaded VDI testing,
    real under ACA production concurrency) — multiple simultaneous
    first-requests could each spawn their own AnalyzerEngine()
    before the global was set. Not corrupting (GIL-safe assignment)
    but wasteful (duplicate multi-second model loads). Lock ensures
    only one thread ever constructs it.
    """
    global _presidio_analyzer
    if _presidio_analyzer is None and _PRESIDIO_AVAILABLE:
        with _presidio_lock:
            if _presidio_analyzer is None:
                _presidio_analyzer = AnalyzerEngine()
                log.info("presidio_analyzer_loaded")
    return _presidio_analyzer


def detect_pii(text: str) -> list[str]:
    """
    Detect PII types present in text via regex (all 8 types).
    Used for masking (middleware.py) and as the regex half of
    check_pii()'s blocking decision.
    """
    found = []
    for pii_type, pattern in PII_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            found.append(pii_type)
    return found


def detect_presidio_entities(text: str) -> list[str]:
    """
    Detect contextual PII (names) via Presidio NER.
    Local model inference — no network call, but still unbounded
    without an explicit cap (v1.4.0: added hard timeout, same
    ThreadPoolExecutor pattern as check_prompt_shields()/
    check_azure_content_safety(), since a slow/adversarially long
    query would otherwise stall this layer indefinitely — Layer 1B
    runs before Prompt Shields/Content Safety, so it would delay
    every request that reaches it, not just PII cases). Fails open
    (returns []) on timeout, any other error, or if
    presidio-analyzer isn't installed — regex remains the
    always-on backstop regardless of this layer's availability.
    """
    if not _PRESIDIO_AVAILABLE or not text or not text.strip():
        return []

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        analyzer = get_presidio_analyzer()
        future = executor.submit(
            analyzer.analyze,
            text=text,
            entities=PRESIDIO_ENTITIES,
            language="en",
            score_threshold=PRESIDIO_SCORE_THRESHOLD,
        )
        results = future.result(timeout=PRESIDIO_TIMEOUT_SECONDS)
        return list({r.entity_type for r in results})
    except concurrent.futures.TimeoutError:
        log.warning(
            "presidio_timeout", timeout=PRESIDIO_TIMEOUT_SECONDS
        )
        return []
    except Exception as e:
        log.warning("presidio_detection_failed", error=str(e))
        return []
    finally:
        # FIX 3 pattern (v1.1.0): shutdown(wait=False) — don't
        # block on an abandoned thread after a timeout.
        executor.shutdown(wait=False)


def check_pii(text: str) -> tuple[bool, str | None]:
    """
    Layer 1B: Block queries containing customer PII.

    Regex (BLOCK_PII_TYPES subset — policy#, NI#, email, phone,
    sort code, postcode) always runs, no dependency. Presidio NER
    (names) adds contextual coverage when available. Either hit →
    block. Never sends the raw query onward to embeddings/cache/
    LLM.
    """
    if not text or not text.strip():
        return True, None

    regex_hits = [
        t for t in detect_pii(text) if t in BLOCK_PII_TYPES
    ]
    presidio_hits = detect_presidio_entities(text)

    if regex_hits or presidio_hits:
        log.warning(
            "pii_detected",
            regex_types=regex_hits,
            presidio_types=presidio_hits,
        )
        return False, "pii"

    return True, None


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
    "sport", "football", "footy", "footie",
    "cricket", "rugby", "tennis",
    "nba", "nfl", "mlb", "formula 1", "grand prix",
    "movie", "film", "flick", "music", "song", "concert",
    "live gig", "gig tickets",
    "politics", "election", "party", "government",
    "brexit", "senate", "congress", "parliament",
    "celebrity", "gossip", "entertainment",
    "dating", "relationship", "romance",
    "travel", "holiday", "visa", "passport",
    "gaming", "video game", "console",
    "xbox", "playstation", "nintendo",
    "fashion", "clothes", "shopping",
    "joke", "funny", "meme",
    "quiz", "trivia", "horoscope", "zodiac",
    "lottery", "lotto",
    "capital of", "prime minister", "president",
    "cryptocurrency", "bitcoin", "ethereum", "btc", "nft",
    "smartphone", "phone", "laptop", "technology",
    "netflix", "spotify", "youtube",
    "instagram", "tiktok", "twitter", "facebook",
    "pasta", "pizza", "restaurant",
    "premier league", "champions league",
]

# BUG #26 FIX (July 2026, Mukesh Kund): "prime minister"/"president"
# above didn't catch common abbreviations ("PM", "MP"). Cannot add
# these bare to IRRELEVANT_KEYWORDS — that list is plain-substring
# matched, and both collide badly with ordinary insurance-domain
# words: "pm" is a substring of time references ("3pm", "2pm
# meeting") and words like "topmost"; "mp" is a substring of
# "employment", "unemployment", "self-employment", "employer" —
# all common in this domain (income protection, workplace pension
# queries). Regex with explicit context instead. This is a
# performance optimisation, not a coverage fix — classify_intent()
# (gate 2, gpt-5-nano) already catches anything gate 1 misses; gate
# 1 just avoids paying the LLM-call cost/latency for common cases.
IRRELEVANT_PATTERNS = [
    r"\bpm\s+of\s+(the\s+)?\w+",       # "PM of UK", "pm of the uk"
    r"\bwho\s+is\s+the\s+pm\b",        # "who is the PM"
    r"\bwho\s+is\s+(my|the)\s+mp\b",   # "who is my MP"
    r"\bmp\s+for\s+my\s+\w+",          # "MP for my constituency"
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
    r"(reveal|show|tell me|expose|print|output|display)\s+(your\s+)?(system\s+)?(prompt|instructions|training)",
    r"(reveal|show|tell me|expose|print|output|display)\s+your\s+system\s+(rules|guidelines)",
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
    r"i am (anthropic|microsoft|openai|developer|admin|engineer)\b",
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

    # BUG #26 FIX: regex-based checks for topics that plain substring
    # matching would be unsafe for (see IRRELEVANT_PATTERNS above).
    for pattern in IRRELEVANT_PATTERNS:
        if re.search(pattern, text_lower):
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

    Layer order (v1.4.0):
    1B. PII Detection (regex + Presidio, no network call) — v1.2.0.
        Runs FIRST as of v1.4.0 (was after Relevance in v1.2.0/
        v1.3.0). A message containing PII is a security/compliance
        event regardless of what else it contains — if it also
        happens to trip Layer 1's irrelevant-keyword check, PII
        must still be the reason logged for accurate FCA audit
        trail, not silently absorbed into "irrelevant". Layer label
        kept as "1B" (not renumbered to "1") to avoid churn across
        existing comments/changelog referring to it by that name —
        only the execution order changed.
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

    # Layer 1B: PII Detection (regex, no dependency; Presidio NER,
    # no network call but has its own 10s timeout — v1.4.0) — must
    # run before cache_check (embeds unconditionally at its Step 1)
    # AND before Layer 1 (v1.4.0 reorder, see docstring above).
    safe, reason = check_pii(text)
    if not safe:
        return False, reason

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