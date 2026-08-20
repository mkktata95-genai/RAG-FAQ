# Model Evaluation Framework — Phase 1

Model-agnostic. No imports from `generator.py`, `retriever.py`, or `graph.py`.
Any team plugs in their own `response_fn` and runs.

## Quick start

```bash
python run_eval.py --dataset sample_golden_dataset.json --model-label my_model_v1
```

Outputs `eval_report.json` (machine-readable) and `eval_report.html` (dashboard).

## Wiring your model

Edit `get_response_fn()` in `run_eval.py`. Your function signature:

```python
def my_response_fn(question: str) -> dict:
    # call your pipeline here — do NOT import it at module level in this
    # framework; import locally inside this wrapper so the framework
    # itself stays decoupled
    return {"answer": "...", "citations": ["chunk_id_or_url", ...]}
```

`citations` is optional — return a plain `str` if you only want generation +
latency metrics, no retrieval metrics.

## Dataset format

JSON (list of objects) or CSV — same columns as `build_golden_dataset_seed.py`
output: `id, product_category, question, expected_answer, source_url,
chunk_id, chunk_index`.

Rows with blank `expected_answer` are skipped by default (unreviewed seed
rows). Pass `--include-unreviewed` to include them anyway — useful for
smoke-testing before SME review finishes.

## Regression / benchmark workflow

```bash
# first run — establish baseline
python run_eval.py --dataset golden_dataset.json --model-label v1 --save-as-baseline

# later run — compare
python run_eval.py --dataset golden_dataset.json --model-label v2 \
    --compare-baseline eval_report_baseline.json
```

Flags any metric that moved >3% in the worse direction (`--threshold` not yet
exposed as CLI arg — edit `regression.compare_to_baseline()` default if needed).

## Files

| File | Purpose |
|---|---|
| `core.py` | `response_fn` contract, `EvalCase`/`EvalRun` data types |
| `metrics_generation.py` | token F1, sequence similarity, optional pluggable embedding similarity |
| `metrics_retrieval.py` | precision@k, recall@k, MRR, hit rate |
| `metrics_judge.py` | optional LLM-as-judge: faithfulness, answer relevance, correctness, context relevance |
| `metrics_operational.py` | refusal detection (explicit flag or heuristic fallback), cost resolution |
| `runner.py` | loads dataset, executes response_fn, aggregates |
| `report.py` | JSON + HTML dashboard output |
| `regression.py` | baseline save/compare |
| `run_eval.py` | CLI entry point — **edit `get_response_fn()`/`get_judge_fn()` here** |

## Status

Framework built and smoke-tested end-to-end — dummy `response_fn`/`judge_fn`,
3-case sample dataset, JSON and CSV loading, backward compat (plain-string
`response_fn`, no `judge_fn`), regression pass/fail, HTML render — all passing.
Not yet run against the real `golden_dataset.json` — pending SME review of
`expected_answer` fields.

## Coverage vs standard enterprise RAG eval practice

| Component | Status |
|---|---|
| Golden dataset | Built (seed generation + SME review workflow) |
| Retrieval metrics (id-matching) | Built — precision@k, recall@k, MRR, hit rate |
| Context relevance (LLM-judged) | Built — via optional `judge_fn` |
| Generation metrics (lexical) | Built — token F1, sequence similarity |
| Generation metrics (embedding) | Built — optional `embed_fn` |
| LLM-as-judge (faithfulness/relevance/correctness) | Built — optional `judge_fn`, see `metrics_judge.py` |
| Operational metrics (latency, error rate) | Built |
| Operational metrics (cost, refusal rate) | Built — optional, response_fn-reported or heuristic fallback |
| Regression/CI gating | Built — baseline save/compare |
| Post-deployment continuous monitoring | Not built — separate Phase 3 item, deliberately out of scope for this pre-launch framework |
| Human-in-the-loop judge calibration | Not built — process activity once a judge_fn is chosen, not a framework feature |

## Optional pluggable hooks

All of these default to `None`/off — framework runs fine without any of them,
just with fewer metrics populated.

| Hook | Passed to | Enables |
|---|---|---|
| `embed_fn(text) -> vector` | `run_evaluation()` | semantic_similarity |
| `judge_fn(question, expected, actual, context) -> dict` | `run_evaluation()` | faithfulness, answer_relevance, correctness, context_relevance |
| `response_fn` returning `citations` | n/a (dict key) | retrieval metrics |
| `response_fn` returning `retrieved_context` | n/a (dict key) | enables judge_fn to score faithfulness/context_relevance |
| `response_fn` returning `refused` | n/a (dict key) | accurate refusal rate (else falls back to generic phrase heuristic) |
| `response_fn` returning `cost_usd` or `input_tokens`/`output_tokens` | n/a (dict key) | cost tracking (token-based needs `price_per_1k_input/output` too) |
