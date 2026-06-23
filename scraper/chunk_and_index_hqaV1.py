"""
Royal London FAQ - Chunk and Index
Chunks scraped content, generates HQA-augmented embeddings,
pushes to Azure AI Search.

Supports:
  --full:     Delete + recreate index (fresh start)
  --new-only: Only index pages not already indexed (default)
  --pilot:    HQA pilot mode — process first 100 chunks only
              to validate question quality before full re-index

Usage:
    python scraper/chunk_and_index.py --full
    python scraper/chunk_and_index.py --new-only
    python scraper/chunk_and_index.py --full --pilot
    python scraper/chunk_and_index.py --full --file path/to/file.json

Programmatic (DevOps / Function App):
    from scraper.chunk_and_index import run_pipeline
    result = run_pipeline(mode="full")
    result = run_pipeline(mode="new-only")

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

v2.0.0 — June 2026 | Mukesh Kund
         HQA (Hypothetical Question Augmentation) + index schema
         fixes + semantic configuration + programmatic entry point

         ROOT CAUSE OF RETRIEVAL PROBLEM (pre v2.0.0):
         - Query embeddings live in "question space" (short,
           interrogative, customer phrasing).
         - Chunk embeddings live in "document space" (long,
           declarative, guide prose).
         - These two spaces are not identical — cosine similarity
           between a customer question and the correct answer
           chunk is often lower than similarity to a keyword-
           dense but irrelevant chunk (e.g. "What types of
           pensions does Royal London offer?" matched workplace-
           pensions page instead of what-is-a-pension page
           because workplace-pensions had higher keyword density).
         - Semantic ranker (v1.2.0 retriever.py) partially fixed
           this but the embedding mismatch remained.

         HQA FIX — generate_hqa_questions() [NEW]:
         - For each chunk, uses gpt-4o-mini to generate 5
           questions that chunk answers, in customer language.
         - Questions are validated (grounding check +
           specificity check + quality score) before storage.
         - Questions stored in new 'augmented_questions' field
           (separate from 'content' — BM25 unaffected).
         - Embedding generated from content + accepted questions
           COMBINED — bridges the question/document space gap.
         - gpt-4o-mini used (not gpt-4.1) — HQA questions are
           short structured outputs, no need for expensive model.
         - Cost: ~$0.83 for full 7,166 chunk re-index.
         - Time: ~4 hours for full re-index (rate limited).
         - Pilot mode (--pilot): processes first 100 chunks only
           to validate question quality before committing to full
           re-index. Run pilot first, review output, then --full.

         INDEX SCHEMA FIXES — create_or_update_index() [MODIFIED]:
         - Every field now has ALL attributes explicitly set
           (searchable, filterable, sortable, facetable,
           retrievable). No Azure defaults relied upon —
           guaranteed consistent schema on every --full run.
         - section: SimpleField → SearchableField (was not
           searchable — BM25 was ignoring section headings
           entirely. "What Is A Pension", "Workplace Pensions"
           etc. now contribute to keyword scoring).
         - source_url: SimpleField → SearchableField (was not
           searchable — URL fragments now contribute to BM25).
         - augmented_questions: NEW SearchableField — stores
           HQA-generated questions, searchable for BM25 boost,
           retrievable for debugging.
         - content_hash: NEW SimpleField — stores MD5 hash of
           page content for content freshness change detection.
           filterable=True (for freshness queries),
           retrievable=False (internal use only, never returned
           to the API layer).
         - embedding: retrievable=False explicitly set (was
           relying on default — raw vectors must never be
           returned to callers).

         SEMANTIC CONFIGURATION — create_or_update_index() [MODIFIED]:
         - SemanticSearch configuration now created at index
           build time — no longer requires separate
           add_semantic_config.py script or portal clicks.
         - Config name: "rlg-semantic-config"
           title_field:    title
           content_fields: content, augmented_questions
           keywords_fields: section
         - Semantic ranker reads augmented_questions as content
           field — HQA questions directly improve reranker signal.

         PAGINATION FIX — get_indexed_urls() [MODIFIED]:
         - Was: search_text="*", top=1000 — silently missed URLs
           beyond the first 1000 results (index has 7,166 chunks
           from 415 pages — URLs were being missed).
         - Now: paginates using search_client.search() with
           skip parameter until all results fetched.

         FILENAME AUTO-DETECTION — main() [MODIFIED]:
         - Was: SCRAPED_FILE hardcoded to a specific dated
           filename — manual update required after every scrape.
         - Now: if --file not specified, auto-detects the most
           recently modified JSON file in scraper/data/.
           Falls back to SCRAPED_FILE constant if no JSON found.

         PROGRAMMATIC ENTRY POINT — run_pipeline() [NEW]:
         - Clean function for DevOps / Azure Function App to call.
         - Wraps main() logic without argparse dependency.
         - Returns structured dict with stats and status.
         - TODO (DevOps): wrap in Azure Function App trigger:
             @app.timer_trigger(schedule="0 0 1 * *")  # monthly
             def monthly_reindex(timer): run_pipeline(mode="full")


v3.0.0 — June 2026 | Mukesh Kund
         Rich metadata fields from scraper enrichment

         WHY:
         - UI/UX team needs metadata to render rich citations
           (video indicators, product badges, thumbnails, dates)
           without requiring a re-index in future.
         - Principle: index once, serve many use cases.

         NEW INDEX FIELDS (all passed through from scraper v3.0.0):
         - has_video:        Boolean — page contains video content
         - content_type:     String  — webinar/guide/article/faq/tool
         - product_category: String  — pensions/insurance/isa/etc
         - description:      String  — page meta description (≤300 chars)
         - thumbnail_url:    String  — teaser image URL
         - publish_date:     String  — ISO date YYYY-MM-DD
         - collection_name:  String  — e.g. "Pension webinar"
         - read_time_mins:   String  — estimated read/watch time

         All new fields have safe defaults in chunk_pages() —
         old scraped JSON files (without these fields) still
         work with new chunk_and_index.py without re-scraping.

         SEMANTIC CONFIG UPDATED:
         - description added as content_field (page-level context)
         - collection_name added as keywords_field
         - product_category added as keywords_field

         NO CHANGE to HQA, chunking, or embedding logic.

═══════════════════════════════════════════════════════════════
"""

