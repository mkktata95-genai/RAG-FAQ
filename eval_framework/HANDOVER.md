# HANDOVER — Adapting this framework to another pipeline

This framework was built and validated against the RLG Digital Assistance
pipeline (LangGraph, Azure OpenAI Foundry, GPT-5 family). If your team is
on the **same Foundry tenant/models but a different pipeline**, most of
the engine below is directly reusable — only `run_eval.py` needs real work.

## What's fully portable — no changes needed

| File | Why it's safe to reuse as-is |
|---|---|
| `eval_core.py` | Generic data types and contract, zero pipeline coupling |
| `metrics_retrieval.py` | Pure math (precision/recall/MRR/hit) — identifier-agnostic |
| `runner.py` | Generic orchestration |
| `regression.py` | Generic baseline compare |
| `metrics_generation.py`* | Generic lexical/embedding scoring — *see citation marker caveat below* |
| `metrics_judge.py`* | Generic judge-calling wrapper — *see prompt wording caveat below* |
| `metrics_operational.py`* | Generic refusal/cost resolution — *see refusal heuristic caveat below* |
| `report.py`* | Generic HTML/JSON output — *see tone-note wording caveat below* |

Items marked `*` are structurally reusable but contain content that was
written for RLG's specific product behavior — read the caveats before
assuming zero changes needed.

## What you MUST change — `run_eval.py`

This is the one file with real integration work, split into three hooks:

### 1. `get_response_fn()` — full rewrite

Nothing here is reusable line-by-line since your pipeline is different.
What must carry over is the **shape** of what you return:

```python
{
    "answer": str,
    "citations": [str, ...],        # optional
    "retrieved_context": [str, ...],# optional
    "refused": bool,                 # optional
    "input_tokens": int,             # optional
    "output_tokens": int,            # optional
}
```

**Before writing this, answer two questions:**

- **What format are your citations in?** (URL? internal doc ID? chunk ID?)
  Whatever it is, your golden dataset's `source_url`/`chunk_id`/
  `relevant_ids` field must use the exact same format, or every retrieval
  metric will silently read as 0 — this bit us during our own build
  (see `eval_core.py` v1.1.0 changelog) and won't throw an error, just
  wrong numbers that look like a real retrieval failure.

- **What does your citation rendering look like in the answer text?**
  If your generator inlines citation markers into the answer (like our
  `[1]`, `[2]` bracket-number style), `metrics_generation.py`'s
  `strip_citation_markers()` regex (`\[\d+\]`) needs to match your format
  or be adjusted/replaced. If you don't inline markers at all, you can
  skip this — the function is a no-op on text with no matches.

### 2. `get_judge_fn()` — mostly reusable client setup, but re-derive the model choice

Since you're on the same Foundry tenant, the `AzureOpenAI` +
`DefaultAzureCredential` + `get_bearer_token_provider` boilerplate should
work unchanged. What you must re-derive yourself:

- **Which model(s) does YOUR pipeline actually use for generation?**
  Don't assume gpt-5-nano is safe just because it was safe for us — that
  was true specifically because RLG's pipeline routes generation to
  gpt-5.6-luna/gpt-5-mini only, with nano reserved for intent
  classification. Your pipeline may route differently. Identify every
  model your pipeline calls in the answer-generation path first, then
  pick a judge model that's in none of them.
- **Reword the tone/framing paragraph in `JUDGE_PROMPT_TEMPLATE`**
  (`metrics_judge.py`). It currently says "empathy statement (e.g.
  bereavement)... mandatory disclaimers" — that's RLG-specific product
  behavior. Replace with whatever required tone/compliance framing your
  own product has, or remove the paragraph if you have none.

### 3. `get_embed_fn()` — reusable client setup, confirm deployment name

Same Foundry auth pattern applies. Just confirm
`AZURE_OPENAI_EMBEDDING_DEPLOYMENT` (env var) points at your actual
embedding deployment name — don't assume `text-embedding-3-large` is
correct for your resource without checking.

### 4. Pricing defaults

`PRICE_PER_1K_INPUT_DEFAULT` / `PRICE_PER_1K_OUTPUT_DEFAULT` at the top of
`run_eval.py` are gpt-5.6-luna's confirmed Aug 2026 rate ($0.20/$1.20 per
1M tokens). Wrong for you unless your primary generation model is the
same. Either update the constants, or always pass
`--price-per-1k-input`/`--price-per-1k-output` explicitly on the CLI.

## Minor cosmetic items (optional, not functional bugs)

- `report.py`'s `tone_note` banner text mentions "empathy openers" —
  reword for your product's actual required framing, or delete the
  banner if not applicable (search for `tone_note` in `report.py`).
- `metrics_operational.py`'s `GENERIC_REFUSAL_PHRASES` list is
  deliberately conservative/generic — fine to reuse, but prefer passing
  an explicit `refused` flag from your own pipeline's gate logic instead
  of relying on phrase-matching, same as we do.

## Ground truth integrity — applies to you regardless of pipeline

Whoever builds your `golden_dataset.json`'s `expected_answer` values must
write them from source content (or product knowledge) independently —
never by paraphrasing what your pipeline's own model actually answered.
Ground truth derived from the model's own output makes every score
circular and meaningless, even though the numbers will look real (often
suspiciously perfect). We hit this exact mistake during our own build —
see chat history / `sample_golden_dataset.json`'s two versions for a
concrete before/after example of what circular vs. independent ground
truth looks like in practice.

## Suggested first steps

1. Read `README.md` in full first — this doc assumes it.
2. Write `get_response_fn()` against your pipeline, confirm citation
   format compatibility with your dataset before running anything.
3. Run once with `--no-judge --no-embed` first (dummy dataset is fine) —
   confirms basic wiring/citations/retrieval scoring work before adding
   judge/embedding complexity.
4. Add `get_judge_fn()`/`get_embed_fn()` once step 3 is clean.
5. Point at your real golden dataset, run for real.
