# ============================================================
# Shared setup — single event loop for the whole eval run
# ============================================================

import asyncio

_loop = asyncio.new_event_loop()


def get_response_fn():
    """
    Wires the digiassist agent pipeline as the response_fn.

    Calls agent.run(question) which returns a StructuredResponse
    (answer, citations, cta, advice_boundary). The agent internally
    invokes search_knowledge_base (Azure AI Search) as a tool call —
    retrieved_context is not directly exposed in the response, so
    judge faithfulness/context_relevance will be N/A unless extended.
    """
    _ensure_pipeline_on_path()
    from src.agent.agent import agent  # noqa: E402
    from src.agent.schema import StructuredResponse  # noqa: E402
    from pydantic import ValidationError  # noqa: E402
    from agent_framework import AgentResponse  # noqa: E402

    def digiassist_response_fn(question: str) -> dict:
        _ensure_pipeline_on_path()

        async def _run() -> AgentResponse:  # type: ignore[type-arg]
            return await agent.run(question)

        result = _loop.run_until_complete(_run())

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
    likely due to MAF touching the shared client's underlying HTTP
    transport during/after a run. A fresh client avoids that entirely.

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

        return _loop.run_until_complete(asyncio.wait_for(_call(), timeout=60))

    return embed_fn


def get_judge_fn():
    """
    Wires a judge LLM using an ISOLATED Azure OpenAI client (same
    rationale as get_embed_fn — do not reuse the agent's shared client).

    Uses a separate model (GOLDEN_JUDGE_MODEL env var, default gpt-5-nano)
    that is NOT in the generation path, to avoid self-evaluation bias.

    Uses the Responses API (client.responses.create), not Chat Completions —
    Chat Completions was observed to hang/return empty content for
    reasoning models in this environment; the Responses API matches the
    pattern the main agent already uses successfully via FoundryChatClient.

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
        api_version=config.azure.foundry.api_version,
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
            resp = _loop.run_until_complete(asyncio.wait_for(_call(), timeout=60))
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