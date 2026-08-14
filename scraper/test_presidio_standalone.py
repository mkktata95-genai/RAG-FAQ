"""
test_presidio_standalone.py — verifies Presidio + regex PII detection
in isolation. No dependency on core/safety.py — safe to run on the
VDI right after `pip install presidio-analyzer` + spaCy model download,
before any wiring into the actual pipeline.

Run:
    python test_presidio_standalone.py
"""

import re
import sys

# ── Same regex set as safety.py (copied, not imported) ─────────
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

# v1.3.0: postcode added — replaces LOCATION as the address signal
# (see PRESIDIO_ENTITIES note below)
BLOCK_PII_TYPES = {
    "policy_number", "ni_number", "email", "phone", "sort_code", "postcode",
}

# v1.3.0: LOCATION dropped. Presidio flags "London" as LOCATION at
# both 0.6 AND 0.85 confidence regardless of context — confirmed on
# VDI this isn't a threshold problem, it's a category problem.
# Unusable as a blocking signal for an insurer named Royal London.
# PERSON alone still covers every contextual-PII case regex
# couldn't (names in free text). Structured addresses are now
# caught via the postcode regex pattern (added to BLOCK_PII_TYPES
# above) instead of NER.
PRESIDIO_ENTITIES = ["PERSON"]
PRESIDIO_SCORE_THRESHOLD = 0.85


def detect_regex(text: str) -> list[str]:
    found = []
    for pii_type, pattern in PII_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            found.append(pii_type)
    return found


# ── Presidio setup (isolated, fails loud here — this script's
# whole point is to tell you clearly if it's NOT working, unlike
# safety.py which fails open silently by design) ────────────────
_PRESIDIO_AVAILABLE = True
_PRESIDIO_ERROR = None
analyzer = None

try:
    from presidio_analyzer import AnalyzerEngine
    analyzer = AnalyzerEngine()
except ImportError as e:
    _PRESIDIO_AVAILABLE = False
    _PRESIDIO_ERROR = f"presidio-analyzer not installed: {e}"
except Exception as e:
    _PRESIDIO_AVAILABLE = False
    _PRESIDIO_ERROR = f"AnalyzerEngine failed to load (check spaCy model): {e}"


def detect_presidio(text: str) -> list[str]:
    if not _PRESIDIO_AVAILABLE or not text.strip():
        return []
    try:
        results = analyzer.analyze(
            text=text,
            entities=PRESIDIO_ENTITIES,
            language="en",
            score_threshold=PRESIDIO_SCORE_THRESHOLD,
        )
        return list({r.entity_type for r in results})
    except Exception as e:
        print(f"  ⚠️  presidio runtime error on this text: {e}")
        return []


def check_pii(text: str) -> tuple[bool, list[str], list[str]]:
    """Returns (blocked, regex_hits, presidio_hits)."""
    if not text or not text.strip():
        return False, [], []
    regex_hits = [t for t in detect_regex(text) if t in BLOCK_PII_TYPES]
    presidio_hits = detect_presidio(text)
    return bool(regex_hits or presidio_hits), regex_hits, presidio_hits


def run_known_risk_case(text: str, label: str) -> bool:
    """
    For cases where a block is a KNOWN spaCy ambiguity, not a code
    bug — e.g. capitalized "Will"/"May"/"Grace" doubling as domain
    terms. Reports the outcome without asserting pass/fail, since
    "should this block?" here is a product decision (accept the
    false-positive risk vs. build an allowlist), not a regression.
    Returns True if it triggered (for the summary tally only).
    """
    blocked, regex_hits, presidio_hits = check_pii(text)
    flag = "⚠️  BLOCKED" if blocked else "   clear  "
    print(f"{flag} | presidio={presidio_hits} | {label} | text={text!r}")
    return blocked


def section(title: str):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def run_case(text: str, expect_block: bool, label: str) -> bool:
    blocked, regex_hits, presidio_hits = check_pii(text)
    status = "PASS" if blocked == expect_block else "FAIL"
    print(
        f"{status} | blocked={blocked!s:5} expected={expect_block!s:5} "
        f"| regex={regex_hits} presidio={presidio_hits} | {label}"
    )
    return status == "PASS"