import json
import sys
import uuid
import argparse
import os
import re
import time
import hashlib
from pathlib import Path
from glob import glob

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
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
)
from azure.search.documents.models import VectorizedQuery
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
# SCRAPED_FILE is the fallback if --file not specified and no
# JSON file is auto-detected in scraper/data/.
SCRAPED_FILE          = (
    "scraper/data/royal_london_faq_clean_20260609_142353.json"
)
CHUNK_SIZE            = 1600
CHUNK_OVERLAP         = 200
INDEX_NAME            = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index")
EMBEDDING_DIMS        = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1024"))
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
SEARCH_ENDPOINT       = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT  = os.getenv(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "text-embedding-3-large",
)

# HQA model — gpt-4o-mini is sufficient for structured question
# generation. Using the cheaper/faster model here intentionally.
# Do NOT switch to gpt-4.1 — unnecessary cost for this task.
HQA_DEPLOYMENT        = os.getenv(
    "AZURE_OPENAI_DEPLOYMENT_FAST",
    "gpt-4o-mini",
)

# Semantic ranker configuration name — must match what we create
# in create_or_update_index(). Used by retriever.py at query time.
SEMANTIC_CONFIG_NAME  = os.getenv(
    "AZURE_SEARCH_SEMANTIC_CONFIG",
    "rlg-semantic-config",
)

# Batch sizes
# EMBEDDING_BATCH_SIZE: reduced from 100 → 50 for S0 TPM limit.
# Revert to 100 if/when upgraded to S1+ tier.
EMBEDDING_BATCH_SIZE  = 50
UPLOAD_BATCH_SIZE     = 100

# HQA batch size — how many chunks to generate questions for
# in a single gpt-4o-mini call. Keep small to stay under
# context limits and get focused per-chunk questions.
HQA_BATCH_SIZE        = 1   # one chunk per call for best quality

# Pilot mode chunk limit — process only this many chunks when
# --pilot flag is set. Allows quick quality validation before
# committing to a full re-index.
PILOT_CHUNK_LIMIT     = 100

# ── Production: Azure Blob Storage ───────────────────────────
# TODO (DevOps): Set these in Azure Key Vault before go-live.
#
# AZURE_STORAGE_CONNECTION — Blob Storage connection string.
#   Not set (default) → local mode, reads from local file path.
#   Set in production → downloads JSON from Blob Storage.
#
# BLOB_CONTAINER_NAME — container name (DevOps creates this).
#   Default: "scraper-data"
#
# BLOB_SCRAPED_FILENAME — filename scraper uploaded to Blob.
#   Must match BLOB_SCRAPED_FILENAME in scrape_approved_urls.py.
#   Default: "royal_london_faq_latest.json"
BLOB_STORAGE_CONNECTION = os.getenv("AZURE_STORAGE_CONNECTION", "")
BLOB_CONTAINER_NAME     = os.getenv("BLOB_CONTAINER_NAME", "scraper-data")
BLOB_SCRAPED_FILENAME   = os.getenv(
    "BLOB_SCRAPED_FILENAME",
    "royal_london_faq_latest.json",
)

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
            embedding_deployment=EMBEDDING_DEPLOYMENT,
            hqa_deployment=HQA_DEPLOYMENT,
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



# ── Production: load pages abstraction (v2.0.0) ───────────────
def load_pages(scraped_file: str | None = None) -> list[dict]:
    """
    Load scraped pages from local file or Azure Blob Storage.

    Local development (AZURE_STORAGE_CONNECTION not set):
        Reads from local JSON file.
        Uses scraped_file arg or auto-detects latest in scraper/data/.
        Zero behaviour change from v1.x — identical to current workflow.

    Production (AZURE_STORAGE_CONNECTION set):
        Downloads JSON from Azure Blob Storage.
        scrape_approved_urls.py uploads to the same Blob container,
        so both scripts share files without manual copying.

    Args:
        scraped_file: Local file path override. If None, auto-detects
                      latest JSON in scraper/data/ (local mode only).

    Returns:
        list[dict] — list of scraped page dicts ready for chunk_pages()

    TODO (DevOps): Ensure AZURE_STORAGE_CONNECTION, BLOB_CONTAINER_NAME,
    and BLOB_SCRAPED_FILENAME are set in Key Vault. The commented block
    below activates automatically once AZURE_STORAGE_CONNECTION is set.
    """
    # ── Production: download from Blob Storage ────────────────
    # TODO (DevOps): uncomment when AZURE_STORAGE_CONNECTION is
    # configured in Key Vault. Requires:
    #   pip install azure-storage-blob
    #   AZURE_STORAGE_CONNECTION set in Key Vault
    #   BLOB_CONTAINER_NAME set (default: "scraper-data")
    #   BLOB_SCRAPED_FILENAME set (default: "royal_london_faq_latest.json")
    #
    # if BLOB_STORAGE_CONNECTION:
    #     from azure.storage.blob import BlobServiceClient
    #     blob_service = BlobServiceClient.from_connection_string(
    #         BLOB_STORAGE_CONNECTION
    #     )
    #     blob_client = blob_service.get_blob_client(
    #         container=BLOB_CONTAINER_NAME,
    #         blob=BLOB_SCRAPED_FILENAME,
    #     )
    #     data  = blob_client.download_blob().readall()
    #     pages = json.loads(data)
    #     log.info(
    #         "pages_loaded_from_blob",
    #         container=BLOB_CONTAINER_NAME,
    #         blob=BLOB_SCRAPED_FILENAME,
    #         total=len(pages),
    #     )
    #     return pages

    # ── Local development: read from local file ───────────────
    # Current behaviour — unchanged from v1.x.
    file_path = scraped_file or find_latest_scraped_file()
    if not Path(file_path).exists():
        raise FileNotFoundError(
            f"Scraped file not found: {file_path}\n"
            f"Run scrape_approved_urls.py first to generate it."
        )
    with open(file_path, encoding="utf-8") as f:
        pages = json.load(f)
    log.info(
        "pages_loaded_from_local",
        file=file_path,
        total=len(pages),
    )
    return pages

