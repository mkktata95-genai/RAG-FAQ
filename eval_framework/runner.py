"""
runner.py — loads a golden dataset and runs any response_fn against it.

Accepts JSON (list of objects) or CSV (matching build_golden_dataset_seed.py
column layout: id, product_category, question, expected_answer, source_url,
chunk_id, chunk_index). Rows with a blank expected_answer are skipped by
default (unreviewed seed rows) — pass include_unreviewed=True to include
them anyway (e.g. for smoke-testing the framework before SME review lands).

CHANGE LOG
v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations

import csv
import json
import statistics
import uuid
from pathlib import Path
from typing import Optional

from eval_core import EvalCase, CaseResult, EvalRun, ResponseFn, invoke_response_fn
from metrics_generation import score_generation, EmbedFn
from metrics_retrieval import score_retrieval
from metrics_judge import score_with_judge, JudgeFn
from metrics_operational import resolve_refusal, resolve_cost


def load_dataset(path: str, include_unreviewed: bool = False) -> list[EvalCase]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        raw_rows = json.loads(p.read_text(encoding="utf-8"))
    elif p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8-sig") as f:
            raw_rows = list(csv.DictReader(f))
    else:
        raise ValueError(f"Unsupported dataset format: {p.suffix} (expected .json or .csv)")

    cases = []
    skipped = 0
    for row in raw_rows:
        expected = (row.get("expected_answer") or "").strip()
        if not expected and not include_unreviewed:
            skipped += 1
            continue
        cases.append(EvalCase(
            id=row.get("id", str(uuid.uuid4())[:8]),
            question=row["question"],
            expected_answer=expected,
            product_category=row.get("product_category", "general"),
            source_url=row.get("source_url", ""),
            chunk_id=row.get("chunk_id", ""),
        ))
    if skipped:
        print(f"[runner] Skipped {skipped} row(s) with blank expected_answer "
              f"(pass include_unreviewed=True to include).")
    return cases


def run_evaluation(
    cases: list[EvalCase],
    response_fn: ResponseFn,
    model_label: str = "unnamed_model",
    embed_fn: Optional[EmbedFn] = None,
    judge_fn: Optional[JudgeFn] = None,
    retrieval_k: int = 5,
    run_id: Optional[str] = None,
    price_per_1k_input: Optional[float] = None,
    price_per_1k_output: Optional[float] = None,
) -> EvalRun:
    """Runs response_fn against every case, computes per-case + aggregate
    metrics. This is the single entry point any team calls.

    judge_fn: optional LLM-as-judge callable (see metrics_judge.py). Skips
    faithfulness/answer_relevance/correctness/context_relevance if omitted.

    price_per_1k_input/output: optional, only used to compute cost from
    token counts when response_fn doesn't already report cost_usd directly.
    """
    run = EvalRun(run_id=run_id or str(uuid.uuid4())[:8], model_label=model_label)

    for case in cases:
        response = invoke_response_fn(response_fn, case.question)

        gen_scores = {}
        judge_scores = {}
        if response.error is None:
            gen_scores = score_generation(case.expected_answer, response.answer, embed_fn)
            judge_scores = score_with_judge(
                judge_fn, case.question, case.expected_answer,
                response.answer, response.retrieved_context,
            )

        ret_scores = score_retrieval(case.relevant_ids, response.citations, k=retrieval_k)

        response.refused = resolve_refusal(response.refused, response.answer)
        response.cost_usd = resolve_cost(
            response.cost_usd, response.input_tokens, response.output_tokens,
            price_per_1k_input, price_per_1k_output,
        )

        run.case_results.append(CaseResult(
            case=case,
            response=response,
            generation_scores=gen_scores,
            retrieval_scores=ret_scores,
            judge_scores=judge_scores,
        ))

    run.aggregate = _aggregate(run.case_results, retrieval_k)
    return run


def _aggregate(results: list[CaseResult], k: int) -> dict:
    n = len(results)
    if n == 0:
        return {"num_cases": 0}

    errors = [r for r in results if r.response.error is not None]
    latencies = [r.response.latency_seconds for r in results]

    def _mean(vals: list[float]) -> Optional[float]:
        return round(statistics.mean(vals), 4) if vals else None

    def _p95(vals: list[float]) -> Optional[float]:
        if not vals:
            return None
        s = sorted(vals)
        idx = min(len(s) - 1, int(round(0.95 * (len(s) - 1))))
        return round(s[idx], 4)

    token_f1_vals = [r.generation_scores["token_f1"] for r in results if "token_f1" in r.generation_scores]
    seq_sim_vals = [r.generation_scores["sequence_similarity"] for r in results if "sequence_similarity" in r.generation_scores]
    sem_sim_vals = [r.generation_scores["semantic_similarity"] for r in results if "semantic_similarity" in r.generation_scores]

    faithfulness_vals = [r.judge_scores["faithfulness"] for r in results if "faithfulness" in r.judge_scores]
    answer_relevance_vals = [r.judge_scores["answer_relevance"] for r in results if "answer_relevance" in r.judge_scores]
    correctness_vals = [r.judge_scores["correctness"] for r in results if "correctness" in r.judge_scores]
    context_relevance_vals = [r.judge_scores["context_relevance"] for r in results if "context_relevance" in r.judge_scores]
    judge_error_count = sum(1 for r in results if "judge_error" in r.judge_scores)

    refused_flags = [r.response.refused for r in results if r.response.refused is not None]
    cost_vals = [r.response.cost_usd for r in results if r.response.cost_usd is not None]

    ret_results = [r for r in results if r.retrieval_scores]
    hit_vals = [1.0 if r.retrieval_scores.get("hit") else 0.0 for r in ret_results]
    mrr_vals = [r.retrieval_scores.get("reciprocal_rank", 0.0) for r in ret_results]
    precision_vals = [r.retrieval_scores.get(f"precision_at_{k}", 0.0) for r in ret_results]
    recall_vals = [r.retrieval_scores.get(f"recall_at_{k}", 0.0) for r in ret_results]

    by_category: dict[str, list[CaseResult]] = {}
    for r in results:
        by_category.setdefault(r.case.product_category, []).append(r)
    category_breakdown = {
        cat: {
            "num_cases": len(rs),
            "avg_token_f1": _mean([x.generation_scores.get("token_f1", 0.0) for x in rs if x.generation_scores]),
        }
        for cat, rs in by_category.items()
    }

    return {
        "num_cases": n,
        "num_errors": len(errors),
        "error_rate": round(len(errors) / n, 4),
        "latency": {
            "mean_seconds": _mean(latencies),
            "p95_seconds": _p95(latencies),
            "max_seconds": round(max(latencies), 4) if latencies else None,
        },
        "generation": {
            "avg_token_f1": _mean(token_f1_vals),
            "avg_sequence_similarity": _mean(seq_sim_vals),
            "avg_semantic_similarity": _mean(sem_sim_vals) if sem_sim_vals else None,
            "answered_rate": round(sum(1 for r in results if r.generation_scores.get("answered")) / n, 4),
        },
        "judge": {
            "avg_faithfulness": _mean(faithfulness_vals),
            "avg_answer_relevance": _mean(answer_relevance_vals),
            "avg_correctness": _mean(correctness_vals),
            "avg_context_relevance": _mean(context_relevance_vals),
            "judge_error_count": judge_error_count,
        } if (faithfulness_vals or answer_relevance_vals or correctness_vals or context_relevance_vals or judge_error_count) else {"note": "no judge_fn supplied — LLM-judge metrics N/A"},
        "operational": {
            "refusal_rate": round(sum(1 for f in refused_flags if f) / len(refused_flags), 4) if refused_flags else None,
            "total_cost_usd": round(sum(cost_vals), 6) if cost_vals else None,
            "avg_cost_usd": _mean(cost_vals),
            "num_cases_with_cost_data": len(cost_vals),
        },
        "retrieval": {
            "num_cases_with_citations": len(ret_results),
            "hit_rate": _mean(hit_vals),
            "mrr": _mean(mrr_vals),
            f"avg_precision_at_{k}": _mean(precision_vals),
            f"avg_recall_at_{k}": _mean(recall_vals),
        } if ret_results else {"num_cases_with_citations": 0, "note": "response_fn returned no citations — retrieval metrics N/A"},
        "by_category": category_breakdown,
    }
