"""
Output Safety Node — checks LLM response before returning to user.
"""

import time
import structlog

from core.safety import check_output
from core.refusal import get_refusal, RefusalReason
from core.schemas import AgentState, Citation
from core.nodes.prompt_builder_node import CONTACT_CITATION

log = structlog.get_logger()


def output_safety_node(state: AgentState) -> AgentState:
    """
    Check LLM response for safety.
    If unsafe → trigger refusal.
    If safe → pass through to formatter.
    """
    start = time.time()

    try:
        if not state.raw_response:
            state.output_safe = True
            return state

        is_safe, reason = check_output(state.raw_response)

        latency = (time.time() - start) * 1000
        state.latency_ms["output_safety"] = latency

        if not is_safe:
            state.output_safe = False
            state.refusal_triggered = True
            state.refusal_reason = reason
            state.final_response = get_refusal(RefusalReason.UNSAFE_OUTPUT)
            state.citations = [Citation(
                index=1,
                url=CONTACT_CITATION["url"],
                title=CONTACT_CITATION["title"],
                section=CONTACT_CITATION["section"],
            )]
            log.warning(
                "output_unsafe",
                reason=reason,
                latency_ms=latency,
            )
        else:
            state.output_safe   = True
            state.final_response = state.raw_response  # promote to final
            log.info("output_safe", latency_ms=latency)

    except Exception as e:
        # Fail open — don't block user on safety error
        log.error("output_safety_error", error=str(e))
        state.output_safe    = True
        state.final_response = state.raw_response  # promote even on error

    return state