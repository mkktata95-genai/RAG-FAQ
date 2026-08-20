"""
metrics_operational.py — refusal detection and cost tracking.

Both are opt-in signals per case:
  - refused: taken from response_fn's explicit "refused" flag if given,
    else falls back to a generic phrase heuristic (best-effort only —
    tune GENERIC_REFUSAL_PHRASES for your domain, or better, have your
    response_fn set "refused" explicitly from your own gate/refusal
    logic, which is always more accurate than a phrase match).
  - cost: taken from response_fn's "cost_usd" if given, else computed
    from input_tokens/output_tokens * a pluggable price-per-token if
    supplied to run_evaluation(). If neither is available, cost is
    simply omitted (not faked as 0).

CHANGE LOG
v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations

from typing import Optional

# Generic, domain-agnostic fallback phrases. Deliberately conservative
# (only very unambiguous refusal language) to minimise false positives.
# Replace/extend with your own pipeline's actual refusal templates for
# a more accurate signal — see refusal.py for the RLG-specific wording,
# but note: don't import it here, this file stays framework-generic.
GENERIC_REFUSAL_PHRASES = [
    "i cannot help with that",
    "i can't help with that",
    "i'm unable to assist",
    "i am unable to assist",
    "i cannot provide",
    "i can't provide",
    "unable to answer that question",
    "outside what i can help with",
    "i'm not able to answer",
    "i am not able to answer",
]


def heuristic_is_refusal(answer_text: str) -> bool:
    if not answer_text.strip():
        return False
    lowered = answer_text.lower()
    return any(phrase in lowered for phrase in GENERIC_REFUSAL_PHRASES)


def resolve_refusal(explicit_flag: Optional[bool], answer_text: str) -> bool:
    """Explicit flag from response_fn always wins — it's always more
    accurate than a generic phrase match. Heuristic is fallback only."""
    if explicit_flag is not None:
        return bool(explicit_flag)
    return heuristic_is_refusal(answer_text)


def resolve_cost(
    explicit_cost_usd: Optional[float],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    price_per_1k_input: Optional[float] = None,
    price_per_1k_output: Optional[float] = None,
) -> Optional[float]:
    """Precedence: explicit cost_usd from response_fn > computed from
    tokens + price table > None (not tracked, don't fake a zero)."""
    if explicit_cost_usd is not None:
        return round(explicit_cost_usd, 6)
    if input_tokens is not None and output_tokens is not None \
            and price_per_1k_input is not None and price_per_1k_output is not None:
        cost = (input_tokens / 1000) * price_per_1k_input + (output_tokens / 1000) * price_per_1k_output
        return round(cost, 6)
    return None