def main():
    section("STEP 0: Presidio availability")
    print(f"_PRESIDIO_AVAILABLE = {_PRESIDIO_AVAILABLE}")
    if not _PRESIDIO_AVAILABLE:
        print(f"\n⚠️  {_PRESIDIO_ERROR}")
        print(
            "\n   Fix:\n"
            "     pip install presidio-analyzer\n"
            "     python -m spacy download en_core_web_lg\n"
            "\n   Continuing — Presidio cases below will show "
            "presidio=[] until fixed.\n"
        )
    else:
        print("Analyzer loaded OK.")

    results = []

    section("STEP 1: Regex-only cases (baseline, no Presidio needed)")
    results.append(run_case(
        "is RL123456 covered for critical illness", True,
        "policy number — regex block"))
    results.append(run_case(
        "my email is test@example.com, can you help", True,
        "email — regex block"))
    results.append(run_case(
        "what is critical illness cover", False,
        "clean insurance query — no block"))

    section("STEP 2: Contextual PII — Presidio should catch these")
    results.append(run_case(
        "my policy is under my late husband Robert Smith's name", True,
        "name in free text — regex CANNOT catch this, Presidio should"))
    results.append(run_case(
        "I live at 42 Baker Street, SW1A 1AA, is that covered", True,
        "postcode present — regex block (was Presidio LOCATION, "
        "now postcode regex per v1.3.0)"))
    results.append(run_case(
        "can you update the beneficiary to Sarah Johnson please", True,
        "name as beneficiary update — Presidio PERSON"))
    results.append(run_case(
        "my adviser John Williams recommended this policy", True,
        "name mid-sentence, no structural marker"))

    section(
        "STEP 3: Known ambiguity — domain words that are also "
        "names (v1.4.0 flagged risk, soft-fail — a block here is "
        "a product decision, not a code bug)"
    )
    known_risk = []
    known_risk.append(run_known_risk_case(
        "how do I update my will", "will (lowercase, legal doc sense)"))
    known_risk.append(run_known_risk_case(
        "How do I write a Will for my pension beneficiaries",
        "Will (capitalized, sentence-initial — highest ambiguity risk)"))
    known_risk.append(run_known_risk_case(
        "is there a grace period for missed payments",
        "grace period (lowercase, insurance term)"))
    known_risk.append(run_known_risk_case(
        "What is the Grace period on my policy",
        "Grace period (Title Case — ambiguity risk)"))
    known_risk.append(run_known_risk_case(
        "my premium payment is due in May",
        "May (calendar month, capitalized)"))
    known_risk.append(run_known_risk_case(
        "will my payment due in May affect my cover",
        "will + May combined — worst case of both risks"))

    section("STEP 4: False positives — should NOT block")
    results.append(run_case(
        "what is Royal London's income protection policy", False,
        "company name should not trigger PERSON/LOCATION"))
    results.append(run_case(
        "is critical illness cover included in my ISA", False,
        "generic product question, no entities"))
    results.append(run_case(
        "what does the London stock exchange do", False,
        "London as generic reference — check for over-triggering"))
    results.append(run_case(
        "how do I contact customer service", False,
        "no PII present"))

    section("STEP 5: Mixed regex + Presidio in same query")
    results.append(run_case(
        "my policy RL123456 is under Robert Smith, is that correct", True,
        "both regex (policy#) and Presidio (name) should fire — "
        "either alone is sufficient to block"))

    section("STEP 6: Edge cases")
    results.append(run_case("", False, "empty string"))
    results.append(run_case("   ", False, "whitespace only"))
    results.append(run_case(
        "12345678 what does that mean", False,
        "8-digit number — account_number excluded from BLOCK_PII_TYPES "
        "by design, should NOT block"))

    section("SUMMARY")
    passed = sum(results)
    total = len(results)
    print(f"Hard cases:  {passed}/{total} passed")

    risk_triggered = sum(known_risk)
    risk_total = len(known_risk)
    print(f"Known-risk cases (Step 3): {risk_triggered}/{risk_total} "
          f"blocked (⚠️  count)")
    if risk_triggered:
        print(
            "  → these are real, expected ambiguities (spaCy weighs "
            "capitalization heavily) — not bugs. Decide: accept the "
            "false-positive risk on 'Will'/'May'/'Grace'-shaped "
            "queries, or build a small allowlist/exception path for "
            "them before production sign-off."
        )

    if not _PRESIDIO_AVAILABLE:
        print(
            "\nNOTE: Presidio was unavailable during this run — STEP 2 "
            "failures and STEP 3 all-clear results are expected until "
            "the install is fixed, not a code bug."
        )
    if passed != total and _PRESIDIO_AVAILABLE:
        print(
            "\nReview FAIL lines above — if STEP 2 under-triggers or "
            "STEP 4 over-triggers, tune PRESIDIO_SCORE_THRESHOLD "
            f"(currently {PRESIDIO_SCORE_THRESHOLD}) in this script, "
            "then apply the same change in core/safety.py."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()