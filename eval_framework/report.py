"""
report.py — dashboard/report output for an EvalRun.

Two outputs:
  - write_json_report(run, path)  -> machine-readable, feeds regression.py
  - write_html_report(run, path)  -> human-readable dashboard, single file,
    no external assets (safe to email / open offline on VDI).

CHANGE LOG
v1.5.0 — Aug 2026 | Mukesh Kund
         Cosmetic redesign — premium/professional visual pass, plus one
         functional addition:
         - Threshold-based KPI card color-coding (green/amber/red left
           border + value color) via new _kpi_class() helper — score
           metrics: green >=0.8, amber 0.5-0.79, red <0.5 (inverted for
           error/refusal rate; latency uses its own thresholds). This
           is functional, not just decorative — bad numbers are now
           visible at a glance without reading every value.
         - Full CSS rebuild: navy/teal palette, CSS custom properties,
           card hover-lift + shadow, table row hover highlight, custom
           rotating arrow on <details> open/close (pure CSS, no JS),
           refined typography and spacing. Still fully self-contained
           (no external fonts/CDN) — stays offline-safe.
         - precision_note/tone_note/sample_note now use shared
           .note-amber/.note-green classes instead of duplicated inline
           styles, for consistency with the new palette.
         ROLLBACK: revert kpi()/_kpi_class() to the plain version
         (v1.4.0), revert the <style> block, revert note classes back
         to inline style="..." strings.

v1.4.0 — Aug 2026 | Mukesh Kund
         Added _reading_guide_html() — dynamic "How to read this report"
         section, open by default (unlike the glossary, which stays
         collapsed), placed above the glossary. Gives priority order for
         interpreting the dashboard: error rate gate check, judge scores
         as primary signal, retrieval metrics for root-cause diagnosis
         (retrieval vs generation problem), precision/lexical metrics as
         secondary, operational metrics as a separate deployability
         question. Sample-size caveat is dynamic — reflects the actual
         num_cases of the run (amber warning below 30 cases, green note
         above), not a hardcoded number, so it stays accurate whether
         run against a 3-question smoke test or the full dataset.
         ROLLBACK: remove _reading_guide_html() and its {reading_guide}
         placement in write_html_report().

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


def _reading_guide_html(num_cases: int) -> str:
    """Dynamic — the sample-size caveat reflects the actual case count
    of this specific run, not a hardcoded number."""
    SMALL_SAMPLE_THRESHOLD = 30
    if num_cases < SMALL_SAMPLE_THRESHOLD:
        sample_note = (
            f'<p class="note-amber">'
            f'<b>Small sample — {num_cases} case(s):</b> this run is useful to '
            f'confirm the framework and pipeline are wired correctly end-to-end, '
            f'but {num_cases} question(s) is not enough to draw a real conclusion '
            f'about overall chatbot quality. Treat this as a plumbing check, not '
            f'a verdict. A meaningful quality conclusion needs the full reviewed '
            f'golden dataset (target: full SME-reviewed set, not a handful of '
            f'demo questions).</p>'
        )
    else:
        sample_note = (
            f'<p class="note-green">'
            f'<b>{num_cases} cases</b> — large enough to draw a meaningful overall '
            f'conclusion, though still worth checking the by-category breakdown '
            f'below for any single category that is thin or skewing the average.</p>'
        )

    return f"""
<details class="glossary" open>
<summary>How to read this report — start here</summary>
<div class="glossary-body">
<ol style="margin:8px 0 0 18px; padding:0;">
<li style="margin-bottom:8px;"><b>Check error rate first.</b> If it isn't
near 0, stop here — that's a technical/plumbing failure (crashes,
timeouts), not a quality question. Fix that before judging anything
else below.</li>

<li style="margin-bottom:8px;"><b>Judge scores are the primary quality
signal</b> — check these before the automated text-matching scores.
<ul style="margin:6px 0 0 18px;">
<li><b>Low faithfulness</b> &rarr; hallucination risk. Usually the most
serious finding for a regulated chatbot — the model stated something not
backed by its source content.</li>
<li><b>Low correctness</b> &rarr; wrong or incomplete answers, even if
not fabricated.</li>
<li><b>Low answer relevance</b> &rarr; answering a different question
than what was asked.</li>
<li><b>Low context relevance</b> &rarr; points at <i>retrieval</i>, not
generation — the model was handed bad content to work with.</li>
</ul></li>

