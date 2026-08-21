"""
report.py — dashboard/report output for an EvalRun.

Two outputs:
  - write_json_report(run, path)  -> machine-readable, feeds regression.py
  - write_html_report(run, path)  -> human-readable dashboard, single file,
    no external assets (safe to email / open offline on VDI).

CHANGE LOG
v1.3.0 — Aug 2026 | Mukesh Kund
         Added METRICS_GLOSSARY_HTML — static, plain-language glossary
         covering every metric on the dashboard (including an
         LLM-as-judge explainer) with a domain-relevant example per
         metric, for non-AI-background readers (e.g. PO). Collapsed by
         default via native <details>/<summary> (no JS needed for a
         whole-section toggle, unlike the per-cell _expandable()).
         Same content every report — not dynamic per what metrics are
         present in a given run.
         ROLLBACK: remove METRICS_GLOSSARY_HTML constant and its
         {METRICS_GLOSSARY_HTML} placement in write_html_report().

v1.2.0 — Aug 2026 | Mukesh Kund
         Added tone_note banner next to the KPI row, explaining that
         expected_answer is factual-content-only and doesn't include
         required empathy/disclaimer framing or citation markers —
         so lexical metrics (not tone-aware) may read lower than judge
         scores on tone-sensitive cases, which is expected, not a
         quality problem. Companion to metrics_judge.py v1.2.0's prompt
         fix and metrics_generation.py v1.1.0's marker stripping.
         ROLLBACK: remove tone_note and its {tone_note} placement.

v1.1.0 — Aug 2026 | Mukesh Kund
         Added click-to-expand for expected_answer/actual_answer in the
         per-case table. Previously hard-truncated at 120 chars with no
         way to see the rest in the HTML view (full text was only in
         eval_report.json) — problem for FCA-regulated content where
         exact wording matters. Pure inline JS toggle, no external
         libs/fetch — full text is already in the DOM, just hidden,
         so it works fully offline.
         ROLLBACK: revert case_rows to c['expected_answer'][:120] /
         c['actual_answer'][:120] with _esc() directly, remove
         _expandable() and the .toggle-link CSS block.

v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from eval_core import EvalRun

# Static glossary — same content in every report, plain-language,
# domain-relevant examples so a non-AI-background reader (e.g. PO)
# can understand each metric without a separate briefing.
METRICS_GLOSSARY_HTML = """
<details class="glossary">
<summary>What do these metrics mean? (click to expand)</summary>
<div class="glossary-body">

<p><b>How scoring works:</b> Two different methods are used together.
(1) <b>Automated text-matching</b> — simple algorithms that compare the
chatbot's answer to a reference answer word-by-word or via similarity
math. Fast and free, but can be misleading — a correct answer phrased
differently can score low here. (2) <b>LLM-as-judge</b> — a separate AI
model (not the same one that answered the question) reads the chatbot's
answer, the reference answer, and the source content, then rates the
answer against a rubric — similar to how a human reviewer would, but
automated so every question gets checked consistently. The judge scores
below (faithfulness, answer relevance, correctness, context relevance)
are generally more reliable than the automated text-matching scores.</p>

<table class="glossary-table">
<tr><th>Metric</th><th>What it means</th><th>Example</th></tr>

<tr><td><b>Faithfulness</b></td>
<td>Does the answer only state facts that are actually backed by the
source content the chatbot retrieved? Catches made-up information.</td>
<td>Source says pension age is 55. Answer says "55" &rarr; faithful (1.0).
Answer says "50" (not in the source) &rarr; unfaithful, near 0.</td></tr>

<tr><td><b>Answer relevance</b></td>
<td>Does the answer actually address the question that was asked?</td>
<td>Question: "How do I report a bereavement?" Answer describes ISA
transfers instead &rarr; low relevance, even if that content is itself
accurate about something else.</td></tr>

