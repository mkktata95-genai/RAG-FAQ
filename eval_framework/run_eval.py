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
v1.2.0 — Aug 2026 | Mukesh Kund
         Demo-readiness pass, for a fully-populated 3-query dashboard
         (no metric left n/a):
         - rlg_response_fn now also returns retrieved_context (chunk
           content, from AgentState.retrieved_chunks) — required for
           judge_fn to score faithfulness/context_relevance.
         - get_embed_fn() added and wired into main() — real
           text-embedding-3-large call, fixes avg_semantic_similarity.
         - get_judge_fn() turned ON by default (was returning None) —
           real gpt-5-nano call, fixes faithfulness/answer_relevance/
           correctness/context_relevance.
         - Real Azure OpenAI pricing defaults added for --price-per-1k-*
           (gpt-5.6-luna confirmed at $0.20/$1.20 per 1M tokens as of
           Aug 2026; gpt-5-mini rate NOT independently confirmed —
           verify against your actual Azure contract before trusting
           cost numbers if gpt-5-mini handled any of the queries).
         Depends on eval_core.py's relevant_ids fix (source_url-based,
         not chunk_id-based) — see eval_core.py v1.1.0 changelog.
         ROLLBACK: restore get_judge_fn() to `return None`, remove
         --price-per-1k-* defaults, remove retrieved_context/embed_fn wiring.

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
import os
import sys

from runner import load_dataset, run_evaluation
from report import write_json_report, write_html_report
from regression import save_baseline, compare_to_baseline, print_regression_summary

# Real Azure OpenAI pricing, per-1k-token, as confirmed Aug 2026 (see
# changelog above for sourcing). Used as CLI defaults for cost tracking.
PRICE_PER_1K_INPUT_DEFAULT = 0.0002    # gpt-5.6-luna: $0.20 / 1M input tokens
PRICE_PER_1K_OUTPUT_DEFAULT = 0.0012   # gpt-5.6-luna: $1.20 / 1M output tokens


def _get_pipeline_root() -> str:
    return (
        os.getenv("RLG_PROJECT_ROOT")
        or os.getenv("RLG_PIPELINE_ROOT")
        or r"C:\Users\MKund\Desktop\RAG"
    )


def _ensure_pipeline_on_path():
    root = _get_pipeline_root()
    if root not in sys.path:
        sys.path.insert(0, root)


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
        _ensure_pipeline_on_path()
        from core.graph import run_query  # noqa: E402

        result = run_query(question)  # returns AgentState (schemas.py)

        return {
            "answer": result.final_response or "",
            "citations": [c.url for c in result.citations],
            # retrieved_context: chunk content, not just URLs — needed
            # for judge_fn to actually assess faithfulness/context
            # relevance against what the model saw, not just where it
            # pointed.
            "retrieved_context": [rc.content for rc in result.retrieved_chunks],
            "refused": result.refusal_triggered,
            "input_tokens": result.token_usage.get("input_tokens"),
            "output_tokens": result.token_usage.get("output_tokens"),
        }

    return rlg_response_fn


def get_embed_fn():
    """
    Optional convenience hook. Wires the real text-embedding-3-large
    deployment (already used elsewhere in the pipeline, per embeddings.py)
    so avg_semantic_similarity is a real cosine-similarity score instead
    of n/a. Returns None to disable — framework runs fine without it.
    """
    _ensure_pipeline_on_path()
    from openai import AzureOpenAI
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = AzureOpenAI(
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        azure_ad_token_provider=token_provider,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )

    def embed_fn(text: str) -> list[float]:
        resp = client.embeddings.create(
            model=os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"),
            input=text,
        )
        return resp.data[0].embedding

    return embed_fn


def get_judge_fn():
    """
    Optional convenience hook. Wires gpt-5-nano as judge — deliberately
    NOT gpt-5.6-luna or gpt-5-mini, since both of those are live
    production generation deployments (DEPLOYMENT_MAIN / DEPLOYMENT_FAST
    in generator.py, routed per-query by supervisor.py); using either as
    judge risks self-evaluation bias on whichever share of traffic they
    generated. gpt-5-nano is judge-only here, never in the generation
    path (it's only used for classify_intent() gating today), so this
    is bias-free by construction.

    Validate scoring quality on ~10-15 real cases against your own
    judgement before trusting it at scale — nano is the smallest model
    in the stack. If scores look unreliable, the fallback is dynamically
    picking whichever of mini/luna did NOT generate that specific
    answer (AgentState.model_used already tracks this per response) —
    not currently implemented, would need score_with_judge() extended
    to accept a per-case model hint.

    Returns None to disable — framework runs fine without it, just
    without faithfulness/answer_relevance/correctness/context_relevance.
    """
    _ensure_pipeline_on_path()
    from openai import AzureOpenAI
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from metrics_judge import example_judge_fn_using_your_llm

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = AzureOpenAI(
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        azure_ad_token_provider=token_provider,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )

    def llm_call(prompt: str) -> str:
        resp = client.chat.completions.create(
            model=os.getenv("GOLDEN_JUDGE_MODEL", "gpt-5-nano"),
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=500,  # gpt-5 family: no temperature param
        )
        return resp.choices[0].message.content

    return example_judge_fn_using_your_llm(llm_call)


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
    parser.add_argument("--price-per-1k-input", type=float, default=PRICE_PER_1K_INPUT_DEFAULT,
                         help=f"$ per 1k input tokens (default {PRICE_PER_1K_INPUT_DEFAULT}, gpt-5.6-luna rate)")
    parser.add_argument("--price-per-1k-output", type=float, default=PRICE_PER_1K_OUTPUT_DEFAULT,
                         help=f"$ per 1k output tokens (default {PRICE_PER_1K_OUTPUT_DEFAULT}, gpt-5.6-luna rate)")
    parser.add_argument("--no-embed", action="store_true", help="Disable embed_fn (semantic similarity)")
    parser.add_argument("--no-judge", action="store_true", help="Disable judge_fn (LLM-as-judge metrics)")
    args = parser.parse_args()

    cases = load_dataset(args.dataset, include_unreviewed=args.include_unreviewed)
    print(f"Loaded {len(cases)} case(s) from {args.dataset}")

    response_fn = get_response_fn()
    embed_fn = None if args.no_embed else get_embed_fn()
    judge_fn = None if args.no_judge else get_judge_fn()

    run = run_evaluation(
        cases, response_fn, model_label=args.model_label,
        embed_fn=embed_fn, judge_fn=judge_fn,
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