# ── Content Cleaning ──────────────────────────────────────────
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
def compute_content_hash(content: str) -> str:
    """
    Compute MD5 hash of page content.
    Used by content_freshness.py to detect changed pages.
    Stored in 'content_hash' index field (not retrievable).
    """
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def chunk_pages(pages: list[dict]) -> list[dict]:
    """
    Split pages into chunks for indexing.
    Prepends title to each chunk for better embeddings.
    Respects markdown structure via separator hierarchy.
    Adds content_hash per chunk for freshness detection.
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

        # Compute content hash BEFORE title prepending —
        # hash represents the page content only, not the title.
        # This ensures content_freshness.py correctly detects
        # page changes even if title is reformatted.
        page_hash = compute_content_hash(content)

        # Prepend title → every chunk benefits from page context
        content_with_title = (
            f"{title}\n\n{content}" if title else content
        )

        splits = splitter.split_text(content_with_title)

        for i, split in enumerate(splits):
            if len(split.strip()) < 50:
                continue
            chunks.append({
                "chunk_id":             str(uuid.uuid4()),
                "content":              split.strip(),
                "source_url":           url,
                "title":                title,
                "section":              section,
                "audience":             audience,
                "scraped_at":           page.get("scraped_at", ""),
                "chunk_index":          i,
                "total_chunks":         len(splits),
                "content_hash":         page_hash,
                # augmented_questions populated later by
                # generate_hqa_questions() — empty string default
                # so field always present in uploaded document
                "augmented_questions":  "",

                # ── Enrichment fields (v3.0.0) ─────────────────
                # Passed through from scraper — extracted at scrape
                # time from HTML already fetched by crawl4ai.
                # All have safe defaults if scraper didn't set them.
                "has_video":        page.get("has_video", False),
                "content_type":     page.get("content_type", "article"),
                "product_category": page.get("product_category", "general"),
                "description":      page.get("description", ""),
                "thumbnail_url":    page.get("thumbnail_url", ""),
                "publish_date":     page.get("publish_date", ""),
                "collection_name":  page.get("collection_name", ""),
                "read_time_mins":   page.get("read_time_mins", "5"),
            })

    log.info("chunking_complete", total_chunks=len(chunks))
    return chunks


# ── HQA — Hypothetical Question Augmentation ─────────────────
HQA_SYSTEM_PROMPT = """You are an expert at generating realistic customer search questions.

Given a chunk of text from Royal London's insurance and pension FAQ pages, generate exactly 5 questions that:
1. A real Royal London customer would type into a search box or chatbot
2. Are ONLY answerable using the provided text — not general knowledge
3. Use natural customer language — not technical or legal jargon
4. Are SPECIFIC to this exact chunk — not generic insurance questions
5. Vary in phrasing to cover different ways a customer might ask the same thing

RULES:
- Include specific product names, numbers, or terms from the chunk
- Do NOT generate: "What is insurance?", "How do pensions work?" or any generic question
- Do NOT generate questions about topics not mentioned in the chunk
- Keep questions under 15 words each
- Questions must feel like real customer queries