<tr><td><b>Correctness (judge)</b></td>
<td>Does the substance of the answer match the true/reference answer,
even if worded differently?</td>
<td>Reference: "pension age is 55, rising to 57 in 2028." Answer: "you
can take your pension from 55, increasing to 57 from 2028" &rarr; correct
(1.0) despite different wording.</td></tr>

<tr><td><b>Context relevance</b></td>
<td>Was the content the chatbot pulled from the knowledge base actually
useful for answering this specific question?</td>
<td>Question about ISA transfers retrieves a page about pensions instead
&rarr; low context relevance — signals a retrieval problem even if the
final answer happens to be okay.</td></tr>

<tr><td><b>Retrieval hit rate</b></td>
<td>Across all questions, in what percentage did the chatbot find and
cite at least the correct source page?</td>
<td>3 questions, correct source cited in all 3 &rarr; hit rate 100%.</td></tr>

<tr><td><b>Retrieval recall</b></td>
<td>Of the source page(s) that should have been cited for a question,
how many actually were?</td>
<td>If a question should cite 1 known page and the chatbot cites it
&rarr; recall 1.0, regardless of how many other pages it also cited.</td></tr>

<tr><td><b>MRR</b> (Mean Reciprocal Rank)</td>
<td>When the right source was found, how high up was it in the list of
citations returned? 1.0 = always the first citation listed.</td>
<td>Correct source cited 1st &rarr; 1.0. Cited 3rd out of 5 &rarr; 0.33.</td></tr>

<tr><td><b>Precision</b></td>
<td>How "clean" the citation list was — see the amber note above the
category table for why this can look artificially low even on good
answers. Treat as a flag to spot-check, not a pass/fail score.</td>
<td>Answer correctly cites 1 known-relevant page plus 1 extra
(unverified, not necessarily wrong) page &rarr; precision 0.5, even
though nothing was actually incorrect.</td></tr>

<tr><td><b>Avg token F1 / sequence similarity / semantic similarity</b></td>
<td>Automated wording-comparison checks (not the AI judge) — useful as a
rough sanity check only. A thorough, correct answer that's longer or
phrased differently than the short reference answer can score low here
even when the judge scores above rate it highly correct.</td>
<td>Reference: "Yes, you can transfer your ISA." Full correct answer with
extra detail about the process &rarr; low word-overlap score despite
being a better, fully correct answer.</td></tr>

<tr><td><b>Refusal rate</b></td>
<td>How often the chatbot declined to answer at all (e.g. for
out-of-scope or restricted topics).</td>
<td>Question outside approved content &rarr; chatbot correctly declines
&rarr; counted here, not as an error.</td></tr>

<tr><td><b>Avg cost / query</b></td>
<td>Real cost in £/$ per question, based on actual token usage and
current model pricing.</td>
<td>A longer, more detailed answer costs more in output tokens than a
short one — this tracks that real spend.</td></tr>

<tr><td><b>Latency (mean / P95)</b></td>
<td>How long, in seconds, the chatbot took to respond. P95 shows the
slower end of typical responses — 95% of queries were faster than this.</td>
<td>Mean 5s, P95 12s &rarr; most answers come back in ~5s, but the
slowest 1-in-20 can take up to 12s.</td></tr>

<tr><td><b>Error rate</b></td>
<td>Percentage of questions where the pipeline itself failed to return
any answer at all — a technical failure, not a quality/correctness
issue.</td>
<td>A timeout or crash mid-request &rarr; counted here, separate from
whether an answer that WAS returned was good or bad.</td></tr>

