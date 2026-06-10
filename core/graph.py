"""
LangGraph assembly — wires all nodes into the agent graph.
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


def pydantic_to_dict(state: AgentState) -> GraphState:
    """Convert Pydantic AgentState to TypedDict."""
    d = state.model_dump()
    d["_query_embedding"] = state.__dict__.get(
        "_query_embedding"
    )
    return d


def dict_to_pydantic(state: GraphState) -> AgentState:
    """Convert LangGraph TypedDict to Pydantic AgentState."""
    clean = {
        k: v for k, v in state.items()
        if not k.startswith("_")
    }
    obj = AgentState(**clean)
    if state.get("_query_embedding"):
        obj.__dict__["_query_embedding"] = (
            state["_query_embedding"]
        )
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
    }

    result = graph.invoke(initial_state)
    return dict_to_pydantic(result)