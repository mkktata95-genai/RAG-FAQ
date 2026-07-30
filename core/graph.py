"""
LangGraph assembly — wires all nodes into the agent graph.

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
         8-node pipeline (Supervisor -> Cache Check -> Input
         Safety -> Retriever -> Generator -> Output Safety ->
         Formatter -> Cache Write).
         pydantic_to_dict()/dict_to_pydantic() special-cased a
         single state.__dict__ extra (_query_embedding) so it
         survives the AgentState <-> GraphState conversion that
         happens at every node boundary.

v1.1.0 — June 2026 | Mukesh Kund
         Fix silent loss of state.__dict__ extras at node
         boundaries (_override_triggered / _override_reason
         / _bereavement)

         ROOT CAUSE:
         - model_dump() only serialises AgentState's DECLARED
           Pydantic fields. Any value set directly on
           state.__dict__ (the "same pattern as
           _query_embedding", per supervisor.py v1.4.0) was
           silently dropped the moment a node's return value
           was converted back to GraphState — UNLESS that key
           had its own explicit special-case here.
         - Only _query_embedding had such a special-case.
         - supervisor.py v1.4.0 started setting
           state.__dict__["_override_triggered"] and
           state.__dict__["_override_reason"] for contextual
           follow-up queries, intending cache_check.py and
           generator.py to read them downstream — but they were
           dropped before cache_check.py ever ran, so
           is_override = state.__dict__.get("_override_triggered",
           False) was ALWAYS False. The entire v1.2.0-v1.5.0
           context-override mechanism (categories A-I) has never
           worked end-to-end.

         LIVE REPRO (request_id=76e0615c-...):
         - "Why didn't you answer my previous question?"
           -> supervisor_start: override_triggered=True,
              override_reason=history_reference  (CORRECT)
           -> cache_check still ran canonical_rewrite ->
              canonical='What is my previous question?'
              (_override_triggered not visible here - WRONG)
           -> generation_complete: citations=0,
              refusal_triggered=True (UNKNOWN PRODUCT RULE
              response, instead of using conversation history)

         FIX:
         - pydantic_to_dict()/dict_to_pydantic() now propagate a
           registered list of single-underscore __dict__ extras
           (_DICT_EXTRA_KEYS) generically, instead of hard-coding
           only _query_embedding.
         - GraphState TypedDict additionally declares each key in
           _DICT_EXTRA_KEYS explicitly. This is REQUIRED, not just
           documentation: LangGraph's StateGraph builds one
           channel per key declared in the state schema, and only
           persists updates for keys it has a channel for. A key
           returned by a node but absent from GraphState's
           annotations would be silently dropped when LangGraph
           merges that node's output into the graph state, even
           if pydantic_to_dict() includes it in the returned dict.
         - run_query()'s initial_state now seeds default values
           for the two new keys (_override_triggered=False,
           _override_reason="") alongside _query_embedding=None.
         - _bereavement (new in this round, set by supervisor.py
           v1.6.0, read by generator.py v1.7.0 for the
           bereavement-specific handoff number) is registered the
           same way and seeded as _bereavement=False — this is
           the first __dict__ extra to be correctly propagated
           from day one rather than retrofitted.
         - To add a future __dict__ extra: add it to
           _DICT_EXTRA_KEYS, add it to GraphState, and seed it in
           run_query()'s initial_state. No other changes to
           pydantic_to_dict()/dict_to_pydantic() are needed.


v1.2.0 — July 2026 | Mukesh Kund
         Sprint 1 refactor — 10-node pipeline, new nodes,
         new GraphState fields.

         NEW NODES:
         - classifier_node (core/nodes/classifier_node.py)
           Position: between supervisor and cache_check.
           Sets state.intent (LLM-based, gpt-4o-mini) and
           state.query_type (rule-based, no LLM).

         - prompt_builder_node (core/nodes/prompt_builder_node.py)
           Position: between retriever and generator.
           Assembles state.built_prompt from retrieved chunks,
           conversation history, and state flags. Generator
           reads state.built_prompt — no longer calls
           build_user_prompt() directly.

         NEW PIPELINE ORDER (10 nodes):
           Supervisor → Classifier → Cache Check → Input Safety
           → Retriever → Prompt Builder → Generator
           → Output Safety → Formatter → Cache Write

         NEW GRAPHSTATE FIELDS:
         - intent: str — set by classifier_node
         - query_type: str — set by classifier_node
         - built_prompt: str — set by prompt_builder_node
         All three declared in GraphState TypedDict (required for
         LangGraph channel creation) and seeded in run_query().

         ROUTING CHANGES:
         - route_after_supervisor: "cache_check" → "classifier"
         - route_after_classifier (NEW): always → "cache_check"
         - All other routes unchanged.

         WHY supervisor → classifier → cache_check (not
         classifier first):
         Supervisor must run before any LLM calls because:
         1. Request ID generation (needed for log correlation)
         2. sanitise_input() — safety, must be absolute first
         3. validate_query_length() — reject invalid early
         4. is_account_lookup() — FCA-sensitive, intercept PII
            before ANY API call including classification
         5. quick_intent_check() — rule-based greeting short-
            circuit, saves gpt-4o-mini call for "hi"/"thanks"
         Classifier then does LLM classification on validated,
         sanitised, non-account-lookup queries only.

         _DICT_EXTRA_KEYS: unchanged. No new __dict__ extras
         added in this sprint — intent, query_type, built_prompt
         are declared Pydantic fields on AgentState (schemas.py
         v1.2.0) and GraphState TypedDict fields, so they
         propagate correctly through node boundaries via the
         normal model_dump() / AgentState(**clean) path without
         needing _DICT_EXTRA_KEYS registration.

v1.3.0 — July 2026 | Mukesh Kund
         Tier 1 batch 2 — pipeline reorder (Bug #24) + edge fix
         for Bug #1/#2.

         PIPELINE REORDER (Bug #24):
         - Input Safety moved from 4th position to 2nd — now runs
           immediately after Supervisor, before Classifier and
           Cache Check.
         - ROOT CAUSE: harmful/weapons/jailbreak queries were
           paying for a classifier LLM call AND a canonical-
           rewrite LLM call — both rejected by Azure OpenAI's own
           built-in content filter — before our own Layers 1-5 in
           safety.py ever ran. Confirmed live: "How to make bomb"
           took ~26s and two wasted API calls before our own
           weapons_content_detected (safety.py v1.1.0) fired in
           7ms. input_safety_node depends only on state.query —
           confirmed via direct read — so reordering is safe.
         - Edge changes: supervisor → input_safety (was
           → classifier). input_safety → classifier (was
           → retriever). cache_check → retriever (was
           → input_safety). classifier → cache_check unchanged
           on success.
         - See supervisor.py v1.10.0 and classifier_node.py v1.3.0
           for the companion routing-function and node changes.

         EDGE FIX (Bug #1/#2):
         - "classifier" → "cache_check" edge changed from
           unconditional (`{"cache_check": "cache_check"}`) to
           conditional with an "end" branch
           (`{"end": END, "cache_check": "cache_check"}`).
         - ROOT CAUSE: classifier_node.py sets
           state.refusal_triggered=True + final_response for
           non-INSURANCE intents (v1.3.0 fix, that file) — but an
           unconditional edge target ignores whatever
           route_after_classifier returns beyond the one key it
           declares. Without "end" in this map, LangGraph would
           have nowhere to route even if route_after_classifier
           correctly returned "end". This is the graph.py half of
           that fix — both were needed together.

         NEW PIPELINE ORDER (10 nodes, same count — just reordered):
           Supervisor → Input Safety → Classifier → Cache Check
           → Retriever → Prompt Builder → Generator
           → Output Safety → Formatter → Cache Write

         Node registration order in build_graph() reordered to
         match (cosmetic only — LangGraph only reads the edges,
         not registration order, but this keeps the file readable).

         ROLLBACK:
         - Revert supervisor edge target to
           {"end": END, "classifier": "classifier"}.
         - Revert classifier edge target to
           {"cache_check": "cache_check"} (remove "end": END).
         - Revert cache_check edge target to
           {"end": END, "input_safety": "input_safety"}.
         - Revert input_safety edge target to
           {"end": END, "retriever": "retriever"}.
         - Revert node registration order (cosmetic, optional).

═══════════════════════════════════════════════════════════════
"""

