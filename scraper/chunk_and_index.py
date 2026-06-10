"""
Royal London FAQ - Chunk and Index
Chunks scraped content, generates embeddings,
pushes to Azure AI Search.

Migration: Cohere → text-embedding-3-large via Azure AI Foundry
Auth:       DefaultAzureCredential + bearer token (no API key required)
Fix:        Uses AZURE_OPENAI_ENDPOINT (.openai.azure.com) for embeddings
            as PROJECT_ENDPOINT does not route embedding requests

Supports:
  --full:     Delete + recreate index (fresh start)
  --new-only: Only index pages not already indexed (default)

Usage:
    python scraper/chunk_and_index.py --full
    python scraper/chunk_and_index.py --new-only
"""

import json
import sys
import uuid
import argparse
import os

import structlog
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
SCRAPED_FILE = (
    "scraper/data/royal_london_faq_clean_20260609_142353.json"
)
CHUNK_SIZE           = 1600
CHUNK_OVERLAP        = 200
INDEX_NAME           = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index")
EMBEDDING_DIMS       = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1024"))
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
SEARCH_ENDPOINT      = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT = os.getenv(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "text-embedding-3-large",
)

# Batch sizes
EMBEDDING_BATCH_SIZE = 100
UPLOAD_BATCH_SIZE    = 100

# ── Singleton clients ─────────────────────────────────────────
_credential    = None
_openai_client = None


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
            dimensions=EMBEDDING_DIMS,
        )
    return _openai_client


def get_search_client() -> SearchClient:
    """Create SearchClient with DefaultAzureCredential."""
    return SearchClient(
        endpoint=SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=get_credential(),
    )


def get_search_index_client() -> SearchIndexClient:
    """Create SearchIndexClient with DefaultAzureCredential."""
    return SearchIndexClient(
        endpoint=SEARCH_ENDPOINT,
        credential=get_credential(),
    )


