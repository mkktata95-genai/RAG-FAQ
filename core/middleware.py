"""
Enterprise middleware:
P1 - Rate limiting
P3 - Request ID tracking
P5 - Token usage tracking
P2 - PII detection/masking

v1.2.0 — August 2026 | Mukesh Kund
         PII blocking moved upstream to safety.py Layer 1B.
         PII_PATTERNS/PII_REPLACEMENTS/detect_pii() now imported
         from core.safety (single source of truth, shared with
         check_pii()'s blocking decision) instead of being
         duplicated here. mask_pii_for_logging() unchanged in
         behaviour — still masks for logging, now defense-in-depth
         rather than the only PII control.
         ROLLBACK: restore local PII_PATTERNS/PII_REPLACEMENTS/
         detect_pii() definitions here; remove the import from
         core.safety; remove check_pii()/Layer 1B from safety.py.

Migration: Mistral model names → gpt-4.1 / gpt-4o-mini in cost tracking

BUG #5/#14 FIX (July 2026, Mukesh Kund): MODEL_COST_PER_1K only had
gpt-4.1/gpt-4o-mini entries and get_token_stats() summed over that
dict's keys instead of the models actually used — every GPT-5 call
(gpt-5-nano, gpt-5-mini, gpt-5.6-luna, i.e. everything post-migration)
priced at $0, and any real gpt-4.1/gpt-4o-mini cost included even if
those models were never called that session. Replaced with
MODEL_PRICING (mirrors chunk_and_index_hqaV4.py v5.8.5 / content_
freshness.py exactly, $/1M tokens, substring-matched via
_get_model_pricing()) and cost is now summed over tokens_by_model's
actual keys, split by input/output token counts (now tracked
separately per model) rather than a single blended rate.
ROLLBACK: restore MODEL_COST_PER_1K and the old sum() one-liner.

# ─────────────────────────────────────────────────────────────
# TODO: PRODUCTION READINESS
# This implementation is suitable for development/testing only.
# Before go-live replace the following:
#
# P1 - Rate Limiting:
#      Current  → in-memory dict (resets on restart,
#                 breaks with multiple instances)
#      Replace  → Azure API Management (APIM)
#                 Built-in rate limiting policies per IP/user
#
# P2 - PII Detection:
#      Current  → v1.2.0: BLOCKED upstream in safety.py Layer 1B
#                 (regex + Presidio NER). Masking here is now
#                 defense-in-depth only, not the primary control.
#
# P5 - Token Usage Tracking:
#      Current  → in-memory dict (resets on restart,
#                 no dashboards, no alerts)
#      Replace  → Azure Application Insights via OpenTelemetry
#                 Persistent metrics, cost dashboards, alerting
#
# P3 - Request ID:
#      Current  → UUID generation (production safe as-is)
#      Enhance  → Flow correlation ID from APIM headers
#                 for end-to-end request tracing
# ─────────────────────────────────────────────────────────────
"""

import re
import uuid
import time
from collections import defaultdict
from datetime import datetime
import structlog

log = structlog.get_logger()

# ── P1: Rate Limiting ─────────────────────────────────────────
# TODO: PRODUCTION → Replace with Azure API Management policies
RATE_LIMIT_PER_MINUTE = 10
RATE_LIMIT_PER_HOUR   = 100

_request_counts: dict = defaultdict(list)


def check_rate_limit(ip: str) -> tuple[bool, str]:
    """
    Check if IP is within rate limits.
    TODO: PRODUCTION → Remove this function entirely.
    Rate limiting handled by Azure API Management (APIM).
    """
    now        = time.time()
    minute_ago = now - 60
    hour_ago   = now - 3600

    # Clean old entries
    _request_counts[ip] = [
        t for t in _request_counts[ip] if t > hour_ago
    ]

    requests_last_minute = sum(
        1 for t in _request_counts[ip] if t > minute_ago
    )
    requests_last_hour = len(_request_counts[ip])

    if requests_last_minute >= RATE_LIMIT_PER_MINUTE:
        return False, (
            "Rate limit exceeded. "
            "Maximum 10 requests per minute."
        )

    if requests_last_hour >= RATE_LIMIT_PER_HOUR:
        return False, (
            "Rate limit exceeded. "
            "Maximum 100 requests per hour."
        )

    _request_counts[ip].append(now)
    return True, ""


# ── P3: Request ID ────────────────────────────────────────────
def generate_request_id() -> str:
    """
    Generate unique request ID.
    TODO: PRODUCTION → Read correlation ID from APIM headers
    (x-ms-client-request-id or x-correlation-id) for
    end-to-end request tracing in Application Insights.
    """
    return str(uuid.uuid4())


