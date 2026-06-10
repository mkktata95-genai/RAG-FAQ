"""
Shared embedding client — single instance reused across all nodes.
Eliminates duplicate embedding generation.

Migration: Cohere → text-embedding-3-large via Azure AI Foundry
Auth:       DefaultAzureCredential + bearer token (no API key required)
Fix:        Uses AZURE_OPENAI_ENDPOINT (.openai.azure.com) for embeddings
            as PROJECT_ENDPOINT does not route embedding requests
"""

import os
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
import structlog

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT  = os.getenv(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "text-embedding-3-large",
)
EMBEDDING_DIMENSIONS  = int(
    os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1024")
)

# Singleton clients — created once, reused
_credential:    DefaultAzureCredential | None = None
_openai_client: AzureOpenAI | None            = None


def get_credential() -> DefaultAzureCredential:
    """Get or create singleton DefaultAzureCredential."""
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
        log.info("credential_created")
    return _credential


def get_openai_client() -> AzureOpenAI:
    """
    Get or create singleton AzureOpenAI client.

    Uses AZURE_OPENAI_ENDPOINT (.openai.azure.com) because
    PROJECT_ENDPOINT does not route embedding requests.
    Auth via DefaultAzureCredential + cognitiveservices audience.
    """
    global _openai_client
    if _openai_client is None:
        if not AZURE_OPENAI_ENDPOINT:
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT is not set in .env"
            )
        token_provider = get_bearer_token_provider(
            get_credential(),
            "https://cognitiveservices.azure.com/.default",
        )
        _openai_client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            azure_ad_token_provider=token_provider,
            api_version="2024-12-01-preview",
        )
        log.info(
            "openai_client_created",
            endpoint=AZURE_OPENAI_ENDPOINT,
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
                    'document' for indexing (kept for API
                    compatibility with old Cohere callers)
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