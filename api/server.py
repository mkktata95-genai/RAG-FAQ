"""
FastAPI server with SSE streaming, rate limiting,
request tracking and enhanced health checks.

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
         FastAPI server with SSE streaming, rate limiting,
         request tracking and health checks.

v1.1.0 — July 2026 | Mukesh Kund
         OpenAI chat client warmup + fake streaming delay removed

         warmup() [MODIFIED]:
         - Added get_openai_client() from generator.py to the
           startup warmup sequence alongside get_search_client()
           and get_embedding_client().
         - WHY: first query was hitting DefaultAzureCredential
           token acquisition cold (~5s) + Azure Search semantic
           reranker cold start (~3-5s) + gpt-4o first token
           latency (~5-10s) all compounding serially → observed
           36s latency on first query (sprint 1 testing, 13 July).
           Warmup eliminates the credential cold-start penalty.
         - Health check already used get_openai_client() — this
           change brings warmup() into alignment.

         TODO — PRODUCTION READINESS (pre go-live):
         - CORS: allow_origins=["*"] → restrict to RLG frontend
           domain only: allow_origins=["https://your-rlg-domain.com"]
         - Rate Limiting: replace in-memory check_rate_limit()
           with Azure API Management (APIM) — in-memory breaks
           with multiple instances.
         - Request Tracking: read X-Correlation-ID header from APIM:
           request_id = request.headers.get("x-correlation-id",
           generate_request_id())
         - Authentication: add APIM subscription key or Azure AD
           token validation before go-live.
         - Monitoring: add Application Insights middleware:
           from azure.monitor.opentelemetry import configure_azure_monitor
           configure_azure_monitor(connection_string=APPINSIGHTS_CONNECTION_STRING)

v1.2.0 — July 2026 | Mukesh Kund
         True token streaming via state.stream_tokens

         stream_response() [MODIFIED]:
         - Was: result.final_response split on spaces, each word
           yielded with asyncio.sleep(0.02) artificial delay
           (~2-4s added latency on top of generation time).
         - Now: reads state.stream_tokens (list[str] populated by
           generator_node v2.3.0 via stream=True OpenAI call).
           Tokens yielded directly with asyncio.sleep(0) (yields
           to event loop only — no artificial delay).
         - Cache hits: full response sent as single chunk (no
           streaming delay for sub-100ms cache responses).
         - Fallback: if stream_tokens is empty, falls back to
           space-split of final_response (defensive).
         - Companion: generator.py v2.3.0, schemas.py v1.1.0.

v1.3.0 — August 2026 | Mukesh Kund
         BUG FIX — Presidio (Layer 1B PII, safety.py v1.2.0) never
         warmed, causing elevated first-query latency.
         - ROOT CAUSE: warmup() warms embeddings, Content Safety,
           Search, and OpenAI chat clients, but never called
           get_presidio_analyzer(). safety.py v1.4.0 made Layer 1B
           run FIRST on every request (before relevance) — so the
           unwarmed AnalyzerEngine()/spaCy model load (multi-second)
           sat directly on the critical path of the first real
           query. Confirmed live: 71.7s first response vs 26.4s
           second response on comparable output length — throughput
           math (second query's tok/s applied to first query's
           token count) implies ~50s of non-generation overhead on
           the first request alone, consistent with an unwarmed
           model load stacking with everything downstream of it.
         - FIX: added get_presidio_analyzer() to warmup(), same
           run_in_executor pattern as get_embedding() (both are
           blocking synchronous calls, not native async).
         - Companion: prompt_builder_node.py v1.7.0 (same testing
           session — bullet-point over-triggering was inflating
           output token count, likely compounding the perceived
           latency on top of this root cause).
         ROLLBACK: remove the get_presidio_analyzer() warmup block
         (not recommended — reintroduces first-query latency spike
         on every fresh deploy).

═══════════════════════════════════════════════════════════════
"""

import json
import time
import asyncio
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

from core.graph import run_query
from core.schemas import ChatRequestBody
from core.middleware import (
    check_rate_limit,
    generate_request_id,
    get_token_stats,
    mask_pii_for_logging,
)

load_dotenv()
log = structlog.get_logger()

app = FastAPI(
    title="RLG AI Assistant API",
    version="1.0.0",
)

# TODO: PRODUCTION → Restrict allow_origins to RLG domain only
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def warmup():
    """
    Warm up all API connections on server start.
    Prevents cold-start latency on first user request.
    """
    log.info("warmup_started")
    try:
        loop = asyncio.get_event_loop()

        # Warm up embedding client
        from core.embeddings import get_embedding
        await loop.run_in_executor(
            None, lambda: get_embedding("warmup")
        )

        # Warm up safety client
        from core.safety import get_safety_client
        get_safety_client()

        # Warm up search client
        from core.nodes.retriever import get_search_client
        get_search_client()

        # Warm up Presidio NER (Layer 1B — PII detection).
        # v1.2.0 added this layer, v1.4.0 made it run FIRST on
        # every request (before relevance) — but warmup() never
        # loaded it, so the first real user query paid the full
        # AnalyzerEngine()/spaCy model cold-start cost (multi-
        # second) on top of everything else. Same fix as the
        # OpenAI client below (v1.1.0) — different symptom
        # (elevated latency on every deploy's first query, not
        # every server's), same root cause class.
        from core.safety import get_presidio_analyzer
        await loop.run_in_executor(None, get_presidio_analyzer)

        # Warm up OpenAI chat client (generator) — prevents
        # DefaultAzureCredential cold-start (~5s) on first query.
        # v1.1.0: this was the primary cause of 36s first-query
        # latency observed in sprint 1 testing (13 July).
        from core.nodes.generator import get_openai_client
        get_openai_client()

        log.info("warmup_complete")
        print("✅ All clients warmed up")

    except Exception as e:
        log.warning("warmup_failed", error=str(e))


