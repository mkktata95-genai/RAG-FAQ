"""
Pydantic schemas shared across all agents.

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
         Core schemas: AgentState, Citation, RetrievedChunk,
         ChatRequestBody, ChatResponse, CacheEntry.

v1.1.0 — June 2026 | Mukesh Kund
         title field added to Citation and RetrievedChunk.

         Citation.title (str = ""):
         - Enables the UI to display the page title alongside
           the citation URL in the citation chip — previously
           only URL and section were available.

         RetrievedChunk.title (str = ""):
         - Required to populate Citation.title — the retriever
           reads this from the index's title field and stores
           it on the chunk so extract_citations() can access it.

v1.2.0 — July 2026 | Mukesh Kund
         Sprint 1 refactor — three new AgentState fields for
         classifier_node and prompt_builder_node.

         intent: str = ""
         - Set by classifier_node (NEW — core/nodes/).
         - Values: "INSURANCE", "GREETING", "CHITCHAT",
           "THANKS", "FAREWELL", "CAPABILITY", "IRRELEVANT",
           "SENSITIVE" (future — empathy-flagged queries).
         - Read by generator_node for model routing
           (intent signals whether gpt-4o or gpt-4.1 is needed)
           and by prompt_builder_node for system prompt selection.
         - Supervisor keeps quick_intent_check() for rule-based
           GREETING/FAREWELL/THANKS short-circuit (no LLM call).
           classifier_node handles all LLM-based classification.

         query_type: str = ""
         - Set by classifier_node (rule-based, no LLM call).
         - Values: "BROAD" or "SPECIFIC".
         - BROAD: entry-point queries covering multiple topics
           ("what types of pensions does Royal London offer?").
           Signals retriever to apply title_questions boost via
           scoring_profile="rl-retrieval-profile" and
           rerank_chunks() fuzzy title matching.
           Also upgrades simple-query model routing:
           BROAD queries get gpt-4o (DEPLOYMENT_FAST) minimum,
           not gpt-4o-mini, because comprehensive multi-type
           answers need more capable generation.
         - SPECIFIC: targeted product queries
           ("what is the MPAA?"). Existing routing unchanged.

         built_prompt: str = ""
         - Set by prompt_builder_node (NEW — core/nodes/).
         - Contains the fully assembled user prompt including
           retrieved context, conversation history, empathy/
           disclaimer/bereavement/override notes.
         - Read by generator_node instead of calling
           build_user_prompt() directly — generator becomes
           a pure LLM-call node with no prompt construction.
         - Empty string default is safe: generator_node
           checks for built_prompt before using it and falls
           back to a minimal prompt if not set (defensive).

v1.3.0 — July 2026 | Mukesh Kund
         stream_tokens field added to AgentState

         stream_tokens: list[str] = []
         - Set by generator_node v2.3.0 when stream=True OpenAI
           call is used. Contains the raw token strings from the
           OpenAI chunk iterator in order of arrival.
         - Read by server.py v1.2.0 stream_response() to yield
           tokens directly to the SSE client — avoids word-
           splitting final_response on spaces (which lost OpenAI
           token boundary information and added ~2-4s artificial
           delay via asyncio.sleep(0.02)).
         - Empty list default: safe — server.py falls back to
           space-split of final_response if list is empty.
         - Not persisted to cache — cache stores final_response
           (assembled string) only. On cache hit, stream_tokens
           stays [] and server.py sends final_response as single
           chunk (no streaming delay for cached responses).

═══════════════════════════════════════════════════════════════
"""
import re
from typing import Any
from pydantic import BaseModel, Field, field_validator

# ── Constants ─────────────────────────────────────────────
MIN_QUERY_LENGTH = 1
MAX_QUERY_LENGTH = 500
MIN_QUERY_WORDS = 1
MAX_QUERY_WORDS = 100
MAX_RESPONSE_WORDS = 400
MAX_RESPONSE_CHARS = 2000
MAX_CONVERSATION_TURNS = 10

# ── Citation ──────────────────────────────────────────────
class Citation(BaseModel):
    index: int
    url: str
    section: str
    title: str = ""

# ── Retrieved Chunk ───────────────────────────────────────
class RetrievedChunk(BaseModel):
    chunk_id: str
    content: str
    source_url: str
    section: str
    title: str = ""
    score: float = 0.0

# ── Agent State ───────────────────────────────────────────
class AgentState(BaseModel):
    query: str
    conversation_history: list[dict] = Field(default_factory=list)
    request_id: str = ""
    # Cache
    cache_hit: bool = False
    cached_response: str | None = None
    # Safety
    input_safe: bool | None = None
    output_safe: bool | None = None
    refusal_triggered: bool = False
    refusal_reason: str | None = None
    # Retrieval
    retrieved_chunks: list[RetrievedChunk] = Field(
        default_factory=list
    )
    # Classification (v1.2.0 — set by classifier_node)
    intent: str = ""
    query_type: str = ""
    # Generation
    raw_response: str | None = None
    model_used: str | None = None
    # Prompt (v1.2.0 — set by prompt_builder_node)
    built_prompt: str = ""
    # Final output
    citations: list[Citation] = Field(default_factory=list)
    final_response: str | None = None
    # Streaming tokens (v2.3.0 — populated by generator_node
    # when stream=True; consumed by server.py stream_response)
    stream_tokens: list[str] = Field(default_factory=list)
    # Flags
    needs_empathy: bool = False
    needs_disclaimer: bool = False
    is_sensitive: bool = False
    # Observability
    latency_ms: dict[str, float] = Field(default_factory=dict)
    token_usage: dict[str, int] = Field(default_factory=dict)
    error: str | None = None

# ── API Models ────────────────────────────────────────────
class ChatRequestBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    conversation_history: list[dict] = Field(default_factory=list)
    session_id: str = ""

    @field_validator("query")
    @classmethod
    def validate_query(cls, v):
        clean = re.sub(r"<[^>]+>", "", v) if v else v
        clean = " ".join(clean.split())
        if not clean:
            raise ValueError("Query cannot be empty.")
        words = clean.split()
        if len(words) > MAX_QUERY_WORDS:
            clean = " ".join(words[:MAX_QUERY_WORDS])
        return clean

class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation]
    cached: bool
    model_used: str | None = None
    request_id: str = ""

# ── Cache Entry ───────────────────────────────────────────
class CacheEntry(BaseModel):
    query: str
    embedding: list[float]
    response: ChatResponse
    timestamp: float