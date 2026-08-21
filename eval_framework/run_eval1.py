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
v1.1.0 — Aug 2026 | Mukesh Kund
         Wired get_response_fn() to the real RLG pipeline via graph.py's
         run_query(). Local import inside the wrapper only — framework
         package itself still has zero pipeline dependency. Maps
         AgentState.final_response -> answer, Citation.url -> citations
         (already parent_url-resolved per retriever.py's citation logic),
         refusal_triggered -> refused, token_usage dict -> cost tracking.
         RLG_PIPELINE_ROOT path is a guess — confirm/adjust before running.
         ROLLBACK: restore dummy_response_fn from v1.0.0.

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

    def rlg_response_fn(question: str) -> dict:
        # Local import — keeps the framework itself free of any RLG
        # pipeline dependency; only this one wrapper function touches it.
        import sys, os
        pipeline_root = os.getenv("RLG_PIPELINE_ROOT", r"C:\Users\MKund\Desktop\RAG\core")
        if pipeline_root not in sys.path:
            sys.path.insert(0, pipeline_root)
        from graph import run_query  # noqa: E402

        result = run_query(question)  # returns AgentState (schemas.py)

        return {
            "answer": result.final_response or "",
            "citations": [c.url for c in result.citations],
            "refused": result.refusal_triggered,
            "input_tokens": result.token_usage.get("input_tokens"),
            "output_tokens": result.token_usage.get("output_tokens"),
        }

    return rlg_response_fn


def get_judge_fn():
    """
    OPTIONAL convenience hook, same pattern as get_response_fn(). Return
    None (default) to skip LLM-as-judge scoring entirely — the framework
    runs fine without it, just without faithfulness/answer_relevance/
    correctness/context_relevance metrics.

    Currently OFF (returns None) — turn on once:
      1. A real golden_dataset.json with SME-reviewed expected_answer
         exists (judge scoring against blank/dummy answers is meaningless)
      2. Judge model choice confirmed — use gpt-5-nano, NOT gpt-5.6-luna
         or gpt-5-mini. Both of those are live production generation
         deployments (DEPLOYMENT_MAIN / DEPLOYMENT_FAST in generator.py,
         routed per-query by supervisor.py), so either would risk
         self-evaluation bias — judging its own answers on a chunk of
         real traffic. gpt-5-nano is never used for generation (only
         classify_intent() gating today), so it's bias-free by
         construction. Validate scoring quality on ~10-15 cases against
         your own judgement before trusting it at scale — nano is the
         smallest model in the stack, so if scores look unreliable,
         fall back to dynamically picking whichever of mini/luna did
         NOT generate that specific answer (AgentState.model_used
         already tracks this per response).

    See metrics_judge.py — example_judge_fn_using_your_llm() wraps any
    plain str->str LLM call function into a valid judge_fn. Example
    wiring (commented out, ready to enable):

        def get_judge_fn():
            import sys, os
            pipeline_root = os.getenv("RLG_PIPELINE_ROOT", r"C:\\Users\\MKund\\Desktop\\RAG\\core")
            if pipeline_root not in sys.path:
                sys.path.insert(0, pipeline_root)
            from openai import AzureOpenAI
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from metrics_judge import example_judge_fn_using_your_llm

            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
            client = AzureOpenAI(
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                azure_ad_token_provider=token_provider,
                api_version="2024-10-21",
            )

            def llm_call(prompt: str) -> str:
                # gpt-5-nano — deliberately NOT gpt-5.6-luna or gpt-5-mini,
                # since both of those generate real production answers
                # (DEPLOYMENT_MAIN / DEPLOYMENT_FAST). nano is judge-only,
                # never in the generation path, so no self-eval bias.
                resp = client.chat.completions.create(
                    model="gpt-5-nano",
                    messages=[{"role": "user", "content": prompt}],
                    max_completion_tokens=500,
                )
                return resp.choices[0].message.content

            return example_judge_fn_using_your_llm(llm_call)
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