<li style="margin-bottom:8px;"><b>Use retrieval metrics to locate WHERE a
problem comes from, not just whether one exists.</b> Low judge scores +
low hit rate/recall &rarr; retrieval is the root cause, fix the search/
index. Low judge scores + high hit rate/recall &rarr; the right content
was found but poorly used — that's a generation/prompt problem instead.
This is the most useful diagnostic split this report gives you.</li>

<li style="margin-bottom:8px;"><b>Treat precision, token F1, and semantic
similarity as secondary checks</b>, not headline numbers — see the two
amber notes below for why they can read artificially low even on a
genuinely good answer. Use them to flag something worth a closer look,
not to conclude something on their own.</li>

<li style="margin-bottom:0;"><b>Operational metrics (refusal rate, cost,
latency) answer a different question: "is this deployable," not "is this
correct."</b> A technically perfect answer that's too slow or too
expensive is still a real problem — just a different category of one.</li>
</ol>
</div>
</details>
{sample_note}
"""


def write_html_report(run: EvalRun, path: str) -> None:
    d = _run_to_dict(run)
    agg = d["aggregate"]
    gen = agg.get("generation", {})
    ret = agg.get("retrieval", {})
    lat = agg.get("latency", {})
    judge = agg.get("judge", {})
    op = agg.get("operational", {})
    reading_guide = _reading_guide_html(agg.get("num_cases", 0))

    def _kpi_class(label: str, value) -> str:
        """Threshold-based color coding. Score metrics (0-1 range,
        higher=better): green >=0.8, amber 0.5-0.79, red <0.5.
        Rate metrics where lower=better (error/refusal rate): inverted.
        Latency: green <10s, amber 10-30s, red >30s. Everything else
        (cases count, cost) stays neutral — no semantic "good/bad"
        threshold to apply."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return "neutral"
        lower_is_better = {"Error rate", "Refusal rate"}
        if label in lower_is_better:
            if v <= 0.05:
                return "good"
            if v <= 0.2:
                return "warn"
            return "bad"
        if "latency" in label.lower():
            if v < 10:
                return "good"
            if v <= 30:
                return "warn"
            return "bad"
        if label in {"Cases", "Avg cost/query ($)"}:
            return "neutral"
        # remaining are 0-1 score metrics, higher = better
        if 0.0 <= v <= 1.0:
            if v >= 0.8:
                return "good"
            if v >= 0.5:
                return "warn"
            return "bad"
        return "neutral"

    def kpi(label, value):
        cls = _kpi_class(label, value)
        return (f'<div class="kpi kpi-{cls}"><div class="kpi-val">{_esc(value)}</div>'
                f'<div class="kpi-label">{_esc(label)}</div></div>')

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
        '<p class="note-amber">'
        '<b>Note on precision:</b> the golden dataset records one known-relevant '
        'chunk per question. Answers that cite more than one source will show '
        'lower precision even when the extra citations are legitimate — those '
        'citations are simply unverified, not confirmed wrong. Treat hit rate '
        'and recall as the primary retrieval metrics; use precision as a '
        'flag to manually spot-check, not as a pass/fail gate.</p>'
    ) if ret.get("num_cases_with_citations") else ""

    tone_note = (
        '<p class="note-amber">'
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
:root {{
  --navy: #0b1f3a;
  --navy-light: #16305a;
  --accent: #0d6e6e;
  --accent-light: #e6f4f4;
  --good: #1a7f37;
  --good-bg: #eafaf0;
  --good-border: #b8e6c8;
  --warn: #9a6700;
  --warn-bg: #fff8e6;
  --warn-border: #f0d080;
  --bad: #c02626;
  --bad-bg: #fdecec;
  --bad-border: #f3bcbc;
  --neutral-bg: #ffffff;
  --border: #e2e5eb;
  --text: #1c2430;
  --text-muted: #66707e;
  --bg: #f4f6f9;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Helvetica Neue", Roboto, Arial, sans-serif;
  margin: 0; padding: 32px 40px 60px;
  color: var(--text); background: var(--bg);
  line-height: 1.45;
}}
h1 {{ font-size: 22px; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 4px; color: var(--navy); }}
h2 {{ font-size: 15px; font-weight: 700; margin: 36px 0 12px; color: var(--navy); letter-spacing: -0.005em; }}
.meta {{ color: var(--text-muted); font-size: 13px; margin-bottom: 24px; }}
.meta b {{ color: var(--text); font-weight: 600; }}

.kpis {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }}
.kpi {{
  background: var(--neutral-bg); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 20px; min-width: 128px;
  box-shadow: 0 1px 2px rgba(16,24,40,0.04);
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  border-left: 4px solid var(--border);
}}
.kpi:hover {{ transform: translateY(-2px); box-shadow: 0 6px 16px rgba(16,24,40,0.08); }}
.kpi-good {{ border-left-color: var(--good); }}
.kpi-good .kpi-val {{ color: var(--good); }}
.kpi-warn {{ border-left-color: var(--warn); }}
.kpi-warn .kpi-val {{ color: var(--warn); }}
.kpi-bad {{ border-left-color: var(--bad); }}
.kpi-bad .kpi-val {{ color: var(--bad); }}
.kpi-neutral {{ border-left-color: var(--navy-light); }}
.kpi-val {{ font-size: 23px; font-weight: 700; letter-spacing: -0.01em; color: var(--navy); }}
.kpi-label {{ font-size: 10.5px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .04em; margin-top: 3px; font-weight: 600; }}

