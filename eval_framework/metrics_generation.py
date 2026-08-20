"""
metrics_generation.py — generation-quality metrics

Model-agnostic. Embedding-based semantic similarity is OPTIONAL and
pluggable (embed_fn) — no hard dependency on Azure OpenAI or any
specific embedding model, per the framework's decoupling requirement.
Falls back to a pure lexical metric (token overlap / ROUGE-L-ish) when
no embed_fn is supplied, so the framework runs with zero external deps.

CHANGE LOG
v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Callable, Optional

EmbedFn = Callable[[str], list[float]]


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def token_f1(expected: str, actual: str) -> float:
    """Simple token-overlap F1 — cheap, dependency-free baseline metric."""
    exp_tokens = _tokenize(expected)
    act_tokens = _tokenize(actual)
    if not exp_tokens or not act_tokens:
        return 0.0
    exp_set, act_set = set(exp_tokens), set(act_tokens)
    overlap = len(exp_set & act_set)
    if overlap == 0:
        return 0.0
    precision = overlap / len(act_set)
    recall = overlap / len(exp_set)
    return 2 * precision * recall / (precision + recall)


def sequence_similarity(expected: str, actual: str) -> float:
    """difflib ratio — catches paraphrase-level closeness token_f1 misses."""
    if not expected or not actual:
        return 0.0
    return SequenceMatcher(None, expected.lower(), actual.lower()).ratio()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def semantic_similarity(expected: str, actual: str, embed_fn: EmbedFn) -> float:
    """Requires a pluggable embed_fn(text) -> vector. Any team can pass
    their own (Azure OpenAI, local model, etc.) — framework doesn't care."""
    if not expected or not actual:
        return 0.0
    return cosine_similarity(embed_fn(expected), embed_fn(actual))


def score_generation(
    expected_answer: str,
    actual_answer: str,
    embed_fn: Optional[EmbedFn] = None,
) -> dict:
    """Returns all applicable generation metrics for one case."""
    scores = {
        "token_f1": round(token_f1(expected_answer, actual_answer), 4),
        "sequence_similarity": round(sequence_similarity(expected_answer, actual_answer), 4),
        "answered": bool(actual_answer.strip()),
    }
    if embed_fn is not None:
        scores["semantic_similarity"] = round(
            semantic_similarity(expected_answer, actual_answer, embed_fn), 4
        )
    return scores
