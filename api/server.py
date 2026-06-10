"""
FastAPI server with SSE streaming, rate limiting,
request tracking and enhanced health checks.

Migration: Fixed health check — get_embedding_client()
           replaced with get_openai_client()

# ─────────────────────────────────────────────────────────────
# TODO: PRODUCTION READINESS
# Before go-live update the following:
#
# CORS:
#      Current  → allow_origins=["*"] (allows any domain)
#      Replace  → Restrict to RLG frontend domain only:
#                 allow_origins=["https://your-rlg-domain.com"]
#
# Rate Limiting:
#      Current  → check_rate_limit() from middleware.py
#                 (in-memory, breaks with multiple instances)
#      Replace  → Remove check_rate_limit() call entirely
#                 Azure API Management (APIM) handles this
#
# Request Tracking:
#      Current  → generate_request_id() creates UUID locally
#      Enhance  → Read X-Correlation-ID header from APIM:
#                 request_id = request.headers.get(
#                     "x-correlation-id",
#                     generate_request_id()
#                 )
#
# Authentication:
#      Current  → No auth (open endpoint)
#      Add      → APIM subscription key or
#                 Azure AD token validation
#                 before go-live with real customers
#
# Streaming:
#      Current  → Word-by-word SSE (good for UX)
#      Consider → Implement true token streaming from
#                 OpenAI stream=True for lower latency
#
# Monitoring:
#      Add      → Application Insights middleware:
#                 from azure.monitor.opentelemetry import configure_azure_monitor
#                 configure_azure_monitor(connection_string=APPINSIGHTS_CONNECTION_STRING)
# ─────────────────────────────────────────────────────────────
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
    Run graph and stream response word by word via SSE.
    TODO: PRODUCTION → Consider true token streaming
    using OpenAI stream=True for lower latency.
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

        # Stream words one by one
        words = result.final_response.split(" ")
        for i, word in enumerate(words):
            token = word if i == len(words) - 1 else word + " "
            yield f"data: {json.dumps({'token': token})}\n\n"
            await asyncio.sleep(0.02)

        # Send final metadata event
        yield f"data: {json.dumps({'citations': [{'index': c.index, 'url': c.url, 'section': c.section, 'title': c.title} for c in result.citations], 'cached': result.cache_hit, 'model_used': result.model_used, 'request_id': request_id, 'latency_ms': result.latency_ms, 'token_usage': result.token_usage, 'needs_empathy': result.needs_empathy, 'needs_disclaimer': result.needs_disclaimer, 'done': True})}\n\n"

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