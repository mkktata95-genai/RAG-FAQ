"""
test_presidio_pii.py — verifies Presidio NER is live and catching
contextual PII (names, addresses) that regex structurally cannot.

Run from project root (where core/ package lives):
    python test_presidio_pii.py

Checks, in order:
  1. Presidio import + model load succeeded (not silently failed open)
  2. Regex-only cases (existing behaviour, should be unaffected)
  3. Contextual PII cases (names/addresses — the actual new coverage)
  4. False-positive cases (should NOT block — legitimate product/
     company names, generic insurance questions)
  5. Mixed regex + Presidio in the same query
"""

import sys

from core.safety import (
    _PRESIDIO_AVAILABLE,
    check_pii,
    detect_pii,
    detect_presidio_entities,
    get_presidio_analyzer,
)


def section(title: str):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def run_case(text: str, expect_block: bool, label: str) -> bool:
    safe, reason = check_pii(text)
    blocked = not safe
    regex_hits = [t for t in detect_pii(text) if t in
                  {"policy_number", "ni_number", "email", "phone", "sort_code"}]
    presidio_hits = detect_presidio_entities(text)

    status = "PASS" if blocked == expect_block else "FAIL"
    print(
        f"{status} | blocked={blocked!s:5} expected={expect_block!s:5} "
        f"| regex={regex_hits} presidio={presidio_hits} | {label}"
    )
    return status == "PASS"


def main():
    section("STEP 0: Presidio availability check")
    print(f"_PRESIDIO_AVAILABLE = {_PRESIDIO_AVAILABLE}")
    if not _PRESIDIO_AVAILABLE:
        print(
            "\n⚠️  Presidio is NOT installed / import failed.\n"
            "   Run: pip install presidio-analyzer\n"
            "        python -m spacy download en_core_web_lg\n"
            "   All Presidio-dependent cases below will show "
            "presidio=[] and effectively test regex-only.\n"
        )
    else:
        try:
            analyzer = get_presidio_analyzer()
            print(f"Analyzer loaded: {analyzer is not None}")
        except Exception as e:
            print(f"⚠️  Analyzer failed to load: {e}")
            print("   Check en_core_web_lg is downloaded.")

    results = []

    section("STEP 1: Regex-only cases (should be unaffected)")
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
        "I live at 42 Baker Street, London, is my postcode covered", True,
        "address in free text — Presidio LOCATION"))
    results.append(run_case(
        "can you update the beneficiary to Sarah Johnson please", True,
        "name as beneficiary update — Presidio PERSON"))
    results.append(run_case(
        "my adviser John Williams recommended this policy", True,
        "name mid-sentence, no structural marker"))

    section("STEP 3: False positives — should NOT block")
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

    section("STEP 4: Mixed regex + Presidio in same query")
    results.append(run_case(
        "my policy RL123456 is under Robert Smith, is that correct", True,
        "both regex (policy#) and Presidio (name) should fire — "
        "either alone is sufficient to block"))

    section("STEP 5: Edge cases")
    results.append(run_case("", False, "empty string"))
    results.append(run_case("   ", False, "whitespace only"))
    results.append(run_case(
        "12345678 what does that mean", False,
        "8-digit number — account_number excluded from BLOCK_PII_TYPES "
        "by design, should NOT block"))

    section("SUMMARY")
    passed = sum(results)
    total = len(results)
    print(f"{passed}/{total} passed")
    if passed != total:
        print("Review FAIL lines above — check score_threshold in "
              "PRESIDIO_SCORE_THRESHOLD (core/safety.py) if Presidio "
              "cases are under/over-triggering.")
        sys.exit(1)


if __name__ == "__main__":
    main()