async def stream_response(
    query: str,
    conversation_history: list[dict],
    request_id: str,
) -> AsyncGenerator[str, None]:
    """
    Run graph, then stream final_response token-by-token via SSE.

    v1.1.0: Replaced fake word-by-word streaming (asyncio.sleep(0.02)
    per word after full pipeline completion) with true streaming:
    - Pipeline still runs synchronously in executor (graph is sync)
    - Once result is ready, response text is yielded token-by-token
      WITHOUT artificial delay — no asyncio.sleep()
    - For cached responses (cache_hit=True), the response is sent
      in a single token chunk — no point simulating streaming for
      a sub-100ms cache hit
    - TODO (next sprint): For non-cached responses, refactor
      generator_node to use OpenAI stream=True so the first token
      appears before the full response is assembled. This requires
      generator_node to become async and yield tokens directly to
      this generator — a larger refactor tracked separately.
      Current bottleneck: DefaultAzureCredential token acquisition
      (~2-5s cold, <100ms warm) — warmup() now warms the chat
      client on startup, eliminating the cold-start latency.
    """
    try:
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: run_query(query, conversation_history),
        )

        if not result.final_response:
            yield f"data: {json.dumps({'error': 'No response generated'})}\n\n"
            return

        citations_payload = [
            {
                'index':   c.index,
                'url':     c.url,
                'section': c.section,
                'title':   c.title,
            }
            for c in result.citations
        ]
        meta_payload = {
            'cached':          result.cache_hit,
            'model_used':      result.model_used,
            'request_id':      request_id,
            'latency_ms':      result.latency_ms,
            'token_usage':     result.token_usage,
            'needs_empathy':   result.needs_empathy,
            'needs_disclaimer':result.needs_disclaimer,
        }

        if result.cache_hit:
            # Cache hit — single chunk, no streaming delay
            yield f"data: {json.dumps({'token': result.final_response})}\n\n"
        else:
            # True token streaming (v1.2.0):
            # generator_node populated state.stream_tokens with the
            # raw OpenAI stream chunks (stream=True). Yield them
            # directly — no word splitting, no artificial delay.
            # Falls back to space-split if stream_tokens is empty
            # (e.g. cache hit path that reached generator somehow).
            stream_tokens = getattr(result, "stream_tokens", None)
            if stream_tokens:
                for token in stream_tokens:
                    yield f"data: {json.dumps({'token': token})}\n\n"
                    await asyncio.sleep(0)  # yield to event loop
            else:
                # Fallback: space-split final_response
                words = result.final_response.split(" ")
                for i, word in enumerate(words):
                    token = word if i == len(words) - 1 else word + " "
                    yield f"data: {json.dumps({'token': token})}\n\n"
                    await asyncio.sleep(0)

        # Final metadata event (citations, model, latency, etc.)
        yield f"data: {json.dumps({**meta_payload, 'citations': citations_payload, 'done': True})}\n\n"

        log.info(
            "stream_complete",
            query=mask_pii_for_logging(query)[:50],
            cached=result.cache_hit,
            request_id=request_id,
        )

    except Exception as e:
        log.error(
            "stream_error",
            error=str(e),
            request_id=request_id,
        )
        yield f"data: {json.dumps({'error': 'An error occurred. Please try again.'})}\n\n"


@app.post("/api/chat")
async def chat(body: ChatRequestBody, request: Request):
    """
    Main chat endpoint with SSE streaming.
    TODO: PRODUCTION → Add APIM auth validation here.
    """
    # TODO: PRODUCTION → Replace with APIM rate limiting
    client_ip         = request.client.host
    allowed, message  = check_rate_limit(client_ip)
    if not allowed:
        raise HTTPException(status_code=429, detail=message)

    # TODO: PRODUCTION → Read from APIM x-correlation-id header
    request_id = generate_request_id()

    log.info(
        "chat_request",
        query=mask_pii_for_logging(body.query)[:50],
        request_id=request_id,
        ip=client_ip,
    )

    return StreamingResponse(
        stream_response(
            body.query,
            body.conversation_history,
            request_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "Connection":       "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Request-ID":     request_id,
        },
    )


@app.get("/api/health")
async def health():
    """
    Enhanced health check.
    Verifies all downstream services are reachable.
    """
    health_status = {
        "status":    "healthy",
        "timestamp": time.time(),
        "services":  {},
    }

    # Check embedding client (OpenAI)
    try:
        from core.embeddings import get_openai_client
        get_openai_client()
        health_status["services"]["embeddings"] = "ok"
    except Exception:
        health_status["services"]["embeddings"] = "error"
        health_status["status"] = "degraded"

    # Check search client
    try:
        from core.nodes.retriever import get_search_client
        get_search_client()
        health_status["services"]["search"] = "ok"
    except Exception:
        health_status["services"]["search"] = "error"
        health_status["status"] = "degraded"

    # Check safety client
    try:
        from core.safety import get_safety_client
        get_safety_client()
        health_status["services"]["safety"] = "ok"
    except Exception:
        health_status["services"]["safety"] = "error"
        health_status["status"] = "degraded"

    # Cache stats
    from core.cache import get_cache
    cache = get_cache()
    health_status["cache"] = {
        "size":      cache.size,
        "threshold": cache.threshold,
        "backend":   "redis" if cache._using_redis else "memory",
    }

    return health_status


@app.get("/api/stats")
async def stats():
    """
    Token usage and session statistics.
    TODO: PRODUCTION → Replace with Application Insights query.
    """
    return get_token_stats()


@app.get("/api/cache/stats")
async def cache_stats():
    """Cache statistics."""
    from core.cache import get_cache
    cache = get_cache()
    return cache.get_stats()