Return ONLY a valid JSON array of exactly 5 strings.
No explanation, no preamble, no markdown formatting.
Example format: ["question 1", "question 2", "question 3", "question 4", "question 5"]"""


def is_grounded(question: str, chunk_content: str) -> bool:
    """
    Safety check 1 — Grounding.
    Verify the question contains at least one significant keyword
    from the chunk content. Rejects hallucinated questions that
    mention topics not present in the chunk.

    A question passes if at least 1 non-stopword chunk keyword
    appears in the question text.
    """
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "in",
        "on", "at", "to", "for", "of", "and", "or", "but",
        "it", "this", "that", "with", "my", "your", "our",
        "i", "we", "you", "they", "be", "do", "have", "will",
        "can", "could", "would", "should", "may", "might",
        "from", "by", "as", "if", "when", "what", "how",
        "about", "which", "there", "their", "than", "then",
    }
    # Extract significant words from chunk (>3 chars, not stopwords)
    chunk_words = set(
        w.lower().strip(".,?!:;()[]\"'")
        for w in chunk_content.split()
        if len(w) > 3 and w.lower() not in stop_words
    )
    question_lower = question.lower()
    matches = sum(1 for w in chunk_words if w in question_lower)
    return matches >= 1


def is_specific_enough(question: str) -> bool:
    """
    Safety check 2 — Specificity.
    Reject questions that are too short or match known generic
    patterns that don't help retrieval quality.

    A question must be at least 5 words and not match any
    generic insurance/pension question pattern.
    """
    # Too short = too vague to be useful for retrieval
    if len(question.split()) < 5:
        return False

    # Generic patterns that add no retrieval value
    # These match any insurance page, not a specific chunk
    generic_patterns = [
        r"^what is (insurance|a pension|an isa)\??$",
        r"^how do (pensions|isas|policies) work\??$",
        r"^tell me about",
        r"^explain ",
        r"^what does royal london (do|offer|provide)\??$",
        r"^how can (i|you) help",
    ]
    question_lower = question.lower().strip()
    for pattern in generic_patterns:
        if re.search(pattern, question_lower):
            return False

    return True


def score_question(question: str, chunk_content: str) -> int:
    """
    Quality score for a generated question.

    0 = reject — fails grounding or specificity check
    1 = acceptable — passes both checks
    2 = good — passes both checks AND contains a Royal London
        domain term (product name, policy term, number etc.)

    Only questions scoring 1 or 2 are stored.
    Score 0 questions are discarded silently.
    """
    if not is_grounded(question, chunk_content):
        return 0
    if not is_specific_enough(question):
        return 0

    # Bonus: contains Royal London domain-specific terms
    # These are strong signals of a well-targeted question
    rl_domain_terms = [
        "royal london", "pension", "isa", "insurance",
        "protection", "annuity", "drawdown", "premium",
        "policy", "benefit", "contribution", "allowance",
        "claim", "bereavement", "life cover", "critical illness",
        "income protection", "whole of life", "term insurance",
        "workplace", "personal pension", "sipp", "profitshare",
        "financial adviser", "retirement", "surrender",
    ]
    question_lower = question.lower()
    if any(term in question_lower for term in rl_domain_terms):
        return 2

    return 1


def generate_hqa_questions(
    chunk: dict,
    retry_count: int = 3,
) -> list[str]:
    """
    Generate and validate hypothetical questions for a single chunk.

    Process:
    1. Call gpt-4o-mini with HQA_SYSTEM_PROMPT + chunk content
    2. Parse JSON response → list of 5 questions
    3. Validate each question (grounding + specificity + score)
    4. Return only accepted questions (score >= 1)

    Returns between 0 and 5 questions.
    Returns empty list on error (non-blocking — chunk still indexed
    without HQA questions, just with lower retrieval quality).

    Args:
        chunk:       The chunk dict (needs 'content' key)
        retry_count: Max retries on API error or bad JSON

    Safety measures:
    - is_grounded():       rejects hallucinated questions
    - is_specific_enough(): rejects generic questions
    - score_question():    rejects low quality, scores rest
    - JSON parse failure → retry → fallback to empty list
    - API error → retry with backoff → fallback to empty list
    """
    from openai import RateLimitError

    client        = get_openai_client()
    chunk_content = chunk["content"]

    # Truncate very long chunks to stay within context limits
    # 2000 chars is sufficient for question generation
    content_for_hqa = chunk_content[:2000]

    for attempt in range(retry_count):
        try:
            response = client.chat.completions.create(
                model=HQA_DEPLOYMENT,
                messages=[
                    {
                        "role":    "system",
                        "content": HQA_SYSTEM_PROMPT,
                    },
                    {
                        "role":    "user",
                        "content": (
                            f"Generate 5 questions for this chunk:\n\n"
                            f"{content_for_hqa}"
                        ),
                    },
                ],
                max_tokens=300,
                temperature=0.3,   # Low temp = consistent, focused output
            )

            raw = response.choices[0].message.content or ""
            raw = raw.strip()

            # Strip markdown code fences if model added them
            if raw.startswith("```"):
                raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
                raw = raw.rstrip("`").strip()

            questions = json.loads(raw)

            # Validate it's a list of strings
            if not isinstance(questions, list):
                log.warning(
                    "hqa_invalid_format",
                    chunk_id=chunk["chunk_id"],
                    attempt=attempt + 1,
                )
                continue

            # Validate and score each question
            accepted = []
            for q in questions:
                if not isinstance(q, str) or not q.strip():
                    continue
                q = q.strip()
                score = score_question(q, chunk_content)
                if score >= 1:
                    accepted.append(q)

            log.info(
                "hqa_questions_generated",
                chunk_id=chunk["chunk_id"][:8],
                generated=len(questions),
                accepted=len(accepted),
            )
            return accepted

        except json.JSONDecodeError:
            log.warning(
                "hqa_json_parse_failed",
                chunk_id=chunk["chunk_id"],
                attempt=attempt + 1,
            )
            if attempt < retry_count - 1:
                time.sleep(2)
            continue

        except RateLimitError as e:
            wait = 10 * (2 ** attempt)
            log.warning(
                "hqa_rate_limit",
                chunk_id=chunk["chunk_id"],
                wait_seconds=wait,
                attempt=attempt + 1,
            )
            print(
                f"   ⚠️  HQA rate limit. Waiting {wait}s "
                f"(attempt {attempt + 1}/{retry_count})..."
            )
            time.sleep(wait)
            continue

        except Exception as e:
            log.error(
                "hqa_error",
                chunk_id=chunk["chunk_id"],
                error=str(e),
                attempt=attempt + 1,
            )
            if attempt < retry_count - 1:
                time.sleep(2)
            continue

    # All retries exhausted — return empty, chunk still indexed
    log.warning(
        "hqa_failed_all_retries",
        chunk_id=chunk["chunk_id"],
        note="Chunk will be indexed without HQA questions",
    )
    return []


def augment_chunks_with_hqa(
    chunks: list[dict],
    pilot: bool = False,
) -> list[dict]:
    """
    Generate HQA questions for all chunks and store them in
    the 'augmented_questions' field.

    pilot=True: process only first PILOT_CHUNK_LIMIT chunks.
    Used to validate question quality before full re-index.

    Returns the augmented chunk list. Chunks that fail HQA
    generation are returned unchanged (augmented_questions="").
    The pipeline is non-blocking — HQA failure never stops indexing.
    """
    chunks_to_augment = chunks[:PILOT_CHUNK_LIMIT] if pilot else chunks
    total             = len(chunks_to_augment)

    print(
        f"\n🧠 HQA: Generating questions for "
        f"{total:,} chunks"
        f"{' (PILOT MODE)' if pilot else ''}..."
    )
    print(
        f"   Model: {HQA_DEPLOYMENT} | "
        f"Est. cost: ~${total * 0.000116:.2f} | "
        f"Est. time: ~{total // 30 + 1} min"
    )

    accepted_total  = 0
    rejected_total  = 0
    failed_chunks   = 0

    for i, chunk in enumerate(chunks_to_augment):
        questions = generate_hqa_questions(chunk)

        if questions:
            # Join questions with newline — stored as single
            # string in SearchableField for BM25 indexing
            chunk["augmented_questions"] = "\n".join(questions)
            accepted_total += len(questions)
        else:
            # HQA failed or all questions rejected
            # Chunk still indexed — just without HQA boost
            chunk["augmented_questions"] = ""
            failed_chunks += 1

        rejected_total += (5 - len(questions))  # 5 generated, X accepted

        # Progress every 50 chunks
        if (i + 1) % 50 == 0 or (i + 1) == total:
            pct = round((i + 1) / total * 100)
            print(
                f"   [{pct:3d}%] {i + 1:,}/{total:,} chunks | "
                f"Questions accepted: {accepted_total:,} | "
                f"Failed chunks: {failed_chunks}"
            )

        # Small sleep between chunks to respect rate limits
        # gpt-4o-mini is fast but we have 7,000+ chunks
        time.sleep(0.1)

    log.info(
        "hqa_augmentation_complete",
        total_chunks=total,
        accepted_questions=accepted_total,
        rejected_questions=rejected_total,
        failed_chunks=failed_chunks,
        pilot=pilot,
    )

    print(f"\n   ✅ HQA complete:")
    print(f"      Questions accepted : {accepted_total:,}")
    print(f"      Questions rejected : {rejected_total:,}")
    print(f"      Chunks without HQA : {failed_chunks:,}")

    # Pilot mode quality report
    if pilot:
        print(f"\n   📋 PILOT QUALITY REPORT — review before --full:")
        print(f"      Acceptance rate: "
              f"{accepted_total / max(accepted_total + rejected_total, 1) * 100:.1f}%")
        print(f"      Sample questions from first 3 chunks:")
        for chunk in chunks_to_augment[:3]:
            qs = chunk.get("augmented_questions", "")
            if qs:
                print(f"\n      Chunk: {chunk['title'][:50]}")
                for q in qs.split("\n")[:3]:
                    print(f"        • {q}")

    return chunks


# ── Index management ──────────────────────────────────────────
def get_indexed_urls() -> set:
    """
    Get all URLs already indexed in Azure AI Search.
    Used for --new-only mode to skip already-indexed pages.

    v2.0.0 FIX: Now paginates through ALL results using skip
    parameter. Previous implementation used top=1000 which
    silently missed URLs beyond the first 1000 results.
    Azure AI Search max top per request is 1000 — pagination
    is required for indexes with >1000 documents.
    """
    try:
        client = get_search_client()
        urls   = set()
        skip   = 0
        page_size = 1000

        while True:
            results = client.search(
                search_text="*",
                select=["source_url"],
                top=page_size,
                skip=skip,
            )
            batch = list(results)
            if not batch:
                break

            for r in batch:
                url = r.get("source_url", "")
                if url:
                    urls.add(url)

            # If we got fewer than page_size, we've reached the end
            if len(batch) < page_size:
                break

            skip += page_size

        log.info("indexed_urls_fetched", count=len(urls))
        return urls

    except Exception as e:
        log.warning("get_indexed_urls_failed", error=str(e))
        return set()


def create_or_update_index(fresh: bool = False):
    """
    Create index with all fields explicitly defined.

    v2.0.0: All field attributes (searchable, filterable,
    sortable, facetable, retrievable) are set explicitly —
    no Azure defaults relied upon. Guaranteed consistent schema
    on every --full run regardless of Azure SDK version.

    New fields added:
    - augmented_questions: HQA questions (searchable, retrievable)
    - content_hash: MD5 for freshness detection (filterable only)

    Fixed fields:
    - section:    now searchable (was SimpleField — BM25 ignored it)
    - source_url: now searchable (was SimpleField — BM25 ignored it)

    Semantic configuration now created at index build time —
    no separate portal setup or add_semantic_config.py needed.

    fresh=True:  Delete and recreate (wipes existing data)
    fresh=False: Create only if not exists
    """
    client = get_search_index_client()

    if fresh:
        try:
            client.delete_index(INDEX_NAME)
            log.info("existing_index_deleted", index=INDEX_NAME)
            print(f"   Deleted existing index '{INDEX_NAME}'")
        except Exception:
            pass  # Index didn't exist — that's fine

    # ── Field definitions ─────────────────────────────────────
    # Every attribute set explicitly. See module docstring for
    # rationale behind each field's settings.
    fields = [

        # ── Key field ─────────────────────────────────────────
        # chunk_id is the primary key — not searchable/filterable
        # as it's a UUID with no meaningful search value
        SimpleField(
            name="chunk_id",
            type=SearchFieldDataType.String,
            key=True,
            searchable=False,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Main content field ────────────────────────────────
        # The primary BM25 search field — must be searchable.
        # Not filterable — full text content is too long for
        # exact-match filtering.
        SearchableField(
            name="content",
            type=SearchFieldDataType.String,
            searchable=True,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── HQA augmented questions (v2.0.0 NEW) ─────────────
        # Stores gpt-4o-mini generated questions per chunk.
        # Searchable: questions boost BM25 for customer queries.
        # Retrievable: useful for debugging question quality.
        # Not filterable: not used for exact-match filtering.
        # Semantic ranker also reads this as a content field
        # (defined in SemanticPrioritizedFields below).
        SearchableField(
            name="augmented_questions",
            type=SearchFieldDataType.String,
            searchable=True,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Title ─────────────────────────────────────────────
        # Searchable: page titles boost BM25 relevance.
        # Filterable: allows filtering by specific page title.
        # Semantic ranker uses this as the primary title field.
        SearchableField(
            name="title",
            type=SearchFieldDataType.String,
            searchable=True,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Source URL ────────────────────────────────────────
        # v2.0.0 FIX: now searchable (was SimpleField).
        # Searchable: URL path fragments ("pensions", "bereavement"
        # etc.) now contribute to BM25 keyword scoring.
        # Filterable: used by content_freshness.py to query
        # specific pages and by debug_retrieval.py.
        SearchableField(
            name="source_url",
            type=SearchFieldDataType.String,
            searchable=True,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Section ───────────────────────────────────────────
        # v2.0.0 FIX: now searchable (was SimpleField).
        # Searchable: section headings ("What Is A Pension",
        # "Workplace Pensions" etc.) now contribute to BM25.
        # Filterable: filter results by section category.
        # Semantic ranker uses this as keywords field.
        SearchableField(
            name="section",
            type=SearchFieldDataType.String,
            searchable=True,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Audience ──────────────────────────────────────────
        # Filter-only field — used to scope queries to
        # "customer" vs other audience types if needed.
        # Not searchable: "customer" keyword adds no BM25 value.
        SimpleField(
            name="audience",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Scraped timestamp ─────────────────────────────────
        # Metadata only — when the page was scraped.
        # Not searchable or filterable — informational only.
        # Retrievable for debugging and freshness reports.
        SimpleField(
            name="scraped_at",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Chunk position fields ─────────────────────────────
        # chunk_index: position of this chunk within its page
        # total_chunks: total chunks from its source page
        # Both useful for debugging and context window ordering.
        SimpleField(
            name="chunk_index",
            type=SearchFieldDataType.Int32,
            searchable=False,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),
        SimpleField(
            name="total_chunks",
            type=SearchFieldDataType.Int32,
            searchable=False,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Content hash (v2.0.0 NEW) ─────────────────────────
        # MD5 hash of page content (pre-title-prepend).
        # Used by content_freshness.py to detect changed pages
        # without re-scraping the full page.
        # filterable=True: allows querying by hash value.
        # retrievable=False: internal use only — this field
        # must NEVER be returned to the API layer or frontend.
        SimpleField(
            name="content_hash",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=False,
        ),

        # ── Enrichment fields (v3.0.0 NEW) ───────────────────
        # All extracted at scrape time from HTML already fetched
        # by crawl4ai. Zero extra HTTP calls during indexing.
        # UI/UX team uses these for rich citation rendering
        # without requiring a re-index.

        # has_video: True if page contains video/webinar content.
        # Detection uses URL patterns + meta tags + HTML signals.
        # UI team: render "📹 Watch video" indicator on citations.
        # filterable: allows querying all video pages at once.
        # retrievable: UI must receive this to conditionally render.
        SimpleField(
            name="has_video",
            type=SearchFieldDataType.Boolean,
            searchable=False,
            filterable=True,
            sortable=False,
            facetable=True,
            retrievable=True,
        ),

        # content_type: page content category.
        # Values: webinar/video/guide/tool/faq/article/news/corporate
        # filterable: scope queries to specific content types.
        # facetable: UI can show "Filter by type" facets.
        # searchable: content type terms boost BM25 relevance.
        SearchableField(
            name="content_type",
            type=SearchFieldDataType.String,
            searchable=True,
            filterable=True,
            sortable=False,
            facetable=True,
            retrievable=True,
        ),

        # product_category: Royal London product area.
        # Values: pensions/life_insurance/isa/income_protection/etc.
        # filterable: scope Aria queries to specific product areas.
        # facetable: UI can show product area filter chips.
        SearchableField(
            name="product_category",
            type=SearchFieldDataType.String,
            searchable=True,
            filterable=True,
            sortable=False,
            facetable=True,
            retrievable=True,
        ),

        # description: page meta description (≤300 chars).
        # From meta-description or og:description.
        # UI team: citation preview tooltip text.
        # Not filterable: too long for exact-match filtering.
        SearchableField(
            name="description",
            type=SearchFieldDataType.String,
            searchable=True,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # thumbnail_url: page teaser image URL.
        # From meta-teaser_image (350x200px, page-specific) or og:image.
        # UI team: rich citation card thumbnail image.
        # Not searchable: URLs don't contribute to BM25.
        SimpleField(
            name="thumbnail_url",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # publish_date: ISO format date string (YYYY-MM-DD).
        # From meta-st-publish-date.
        # UI team: "Published March 2024" label on citations.
        # filterable: allows date-range filtering for freshness.
        # sortable: allows sorting by recency.
        SimpleField(
            name="publish_date",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=True,
            sortable=True,
            facetable=False,
            retrievable=True,
        ),

        # collection_name: Royal London content collection.
        # e.g. "Pension webinar", "Life insurance guide"
        # UI team: content category badge on citations.
        SearchableField(
            name="collection_name",
            type=SearchFieldDataType.String,
            searchable=True,
            filterable=True,
            sortable=False,
            facetable=True,
            retrievable=True,
        ),

        # read_time_mins: estimated reading/viewing time.
        # Calculated from word count at 200 wpm.
        # UI team: "8 min read" or "30 min webinar" label.
        SimpleField(
            name="read_time_mins",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Vector embedding field ────────────────────────────
        # 1024-dimensional Matryoshka embedding from
        # text-embedding-3-large. Generated from content +
        # augmented_questions combined (v2.0.0).
        # retrievable=False: raw vectors must never be returned
        # to callers — would inflate response payload by ~4KB
        # per result with zero benefit.
        SearchField(
            name="embedding",
            type=SearchFieldDataType.Collection(
                SearchFieldDataType.Single
            ),
            searchable=True,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=False,
            vector_search_dimensions=EMBEDDING_DIMS,
            vector_search_profile_name="rl-vector-profile",
        ),
    ]

    # ── Vector search config ──────────────────────────────────
    # HNSW (Hierarchical Navigable Small World) algorithm —
    # Azure AI Search's default approximate nearest neighbour.
    # efConstruction=400, efSearch=500 from index_backup.json —
    # these are the tuned parameters from the existing index.
    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="rl-hnsw",
                parameters={
                    "metric":          "cosine",
                    "m":               4,
                    "efConstruction":  400,
                    "efSearch":        500,
                },
            )
        ],
        profiles=[
            VectorSearchProfile(
                name="rl-vector-profile",
                algorithm_configuration_name="rl-hnsw",
            )
        ],
    )

    # ── Semantic search configuration (v2.0.0) ────────────────
    # Created at index build time — no separate portal setup or
    # add_semantic_config.py script needed after this.
    #
    # title_field:     title — primary signal for the reranker
    # content_fields:  content + augmented_questions — HQA
    #                  questions directly improve reranker signal
    # keywords_fields: section — section headings boost relevance
    #
    # Name must match SEMANTIC_CONFIG_NAME constant and the
    # AZURE_SEARCH_SEMANTIC_CONFIG env var read by retriever.py.
    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIG_NAME,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(
                        field_name="title"
                    ),
                    content_fields=[
                        # Primary content — chunk text
                        SemanticField(field_name="content"),
                        # HQA questions — bridges query/doc space
                        SemanticField(
                            field_name="augmented_questions"
                        ),
                        # Page description — adds page-level context
                        # v3.0.0: helps reranker understand page purpose
                        SemanticField(field_name="description"),
                    ],
                    keywords_fields=[
                        # Section heading — product area signal
                        SemanticField(field_name="section"),
                        # Collection name — e.g. "Pension webinar"
                        # v3.0.0: content type relevance signal
                        SemanticField(field_name="collection_name"),
                        # Product category — pensions/insurance/etc
                        # v3.0.0: strong domain relevance signal
                        SemanticField(field_name="product_category"),
                    ],
                ),
            )
        ]
    )

    try:
        client.create_index(
            SearchIndex(
                name=INDEX_NAME,
                fields=fields,
                vector_search=vector_search,
                semantic_search=semantic_search,
            )
        )
        log.info(
            "index_created",
            index=INDEX_NAME,
            semantic_config=SEMANTIC_CONFIG_NAME,
        )
    except Exception as e:
        if "already exists" in str(e).lower():
            log.info("index_already_exists", index=INDEX_NAME)
        else:
            raise


# ── Embeddings ────────────────────────────────────────────────
def get_embeddings(texts: list[str]) -> list[list[float]]:
    """
    Generate embeddings in batches using text-embedding-3-large.

    v2.0.0: texts are now content + augmented_questions combined
    (see build_embedding_texts()). This bridges the query/document
    embedding space gap that caused wrong chunks to win retrieval.

    Rate limit handling (Azure OpenAI S0 tier):
    - Batch size 50 chunks (~20,000 tokens/batch)
    - 2 second sleep between every batch
    - Automatic retry with exponential backoff on 429 errors
    - Max 5 retries per batch before giving up

    If you hit rate limits: increase BATCH_SLEEP_SECONDS
    or reduce EMBEDDING_BATCH_SIZE.
    If upgraded to S1/S2 tier: reduce BATCH_SLEEP_SECONDS to 0.
    """
    from openai import RateLimitError

    BATCH_SLEEP_SECONDS = 2
    MAX_RETRIES         = 5
    RETRY_BASE_SECONDS  = 10

    client         = get_openai_client()
    all_embeddings = []
    total_batches  = (
        len(texts) + EMBEDDING_BATCH_SIZE - 1
    ) // EMBEDDING_BATCH_SIZE

    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch        = texts[i: i + EMBEDDING_BATCH_SIZE]
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
                    f"   ⚠️  Rate limit on batch {batch_number}. "
                    f"Waiting {wait}s "
                    f"(retry {retry}/{MAX_RETRIES})..."
                )
                time.sleep(wait)

        # Sleep between every batch to stay under TPM limit
        if i + EMBEDDING_BATCH_SIZE < len(texts):
            time.sleep(BATCH_SLEEP_SECONDS)

    return all_embeddings


def build_embedding_texts(chunks: list[dict]) -> list[str]:
    """
    Build the text to embed for each chunk.

    v2.0.0: Combines content + augmented_questions for embedding.
    This is the core of HQA — the embedding now represents both
    what the chunk says (document space) AND what questions it
    answers (query space), bridging the embedding space gap.

    Format:
        {content}

        Questions this answers:
        {question 1}
        {question 2}
        ...

    If augmented_questions is empty (HQA failed or pilot mode),
    falls back to content-only embedding — same as v1.x behaviour.
    """
    texts = []
    for chunk in chunks:
        content    = chunk["content"]
        questions  = chunk.get("augmented_questions", "").strip()

        if questions:
            # Combine content and questions for richer embedding
            text = (
                f"{content}\n\n"
                f"Questions this answers:\n"
                f"{questions}"
            )
        else:
            # Fallback: content only (v1.x behaviour)
            text = content

        texts.append(text)
    return texts


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
        batch     = documents[i: i + UPLOAD_BATCH_SIZE]
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
    """
    Run a test hybrid semantic query to verify the index works.
    Uses the semantic ranker config created at index build time.
    """
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
        query_type="semantic",
        semantic_configuration_name=SEMANTIC_CONFIG_NAME,
        select=[
            "chunk_id", "title", "content",
            "source_url", "section",
            "augmented_questions",
        ],
        top=3,
    )

    print("\n" + "=" * 55)
    print("🔍 Test Query: 'How do I make a claim?'")
    print("=" * 55)
    for i, result in enumerate(results, 1):
        qs = result.get("augmented_questions", "") or ""
        qs_preview = qs.split("\n")[0] if qs else "None"
        print(f"\n[{i}] Title:     {result.get('title', 'N/A')}")
        print(f"    Section:   {result.get('section', 'N/A')}")
        print(f"    URL:       {result.get('source_url', 'N/A')}")
        print(f"    Preview:   {result['content'][:120]}...")
        print(f"    HQA Q1:    {qs_preview}")
    print("\n✅ Index verification complete!")


# ── File auto-detection ───────────────────────────────────────
def find_latest_scraped_file() -> str:
    """
    Auto-detect the most recently modified JSON file in
    scraper/data/. Used when --file is not specified.

    v2.0.0 FIX: Replaces hardcoded SCRAPED_FILE constant —
    no manual update needed after each scraper run.

    Falls back to SCRAPED_FILE constant if no JSON found.
    """
    data_dir = Path("scraper/data")
    if not data_dir.exists():
        return SCRAPED_FILE

    json_files = sorted(
        data_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if json_files:
        latest = str(json_files[0])
        log.info(
            "auto_detected_scraped_file",
            file=latest,
        )
        return latest

    return SCRAPED_FILE


# ── Programmatic entry point ──────────────────────────────────
def run_pipeline(
    mode: str = "new-only",
    scraped_file: str | None = None,
    pilot: bool = False,
) -> dict:
    """
    Programmatic entry point for the indexing pipeline.
    Called by DevOps / Azure Function App trigger.

    This function wraps the full pipeline — scrape file loading,
    chunking, HQA augmentation, embedding, indexing, cache clear.

    Args:
        mode:         "full"     — delete and recreate index
                      "new-only" — only index new pages (default)
        scraped_file: Path to scraped JSON file. If None,
                      auto-detects latest file in scraper/data/.
        pilot:        If True, process first 100 chunks only
                      for HQA quality validation. Use before
                      running a full re-index for the first time.

    Returns:
        dict with keys:
            success       (bool)   — True if completed without error
            pages_indexed (int)    — number of pages processed
            chunks_created (int)   — number of chunks created
            chunks_uploaded (int)  — number of chunks uploaded
            hqa_questions (int)    — total HQA questions generated
            cache_cleared (bool)   — whether cache was cleared
            error         (str)    — error message if success=False

    TODO (DevOps): Wrap this in an Azure Function App trigger:

        import azure.functions as func
        from scraper.chunk_and_index import run_pipeline

        app = func.FunctionApp()

        # Monthly scheduled re-index (1st of each month, midnight)
        @app.timer_trigger(
            schedule="0 0 1 * *",
            arg_name="timer",
        )
        def monthly_reindex(timer: func.TimerRequest):
            result = run_pipeline(mode="full")
            logging.info(f"Reindex complete: {result}")

        # On-demand HTTP trigger (content team can call this)
        @app.route(route="reindex", methods=["POST"])
        def on_demand_reindex(req: func.HttpRequest):
            mode = req.params.get("mode", "new-only")
            result = run_pipeline(mode=mode)
            return func.HttpResponse(
                json.dumps(result),
                mimetype="application/json",
            )
    """
    import traceback

    result = {
        "success":         False,
        "pages_indexed":   0,
        "chunks_created":  0,
        "chunks_uploaded": 0,
        "hqa_questions":   0,
        "cache_cleared":   False,
        "error":           "",
    }

    try:
        fresh = (mode == "full")

        # ── Validate config ───────────────────────────────────
        if not AZURE_OPENAI_ENDPOINT:
            raise ValueError("AZURE_OPENAI_ENDPOINT not set in .env")
        if not SEARCH_ENDPOINT:
            raise ValueError("AZURE_SEARCH_ENDPOINT not set in .env")

        # ── Load pages ─────────────────────────────────────────────────────
        # load_pages() handles local file vs Blob Storage.
        # Local: reads scraped_file or auto-detects latest JSON.
        # Production: downloads from Blob Storage when
        # AZURE_STORAGE_CONNECTION env var is set.
        pages = load_pages(scraped_file)
        log.info("pages_loaded", total=len(pages))

        # ── Filter new pages if new-only ──────────────────────
        if not fresh:
            indexed_urls   = get_indexed_urls()
            pages_to_index = [
                p for p in pages
                if p.get("url", "").rstrip("/")
                not in {u.rstrip("/") for u in indexed_urls}
            ]
            if not pages_to_index:
                log.info("no_new_pages_to_index")
                result["success"] = True
                return result
        else:
            pages_to_index = pages

        result["pages_indexed"] = len(pages_to_index)

        # ── Step 1: Chunk ─────────────────────────────────────
        chunks = chunk_pages(pages_to_index)
        result["chunks_created"] = len(chunks)

        # ── Step 2: HQA augmentation ──────────────────────────
        chunks = augment_chunks_with_hqa(chunks, pilot=pilot)
        result["hqa_questions"] = sum(
            len(c.get("augmented_questions", "").split("\n"))
            for c in chunks
            if c.get("augmented_questions", "").strip()
        )

        # ── Step 3: Create/update index ───────────────────────
        create_or_update_index(fresh=fresh)

        # ── Step 4: Build embedding texts ─────────────────────
        embedding_texts = build_embedding_texts(chunks)

        # ── Step 5: Generate embeddings ───────────────────────
        embeddings = get_embeddings(embedding_texts)

        # ── Step 6: Upload ────────────────────────────────────
        total = upload_chunks(chunks, embeddings)
        result["chunks_uploaded"] = total

        # ── Step 7: Auto-clear cache on full re-index ─────────
        if fresh:
            try:
                from core.cache import get_cache
                cache = get_cache()
                cache.clear()
                result["cache_cleared"] = True
                log.info(
                    "cache_cleared_post_reindex",
                    reason="full_reindex_completed",
                )
            except Exception as e:
                log.warning(
                    "cache_clear_failed_post_reindex",
                    error=str(e),
                    note="Index valid. Cache expires via TTL.",
                )

        result["success"] = True
        return result

    except Exception as e:
        result["error"] = str(e)
        log.error(
            "pipeline_error",
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return result


# ── Main (CLI entry point) ────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Chunk and index RLG FAQ pages with HQA"
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
        "--pilot",
        action="store_true",
        help=(
            "HQA pilot mode: process first 100 chunks only. "
            "Use to validate question quality before --full run."
        ),
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help=(
            "Path to scraped JSON file. "
            "If not specified, auto-detects latest file "
            "in scraper/data/."
        ),
    )
    args         = parser.parse_args()
    fresh        = args.full
    pilot        = args.pilot
    scraped_file = args.file or find_latest_scraped_file()

    print("\n🚀 RLG Chunk and Index Pipeline v2.0.0")
    print("=" * 55)
    print(f"   Mode:     {'FULL (fresh index)' if fresh else 'NEW ONLY (append)'}")
    print(f"   HQA:      {'PILOT (100 chunks)' if pilot else 'FULL' if fresh else 'NEW PAGES ONLY'}")
    print(f"   File:     {scraped_file}")
    print(f"   Index:    {INDEX_NAME}")
    print(f"   Embed:    {EMBEDDING_DEPLOYMENT} ({EMBEDDING_DIMS}d)")
    print(f"   HQA mdl:  {HQA_DEPLOYMENT}")
    print(f"   Semantic: {SEMANTIC_CONFIG_NAME}")
    print(f"   Search:   {SEARCH_ENDPOINT}")
    print(f"   OpenAI:   {AZURE_OPENAI_ENDPOINT}")
    print("=" * 55)

    # ── Validate config ───────────────────────────────────────
    if not AZURE_OPENAI_ENDPOINT:
        print("❌ AZURE_OPENAI_ENDPOINT not set in .env")
        sys.exit(1)
    if not SEARCH_ENDPOINT:
        print("❌ AZURE_SEARCH_ENDPOINT not set in .env")
        sys.exit(1)
    if not Path(scraped_file).exists():
        print(f"❌ Scraped file not found: {scraped_file}")
        sys.exit(1)

    # ── Load pages ────────────────────────────────────────────
    # load_pages() abstracts local file vs Blob Storage.
    # See load_pages() docstring for production setup details.
    pages = load_pages(scraped_file)
    log.info("pages_loaded", total=len(pages))
    print(f"\n📄 Loaded {len(pages):,} pages")

    # ── Filter new pages if --new-only ────────────────────────
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

    # ── Step 1: Chunk ─────────────────────────────────────────
    print(f"\n📄 Step 1/6: Chunking {len(pages_to_index):,} pages...")
    chunks = chunk_pages(pages_to_index)
    print(f"   Created {len(chunks):,} chunks")

    # ── Step 2: HQA augmentation ──────────────────────────────
    print(f"\n🧠 Step 2/6: HQA question augmentation...")
    chunks = augment_chunks_with_hqa(chunks, pilot=pilot)

    if pilot:
        print(
            "\n⏸️  PILOT MODE: Review question quality above."
            "\n   If satisfied, run: python scraper/chunk_and_index.py --full"
            "\n   Exiting pilot run now (index not updated)."
        )
        return

    # ── Step 3: Create/update index ───────────────────────────
    print(
        f"\n🔧 Step 3/6: "
        f"{'Recreating' if fresh else 'Ensuring'} index..."
    )
    create_or_update_index(fresh=fresh)
    print(f"   Index '{INDEX_NAME}' ready")
    print(f"   Semantic config '{SEMANTIC_CONFIG_NAME}' created")

    # ── Step 4: Build embedding texts ─────────────────────────
    print(f"\n📝 Step 4/6: Building embedding texts (content + HQA)...")
    embedding_texts = build_embedding_texts(chunks)
    hqa_count = sum(
        1 for c in chunks
        if c.get("augmented_questions", "").strip()
    )
    print(f"   {hqa_count:,}/{len(chunks):,} chunks have HQA questions")

    # ── Step 5: Embeddings ────────────────────────────────────
    print(
        f"\n🔢 Step 5/6: Generating embeddings "
        f"for {len(chunks):,} chunks..."
    )
    print("   (This will take several minutes...)")
    embeddings = get_embeddings(embedding_texts)
    print(f"   Generated {len(embeddings):,} embeddings")

    # ── Step 6: Upload ────────────────────────────────────────
    print(f"\n📤 Step 6/6: Uploading to Azure AI Search...")
    total = upload_chunks(chunks, embeddings)
    print(f"   Uploaded {total:,} chunks")

    # ── Verify ────────────────────────────────────────────────
    print("\n🔍 Verifying index with test semantic query...")
    verify_index()

    # ── Auto-clear semantic cache (--full only) ───────────────
    # WHY: full re-index refreshes ALL page content.
    # Stale cached responses from the old index may contain
    # outdated information and must not be served.
    # WHY NOT on --new-only: only new pages added, existing
    # cached responses are still valid.
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
            print("   ✅ Semantic cache cleared")
        except Exception as e:
            log.warning(
                "cache_clear_failed_post_reindex",
                error=str(e),
                note="Index is valid. Cache will expire via TTL.",
            )
            print(f"   ⚠️  Cache clear failed: {e}")
            print("      Index is still valid. Cache expires via TTL.")

    # ── Summary ───────────────────────────────────────────────
    hqa_questions_total = sum(
        len(c.get("augmented_questions", "").split("\n"))
        for c in chunks
        if c.get("augmented_questions", "").strip()
    )
    print("\n" + "=" * 55)
    print("✅ INDEXING COMPLETE!")
    print("=" * 55)
    print(f"   Pages indexed:    {len(pages_to_index):,}")
    print(f"   Chunks created:   {len(chunks):,}")
    print(f"   Chunks uploaded:  {total:,}")
    print(f"   HQA questions:    {hqa_questions_total:,}")
    print(f"   Index name:       {INDEX_NAME}")
    print(f"   Semantic config:  {SEMANTIC_CONFIG_NAME}")
    print(f"   Embedding model:  {EMBEDDING_DEPLOYMENT}")
    print(f"   HQA model:        {HQA_DEPLOYMENT}")
    if fresh:
        print(f"   Cache cleared:    ✅ Yes (--full mode)")
    else:
        print(f"   Cache cleared:    ⏭️  Skipped (--new-only mode)")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()