"""
core.py — Model Evaluation Framework: core interfaces & data types

Defines the pluggable contract any team's chatbot/model can be evaluated
against. Deliberately has ZERO imports from the RLG RAG pipeline
(generator.py, retriever.py, graph.py) — any team points their own
response function at this framework without coupling.

CHANGE LOG
v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, runtime_checkable, Union


# ═══════════════════════════════════════════════════════════════
# THE PLUGGABLE CONTRACT
# ═══════════════════════════════════════════════════════════════
# Any team implements ONE of these two shapes:
#
#   def my_response_fn(question: str) -> str:
#       return "the answer"
#
#   def my_response_fn(question: str) -> dict:
#       return {
#           "answer": "the answer",
#           "citations": ["url_or_chunk_id", ...],       # optional — enables retrieval metrics
#           "retrieved_context": ["chunk text 1", ...],  # optional — enables LLM-judge faithfulness/context-relevance
#           "refused": False,                             # optional — explicit refusal flag
#           "input_tokens": 512,                          # optional — enables cost tracking
#           "output_tokens": 128,                         # optional
#           "cost_usd": 0.0031,                           # optional — precomputed cost, takes precedence over token-based calc
#       }
#
# Every extra field is optional. A plain string, or a dict with only
# "answer", still works exactly as before — nothing below is required.

@runtime_checkable
class ResponseFn(Protocol):
    def __call__(self, question: str) -> Union[str, dict]: ...


@dataclass
class NormalizedResponse:
    """Internal normalised shape regardless of what response_fn returned."""
    answer: str
    citations: list[str] = field(default_factory=list)
    retrieved_context: list[str] = field(default_factory=list)
    latency_seconds: float = 0.0
    error: Optional[str] = None
    refused: Optional[bool] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None


def invoke_response_fn(fn: ResponseFn, question: str) -> NormalizedResponse:
    """Call the plugged-in response_fn, time it, normalise the output,
    and never let a single failing case crash the whole eval run."""
    start = time.perf_counter()
    try:
        raw = fn(question)
        elapsed = time.perf_counter() - start
        if isinstance(raw, dict):
            return NormalizedResponse(
                answer=str(raw.get("answer", "")),
                citations=[str(c) for c in raw.get("citations", []) or []],
                retrieved_context=[str(c) for c in raw.get("retrieved_context", []) or []],
                latency_seconds=elapsed,
                refused=raw.get("refused"),
                input_tokens=raw.get("input_tokens"),
                output_tokens=raw.get("output_tokens"),
                cost_usd=raw.get("cost_usd"),
            )
        return NormalizedResponse(answer=str(raw), latency_seconds=elapsed)
    except Exception as exc:  # noqa: BLE001 — deliberately broad; one bad case must not kill the run
        elapsed = time.perf_counter() - start
        return NormalizedResponse(answer="", latency_seconds=elapsed, error=repr(exc))


# ═══════════════════════════════════════════════════════════════
# GOLDEN DATASET CASE
# ═══════════════════════════════════════════════════════════════

@dataclass
class EvalCase:
    id: str
    question: str
    expected_answer: str
    product_category: str = "general"
    source_url: str = ""
    chunk_id: str = ""
    # relevant_ids: what a "correct" retrieval should return for this
    # question. Defaults to [chunk_id] if not explicitly set — supports
    # datasets where a question maps to more than one relevant chunk.
    relevant_ids: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.relevant_ids and self.chunk_id:
            self.relevant_ids = [self.chunk_id]


@dataclass
class CaseResult:
    case: EvalCase
    response: NormalizedResponse
    generation_scores: dict = field(default_factory=dict)
    retrieval_scores: dict = field(default_factory=dict)
    judge_scores: dict = field(default_factory=dict)


@dataclass
class EvalRun:
    """Aggregate result of running one response_fn against the full dataset."""
    run_id: str
    model_label: str
    case_results: list[CaseResult] = field(default_factory=list)
    aggregate: dict = field(default_factory=dict)
