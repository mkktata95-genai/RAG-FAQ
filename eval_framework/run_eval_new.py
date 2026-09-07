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
v2.2.2 — Sep 2026 | Mukesh Kund
         Judge cost now reaches the reports, not just the console.
         Previously the judge-cost block ran AFTER write_json_report/
         write_html_report, so the computed value was printed but never
         written to either file. Moved the block before both writes and
         inject it into run.aggregate["operational"]["judge_cost_usd"]
         (+ "judge_model" for context) — write_json_report dumps
         run.aggregate as-is, and report.py v1.6.0 adds a matching
         "Judge cost ($)" KPI card that reads the same key. No change
         to how the cost itself is calculated (still v2.2.1's
         MODEL_PRICING + _resolve_pricing(judge_model)).
         ROLLBACK: move the judge-cost block back below the two write_*
         calls and drop the run.aggregate[...] assignment lines — the
         print() alone still works standalone.

v2.2.1 — Sep 2026 | Mukesh Kund
         Corrected MODEL_PRICING against the official OpenAI pricing
         page (developers.openai.com/api/docs/pricing), fetched
         directly — v2.2.0's numbers were from third-party trackers,
         not the primary source, as flagged at the time.
         - gpt-5.6-sol corrected: was $5.00/$30.00 (tracker), official
           is $4.00/$20.00 (promotional pricing through Nov 21, 2026).
         - gpt-5.6-terra and gpt-5.6-luna confirmed unchanged
           ($2.00/$12.00 and $0.20/$1.20) — trackers were accurate here.
         - Added gpt-6-astra ($10.00/$50.00) and gpt-5.6-cyber
           ($12.50/$75.00), now on the official flagship/cyber tables.
         - gpt-5, gpt-5-mini, gpt-5-nano, gpt-5.5, gpt-5.4 family, and
           gpt-4o/4o-mini do NOT appear on the current official pricing
           page at all (only gpt-6-astra + gpt-5.6-* family listed under
           "Flagship models"). Kept as a separate UNCONFIRMED block in
           MODEL_PRICING rather than deleted, since GOLDEN_JUDGE_MODEL
           defaults to gpt-5-mini — but these values are last known from
           trackers, not verified against the official source, and
           gpt-5-mini has a published Dec 2026 sunset. Confirm the
           deployed judge model's actual current rate before trusting
           the judge cost number if it falls in this block.
         ROLLBACK: revert MODEL_PRICING to v2.2.0's single flat block
         (gpt-5.6-sol at 0.005/0.03, no gpt-6-astra/cyber entries). No
         other logic changed in this version — _resolve_pricing() and
         all call sites are untouched.

v2.2.0 — Sep 2026 | Mukesh Kund
         Real per-model pricing + judge cost tracking, replacing the
         single flat PRICE_PER_1K_INPUT/OUTPUT_DEFAULT applied
         regardless of which model actually generated the tokens.
         - New MODEL_PRICING table (GPT-5 series: nano/mini/gpt-5,
           5.6-luna/terra/sol, 5.5, 5.4 family, 4o/4o-mini), sourced
           from OpenAI public pricing trackers, checked Sep 2026 — NOT
           fetched from Azure Foundry's own pricing page; Azure rates
           can differ (region/PTU/enterprise agreement). Verify against
           the actual Azure contract before treating as authoritative.
         - New _resolve_pricing(model_name, ...) — exact/substring
           match against MODEL_PRICING, falls back to the flat
           defaults with a printed WARNING (never silent) if unmatched.
         - Main agent cost: auto-resolved from
           config.azure.foundry.model_deployment_name; still
           overridable via --price-per-1k-input/--price-per-1k-output
           (both flags changed default from hardcoded values to None
           so "was it auto-resolved or overridden" is unambiguous).
         - Judge cost: previously untracked entirely — llm_call() in
           get_judge_fn() discarded resp.usage. Now accumulated across
           the run into module-level _judge_usage_totals (judge may be
           called multiple times per case) and priced via
           GOLDEN_JUDGE_MODEL, printed as its own line after the run —
           kept separate from avg_cost/query since it's a distinct
           cost source, not folded into the main agent's number.
         ROLLBACK: remove MODEL_PRICING, _resolve_pricing(),
         _judge_usage_totals, the usage-capture block in llm_call(),
         restore the two --price-per-1k-* argparse defaults to the
         PRICE_PER_1K_*_DEFAULT constants, remove the price
         auto-resolution block in main() (revert to
         args.price_per_1k_input/output passed directly), and remove
         the judge cost print block. v2.1.0's cost-tracking wiring
         (input_tokens/output_tokens in digiassist_response_fn) is
         unaffected either way.

