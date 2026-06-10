"""
Input Safety Node — checks query before RAG pipeline.
"""

import time
import structlog
from core.safety import check_input
from core.refusal import get_refusal, RefusalReason
from core.schemas import AgentState

log = structlog.get_logger()


def input_safety_node(state: AgentState) -> AgentState:
    """
    Check input query for safety and relevance.

    Refusal types:
    - irrelevant → OUT_OF_SCOPE (no contact number)
    - harmful    → HARMFUL (no contact number)
    - other      → GENERAL (with contact number)
    """
    start = time.time()

    try:
        is_safe, reason = check_input(state.query)
        latency = (time.time() - start) * 1000
        state.latency_ms["input_safety"] = latency

        if not is_safe:
            state.input_safe = False
            state.refusal_triggered = True
            state.refusal_reason = reason

            if reason == "irrelevant":
                # Out of scope — no contact number needed
                state.final_response = get_refusal(
                    RefusalReason.OUT_OF_SCOPE
                )
                log.info(
                    "query_out_of_scope",
                    query=state.query[:50],
                    latency_ms=round(latency),
                )
            elif reason == "harmful":
                # Harmful/unsafe — firm refusal, no contact
                state.final_response = get_refusal(
                    RefusalReason.HARMFUL
                )
                log.warning(
                    "query_harmful",
                    query=state.query[:50],
                    latency_ms=round(latency),
                )
            else:
                # Unknown reason — general refusal with contact
                state.final_response = get_refusal(
                    RefusalReason.GENERAL
                )
                log.warning(
                    "input_unsafe",
                    reason=reason,
                    latency_ms=round(latency),
                )
        else:
            state.input_safe = True
            log.info(
                "input_safe",
                latency_ms=round(latency),
            )

    except Exception as e:
        # Fail open — don't block user on safety error
        log.error("input_safety_error", error=str(e))
        state.input_safe = True

    return state