import structlog
from langgraph.graph import StateGraph, END
from typing import TypedDict, Any

from core.schemas import AgentState
from core.nodes.supervisor import (
    supervisor_node,
    response_formatter_node,
    route_after_supervisor,
    route_after_classifier,
    route_after_cache,
    route_after_input_safety,
    route_after_retriever,
    route_after_generator,
    route_after_output_safety,
)
from core.nodes.classifier_node import classifier_node
from core.nodes.cache_check import cache_check_node
from core.nodes.input_safety import input_safety_node
from core.nodes.retriever import retriever_node
from core.nodes.prompt_builder_node import prompt_builder_node
from core.nodes.generator import generator_node
from core.nodes.output_safety import output_safety_node
from core.nodes.cache_write import cache_write_node

log = structlog.get_logger()


# ── LangGraph State ───────────────────────────────────────
class GraphState(TypedDict):
    query: str
    conversation_history: list[dict]
    request_id: str
    cache_hit: bool
    cached_response: Any
    input_safe: Any
    output_safe: Any
    refusal_triggered: bool
    refusal_reason: Any
    retrieved_chunks: list
    raw_response: Any
    model_used: Any
    citations: list
    final_response: Any
    needs_empathy: bool
    needs_disclaimer: bool
    is_sensitive: bool
    latency_ms: dict
    token_usage: dict
    stream_tokens: list   # v2.3.0 — set by generator_node, read by server.py
    error: Any
    # v1.2.0 — new Sprint 1 fields set by classifier_node
    # and prompt_builder_node. Declared here so LangGraph
    # creates channels for them — without a declared key,
    # LangGraph silently drops the value when merging node
    # output into graph state (same issue as _override_triggered
    # was before v1.1.0 _DICT_EXTRA_KEYS fix).
    intent: str
    query_type: str
    built_prompt: str
    # v1.1.0 — state.__dict__ extras (see _DICT_EXTRA_KEYS)
    _query_embedding: Any
    _override_triggered: Any
    _override_reason: Any
    _bereavement: Any
    _skip_cache: Any      # v1.2.0 — set by supervisor for recommendation queries


