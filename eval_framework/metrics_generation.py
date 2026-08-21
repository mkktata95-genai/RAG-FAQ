"""
metrics_generation.py — generation-quality metrics

Model-agnostic. Embedding-based semantic similarity is OPTIONAL and
pluggable (embed_fn) — no hard dependency on Azure OpenAI or any
specific embedding model, per the framework's decoupling requirement.
Falls back to a pure lexical metric (token overlap / ROUGE-L-ish) when
no embed_fn is supplied, so the framework runs with zero external deps.

CHANGE LOG
v1.1.0 — Aug 2026 | Mukesh Kund
         Added strip_citation_markers() and applied it in score_generation()
         before token_f1/sequence_similarity/semantic_similarity scoring.
         Citation markers like [1], [2] are UI rendering syntax injected
         by the generator, not semantic content — they never appear in a
         hand-written expected_answer, so leaving them in was mechanically
         penalizing every answer that cites a source (i.e. most answers).
         "answered" check still uses the raw unstripped text.
         ROLLBACK: remove strip_citation_markers() call, score against
         raw actual_answer directly.

v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Callable, Optional

EmbedFn = Callable[[str], list[float]]

_CITATION_MARKER_RE = re.compile(r"\[\d+\]")


def strip_citation_markers(text: str) -> str:
    """Removes inline citation markers like [1], [2] before scoring.
    These are UI rendering syntax injected by the generator (matched
    against a separate citations list), not semantic content — they
    never appear in a hand-written expected_answer, so leaving them in
    mechanically drags down every lexical/sequence metric for any
    answer that cites a source (i.e. most answers). Collapses any
    resulting double-space from the removal."""
    return re.sub(r"\s{2,}", " ", _CITATION_MARKER_RE.sub("", text)).strip()


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
    """Returns all applicable generation metrics for one case.
    actual_answer has citation markers ([1], [2], ...) stripped before
    scoring — see strip_citation_markers(). "answered" still checks the
    raw (unstripped) text, so a citation-only response isn't wrongly
    counted as unanswered."""
    cleaned_actual = strip_citation_markers(actual_answer)
    scores = {
        "token_f1": round(token_f1(expected_answer, cleaned_actual), 4),
        "sequence_similarity": round(sequence_similarity(expected_answer, cleaned_actual), 4),
        "answered": bool(actual_answer.strip()),
    }
    if embed_fn is not None:
        scores["semantic_similarity"] = round(
            semantic_similarity(expected_answer, cleaned_actual, embed_fn), 4
        )
    return scores