table {{ border-collapse: collapse; width: 100%; background: var(--neutral-bg); font-size: 12.5px; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 2px rgba(16,24,40,0.04); }}
th, td {{ border-bottom: 1px solid var(--border); padding: 9px 12px; text-align: left; vertical-align: top; }}
th {{ background: var(--navy); color: #fff; font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: .03em; position: sticky; top: 0; }}
tbody tr {{ transition: background-color 0.1s ease; }}
tbody tr:hover {{ background-color: var(--accent-light); }}
.err-row {{ background: var(--bad-bg); }}
.err-row:hover {{ background-color: var(--bad-bg); }}

.toggle-link {{ font-size: 11px; color: var(--accent); text-decoration: none; white-space: nowrap; font-weight: 600; }}
.toggle-link:hover {{ text-decoration: underline; }}

.glossary {{
  margin-bottom: 18px; background: var(--neutral-bg); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 20px; box-shadow: 0 1px 2px rgba(16,24,40,0.04);
}}
.glossary summary {{
  cursor: pointer; font-weight: 700; font-size: 14px; color: var(--navy);
  list-style: none; display: flex; align-items: center; gap: 8px;
}}
.glossary summary::-webkit-details-marker {{ display: none; }}
.glossary summary::before {{
  content: "▸"; display: inline-block; color: var(--accent); font-size: 12px;
  transition: transform 0.15s ease;
}}
.glossary[open] summary::before {{ transform: rotate(90deg); }}
.glossary summary:hover {{ color: var(--accent); }}
.glossary-body {{ margin-top: 16px; font-size: 13px; line-height: 1.6; }}
.glossary-table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
.glossary-table th, .glossary-table td {{ border: 1px solid var(--border); padding: 9px 11px; text-align: left; vertical-align: top; font-size: 12px; }}
.glossary-table th {{ background: var(--navy); color: #fff; }}
.glossary-table tr:nth-child(even) td {{ background: #fafbfc; }}
.glossary-table td:first-child {{ white-space: nowrap; width: 170px; font-weight: 600; color: var(--navy); }}

.note-amber {{ font-size: 12.5px; color: var(--warn); background: var(--warn-bg); border: 1px solid var(--warn-border); border-radius: 8px; padding: 10px 14px; margin-bottom: 12px; }}
.note-green {{ font-size: 12.5px; color: var(--good); background: var(--good-bg); border: 1px solid var(--good-border); border-radius: 8px; padding: 10px 14px; margin-bottom: 12px; }}
</style></head>
<body>
<h1>Model Evaluation Report</h1>
<div class="meta">Run ID: <b>{_esc(run.run_id)}</b> &nbsp;|&nbsp; Model: <b>{_esc(run.model_label)}</b></div>
<div class="kpis">{kpis}</div>
{reading_guide}
{METRICS_GLOSSARY_HTML}
{precision_note}
{tone_note}
<h2>By category</h2>
<table><tr><th>Category</th><th>Cases</th><th>Avg token F1</th></tr>{cat_rows}</table>
<h2>Per-case detail</h2>
<table><tr><th>ID</th><th>Category</th><th>Question</th><th>Expected</th><th>Actual</th><th>Token F1</th><th>Faithfulness</th><th>Correctness</th><th>Retrieval hit</th><th>Refused</th><th>Latency (s)</th><th>Error</th></tr>{case_rows}</table>
</body></html>"""
    Path(path).write_text(html, encoding="utf-8")