# v1.1.0 — single-underscore state.__dict__ extras that must
# survive every AgentState <-> GraphState conversion. Each entry
# here MUST also:
#   1. be declared as a key in GraphState above, and
#   2. be seeded with a default value in run_query()'s
#      initial_state below.
# See CHANGE LOG v1.1.0 for why both steps are required.
_DICT_EXTRA_KEYS = (
    "_query_embedding",
    "_override_triggered",
    "_override_reason",
    "_bereavement",
    "_skip_cache",       # v1.2.0 — recommendation query cache bypass
)


def pydantic_to_dict(state: AgentState) -> GraphState:
    """Convert Pydantic AgentState to TypedDict."""
    d = state.model_dump()
    for key in _DICT_EXTRA_KEYS:
        d[key] = state.__dict__.get(key)
    return d


def dict_to_pydantic(state: GraphState) -> AgentState:
    """Convert LangGraph TypedDict to Pydantic AgentState."""
    clean = {
        k: v for k, v in state.items()
        if not k.startswith("_")
    }
    obj = AgentState(**clean)
    for key in _DICT_EXTRA_KEYS:
        if key in state:
            obj.__dict__[key] = state[key]
    return obj


# ── Node Wrappers ─────────────────────────────────────────
def _supervisor(state: GraphState) -> GraphState:
    result = supervisor_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _classifier(state: GraphState) -> GraphState:
    result = classifier_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _cache_check(state: GraphState) -> GraphState:
    result = cache_check_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _input_safety(state: GraphState) -> GraphState:
    result = input_safety_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _retriever(state: GraphState) -> GraphState:
    result = retriever_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _prompt_builder(state: GraphState) -> GraphState:
    result = prompt_builder_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _generator(state: GraphState) -> GraphState:
    result = generator_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _output_safety(state: GraphState) -> GraphState:
    result = output_safety_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _response_formatter(state: GraphState) -> GraphState:
    result = response_formatter_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _cache_write(state: GraphState) -> GraphState:
    result = cache_write_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


# ── Router Wrappers ───────────────────────────────────────
def _route_after_supervisor(state: GraphState) -> str:
    return route_after_supervisor(dict_to_pydantic(state))


def _route_after_classifier(state: GraphState) -> str:
    return route_after_classifier(dict_to_pydantic(state))


def _route_after_cache(state: GraphState) -> str:
    return route_after_cache(dict_to_pydantic(state))


def _route_after_input_safety(state: GraphState) -> str:
    return route_after_input_safety(dict_to_pydantic(state))


def _route_after_retriever(state: GraphState) -> str:
    return route_after_retriever(dict_to_pydantic(state))


def _route_after_generator(state: GraphState) -> str:
    return route_after_generator(dict_to_pydantic(state))


def _route_after_output_safety(state: GraphState) -> str:
    return route_after_output_safety(dict_to_pydantic(state))


