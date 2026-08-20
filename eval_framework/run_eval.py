"""
run_eval.py — entry point. Wire your team's response_fn here and run.

    python run_eval.py --dataset sample_golden_dataset.json --model-label my_model_v1

get_response_fn() below is just ONE convenience wiring point if you want
to use this CLI as-is. It is NOT required — any team can instead import
runner.run_evaluation() directly in their own script and pass in a
response_fn written however they like (any calling convention, any
pipeline, any framework). This file has no special status; nothing else
in the package imports it or depends on its contents.

CHANGE LOG
v1.0.0 — Aug 2026 | Mukesh Kund — initial version, smoke-tested against
         sample_golden_dataset.json with a dummy response_fn.
"""

from __future__ import annotations

import argparse

from runner import load_dataset, run_evaluation
from report import write_json_report, write_html_report
from regression import save_baseline, compare_to_baseline, print_regression_summary


def get_response_fn():
    """
    Optional convenience hook for this CLI. Edit it if you want to use
    `python run_eval.py ...`. Not required — see module docstring.

    Any callable of this shape works, regardless of what's inside it:
        str                                    -> just the answer
        {"answer": str, "citations": [str,...]} -> answer + retrieval metrics
    """
    def dummy_response_fn(question: str) -> dict:
        # Placeholder — replace with your model. This dummy just echoes
        # back something plausible so the framework can be smoke-tested.
        return {
            "answer": f"[dummy answer for]: {question}",
            "citations": ["chunk_pension_age_001"],
        }
    return dummy_response_fn


def get_judge_fn():
    """
    OPTIONAL convenience hook, same pattern as get_response_fn(). Return
    None (default) to skip LLM-as-judge scoring entirely — the framework
    runs fine without it, just without faithfulness/answer_relevance/
    correctness/context_relevance metrics.

    See metrics_judge.py — example_judge_fn_using_your_llm() wraps any
    plain str->str LLM call function into a valid judge_fn if you want
    to wire one up quickly.
    """
    return None


def main():
    parser = argparse.ArgumentParser(description="Run the Model Evaluation Framework")
    parser.add_argument("--dataset", required=True, help="Path to golden dataset (.json or .csv)")
    parser.add_argument("--model-label", default="unnamed_model")
    parser.add_argument("--include-unreviewed", action="store_true",
                         help="Include rows with blank expected_answer (smoke-testing only)")
    parser.add_argument("--out-prefix", default="eval_report", help="Prefix for output files")
    parser.add_argument("--save-as-baseline", action="store_true",
                         help="Store this run's aggregate metrics as the new baseline")
    parser.add_argument("--compare-baseline", default=None,
                         help="Path to a previously saved baseline JSON to regress against")
    parser.add_argument("--price-per-1k-input", type=float, default=None,
                         help="Optional $ per 1k input tokens, used to compute cost if response_fn doesn't report cost_usd directly")
    parser.add_argument("--price-per-1k-output", type=float, default=None,
                         help="Optional $ per 1k output tokens")
    args = parser.parse_args()

    cases = load_dataset(args.dataset, include_unreviewed=args.include_unreviewed)
    print(f"Loaded {len(cases)} case(s) from {args.dataset}")

    response_fn = get_response_fn()
    judge_fn = get_judge_fn()
    run = run_evaluation(
        cases, response_fn, model_label=args.model_label, judge_fn=judge_fn,
        price_per_1k_input=args.price_per_1k_input,
        price_per_1k_output=args.price_per_1k_output,
    )

    json_path = f"{args.out_prefix}.json"
    html_path = f"{args.out_prefix}.html"
    write_json_report(run, json_path)
    write_html_report(run, html_path)
    print(f"\nReports written: {json_path}, {html_path}")
    print(f"Aggregate summary: {run.aggregate}")

    if args.save_as_baseline:
        baseline_path = f"{args.out_prefix}_baseline.json"
        save_baseline(run, baseline_path)
        print(f"Baseline saved: {baseline_path}")

    if args.compare_baseline:
        result = compare_to_baseline(run, args.compare_baseline)
        print_regression_summary(result)


if __name__ == "__main__":
    main()