# ── Chunking ──────────────────────────────────────────────────
def chunk_pages(pages: list[dict]) -> list[dict]:
    """
    Split pages into chunks for indexing.
    Prepends title to each chunk for better embeddings.
    Respects markdown structure via separator hierarchy.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    chunks = []
    for page in pages:
        content  = page.get("content", "").strip()
        title    = page.get("title", "")
        url      = page.get("url", "")
        section  = page.get("section", "")
        audience = page.get("audience", "customer")

        if not content or len(content) < 50:
            log.warning("skipping_empty_page", url=url)
            continue

        # Prepend title → every chunk benefits from page context
        content_with_title = (
            f"{title}\n\n{content}" if title else content
        )

        splits = splitter.split_text(content_with_title)

        for i, split in enumerate(splits):
            if len(split.strip()) < 50:
                continue
            chunks.append({
                "chunk_id":     str(uuid.uuid4()),
                "content":      split.strip(),
                "source_url":   url,
                "title":        title,
                "section":      section,
                "audience":     audience,
                "scraped_at":   page.get("scraped_at", ""),
                "chunk_index":  i,
                "total_chunks": len(splits),
            })

    log.info("chunking_complete", total_chunks=len(chunks))
    return chunks


# ── Index management ──────────────────────────────────────────
def get_indexed_urls() -> set:
    """
    Get all URLs already indexed in Azure AI Search.
    Used for --new-only mode.
    """
    try:
        client  = get_search_client()
        results = client.search(
            search_text="*",
            select=["source_url"],
            top=1000,
        )
        urls = set()
        for r in results:
            urls.add(r.get("source_url", ""))

        log.info("indexed_urls_fetched", count=len(urls))
        return urls
    except Exception as e:
        log.warning("get_indexed_urls_failed", error=str(e))
        return set()


def create_or_update_index(fresh: bool = False):
    """
    Create index with vector search support.
    fresh=True:  Delete and recreate (wipes existing data)
    fresh=False: Create only if not exists
    """
    client = get_search_index_client()

    if fresh:
        try:
            client.delete_index(INDEX_NAME)
            log.info("existing_index_deleted", index=INDEX_NAME)
        except Exception:
            pass  # Index didn't exist — that's fine

    fields = [
        SimpleField(
            name="chunk_id",
            type=SearchFieldDataType.String,
            key=True,
        ),
        SearchableField(
            name="content",
            type=SearchFieldDataType.String,
        ),
        SearchableField(
            name="title",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="source_url",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="section",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="audience",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="scraped_at",
            type=SearchFieldDataType.String,
        ),
        SimpleField(
            name="chunk_index",
            type=SearchFieldDataType.Int32,
        ),
        SimpleField(
            name="total_chunks",
            type=SearchFieldDataType.Int32,
        ),
        SearchField(
            name="embedding",
            type=SearchFieldDataType.Collection(
                SearchFieldDataType.Single
            ),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIMS,
            vector_search_profile_name="rl-vector-profile",
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(name="rl-hnsw")
        ],
        profiles=[
            VectorSearchProfile(
                name="rl-vector-profile",
                algorithm_configuration_name="rl-hnsw",
            )
        ],
    )

    try:
        client.create_index(
            SearchIndex(
                name=INDEX_NAME,
                fields=fields,
                vector_search=vector_search,
            )
        )
        log.info("index_created", index=INDEX_NAME)
    except Exception as e:
        if "already exists" in str(e).lower():
            log.info("index_already_exists", index=INDEX_NAME)
        else:
            raise


# ── Embeddings ────────────────────────────────────────────────
def get_embeddings(
    texts: list[str],
) -> list[list[float]]:
    """
    Generate embeddings in batches using text-embedding-3-large.
    Processes in batches of 100 for safe memory usage.
    """
    client         = get_openai_client()
    all_embeddings = []
    total_batches  = (
        len(texts) + EMBEDDING_BATCH_SIZE - 1
    ) // EMBEDDING_BATCH_SIZE

    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch    = texts[i : i + EMBEDDING_BATCH_SIZE]
        response = client.embeddings.create(
            input=batch,
            model=EMBEDDING_DEPLOYMENT,
            dimensions=EMBEDDING_DIMS,
        )
        # Sort by index to guarantee order matches input
        sorted_data = sorted(response.data, key=lambda e: e.index)
        all_embeddings.extend([e.embedding for e in sorted_data])

        log.info(
            "embeddings_batch_done",
            batch=i // EMBEDDING_BATCH_SIZE + 1,
            total_batches=total_batches,
            chunk_count=len(all_embeddings),
        )

    return all_embeddings


# ── Upload ────────────────────────────────────────────────────
def upload_chunks(
    chunks: list[dict],
    embeddings: list[list[float]],
) -> int:
    """Upload chunks with embeddings to Azure AI Search."""
    client    = get_search_client()
    documents = [
        {**chunk, "embedding": emb}
        for chunk, emb in zip(chunks, embeddings)
    ]
    total_uploaded = 0

    for i in range(0, len(documents), UPLOAD_BATCH_SIZE):
        batch     = documents[i : i + UPLOAD_BATCH_SIZE]
        result    = client.upload_documents(documents=batch)
        succeeded = sum(1 for r in result if r.succeeded)
        total_uploaded += succeeded
        log.info(
            "upload_batch_done",
            uploaded=total_uploaded,
            total=len(documents),
        )

    log.info("upload_complete", total=total_uploaded)
    return total_uploaded


# ── Verify ────────────────────────────────────────────────────
def verify_index():
    """Run a test hybrid query to verify the index works."""
    client     = get_openai_client()
    test_query = "How do I make a claim?"

    response = client.embeddings.create(
        input=[test_query],
        model=EMBEDDING_DEPLOYMENT,
        dimensions=EMBEDDING_DIMS,
    )
    query_embedding = response.data[0].embedding

    search_client = get_search_client()
    results = search_client.search(
        search_text=test_query,
        vector_queries=[
            VectorizedQuery(
                vector=query_embedding,
                k_nearest_neighbors=3,
                fields="embedding",
            )
        ],
        select=["chunk_id", "title", "content",
                "source_url", "section"],
        top=3,
    )

    print("\n" + "=" * 55)
    print("🔍 Test Query: 'How do I make a claim?'")
    print("=" * 55)
    for i, result in enumerate(results, 1):
        print(f"\n[{i}] Title:   {result.get('title', 'N/A')}")
        print(f"    Section: {result.get('section', 'N/A')}")
        print(f"    URL:     {result.get('source_url', 'N/A')}")
        print(f"    Preview: {result['content'][:150]}...")
    print("\n✅ Index verification complete!")


# ── Main ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Chunk and index RLG FAQ pages"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Delete and recreate index (fresh start)",
    )
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Only index pages not already indexed (default)",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=SCRAPED_FILE,
        help="Path to scraped JSON file",
    )
    args         = parser.parse_args()
    fresh        = args.full
    scraped_file = args.file

    print("\n🚀 RLG Chunk and Index Pipeline")
    print("=" * 55)
    print(f"   Mode:     {'FULL (fresh index)' if fresh else 'NEW ONLY (append)'}")
    print(f"   File:     {scraped_file}")
    print(f"   Index:    {INDEX_NAME}")
    print(f"   Model:    {EMBEDDING_DEPLOYMENT}")
    print(f"   Dims:     {EMBEDDING_DIMS}")
    print(f"   Search:   {SEARCH_ENDPOINT}")
    print(f"   OpenAI:   {AZURE_OPENAI_ENDPOINT}")
    print("=" * 55)

    # ── Validate config ───────────────────────────────
    if not AZURE_OPENAI_ENDPOINT:
        print("❌ AZURE_OPENAI_ENDPOINT not set in .env")
        sys.exit(1)
    if not SEARCH_ENDPOINT:
        print("❌ AZURE_SEARCH_ENDPOINT not set in .env")
        sys.exit(1)

    # ── Load pages ────────────────────────────────────
    with open(scraped_file, encoding="utf-8") as f:
        pages = json.load(f)
    log.info("pages_loaded", total=len(pages))
    print(f"\n📄 Loaded {len(pages):,} pages")

    # ── Filter new pages if --new-only ────────────────
    if not fresh:
        print("\n🔍 Checking which pages are already indexed...")
        indexed_urls   = get_indexed_urls()
        print(f"   Already indexed: {len(indexed_urls):,} URLs")

        pages_to_index = [
            p for p in pages
            if p.get("url", "").rstrip("/")
            not in {u.rstrip("/") for u in indexed_urls}
        ]
        print(f"   New pages to index: {len(pages_to_index):,}")

        if not pages_to_index:
            print("\n✅ All pages already indexed! Nothing to do.")
            return
    else:
        pages_to_index = pages

    # ── Step 1: Chunk ─────────────────────────────────
    print(f"\n📄 Step 1/4: Chunking {len(pages_to_index):,} pages...")
    chunks = chunk_pages(pages_to_index)
    print(f"   Created {len(chunks):,} chunks")

    # ── Step 2: Create/update index ───────────────────
    print(f"\n🔧 Step 2/4: {'Recreating' if fresh else 'Ensuring'} index...")
    create_or_update_index(fresh=fresh)
    print(f"   Index '{INDEX_NAME}' ready")

    # ── Step 3: Embeddings ────────────────────────────
    print(
        f"\n🧠 Step 3/4: Generating embeddings "
        f"for {len(chunks):,} chunks..."
    )
    print("   (This will take a few minutes...)")
    embeddings = get_embeddings([c["content"] for c in chunks])
    print(f"   Generated {len(embeddings):,} embeddings")

    # ── Step 4: Upload ────────────────────────────────
    print(f"\n📤 Step 4/4: Uploading to Azure AI Search...")
    total = upload_chunks(chunks, embeddings)
    print(f"   Uploaded {total:,} chunks")

    # ── Verify ────────────────────────────────────────
    print("\n🔍 Verifying index with test query...")
    verify_index()

    # ── Summary ───────────────────────────────────────
    print("\n" + "=" * 55)
    print("✅ INDEXING COMPLETE!")
    print("=" * 55)
    print(f"   Pages indexed:   {len(pages_to_index):,}")
    print(f"   Chunks created:  {len(chunks):,}")
    print(f"   Chunks uploaded: {total:,}")
    print(f"   Index name:      {INDEX_NAME}")
    print(f"   Embedding model: {EMBEDDING_DEPLOYMENT}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()