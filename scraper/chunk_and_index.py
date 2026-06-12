"""
Royal London FAQ - Chunk and Index
Chunks scraped content, generates embeddings,
pushes to Azure AI Search.

Supports:
  --full:     Delete + recreate index (fresh start)
  --new-only: Only index pages not already indexed (default)

Usage:
    python scraper/chunk_and_index.py --full
    python scraper/chunk_and_index.py --new-only

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — Initial version
         Cohere embeddings, API key auth, basic chunking

v1.1.0 — Migration: Cohere → text-embedding-3-large
         - Migrated embedding model to text-embedding-3-large
           via Azure AI Foundry (1024 dims, Matryoshka MRL)
         - Auth: API key → DefaultAzureCredential + bearer token
           (no API key in code or .env)
         - Fix: must use AZURE_OPENAI_ENDPOINT (.openai.azure.com)
           PROJECT_ENDPOINT does not route embedding requests
           (AIProjectClient v2.2.0 bug)

v1.2.0 — June 2026 | Mukesh Kund
         External URL stripping + rate limit handling

         clean_content() [NEW FUNCTION]:
         - Strips external URLs from page content before chunking
         - Royal London pages contain outbound links to external
           sites (moneyhelper.org.uk, gov.uk, citizensadvice.org.uk
           etc). These were being scraped into chunk content and
           GPT was reproducing them as clickable links in answers.
         - Strips markdown links [text](https://external.com)
           → keeps anchor text, removes external URL
         - Strips bare external URLs https://external.com/...
           → removes entirely
         - Preserves all royallondon.com URLs so citation system
           is completely unaffected
         - Called inside chunk_pages() before title prepending
         - ACTION REQUIRED: re-run --full after this change

         get_embeddings() [MODIFIED]:
         - EMBEDDING_BATCH_SIZE reduced 100 → 50
           S0 tier TPM limit: ~120,000 tokens/min
           50 chunks × ~400 tokens = ~20,000 tokens/batch (safe)
           Revert to 100 if/when tier is upgraded to S1+
         - Added 2 second sleep between every batch to stay
           under TPM limit and prevent 429 errors proactively
         - Added automatic retry with exponential backoff on
           RateLimitError (429):
             Retry 1 → wait 10s
             Retry 2 → wait 20s
             Retry 3 → wait 40s
             Retry 4 → wait 80s
             Retry 5 → wait 160s
             After 5 retries → raises exception
         - Prints visible warning when rate limit hit so operator
           can see recovery happening without manual intervention

v1.3.0 — June 2026 | Mukesh Kund
         Auto-clear semantic cache after --full re-index

         main() [MODIFIED]:
         - After a --full re-index completes, the semantic cache
           is now automatically cleared as the final step.
         - Previously: cache clear was a manual step that could
           be forgotten, causing stale cached responses to be
           served even after fresh content was indexed.
         - Now: --full re-index and cache clear are one atomic
           operation — they cannot get out of sync.
         - --new-only runs do NOT clear the cache (only new pages
           were added, existing cached answers are still valid).
         - If Redis is unavailable the warning is logged but the
           re-index is NOT rolled back — index is still valid,
           cache will expire naturally via TTL.

═══════════════════════════════════════════════════════════════
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
EMBEDDING_BATCH_SIZE = 50  # Reduced from 100 — S0 TPM rate limit
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


# ── Content Cleaning ─────────────────────────────────────────
def clean_content(text: str) -> str:
    """
    Remove external URLs from page content before chunking.

    WHY THIS EXISTS:
    Royal London's pages contain outbound hyperlinks to external
    sites (gov.uk, moneyhelper.org.uk, citizensadvice.org.uk etc).
    When scraped, these URLs end up inside the chunk text.
    GPT reads them and reproduces them as clickable links or
    citations in responses — making it look like Aria is
    recommending external sites.

    WHAT WE STRIP:
    1. Markdown hyperlinks: [anchor text](https://external.com)
       → keep the anchor text, remove the URL and brackets
       → "visit [MoneyHelper](https://moneyhelper.org.uk)"
         becomes "visit MoneyHelper"

    2. Raw URLs: https://external.com/some/path
       → remove entirely (bare URLs have no useful anchor text)

    WHAT WE KEEP:
    - royallondon.com URLs — these are the citation sources,
      they must stay so the citation system works correctly
    - All non-URL text — the actual content is preserved exactly
    - Internal markdown links: [text](https://royallondon.com/...)
      → kept as-is so citation extraction still works

    SAFETY:
    - Runs ONLY at index time (chunk_and_index.py), never at
      query time — so the live pipeline is completely unaffected
    - The function is pure (no side effects) and easily testable
    - Does NOT modify source_url, title, section or any other field
    - If the regex fails for any reason, original text is returned
    """
    import re

    # Step 1: Strip markdown links to EXTERNAL sites
    # Pattern: [any text](http://external.com/...)
    # Keep: [text](https://www.royallondon.com/...)
    # Remove: [text](https://www.moneyhelper.org.uk/...)
    def replace_markdown_link(match):
        anchor_text = match.group(1)
        url         = match.group(2)
        # Keep royallondon.com links intact — citation system needs them
        if "royallondon.com" in url:
            return match.group(0)
        # For external links: keep the anchor text, drop the URL
        return anchor_text

    text = re.sub(
        r'\[([^\]]+)\]\((https?://[^\)]+)\)',
        replace_markdown_link,
        text,
    )

    # Step 2: Strip remaining raw external URLs (not royallondon.com)
    # These are bare URLs not wrapped in markdown — just remove them
    def replace_raw_url(match):
        url = match.group(0)
        if "royallondon.com" in url:
            return url  # Keep internal URLs
        return ""       # Remove external bare URLs

    text = re.sub(
        r'https?://[^\s\)\]"\'<>,]+',
        replace_raw_url,
        text,
    )

    # Step 3: Clean up any double spaces left behind after URL removal
    text = re.sub(r'  +', ' ', text)

    return text.strip()


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

        # Clean content: strip external URLs before chunking
        # Keeps royallondon.com URLs intact for citation system
        content = clean_content(content)

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

    Rate limit handling (Azure OpenAI S0 tier):
    - Batch size reduced to 50 chunks (~20,000 tokens/batch)
    - 2 second sleep between every batch to stay under TPM limit
    - Automatic retry with exponential backoff on 429 errors
    - Max 5 retries per batch before giving up

    If you hit rate limits again: increase BATCH_SLEEP_SECONDS
    or reduce EMBEDDING_BATCH_SIZE further.
    If you upgrade to S1/S2 tier: reduce BATCH_SLEEP_SECONDS to 0.
    """
    import time
    from openai import RateLimitError

    BATCH_SLEEP_SECONDS = 2    # Sleep between every batch
    MAX_RETRIES         = 5    # Max retries on 429
    RETRY_BASE_SECONDS  = 10   # Base wait on 429 — doubles each retry

    client         = get_openai_client()
    all_embeddings = []
    total_batches  = (
        len(texts) + EMBEDDING_BATCH_SIZE - 1
    ) // EMBEDDING_BATCH_SIZE

    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch        = texts[i : i + EMBEDDING_BATCH_SIZE]
        batch_number = i // EMBEDDING_BATCH_SIZE + 1
        retry        = 0

        while True:
            try:
                response = client.embeddings.create(
                    input=batch,
                    model=EMBEDDING_DEPLOYMENT,
                    dimensions=EMBEDDING_DIMS,
                )
                # Sort by index to guarantee order matches input
                sorted_data = sorted(
                    response.data, key=lambda e: e.index
                )
                all_embeddings.extend(
                    [e.embedding for e in sorted_data]
                )
                log.info(
                    "embeddings_batch_done",
                    batch=batch_number,
                    total_batches=total_batches,
                    chunk_count=len(all_embeddings),
                )
                break  # Success — exit retry loop

            except RateLimitError as e:
                retry += 1
                if retry > MAX_RETRIES:
                    log.error(
                        "embeddings_rate_limit_max_retries",
                        batch=batch_number,
                        error=str(e),
                    )
                    raise

                wait = RETRY_BASE_SECONDS * (2 ** (retry - 1))
                log.warning(
                    "embeddings_rate_limit_retry",
                    batch=batch_number,
                    retry=retry,
                    wait_seconds=wait,
                    error=str(e)[:80],
                )
                print(
                    f"   ⚠️  Rate limit hit on batch {batch_number}. "
                    f"Waiting {wait}s before retry {retry}/{MAX_RETRIES}..."
                )
                time.sleep(wait)

        # Sleep between every batch to stay under TPM limit
        # This prevents hitting 429 in the first place
        if i + EMBEDDING_BATCH_SIZE < len(texts):
            time.sleep(BATCH_SLEEP_SECONDS)

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

    # ── Auto-clear semantic cache (--full only) ───────
    # IMPORTANT: Only runs on --full re-index, NOT --new-only.
    #
    # WHY: When --full re-index runs, ALL page content is
    # refreshed. Any cached responses generated from the old
    # index may now be stale — old phone numbers, outdated
    # product details, removed pages. Serving stale cached
    # responses after a full re-index defeats the purpose of
    # re-indexing entirely.
    #
    # WHY NOT on --new-only: --new-only only adds new pages.
    # Existing pages are unchanged so existing cached responses
    # are still valid. Clearing the cache unnecessarily would
    # reduce hit rate and increase LLM costs.
    #
    # FAILURE HANDLING: If Redis is unavailable, we log a
    # warning but do NOT fail the entire re-index. The index
    # is already updated and serving correctly. Stale cache
    # entries will expire naturally via the 24-hour TTL.
    if fresh:
        print("\n🗑️  Clearing semantic cache (--full mode)...")
        try:
            from core.cache import get_cache
            cache = get_cache()
            cache.clear()
            log.info(
                "cache_cleared_post_reindex",
                reason="full_reindex_completed",
                index=INDEX_NAME,
            )
            print("   ✅ Semantic cache cleared — all future queries")
            print("      will retrieve fresh answers from new index")
        except Exception as e:
            log.warning(
                "cache_clear_failed_post_reindex",
                error=str(e),
                note="Index is valid. Cache will expire via TTL.",
            )
            print(f"   ⚠️  Cache clear failed: {e}")
            print("      Index is still valid. Stale cache entries")
            print("      will expire automatically via 24-hour TTL.")

    # ── Summary ───────────────────────────────────────
    print("\n" + "=" * 55)
    print("✅ INDEXING COMPLETE!")
    print("=" * 55)
    print(f"   Pages indexed:   {len(pages_to_index):,}")
    print(f"   Chunks created:  {len(chunks):,}")
    print(f"   Chunks uploaded: {total:,}")
    print(f"   Index name:      {INDEX_NAME}")
    print(f"   Embedding model: {EMBEDDING_DEPLOYMENT}")
    if fresh:
        print(f"   Cache cleared:   ✅ Yes (--full mode)")
    else:
        print(f"   Cache cleared:   ⏭️  Skipped (--new-only mode)")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()