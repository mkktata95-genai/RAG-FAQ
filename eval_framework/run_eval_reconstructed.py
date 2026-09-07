"""
run_eval.py — entry point. Wires the digiassist agent pipeline as the
response_fn and runs the Model Evaluation Framework against it.

    python run_eval.py --dataset sample_golden_dataset.json --model-label demo_v21

get_response_fn() below is just ONE convenience wiring point if you want
to use this CLI as-is. It is NOT required — any team can instead import
runner.run_evaluation() directly in their own script and pass in a
response_fn written however they like. This file has no special status;
nothing else in the package imports it or depends on its contents.

CHANGE LOG
v2.0.0 — Sep 2026 | Mukesh Kund
         Retrieved-context capture switched from a direct edit of the
         production src/agent/tools/search.py to a non-invasive eval-only
         monkey-patch (_search_patch.py). search.py is now back to its
         original, colleague-owned state — untouched. See _search_patch.py
         for the patching mechanism and its concurrency-safety caveat
         (sequential eval runs only, not safe for production reuse).
         ROLLBACK: this only affects retrieved_context population; if
         _search_patch import fails, retrieved_context will just be
         empty and judge faithfulness/context_relevance scores will
         read low/zero, same as before this was wired up. No other
         functionality is affected by rolling this back.

v1.9.0 — Sep 2026 | Mukesh Kund (adapted for digiassist-agent / MAF by
         collaborator, then debugged jointly)
         - Consolidated three separate event loops (_loop / _embed_loop /
           _judge_loop) into a single module-level _EVAL_LOOP, explicitly
           set via asyncio.set_event_loop() — fixes intermittent
           TimeoutError hangs seen when each function created its own loop.
         - get_embed_fn() and get_judge_fn() now build fully ISOLATED
           AsyncAzureOpenAI clients rather than reusing
           src.core.embedding.openai_client (the agent's own shared
           client) — reusing the shared client caused hangs on the
           second+ call after agent.run() had already used it internally
           for search_knowledge_base's embedding step.
         - get_judge_fn() switched from client.chat.completions.create()
           to client.responses.create() (Responses API), with its own
           hardcoded api_version="2025-03-01-preview" (the shared
           config.azure.foundry.api_version is older and does not support
           the Responses API). Chat Completions was hanging/returning
           empty content unpredictably for this reasoning model; the
           Responses API matches what MAF's own FoundryChatClient uses
           successfully for the main agent.
         - get_response_fn() rewritten entirely around agent.run(question)
           (MAF's Agent instance, imported correctly as
           `from src.agent.agent import agent` — NOT `from src.agent import
           agent`, which imports the module, not the Agent instance).
         ROLLBACK: this is a full rewrite for the MAF/digiassist codebase;
         there is no meaningful partial rollback. Prior versions targeted
         the original LangGraph-based RLG pipeline (see v1.0.0-v1.3.0
         below) and will not work against this repo at all.

v1.3.0 — Aug 2026 | Mukesh Kund  [ORIGINAL — targeted the LangGraph RLG
         pipeline, superseded by v1.9.0 above for this repo]
         Fix: judge_fn's max_completion_tokens raised 500 -> 3000.
         Real run showed all 3 cases failing with
         "JSONDecodeError: Expecting value: line 1 column 1 (char 0)" —
         gpt-5-nano returned empty content. Root cause: GPT-5-family
         reasoning models spend hidden reasoning tokens out of the same
         max_completion_tokens budget before writing visible output.

v1.2.0 — Aug 2026 | Mukesh Kund  [ORIGINAL — LangGraph pipeline]
         Demo-readiness pass: retrieved_context wiring, get_embed_fn()
         added, get_judge_fn() turned on by default, real pricing
         defaults added.

v1.1.0 — Aug 2026 | Mukesh Kund  [ORIGINAL — LangGraph pipeline]
         Wired get_response_fn() to the real RLG pipeline via graph.py's
         run_query().

v1.0.0 — Aug 2026 | Mukesh Kund — initial version, smoke-tested against
         sample_golden_dataset.json with a dummy response_fn.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from runner import load_dataset, run_evaluation
from report import write_json_report, write_html_report
from regression import save_baseline, compare_to_baseline, print_regression_summary

# Real Azure OpenAI pricing, per-1k-token. Used as CLI defaults for cost
# tracking. NOT independently re-confirmed for the digiassist/MAF repo's
# actual deployments (gpt-5-nano main agent, gpt-5-mini judge) — verify
# against your actual Azure contract before trusting cost numbers.
PRICE_PER_1K_INPUT_DEFAULT = 0.0002
PRICE_PER_1K_OUTPUT_DEFAULT = 0.0012

# Project root — one level up from eval_framework/ (this file's directory
# sits directly under the digiassist-agent repo root).
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Single persistent event loop for the whole eval run. Explicitly set as
# the current event loop (not just created) — some async Azure/httpx/anyio
# internals call asyncio.get_event_loop() rather than using a loop passed
# to them directly; setting it explicitly avoids hidden loop-mismatch
# behaviour. A fresh loop per top-level call (the original pattern) kills
# the agent's singleton async clients (Search, Cosmos, OpenAI) on the
# second+ case; a persistent loop keeps them alive across the whole run.
_EVAL_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_EVAL_LOOP)


def _ensure_pipeline_on_path():
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)


def get_response_fn():
    """
    Wires the digiassist agent pipeline as the response_fn.

    Calls agent.run(question) which returns a StructuredResponse
    (answer, citations, cta, advice_boundary). The agent internally
    invokes search_knowledge_base (Azure AI Search) as a tool call.
    Retrieved chunk content is captured via the non-invasive
    _search_patch module (see that file's docstring) and surfaced here
    as retrieved_context, so judge_fn can score faithfulness and
    context_relevance against real retrieved text rather than an empty
    list or citation URLs alone.
    """
    _ensure_pipeline_on_path()

    # Must import BEFORE src.agent.agent, so the agent's tool list is
    # built from the already-patched search_knowledge_base FunctionTool.
    import _search_patch  # noqa: E402
    from _search_patch import _last_retrieved_chunks  # noqa: E402

    from src.agent.agent import agent  # noqa: E402
    from src.agent.schema import StructuredResponse  # noqa: E402
    from pydantic import ValidationError  # noqa: E402
    from agent_framework import AgentResponse  # noqa: E402

    def digiassist_response_fn(question: str) -> dict:
        _ensure_pipeline_on_path()

        async def _run() -> AgentResponse:  # type: ignore[type-arg]
            return await agent.run(question)

        result = _EVAL_LOOP.run_until_complete(_run())

        # Parse the structured response — try result.value first,
        # fall back to parsing result.text (mirrors local.py logic).
        parsed = None
        if isinstance(result.value, StructuredResponse):
            parsed = result.value
        elif result.value is not None:
            try:
                parsed = StructuredResponse.model_validate(result.value)
            except ValidationError:
                pass
        if parsed is None:
            try:
                parsed = StructuredResponse.model_validate_json(result.text)
            except (ValidationError, Exception):
                # Could not parse structured response — return raw text
                return {"answer": result.text or ""}

        return {
            "answer": parsed.answer,
            "citations": [c.url for c in parsed.citations],
            "retrieved_context": list(_last_retrieved_chunks),
            "refused": False,
        }

    return digiassist_response_fn


def get_embed_fn():
    """
    Reuses the project's existing embedding config (src/core/config.py,
    src/core/credential.py) so avg_semantic_similarity uses the same
    model as the search pipeline — but via an ISOLATED OpenAI client,
    not the shared one from src/core/embedding.py.

    Rationale: the agent's search_knowledge_base tool also calls
    get_embedding() using the shared client during agent.run(). Reusing
    that same client afterward for eval scoring caused hangs/timeouts,
    likely due to something about the shared client's underlying HTTP
    transport state after being used inside an agent.run() call. A
    fresh, isolated client avoids that entirely.

    Returns None to disable — framework runs fine without it.
    """
    _ensure_pipeline_on_path()
    from openai import AsyncAzureOpenAI  # noqa: E402
    from azure.identity.aio import get_bearer_token_provider  # noqa: E402
    from src.core.config import config  # noqa: E402
    from src.core.credential import async_credential  # noqa: E402

    token_provider = get_bearer_token_provider(
        async_credential,
        "https://cognitiveservices.azure.com/.default",
    )
    eval_embed_client = AsyncAzureOpenAI(
        azure_endpoint=config.azure.foundry.azure_openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version=config.azure.foundry.api_version,
    )

    def embed_fn(text: str) -> list[float]:
        async def _call():
            response = await eval_embed_client.embeddings.create(
                input=[text],
                model=config.azure.foundry.embedding_model_deployment_name,
                dimensions=config.tools.search_knowledge_base.embedding_dimensions,
            )
            return response.data[0].embedding

        try:
            return _EVAL_LOOP.run_until_complete(asyncio.wait_for(_call(), timeout=60))
        except asyncio.TimeoutError:
            raise ValueError("Embedding call timed out after 60s")

    return embed_fn


def get_judge_fn():
    """
    Wires a judge LLM using an ISOLATED Azure OpenAI client (same
    rationale as get_embed_fn — do not reuse the agent's shared client).

    Uses a separate model (GOLDEN_JUDGE_MODEL env var, default
    gpt-5-mini) that is NOT in the generation path, to avoid
    self-evaluation bias.

    Uses the Responses API (client.responses.create), not Chat
    Completions — Chat Completions was observed to hang / return empty
    content for this reasoning model in this environment; the Responses
    API matches the pattern the main agent already uses successfully
    via FoundryChatClient. Requires api_version >= "2025-03-01-preview",
    which is newer than the shared config.azure.foundry.api_version, so
    it is hardcoded here rather than read from config.

    Returns None to disable — framework runs fine without it, just
    without faithfulness/answer_relevance/correctness/context_relevance.
    """
    _ensure_pipeline_on_path()
    from openai import AsyncAzureOpenAI  # noqa: E402
    from azure.identity.aio import get_bearer_token_provider  # noqa: E402
    from src.core.config import config  # noqa: E402
    from src.core.credential import async_credential  # noqa: E402
    from metrics_judge import example_judge_fn_using_your_llm  # noqa: E402

    token_provider = get_bearer_token_provider(
        async_credential,
        "https://cognitiveservices.azure.com/.default",
    )
    eval_judge_client = AsyncAzureOpenAI(
        azure_endpoint=config.azure.foundry.azure_openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2025-03-01-preview",  # Responses API requires this or later
    )

    def llm_call(prompt: str) -> str:
        async def _call():
            return await eval_judge_client.responses.create(
                model=os.getenv("GOLDEN_JUDGE_MODEL", "gpt-5-mini"),
                input=prompt,
                max_output_tokens=10000,
                reasoning={"effort": "minimal"},
            )

        try:
            resp = _EVAL_LOOP.run_until_complete(asyncio.wait_for(_call(), timeout=60))
        except asyncio.TimeoutError:
            raise ValueError(
                "Judge LLM call timed out after 60s — check Foundry "
                "deployment health or reasoning_effort/max_output_tokens "
                "settings."
            )

        content = resp.output_text
        if not content or not content.strip():
            raise ValueError(
                "Judge LLM returned empty content — likely max_output_tokens "
                "still too low (reasoning tokens consumed the full budget). "
                "Try raising it further."
            )
        return content

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
                         help=f"$ per 1k input tokens (default {PRICE_PER_1K_INPUT_DEFAULT})")
    parser.add_argument("--price-per-1k-output", type=float, default=PRICE_PER_1K_OUTPUT_DEFAULT,
                         help=f"$ per 1k output tokens (default {PRICE_PER_1K_OUTPUT_DEFAULT})")
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

    _EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(_EVAL_DIR, f"{args.out_prefix}.json")
    html_path = os.path.join(_EVAL_DIR, f"{args.out_prefix}.html")
    write_json_report(run, json_path)
    write_html_report(run, html_path)
    print(f"\nReports written: {json_path}, {html_path}")
    print(f"Aggregate summary: {run.aggregate}")

    if args.save_as_baseline:
        baseline_path = os.path.join(_EVAL_DIR, f"{args.out_prefix}_baseline.json")
        save_baseline(run, baseline_path)
        print(f"Baseline saved: {baseline_path}")

    if args.compare_baseline:
        result = compare_to_baseline(run, args.compare_baseline)
        print_regression_summary(result)


if __name__ == "__main__":
    main()
    # python run_eval.py --dataset sample_golden_dataset.json --model-label digiassist_v1 --no-judge --no-embed