# ── Build Graph ───────────────────────────────────────────
def build_graph():
    """
    Compile the 10-node LangGraph pipeline.

    v1.3.0 pipeline order (Bug #24 — safety moved earlier):
    Supervisor → Input Safety → Classifier → Cache Check
    → Retriever → Prompt Builder → Generator → Output Safety
    → Formatter → Cache Write
    """
    graph = StateGraph(GraphState)

    # ── Register all nodes ────────────────────────────────
    # Registration order matches pipeline order for readability;
    # LangGraph itself only cares about the edges below.
    graph.add_node("supervisor",        _supervisor)
    graph.add_node("input_safety",      _input_safety)      # v1.3.0: moved up (was 4th)
    graph.add_node("classifier",        _classifier)
    graph.add_node("cache_check",       _cache_check)
    graph.add_node("retriever",         _retriever)
    graph.add_node("prompt_builder",    _prompt_builder)
    graph.add_node("generator",         _generator)
    graph.add_node("output_safety",     _output_safety)
    graph.add_node("response_formatter",_response_formatter)
    graph.add_node("cache_write",       _cache_write)

    graph.set_entry_point("supervisor")

    # ── Supervisor → Input Safety (or END for greetings) ──
    # v1.3.0 (Bug #24): target changed from "classifier" to
    # "input_safety" — safety checks now run before any LLM call.
    # See supervisor.py v1.10.0 CHANGE LOG for full rationale.
    graph.add_conditional_edges(
        "supervisor",
        _route_after_supervisor,
        {"end": END, "input_safety": "input_safety"},
    )

    # ── Input Safety → Classifier (or END if unsafe) ──────
    # v1.3.0: moved earlier in the pipeline (was 4th, now 2nd).
    graph.add_conditional_edges(
        "input_safety",
        _route_after_input_safety,
        {"end": END, "classifier": "classifier"},
    )

    # ── Classifier → Cache Check (or END for non-INSURANCE) ─
    # v1.3.0 FIX (Bug #1/#2): now conditional. classifier_node
    # sets state.refusal_triggered=True alongside final_response
    # for non-INSURANCE intents (classifier_node.py v1.3.0) —
    # this edge map addition is what lets that flag actually
    # short-circuit to END instead of always continuing to
    # cache_check regardless. See supervisor.py v1.10.0 CHANGE
    # LOG for the confirmed live repro this fixes.
    graph.add_conditional_edges(
        "classifier",
        _route_after_classifier,
        {"end": END, "cache_check": "cache_check"},
    )

    # ── Cache Check → Retriever ────────────────────────────
    # v1.3.0: target changed from "input_safety" to "retriever" —
    # input_safety now runs earlier (see above), so a cache miss
    # goes straight to retrieval.
    graph.add_conditional_edges(
        "cache_check",
        _route_after_cache,
        {"end": END, "retriever": "retriever"},
    )
    graph.add_conditional_edges(
        "retriever",
        _route_after_retriever,
        {"end": END, "prompt_builder": "prompt_builder"},  # v1.2.0: was generator
    )

    # Prompt builder always continues to generator
    graph.add_edge("prompt_builder", "generator")

    graph.add_conditional_edges(
        "generator",
        _route_after_generator,
        {"end": END, "output_safety": "output_safety"},
    )
    graph.add_conditional_edges(
        "output_safety",
        _route_after_output_safety,
        {"end": END, "response_formatter": "response_formatter"},
    )

    graph.add_edge("response_formatter", "cache_write")
    graph.add_edge("cache_write", END)

    return graph.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
        log.info("graph_compiled")
    return _graph


def run_query(
    query: str,
    conversation_history: list[dict] | None = None,
) -> AgentState:
    """Main entry point to run a query through the graph."""
    graph = get_graph()

    initial_state: GraphState = {
        "query": query,
        "conversation_history": conversation_history or [],
        "request_id": "",
        "cache_hit": False,
        "cached_response": None,
        "input_safe": None,
        "output_safe": None,
        "refusal_triggered": False,
        "refusal_reason": None,
        "retrieved_chunks": [],
        "raw_response": None,
        "model_used": None,
        "citations": [],
        "final_response": None,
        "needs_empathy": False,
        "needs_disclaimer": False,
        "is_sensitive": False,
        "latency_ms": {},
        "token_usage": {},
        "stream_tokens": [],
        "error": None,
        # v1.2.0 — Sprint 1 new fields (declared AgentState
        # Pydantic fields + GraphState TypedDict keys)
        "intent":       "",
        "query_type":   "",
        "built_prompt": "",
        # v1.1.0 — state.__dict__ extras (see _DICT_EXTRA_KEYS)
        "_query_embedding":   None,
        "_override_triggered": False,
        "_override_reason":   "",
        "_bereavement":        False,
        "_skip_cache":         False,   # v1.2.0 — recommendation cache bypass
    }

    result = graph.invoke(initial_state)
    return dict_to_pydantic(result)