v2.1.0 — Sep 2026 | Mukesh Kund
         Fixed avg_cost/query returning None. digiassist_response_fn's
         return dict never included input_tokens/output_tokens, so
         runner.py's resolve_cost() had nothing to compute from.
         agent.run()'s return value does not expose token counts
         directly (unlike the original LangGraph AgentState). Fix:
         added a module-level in-memory OpenTelemetry span exporter
         (_span_exporter) + enable_instrumentation() at the top of this
         file — same pattern already proven in
         utils/compaction/benchmark.py's _extract_turn_telemetry().
         New _extract_token_usage() sums gen_ai.usage.input_tokens/
         output_tokens off 'chat' spans finished during each
         agent.run() call; digiassist_response_fn clears spans before
         the call and reads them after, adding input_tokens/
         output_tokens to both return paths (parsed and raw-text
         fallback). No production code touched — purely local
         telemetry capture, mirrors benchmark.py exactly.
         ROLLBACK: remove the 5 new import lines, the _span_exporter/
         _provider/enable_instrumentation() block, _extract_token_usage(),
         the two `_span_exporter.clear()` / `usage = ...` lines, and the
         two `input_tokens`/`output_tokens` keys from the return dicts.
         Everything else (retrieved_context, citations, answer) is
         untouched and unaffected by rollback.

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

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from agent_framework.observability import enable_instrumentation

# Real Azure OpenAI pricing, per-1k-token. Used as CLI defaults for cost
# tracking. NOT independently re-confirmed for the digiassist/MAF repo's
# actual deployments (gpt-5-nano main agent, gpt-5-mini judge) — verify
# against your actual Azure contract before trusting cost numbers.
PRICE_PER_1K_INPUT_DEFAULT = 0.0002
PRICE_PER_1K_OUTPUT_DEFAULT = 0.0012

# Real per-1k-token pricing. Source: https://developers.openai.com/api/docs/pricing
# (official page, fetched Sep 2026, Standard tier / short-context rates) —
# NOT Azure OpenAI Foundry's own pricing page. Azure pricing can differ
# from OpenAI's direct API (region / PTU vs pay-as-you-go / enterprise
# agreement) — verify against the actual Azure contract before treating
# these as authoritative for any report shown outside the team.
# Values are per-1k (converted from the per-1M rates OpenAI publishes).
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # --- Confirmed on the official pricing page as of Sep 2026 ---
    # model_name: (price_per_1k_input, price_per_1k_output)
    "gpt-6-astra": (0.01000, 0.05000),
    "gpt-5.6-sol": (0.00400, 0.02000),   # promotional pricing through Nov 21, 2026
    "gpt-5.6-terra": (0.00200, 0.01200),
    "gpt-5.6-luna": (0.00020, 0.00120),
    "gpt-5.6-cyber": (0.01250, 0.07500),
    # --- NOT on the current official pricing page — absent entirely,
    # not just re-tiered. These are third-party-tracker figures from
    # before this was checked against the official source; treat as
    # UNCONFIRMED / possibly stale (gpt-5-mini in particular has a
    # published Dec 2026 sunset date). If GOLDEN_JUDGE_MODEL or
    # model_deployment_name resolves to one of these, confirm the
    # actual current rate with Azure/OpenAI before trusting the number. ---
    "gpt-5-nano": (0.00005, 0.00040),
    "gpt-5-mini": (0.00025, 0.00200),
    "gpt-5": (0.00125, 0.01000),
    "gpt-5.5": (0.00500, 0.03000),
    "gpt-5.4-nano": (0.00020, 0.00125),
    "gpt-5.4-mini": (0.00075, 0.00450),
    "gpt-5.4": (0.00250, 0.01500),
    "gpt-4o-mini": (0.00015, 0.00060),
    "gpt-4o": (0.00250, 0.01000),
}


def _resolve_pricing(model_name: str, fallback_in: float, fallback_out: float) -> tuple[float, float]:
    """
    Look up real per-1k pricing for a model/deployment name. Azure
    Foundry deployment names are often aliases (e.g. "prod-main") that
    don't match the underlying model name exactly, so this tries an
    exact match first, then a substring match either direction, before
    falling back to the given flat defaults (with a printed warning —
    silent fallback would make cost numbers look precise when they
    aren't).
    """
    key = model_name.lower().strip()
    if key in MODEL_PRICING:
        return MODEL_PRICING[key]
    for name, prices in MODEL_PRICING.items():
        if name in key or key in name:
            return prices
    print(
        f"WARNING: no pricing entry for model '{model_name}' — falling back to "
        f"${fallback_in}/${fallback_out} per 1k in/out (flat default, not "
        f"model-specific). Add '{model_name}' to MODEL_PRICING in run_eval.py "
        f"or pass --price-per-1k-input/--price-per-1k-output explicitly.",
        flush=True,
    )
    return fallback_in, fallback_out

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

# In-memory OpenTelemetry span capture — same pattern as
# utils/compaction/benchmark.py's _span_exporter. No data sent to
# Application Insights; purely local, used to recover per-call
# gen_ai.usage.* token counts that agent.run()'s return value does not
# expose directly, so avg_cost/query can be computed. Set up at module
# level so it's active before the agent (imported inside
# get_response_fn()) makes its first call.
_span_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_span_exporter))
trace.set_tracer_provider(_provider)
enable_instrumentation()


def _ensure_pipeline_on_path():
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)