</table>
</div>
</details>
"""


def _run_to_dict(run: EvalRun) -> dict:
    return {
        "run_id": run.run_id,
        "model_label": run.model_label,
        "aggregate": run.aggregate,
        "cases": [
            {
                "id": r.case.id,
                "question": r.case.question,
                "product_category": r.case.product_category,
                "expected_answer": r.case.expected_answer,
                "actual_answer": r.response.answer,
                "latency_seconds": round(r.response.latency_seconds, 4),
                "error": r.response.error,
                "generation_scores": r.generation_scores,
                "retrieval_scores": r.retrieval_scores,
                "judge_scores": r.judge_scores,
                "citations": r.response.citations,
                "refused": r.response.refused,
                "cost_usd": r.response.cost_usd,
            }
            for r in run.case_results
        ],
    }


def write_json_report(run: EvalRun, path: str) -> None:
    Path(path).write_text(json.dumps(_run_to_dict(run), indent=2), encoding="utf-8")


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def write_html_report(run: EvalRun, path: str) -> None:
    d = _run_to_dict(run)
    agg = d["aggregate"]
    gen = agg.get("generation", {})
    ret = agg.get("retrieval", {})
    lat = agg.get("latency", {})
    judge = agg.get("judge", {})
    op = agg.get("operational", {})

    def kpi(label, value):
        return f'<div class="kpi"><div class="kpi-val">{_esc(value)}</div><div class="kpi-label">{_esc(label)}</div></div>'

    kpis = "".join([
        kpi("Cases", agg.get("num_cases", 0)),
        kpi("Error rate", agg.get("error_rate", "-")),
        kpi("Avg token F1", gen.get("avg_token_f1", "-")),
        kpi("Avg semantic sim", gen.get("avg_semantic_similarity", "n/a")),
        kpi("Faithfulness", judge.get("avg_faithfulness", "n/a")),
        kpi("Answer relevance", judge.get("avg_answer_relevance", "n/a")),
        kpi("Correctness (judge)", judge.get("avg_correctness", "n/a")),
        kpi("Context relevance", judge.get("avg_context_relevance", "n/a")),
        kpi("Retrieval hit rate", ret.get("hit_rate", "n/a")),
        kpi("Retrieval recall", ret.get(f"avg_recall_at_5", "n/a")),
        kpi("MRR", ret.get("mrr", "n/a")),
        kpi("Refusal rate", op.get("refusal_rate", "n/a")),
        kpi("Avg cost/query ($)", op.get("avg_cost_usd", "n/a")),
        kpi("Mean latency (s)", lat.get("mean_seconds", "-")),
        kpi("P95 latency (s)", lat.get("p95_seconds", "-")),
    ])
    precision_note = (
        '<p style="font-size:12px;color:#a05a00;background:#fff8e6;'
        'border:1px solid #f0d080;border-radius:6px;padding:8px 12px;">'
        '<b>Note on precision:</b> the golden dataset records one known-relevant '
        'chunk per question. Answers that cite more than one source will show '
        'lower precision even when the extra citations are legitimate — those '
        'citations are simply unverified, not confirmed wrong. Treat hit rate '
        'and recall as the primary retrieval metrics; use precision as a '
        'flag to manually spot-check, not as a pass/fail gate.</p>'
    ) if ret.get("num_cases_with_citations") else ""

    tone_note = (
        '<p style="font-size:12px;color:#a05a00;background:#fff8e6;'
        'border:1px solid #f0d080;border-radius:6px;padding:8px 12px;">'
        '<b>Note on scoring vs required tone/formatting:</b> expected_answer '
        'is factual content only — it does not include required empathy '
        'openers, disclaimers, or citation marker syntax ([1], [2], ...) that '
        'the chatbot is required to add. Citation markers are stripped before '
        'scoring; judge_fn is explicitly instructed to score factual content '
        'only and ignore tone/framing. Lexical metrics (token F1, sequence '
        'similarity) are NOT tone-aware and may still read lower than the '
        'judge scores on answers with required empathy framing — this is '
        'expected, not a quality issue; treat judge scores as the more '
        'reliable signal when the two disagree on a tone-sensitive case.</p>'
    )

    def _expandable(text: str, max_len: int = 120) -> str:
        """Truncated by default with a 'show more' toggle — full text
        is always in the DOM (just hidden), no re-fetch needed, works
        fully offline."""
        escaped_full = _esc(text)
        if len(text) <= max_len:
            return escaped_full
        escaped_short = _esc(text[:max_len])
        return (
            f'<span class="truncated">{escaped_short}&hellip; '
            f'<a href="#" class="toggle-link" onclick="'
            f'this.parentElement.style.display=\'none\';'
            f'this.parentElement.nextElementSibling.style.display=\'inline\';'
            f'return false;">show more</a></span>'
            f'<span class="full" style="display:none;">{escaped_full} '
            f'<a href="#" class="toggle-link" onclick="'
            f'this.parentElement.style.display=\'none\';'
            f'this.parentElement.previousElementSibling.style.display=\'inline\';'
            f'return false;">show less</a></span>'
        )

    cat_rows = "".join(
        f"<tr><td>{_esc(cat)}</td><td>{stats['num_cases']}</td><td>{stats.get('avg_token_f1', '-')}</td></tr>"
        for cat, stats in agg.get("by_category", {}).items()
    )

    case_rows = "".join(
        f"""<tr class="{'err-row' if c['error'] else ''}">
            <td>{_esc(c['id'])}</td>
            <td>{_esc(c['product_category'])}</td>
            <td>{_esc(c['question'])}</td>
            <td>{_expandable(c['expected_answer'])}</td>
            <td>{_expandable(c['actual_answer'])}</td>
            <td>{c['generation_scores'].get('token_f1', '-')}</td>
            <td>{c['judge_scores'].get('faithfulness', '-')}</td>
            <td>{c['judge_scores'].get('correctness', '-')}</td>
            <td>{c['retrieval_scores'].get('hit', '-') if c['retrieval_scores'] else '-'}</td>
            <td>{c['refused']}</td>
            <td>{c['latency_seconds']}</td>
            <td>{_esc(c['error'] or '')}</td>
        </tr>"""
        for c in d["cases"]
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Eval Report — {_esc(run.model_label)}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 24px; color: #1a1a1a; background: #fafafa; }}
h1 {{ font-size: 20px; }}
h2 {{ font-size: 15px; margin-top: 32px; }}
.meta {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
.kpis {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }}
.kpi {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px 18px; min-width: 120px; }}
.kpi-val {{ font-size: 22px; font-weight: 600; }}
.kpi-label {{ font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: .03em; }}
table {{ border-collapse: collapse; width: 100%; background: #fff; font-size: 12px; }}
th, td {{ border: 1px solid #e0e0e0; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f0f0f0; position: sticky; top: 0; }}
.err-row {{ background: #fff0f0; }}
.toggle-link {{ font-size: 11px; color: #0066cc; text-decoration: none; white-space: nowrap; }}
.toggle-link:hover {{ text-decoration: underline; }}
.glossary {{ margin-bottom: 24px; background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px 16px; }}
.glossary summary {{ cursor: pointer; font-weight: 600; font-size: 14px; }}
.glossary-body {{ margin-top: 14px; font-size: 13px; line-height: 1.5; }}
.glossary-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
.glossary-table th, .glossary-table td {{ border: 1px solid #e0e0e0; padding: 8px 10px; text-align: left; vertical-align: top; font-size: 12px; }}
.glossary-table th {{ background: #f0f0f0; }}
.glossary-table td:first-child {{ white-space: nowrap; width: 160px; }}
</style></head>
<body>
<h1>Model Evaluation Report</h1>
<div class="meta">Run ID: {_esc(run.run_id)} &nbsp;|&nbsp; Model: {_esc(run.model_label)}</div>
<div class="kpis">{kpis}</div>
{METRICS_GLOSSARY_HTML}
{precision_note}
{tone_note}
<h2>By category</h2>
<table><tr><th>Category</th><th>Cases</th><th>Avg token F1</th></tr>{cat_rows}</table>
<h2>Per-case detail</h2>
<table><tr><th>ID</th><th>Category</th><th>Question</th><th>Expected</th><th>Actual</th><th>Token F1</th><th>Faithfulness</th><th>Correctness</th><th>Retrieval hit</th><th>Refused</th><th>Latency (s)</th><th>Error</th></tr>{case_rows}</table>
</body></html>"""
    Path(path).write_text(html, encoding="utf-8")