# ── P2: PII Detection + Masking ───────────────────────────────
# v1.2.0: Blocking now happens upstream in safety.py Layer 1B
# (check_pii, regex + Presidio) — a query containing PII never
# reaches this point for masking in the normal case, since
# input_safety_node refuses it before cache_check/generator run.
# mask_pii_for_logging() remains here as defense-in-depth for any
# log line reached via another path (e.g. errors, edge cases) and
# for the account-lookup log line in supervisor.py.
#
# PII_PATTERNS/PII_REPLACEMENTS are no longer defined here —
# single source of truth is safety.py (shared with check_pii()'s
# blocking decision). Imported, not duplicated.
from core.safety import PII_PATTERNS, PII_REPLACEMENTS, detect_pii  # noqa: E402


def mask_pii_for_logging(text: str) -> str:
    """Mask PII before logging. Original text unchanged."""
    masked = text
    for pii_type, pattern in PII_PATTERNS.items():
        replacement = PII_REPLACEMENTS[pii_type]
        masked = re.sub(
            pattern, replacement, masked, flags=re.IGNORECASE
        )
    return masked


# ── P5: Token Usage Tracking ──────────────────────────────────
# TODO: PRODUCTION → Replace with Azure Application Insights
# Use OpenTelemetry SDK to emit custom metrics:
#   from azure.monitor.opentelemetry import configure_azure_monitor
#   configure_azure_monitor(connection_string=APPINSIGHTS_CONNECTION_STRING)
_token_usage: dict = {
    "total_input_tokens":  0,
    "total_output_tokens": 0,
    "total_requests":      0,
    "requests_by_model":   defaultdict(int),
    "tokens_by_model":     defaultdict(int),
    "input_tokens_by_model":  defaultdict(int),
    "output_tokens_by_model": defaultdict(int),
    "session_start":       datetime.utcnow().isoformat(),
}

# GPT model pricing — USD per 1,000,000 tokens (input, output).
# Mirrors chunk_and_index_hqaV4.py v5.8.5 / content_freshness.py
# MODEL_PRICING exactly — keep in sync when adding new models or
# pricing changes. Substring-matched via _get_model_pricing() so
# any deployment name containing the key (e.g. "gpt-5-mini",
# "gpt-5.6-luna") resolves correctly.
# TODO: PRODUCTION → Pull actual costs from Azure Cost Management API
MODEL_PRICING = {
    "gpt-5-mini":  (1.25,  5.00),
    "gpt-5-nano":  (0.50,  2.00),
    "gpt-5.1":     (2.00,  8.00),
    "gpt-5":       (2.00,  8.00),
    "gpt-4o-mini": (0.15,  0.60),
    "gpt-4o":      (2.50, 10.00),
    "gpt-4.1":     (2.00,  8.00),
}


def _get_model_pricing(model_name: str) -> tuple[float, float]:
    name = model_name.lower()
    for key, prices in MODEL_PRICING.items():
        if key in name:
            return prices
    return (2.00, 8.00)


def track_token_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
):
    """
    Track token usage per model.
    TODO: PRODUCTION → Emit to Application Insights as custom metric.
    """
    _token_usage["total_input_tokens"]  += input_tokens
    _token_usage["total_output_tokens"] += output_tokens
    _token_usage["total_requests"]      += 1
    _token_usage["requests_by_model"][model] += 1
    _token_usage["tokens_by_model"][model]   += (
        input_tokens + output_tokens
    )
    _token_usage["input_tokens_by_model"][model]  += input_tokens
    _token_usage["output_tokens_by_model"][model] += output_tokens

    log.info(
        "token_usage",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def get_token_stats() -> dict:
    """
    Get current token usage statistics.
    TODO: PRODUCTION → Query from Application Insights instead.
    """
    estimated_cost = 0.0
    for model in _token_usage["tokens_by_model"]:
        input_price, output_price = _get_model_pricing(model)
        model_input  = _token_usage["input_tokens_by_model"].get(model, 0)
        model_output = _token_usage["output_tokens_by_model"].get(model, 0)
        estimated_cost += (
            (model_input  / 1_000_000) * input_price
            + (model_output / 1_000_000) * output_price
        )

    return {
        "total_input_tokens":  _token_usage["total_input_tokens"],
        "total_output_tokens": _token_usage["total_output_tokens"],
        "total_tokens": (
            _token_usage["total_input_tokens"] +
            _token_usage["total_output_tokens"]
        ),
        "total_requests":     _token_usage["total_requests"],
        "requests_by_model":  dict(
            _token_usage["requests_by_model"]
        ),
        "tokens_by_model":    dict(
            _token_usage["tokens_by_model"]
        ),
        "input_tokens_by_model": dict(
            _token_usage["input_tokens_by_model"]
        ),
        "output_tokens_by_model": dict(
            _token_usage["output_tokens_by_model"]
        ),
        "session_start":      _token_usage["session_start"],
        "estimated_cost_usd": round(estimated_cost, 4),
    }