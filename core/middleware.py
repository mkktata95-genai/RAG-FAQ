"""
Enterprise middleware:
P1 - Rate limiting
P3 - Request ID tracking
P5 - Token usage tracking
P2 - PII detection/masking

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
#      Current  → regex patterns (misses contextual PII
#                 like names, addresses in sentences)
#      Replace  → Azure AI Content Safety PII detection API
#                 More accurate, GDPR compliant, FCA aligned
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
# TODO: PRODUCTION → Replace regex with Azure AI Content Safety
# PII detection API for FCA-compliant, accurate PII handling
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

PII_REPLACEMENTS = {
    "policy_number":  "[POLICY_NUMBER]",
    "ni_number":      "[NI_NUMBER]",
    "date_of_birth":  "[DATE]",
    "email":          "[EMAIL]",
    "phone":          "[PHONE]",
    "sort_code":      "[SORT_CODE]",
    "account_number": "[ACCOUNT_NUMBER]",
    "postcode":       "[POSTCODE]",
}


def detect_pii(text: str) -> list[str]:
    """Detect PII types present in text."""
    found = []
    for pii_type, pattern in PII_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            found.append(pii_type)
    return found


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