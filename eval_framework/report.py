"""
report.py — dashboard/report output for an EvalRun.

Two outputs:
  - write_json_report(run, path)  -> machine-readable, feeds regression.py
  - write_html_report(run, path)  -> human-readable dashboard, single file,
    no external assets (safe to email / open offline on VDI).

CHANGE LOG
v1.0.0 — Aug 2026 | Mukesh Kund — initial version.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from core import EvalRun


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

    cat_rows = "".join(
        f"<tr><td>{_esc(cat)}</td><td>{stats['num_cases']}</td><td>{stats.get('avg_token_f1', '-')}</td></tr>"
        for cat, stats in agg.get("by_category", {}).items()
    )

    case_rows = "".join(
        f"""<tr class="{'err-row' if c['error'] else ''}">
            <td>{_esc(c['id'])}</td>
            <td>{_esc(c['product_category'])}</td>
            <td>{_esc(c['question'])}</td>
            <td>{_esc(c['expected_answer'][:120])}</td>
            <td>{_esc(c['actual_answer'][:120])}</td>
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
</style></head>
<body>
<h1>Model Evaluation Report</h1>
<div class="meta">Run ID: {_esc(run.run_id)} &nbsp;|&nbsp; Model: {_esc(run.model_label)}</div>
<div class="kpis">{kpis}</div>
{precision_note}
<h2>By category</h2>
<table><tr><th>Category</th><th>Cases</th><th>Avg token F1</th></tr>{cat_rows}</table>
<h2>Per-case detail</h2>
<table><tr><th>ID</th><th>Category</th><th>Question</th><th>Expected</th><th>Actual</th><th>Token F1</th><th>Faithfulness</th><th>Correctness</th><th>Retrieval hit</th><th>Refused</th><th>Latency (s)</th><th>Error</th></tr>{case_rows}</table>
</body></html>"""
    Path(path).write_text(html, encoding="utf-8")
