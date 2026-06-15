"""
Cache Write Node — stores successful response in semantic cache.

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
         Writes response to semantic cache unless
         refusal_triggered, no final_response, cache_hit, or
         no embedding available.

v1.1.0 — June 2026 | Mukesh Kund
         Skip cache write for sensitive disclosures (empathy)

         cache_write_node() [MODIFIED]:
         - Added skip condition: state.needs_empathy is True
         - WHY: state.needs_empathy is now set in supervisor.py
           (v1.5.0) for genuine-distress queries (terminal
           illness, bereavement, redundancy, financial hardship
           etc), and cache_check.py (v1.5.0) skips the semantic
           cache entirely for these queries so the full pipeline
           always runs.
         - Without this skip, a sensitive exchange's response
           (which includes empathy + human handoff, tailored to
           THIS customer's disclosure) could be written to cache
           and later served cold — without empathy/handoff — to
           a different customer whose query happens to be
           semantically similar after normalisation.
         - Mirrors the existing refusal_triggered / cache_hit
           skip conditions below.

═══════════════════════════════════════════════════════════════
"""

import time
import structlog

from core.cache import get_cache
from core.schemas import AgentState, ChatResponse

log = structlog.get_logger()


def cache_write_node(state: AgentState) -> AgentState:
    """
    Write successful response to semantic cache.
    Only writes if response is valid and not a refusal.
    """
    start = time.time()

    try:
        # Skip if refusal or no response
        if state.refusal_triggered or not state.final_response:
            log.info("cache_write_skipped", reason="refusal_or_no_response")
            return state

        # Skip if cache was already a hit
        if state.cache_hit:
            log.info("cache_write_skipped", reason="cache_hit")
            return state

        # Skip if this is a sensitive disclosure (v1.1.0)
        # state.needs_empathy is set in supervisor.py and
        # cache_check.py already skipped the semantic cache for
        # this query entirely (see cache_check.py v1.5.0). Do not
        # write this response to cache either — it contains
        # empathy/handoff text tailored to THIS customer's
        # disclosure and must not be replayed to a different
        # customer whose query is semantically similar after
        # normalisation.
        if state.needs_empathy:
            log.info("cache_write_skipped", reason="needs_empathy")
            return state

        # Get query embedding from state
        embedding = state.__dict__.get("_query_embedding")
        if not embedding:
            log.warning("cache_write_skipped", reason="no_embedding")
            return state

        # Build response object
        response = ChatResponse(
            answer=state.final_response,
            citations=state.citations,
            cached=False,
            model_used=state.model_used,
        )

        # Write to cache
        cache = get_cache()
        cache.set(
            query=state.query,
            embedding=embedding,
            response=response,
        )

        latency = (time.time() - start) * 1000
        state.latency_ms["cache_write"] = latency

        log.info(
            "cache_write_complete",
            cache_size=cache.size,
            latency_ms=latency,
        )

    except Exception as e:
        log.error("cache_write_error", error=str(e))

    return state