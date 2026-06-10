"""
Shared embedding client — single instance reused across all nodes.
Eliminates duplicate embedding generation.

Migration: Cohere → text-embedding-3-large via Azure AI Foundry
Auth:       DefaultAzureCredential (no API key required)
"""

import os
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.ai.projects import AIProjectClient
from dotenv import load_dotenv
import structlog

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
PROJECT_ENDPOINT      = os.getenv("PROJECT_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT  = os.getenv(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "text-embedding-3-large",
)
EMBEDDING_DIMENSIONS  = int(
    os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1024")
)

# Singleton clients — created once, reused
_project_client: AIProjectClient | None = None
_openai_client:  AzureOpenAI | None     = None


def get_project_client() -> AIProjectClient:
    """Get or create singleton AIProjectClient."""
    global _project_client
    if _project_client is None:
        if not PROJECT_ENDPOINT:
            raise ValueError(
                "PROJECT_ENDPOINT is not set in .env"
            )
        _project_client = AIProjectClient(
            endpoint=PROJECT_ENDPOINT,
            credential=DefaultAzureCredential(),
        )
        log.info("project_client_created", endpoint=PROJECT_ENDPOINT)
    return _project_client


def get_openai_client() -> AzureOpenAI:
    """
    Get or create singleton OpenAI client via AIProjectClient.
    Uses DefaultAzureCredential — no API key needed.
    """
    global _openai_client
    if _openai_client is None:
        project = get_project_client()
        _openai_client = project.inference.get_azure_openai_client(
            api_version="2024-12-01-preview"
        )
        log.info(
            "openai_client_created",
            deployment=EMBEDDING_DEPLOYMENT,
            dimensions=EMBEDDING_DIMENSIONS,
        )
    return _openai_client


def get_embedding(
    text: str,
    input_type: str = "query",
) -> list[float]:
    """
    Generate embedding for a single text.
    Reuses singleton client for connection pooling.

    Args:
        text:       Text to embed
        input_type: 'query' for search queries,
                    'document' for indexing (ignored by OpenAI,
                    kept for API compatibility with old Cohere calls)
    """
    client = get_openai_client()
    response = client.embeddings.create(
        input=[text],
        model=EMBEDDING_DEPLOYMENT,
        dimensions=EMBEDDING_DIMENSIONS,
    )
    embedding = response.data[0].embedding
    log.debug(
        "embedding_generated",
        input_type=input_type,
        dims=len(embedding),
    )
    return embedding


def get_embeddings_batch(
    texts: list[str],
    input_type: str = "document",
) -> list[list[float]]:
    """
    Generate embeddings for multiple texts in a single API call.
    OpenAI supports up to 2048 inputs per request.

    Args:
        texts:      List of texts to embed
        input_type: Kept for API compatibility — not used by OpenAI
    """
    if not texts:
        return []

    client = get_openai_client()
    response = client.embeddings.create(
        input=texts,
        model=EMBEDDING_DEPLOYMENT,
        dimensions=EMBEDDING_DIMENSIONS,
    )

    # Sort by index to ensure order matches input
    embeddings = sorted(response.data, key=lambda e: e.index)
    log.debug(
        "embeddings_batch_generated",
        count=len(embeddings),
        dims=EMBEDDING_DIMENSIONS,
    )
    return [e.embedding for e in embeddings]