def _extract_token_usage() -> dict:
    """
    Sum gen_ai.usage.input_tokens/output_tokens across all 'chat'
    spans finished since the last clear(). Mirrors benchmark.py's
    _extract_turn_telemetry(), trimmed to just the totals
    digiassist_response_fn needs for cost tracking.
    """
    spans = _span_exporter.get_finished_spans()

    total_input = 0
    total_output = 0
    for span in spans:
        attrs = dict(span.attributes or {})
        if attrs.get("gen_ai.operation.name") == "chat":
            total_input += int(attrs.get("gen_ai.usage.input_tokens") or 0)  # type: ignore[arg-type]
            total_output += int(attrs.get("gen_ai.usage.output_tokens") or 0)  # type: ignore[arg-type]

    _span_exporter.clear()
    return {"input_tokens": total_input, "output_tokens": total_output}


# Judge cost was previously untracked entirely — llm_call() in
# get_judge_fn() discarded resp.usage. Accumulated across the whole eval
# run (judge may be called multiple times per case: faithfulness,
# correctness, answer_relevance, context_relevance), then priced
# separately in main() since it's a distinct cost source from the main
# agent under test.
_judge_usage_totals = {"input_tokens": 0, "output_tokens": 0}


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

        _span_exporter.clear()

        async def _run() -> AgentResponse:  # type: ignore[type-arg]
            return await agent.run(question)

        result = _EVAL_LOOP.run_until_complete(_run())

        usage = _extract_token_usage()

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
                return {
                    "answer": result.text or "",
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                }

        return {
            "answer": parsed.answer,
            "citations": [c.url for c in parsed.citations],
            "retrieved_context": list(_last_retrieved_chunks),
            "refused": False,
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
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
        usage = getattr(resp, "usage", None)
        if usage is not None:
            _judge_usage_totals["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            _judge_usage_totals["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

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
    parser.add_argument("--price-per-1k-input", type=float, default=None,
                         help=f"$ per 1k input tokens (default: auto-resolved from "
                              f"config.azure.foundry.model_deployment_name via "
                              f"MODEL_PRICING, falling back to {PRICE_PER_1K_INPUT_DEFAULT})")
    parser.add_argument("--price-per-1k-output", type=float, default=None,
                         help=f"$ per 1k output tokens (default: auto-resolved, "
                              f"falling back to {PRICE_PER_1K_OUTPUT_DEFAULT})")
    parser.add_argument("--no-embed", action="store_true", help="Disable embed_fn (semantic similarity)")
    parser.add_argument("--no-judge", action="store_true", help="Disable judge_fn (LLM-as-judge metrics)")
    args = parser.parse_args()

    cases = load_dataset(args.dataset, include_unreviewed=args.include_unreviewed)
    print(f"Loaded {len(cases)} case(s) from {args.dataset}")

    response_fn = get_response_fn()
    embed_fn = None if args.no_embed else get_embed_fn()
    judge_fn = None if args.no_judge else get_judge_fn()

    _ensure_pipeline_on_path()
    from src.core.config import config as _cfg  # noqa: E402
    main_model = _cfg.azure.foundry.model_deployment_name
    judge_model = os.getenv("GOLDEN_JUDGE_MODEL", "gpt-5-mini")

    price_in = args.price_per_1k_input
    price_out = args.price_per_1k_output
    if price_in is None or price_out is None:
        resolved_in, resolved_out = _resolve_pricing(
            main_model, PRICE_PER_1K_INPUT_DEFAULT, PRICE_PER_1K_OUTPUT_DEFAULT
        )
        price_in = resolved_in if price_in is None else price_in
        price_out = resolved_out if price_out is None else price_out
    print(f"Pricing '{main_model}': ${price_in}/1k in, ${price_out}/1k out", flush=True)

    run = run_evaluation(
        cases, response_fn, model_label=args.model_label,
        embed_fn=embed_fn, judge_fn=judge_fn,
        price_per_1k_input=price_in,
        price_per_1k_output=price_out,
    )

    # Judge cost is a run-level total (the judge is called multiple times
    # per case — one call per metric — so it doesn't fit the per-case
    # cost_usd field runner.py already populates for the main agent).
    # Injected into run.aggregate["operational"] BEFORE the reports are
    # written, so both write_json_report (dumps run.aggregate as-is) and
    # write_html_report (see report.py v1.6.0) pick it up. Previously
    # this was only printed to console after both reports were already
    # written — never reached either file.
    if not args.no_judge:
        judge_price_in, judge_price_out = _resolve_pricing(
            judge_model, PRICE_PER_1K_INPUT_DEFAULT, PRICE_PER_1K_OUTPUT_DEFAULT
        )
        judge_cost = (
            _judge_usage_totals["input_tokens"] / 1000 * judge_price_in
            + _judge_usage_totals["output_tokens"] / 1000 * judge_price_out
        )
        run.aggregate.setdefault("operational", {})["judge_cost_usd"] = round(judge_cost, 4)
        run.aggregate["operational"]["judge_model"] = judge_model
        print(
            f"Judge cost ('{judge_model}'): ${judge_cost:.4f} "
            f"(in={_judge_usage_totals['input_tokens']}, "
            f"out={_judge_usage_totals['output_tokens']})",
            flush=True,
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