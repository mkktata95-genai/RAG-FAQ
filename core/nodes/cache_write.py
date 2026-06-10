"""
Cache Write Node — stores successful response in semantic cache.
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