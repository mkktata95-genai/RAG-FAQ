"""
Pydantic schemas shared across all agents.
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
    title: str = ""          # ← NEW

# ── Retrieved Chunk ───────────────────────────────────────
class RetrievedChunk(BaseModel):
    chunk_id: str
    content: str
    source_url: str
    section: str
    title: str = ""          # ← NEW
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
    # Generation
    raw_response: str | None = None
    model_used: str | None = None
    # Final output
    citations: list[Citation] = Field(default_factory=list)
    final_response: str | None = None
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