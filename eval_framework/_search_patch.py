"""
Eval-only instrumentation: captures retrieved chunk text from
search_knowledge_base's tool call, WITHOUT modifying production
search.py. Not concurrency-safe — sequential eval runs only.

Must be imported BEFORE `from src.agent.agent import agent` triggers
tool-list construction, otherwise the agent's tools list will hold a
reference to the original (unpatched) FunctionTool.
"""

import json

import src.agent.tools.search as _search_module

_last_retrieved_chunks: list[str] = []

_tool_obj = _search_module.search_knowledge_base   # FunctionTool instance
_original_func = _tool_obj.func                    # underlying async callable


async def _wrapped(query):
    result_json = await _original_func(query)
    try:
        data = json.loads(result_json)
        hits = data.get("hits", [])
        _last_retrieved_chunks.clear()
        _last_retrieved_chunks.extend(
            f"{h.get('title', '')}: {h.get('content', '')}" for h in hits
        )
    except Exception:
        pass  # never let instrumentation break the actual tool call
    return result_json


_tool_obj.func = _wrapped