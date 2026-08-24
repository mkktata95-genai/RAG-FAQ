"""
metrics_retrieval.py — retrieval-quality metrics

Only computed when response_fn returns citations (chunk_ids or
source_urls). Comparison is against EvalCase.relevant_ids, which
defaults to [chunk_id] from the golden dataset — supports multi-relevant
cases if the dataset later specifies more than one relevant id per
question.

Matching is done on normalised string identity (case/whitespace
insensitive) so it works whether teams cite chunk_id, source_url, or
their own internal doc id — as long as the golden dataset's
relevant_ids uses the same identifier space as the citations returned.

CHANGE LOG
v1.1.0 — Aug 2026 | Mukesh Kund
         Fix: score_retrieval() now also returns {} (N/A) when
         relevant_ids is empty, not just when citations is empty. Found
         while discussing handover to a team whose golden dataset might
         have neither source_url nor chunk_id set — without this guard,
         a response_fn returning real citations against a dataset with
         no ground truth would score as 0.0/False across all retrieval
         metrics, misleadingly looking like total retrieval failure
         rather than "not applicable, no ground truth to check against."
         ROLLBACK: revert condition to `if not citations:` only.

v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations


def _norm(s: str) -> str:
    return s.strip().lower()


def hit(relevant_ids: list[str], citations: list[str]) -> bool:
    """Did at least one returned citation match a relevant id?"""
    rel = {_norm(r) for r in relevant_ids}
    cit = {_norm(c) for c in citations}
    return bool(rel & cit)


def precision_at_k(relevant_ids: list[str], citations: list[str], k: int) -> float:
    if not citations:
        return 0.0
    top_k = citations[:k]
    rel = {_norm(r) for r in relevant_ids}
    matched = sum(1 for c in top_k if _norm(c) in rel)
    return matched / len(top_k)


def recall_at_k(relevant_ids: list[str], citations: list[str], k: int) -> float:
    if not relevant_ids:
        return 0.0
    top_k = {_norm(c) for c in citations[:k]}
    rel = {_norm(r) for r in relevant_ids}
    matched = len(rel & top_k)
    return matched / len(rel)


def reciprocal_rank(relevant_ids: list[str], citations: list[str]) -> float:
    """1/rank of the first correct citation, 0 if none found."""
    rel = {_norm(r) for r in relevant_ids}
    for i, c in enumerate(citations, start=1):
        if _norm(c) in rel:
            return 1.0 / i
    return 0.0


def score_retrieval(relevant_ids: list[str], citations: list[str], k: int = 5) -> dict:
    """Returns all applicable retrieval metrics for one case.
    Returns empty dict (metric N/A, not 0) if EITHER side is missing:
      - no citations were provided by response_fn, OR
      - no relevant_ids exist for this case (golden dataset has neither
        source_url nor chunk_id set — there's no ground truth to check
        against, so scoring would be meaningless 0s/False that look like
        retrieval failure rather than "not applicable here").
    This avoids silently penalising response_fns or datasets that don't
    expose one side or the other.

    IMPORTANT: the golden dataset records exactly ONE known-relevant chunk
    per question (by construction — each seed question was generated from
    a single source chunk; SME review does not attempt to identify whether
    a good answer would legitimately draw on additional chunks, since that
    judgement is not reliably something a human reviewer reading one page
    can make). This means `precision_at_k` will look artificially low for
    any answer that (correctly) cites more than one source, because extra
    citations are only ever "unverified", not actually wrong.

    Treat `hit` and `recall_at_k` as the primary, trustworthy metrics.
    Treat `precision_at_k` as informational only — do not gate release
    decisions on it without a manual look at the flagged cases.
    """
    if not citations or not relevant_ids:
        return {}
    return {
        "hit": hit(relevant_ids, citations),
        f"precision_at_{k}": round(precision_at_k(relevant_ids, citations, k), 4),
        f"recall_at_{k}": round(recall_at_k(relevant_ids, citations, k), 4),
        "reciprocal_rank": round(reciprocal_rank(relevant_ids, citations), 4),
        "num_citations_returned": len(citations),
    }