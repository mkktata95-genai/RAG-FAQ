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

═══════════════════════════════════════════════════════════════
"""

import structlog
from langgraph.graph import StateGraph, END
from typing import TypedDict, Any

from core.schemas import AgentState
from core.nodes.supervisor import (
    supervisor_node,
    response_formatter_node,
    route_after_cache,
    route_after_input_safety,
    route_after_retriever,
    route_after_generator,
    route_after_output_safety,
)
from core.nodes.cache_check import cache_check_node
from core.nodes.input_safety import input_safety_node
from core.nodes.retriever import retriever_node
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
    error: Any
    _query_embedding: Any
    # v1.1.0 — declared so LangGraph creates channels for these
    # state.__dict__ extras (see _DICT_EXTRA_KEYS and CHANGE LOG
    # above). Without a declared key here, LangGraph silently
    # drops the value when merging a node's returned dict into
    # the graph state, regardless of what pydantic_to_dict()
    # returns.
    _override_triggered: Any
    _override_reason: Any
    _bereavement: Any


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


def _cache_check(state: GraphState) -> GraphState:
    result = cache_check_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _input_safety(state: GraphState) -> GraphState:
    result = input_safety_node(dict_to_pydantic(state))
    return pydantic_to_dict(result)


def _retriever(state: GraphState) -> GraphState:
    result = retriever_node(dict_to_pydantic(state))
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
    """
    Route after supervisor:
    - Greeting/chitchat handled → END
    - Insurance query → cache_check
    """
    if (
        state.get("final_response")
        and not state.get("cache_hit")
        and not state.get("refusal_triggered")
    ):
        return "end"
    return "cache_check"


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
    graph = StateGraph(GraphState)

    graph.add_node("supervisor", _supervisor)
    graph.add_node("cache_check", _cache_check)
    graph.add_node("input_safety", _input_safety)
    graph.add_node("retriever", _retriever)
    graph.add_node("generator", _generator)
    graph.add_node("output_safety", _output_safety)
    graph.add_node("response_formatter", _response_formatter)
    graph.add_node("cache_write", _cache_write)

    graph.set_entry_point("supervisor")

    # ── Conditional route after supervisor ────────────────
    graph.add_conditional_edges(
        "supervisor",
        _route_after_supervisor,
        {"end": END, "cache_check": "cache_check"},
    )

    graph.add_conditional_edges(
        "cache_check",
        _route_after_cache,
        {"end": END, "input_safety": "input_safety"},
    )
    graph.add_conditional_edges(
        "input_safety",
        _route_after_input_safety,
        {"end": END, "retriever": "retriever"},
    )
    graph.add_conditional_edges(
        "retriever",
        _route_after_retriever,
        {"end": END, "generator": "generator"},
    )
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
        "error": None,
        "_query_embedding": None,
        # v1.1.0 — see _DICT_EXTRA_KEYS / CHANGE LOG above.
        "_override_triggered": False,
        "_override_reason": "",
        "_bereavement": False,
    }

    result = graph.invoke(initial_state)
    return dict_to_pydantic(result)