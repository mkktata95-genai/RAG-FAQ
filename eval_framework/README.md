# Model Evaluation Framework — Phase 1

Model-agnostic. No imports from `generator.py`, `retriever.py`, or `graph.py`
(or any other pipeline module). Any team plugs in their own `response_fn`
and runs.

## Quick start

```bash
python run_eval.py --dataset sample_golden_dataset.json --model-label my_model_v1
```

Outputs `eval_report.json` (machine-readable) and `eval_report.html`
(dashboard — includes a built-in "What do these metrics mean?" glossary).

## Wiring your model

Edit `get_response_fn()` in `run_eval.py`. Your function signature:

```python
def my_response_fn(question: str) -> dict:
    # call your pipeline here — do NOT import it at module level in this
    # framework; import locally inside this wrapper so the framework
    # itself stays decoupled
    return {
        "answer": "...",
        "citations": ["url_or_id", ...],       # optional — enables retrieval metrics
        "retrieved_context": ["chunk text", ...],  # optional — enables judge faithfulness/context_relevance
        "refused": False,                       # optional — accurate refusal rate
        "input_tokens": 512,                    # optional — cost tracking
        "output_tokens": 128,                   # optional
    }
```

A plain `str` return also works — just get generation + latency metrics,
no retrieval/judge/cost metrics.

### ⚠️ The one thing that will silently break if you get it wrong

`citations` must be in the **same identifier space** as your golden
dataset's `source_url` (or `chunk_id`, if you set it explicitly). If your
`response_fn` returns URLs but your dataset only has `chunk_id`s (or vice
versa), every retrieval metric (hit rate, precision, recall, MRR) will
silently read as 0 — no error, no warning, just wrong numbers that look
like a real retrieval failure. See `eval_core.py`'s `EvalCase.relevant_ids`
docstring for the exact resolution order.

## Dataset format

JSON (list of objects) or CSV — same columns as
`build_golden_dataset_seed.py` output: `id, product_category, question,
expected_answer, source_url, chunk_id, chunk_index`.

Rows with blank `expected_answer` are skipped by default (unreviewed seed
rows). Pass `--include-unreviewed` to include them anyway — useful for
smoke-testing before SME review finishes.

**Ground truth integrity matters.** `expected_answer` must be written
independently — from the source content, or by an SME who hasn't seen the
model's answer — never derived from or paraphrased off what your pipeline
actually returned. Ground truth written from the model's own output makes
the eval circular (you're testing "does the model agree with itself," not
"is the model correct"), and every score becomes meaningless even though
the numbers look real. This applies to both `expected_answer` and any
`relevant_ids`/`source_url` used for retrieval scoring.

## LLM-as-judge — model selection matters

If you enable `judge_fn` (see `get_judge_fn()` in `run_eval.py`), pick a
judge model that is **not** used anywhere in your own answer-generation
pipeline — including any fast-path/routing model, not just your primary
one. Judging your own output risks self-evaluation bias (a model tends to
score its own phrasing/reasoning favorably). Identify every model your
pipeline actually routes to before picking a judge — don't assume a
"small" model is automatically generation-free just because it worked out
that way for us.

## Citation marker stripping

`metrics_generation.py`'s `strip_citation_markers()` assumes bracket-number
citation syntax (`[1]`, `[2]`, ...) is stripped before scoring, since that
formatting is UI rendering noise, not semantic content, and mechanically
drags down lexical metrics otherwise. **If your pipeline renders citations
differently** (footnotes, "Source: X" inline, no markers at all), update
that regex to match your format — or scores will carry unnecessary noise.

## Regression / benchmark workflow

```bash
# first run — establish baseline
python run_eval.py --dataset golden_dataset.json --model-label v1 --save-as-baseline

# later run — compare
python run_eval.py --dataset golden_dataset.json --model-label v2 \
    --compare-baseline eval_report_baseline.json
```

Flags any metric that moved >3% in the worse direction (`--threshold` not
yet exposed as CLI arg — edit `regression.compare_to_baseline()` default
if needed).

## Files

| File | Purpose |
|---|---|
| `eval_core.py` | `response_fn` contract, `EvalCase`/`EvalRun` data types (named to avoid colliding with any pipeline's own `core` package) |
| `metrics_generation.py` | token F1, sequence similarity, optional pluggable embedding similarity, citation marker stripping |
| `metrics_retrieval.py` | precision@k, recall@k, MRR, hit rate |
| `metrics_judge.py` | optional LLM-as-judge: faithfulness, answer relevance, correctness, context relevance |
| `metrics_operational.py` | refusal detection (explicit flag or heuristic fallback), cost resolution |
| `runner.py` | loads dataset, executes response_fn, aggregates |
| `report.py` | JSON + HTML dashboard output, including the metrics glossary |
| `regression.py` | baseline save/compare |
| `run_eval.py` | CLI entry point — **edit `get_response_fn()`/`get_judge_fn()`/`get_embed_fn()` here** |
| `backfill_sample_dataset.py` | optional one-off helper — pulls real citation URLs from a prior run's output into a sample dataset |

See `HANDOVER.md` for a checklist when adapting this framework to a
different pipeline (same or different underlying models).

## Status

Framework built and validated end-to-end against a real production
pipeline (not just dummy data) — real retrieval, real judge scoring (via
gpt-5-nano, selected specifically to avoid self-evaluation bias against
the pipeline's own generation models), real cost tracking, independently-
sourced ground truth (verified against source content, not derived from
model output). HTML dashboard includes a built-in metrics glossary with
domain examples and two caveat banners (precision limitations, tone/
formatting scoring philosophy) so the report is self-explaining for non-
technical reviewers.

## Coverage vs standard enterprise RAG eval practice

| Component | Status |
|---|---|
| Golden dataset | Built (seed generation + SME review workflow) |
| Retrieval metrics (id-matching) | Built — precision@k, recall@k, MRR, hit rate |
| Context relevance (LLM-judged) | Built — via optional `judge_fn` |
| Generation metrics (lexical) | Built — token F1, sequence similarity, citation-marker-aware |
| Generation metrics (embedding) | Built — optional `embed_fn` |
| LLM-as-judge (faithfulness/relevance/correctness) | Built — optional `judge_fn`, tone/framing-aware prompt |
| Operational metrics (latency, error rate) | Built |
| Operational metrics (cost, refusal rate) | Built — optional, response_fn-reported or heuristic fallback |
| Regression/CI gating | Built — baseline save/compare |
| Post-deployment continuous monitoring | Not built — separate Phase 3 item, deliberately out of scope for this pre-launch framework |
| Human-in-the-loop judge calibration | Not built — process activity once a judge_fn is chosen, not a framework feature |

## Optional pluggable hooks

All of these default to `None`/off — framework runs fine without any of
them, just with fewer metrics populated.

| Hook | Passed to | Enables |
|---|---|---|
| `embed_fn(text) -> vector` | `run_evaluation()` | semantic_similarity |
| `judge_fn(question, expected, actual, context) -> dict` | `run_evaluation()` | faithfulness, answer_relevance, correctness, context_relevance |
| `response_fn` returning `citations` | n/a (dict key) | retrieval metrics — **must match golden dataset's identifier space, see warning above** |
| `response_fn` returning `retrieved_context` | n/a (dict key) | enables judge_fn to score faithfulness/context_relevance |
| `response_fn` returning `refused` | n/a (dict key) | accurate refusal rate (else falls back to generic phrase heuristic) |
| `response_fn` returning `cost_usd` or `input_tokens`/`output_tokens` | n/a (dict key) | cost tracking (token-based needs `price_per_1k_input/output` too) |
