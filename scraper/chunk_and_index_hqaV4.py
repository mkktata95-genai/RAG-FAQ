"""
Royal London FAQ - Chunk and Index (v4 — SHA-256 hashes)
Chunks scraped content, generates HQA-augmented embeddings
plus broad entry-point title_questions, pushes to Azure AI
Search. Targets rlg-faq-index-v4 — rlg-faq-index-v3 and
rlg-faq-index-v3-baseline are untouched and remain live.

WHY v4 (not a re-run of v3):
  v3/v3-baseline were built with MD5 content hashes and
  content_hash retrievable=False. The nightly content_freshness.py
  job requires SHA-256 hashes and retrievable=True. Rather than
  wipe the live v3 index, v4 is a clean build with the correct
  schema from day one. v3 stays live as fallback until v4 is
  validated in production.

Supports:
  --full:     Delete + recreate index (fresh start)
  --new-only: Only index pages not already indexed (default)
  --pilot:    HQA pilot mode — process first 100 chunks only
              to validate question quality before full re-index
  --no-hqa:   Baseline mode — skip ALL LLM calls (no HQA
              questions, no title_questions). Uses page title
              as the only implicit retrieval signal. Targets
              rlg-faq-index-v4-baseline for clean A/B comparison
              against the full HQA index (rlg-faq-index-v4).
  --dry-run:  Validate config, load pages, chunk — but do NOT
              create/update the index or upload anything.
              Useful for testing without touching Azure Search.

Usage:
    # Full HQA run — creates rlg-faq-index-v4
    python scraper/chunk_and_index_hqaV4.py --full

    # Baseline run — no LLM calls — creates rlg-faq-index-v4-baseline
    python scraper/chunk_and_index_hqaV4.py --full --no-hqa

    # Pilot — validate HQA quality on first 100 chunks only
    python scraper/chunk_and_index_hqaV4.py --full --pilot

    # Dry run — validate config + chunking, no index changes
    python scraper/chunk_and_index_hqaV4.py --full --dry-run

    # Custom file
    python scraper/chunk_and_index_hqaV4.py --full --file path/to/file.json

Programmatic (DevOps / Container Apps Job):
    from scraper.chunk_and_index_hqaV4 import run_pipeline
    result = run_pipeline(mode="full")
    result = run_pipeline(mode="full", no_hqa=True)   # baseline

═══════════════════════════════════════════════════════════════
PRODUCTION — AZURE CONTAINER APPS JOB (DevOps)
═══════════════════════════════════════════════════════════════

# TODO (DevOps): Create Container Apps Job: aria-indexer-job
# Trigger: manual only (ADO pipeline, after aria-scraper-job completes)
#          NOT scheduled — indexer runs on demand after a full scrape
#
# ── DOCKERFILE ──────────────────────────────────────────────────
#
#   FROM python:3.11-slim
#   RUN apt-get update && apt-get install -y \
#       ca-certificates && rm -rf /var/lib/apt/lists/*
#   COPY requirements.txt .
#   RUN pip install -r requirements.txt
#   # No playwright needed — indexer uses Azure SDK + OpenAI only
#   COPY . .
#   CMD ["python", "scraper/chunk_and_index_hqaV4.py", "--full"]
#
# ── AZURE MANAGED IDENTITY ─────────────────────────────────────────
#
#   Container app must have User/System-Assigned Managed Identity
#   with these RBAC roles:
#
#   Resource                    Role
#   ─────────────────────────── ────────────────────────────────
#   Azure AI Search             Search Index Data Contributor
#   Azure OpenAI                Cognitive Services OpenAI User
#   Azure Blob Storage          Storage Blob Data Reader
#                               (reads scraper JSON output)
#   Azure Key Vault             Key Vault Secrets User
#   Azure Cache for Redis       (connection string from Key Vault)
#
#   DefaultAzureCredential picks up Managed Identity automatically
#   in Container Apps — no service principal or API keys needed.
#   TODO (DevOps): assign identity to aria-indexer-job + grant roles.
#
# ── AZURE KEY VAULT — required secrets ────────────────────────
#
#   Secret Name                              Value
#   ──────────────────────────────────────── ────────────────────
#   AZURE-SEARCH-ENDPOINT                    https://<name>.search.windows.net
#   AZURE-SEARCH-INDEX-NAME                  rlg-faq-index-v4
#   AZURE-SEARCH-BASELINE-INDEX-NAME         rlg-faq-index-v4-baseline
#   AZURE-OPENAI-ENDPOINT                    https://<name>.openai.azure.com
#   AZURE-OPENAI-EMBEDDING-DEPLOYMENT        text-embedding-3-large
#   AZURE-OPENAI-EMBEDDING-DIMENSIONS        1536
#   AZURE-OPENAI-DEPLOYMENT-HQA             gpt-4o-mini
#   AZURE-STORAGE-CONNECTION                 Blob conn string
#                                            (to read scraper JSON output)
#   BLOB-CONTAINER-NAME                      scraper-data
#   BLOB-SCRAPED-FILENAME                    royal_london_faq_latest.json
#   REDIS-URL                                rediss://<name>.redis.cache.windows.net:6380
#                                            (for cache clear after --full)
#   AZURE-SEARCH-SEMANTIC-CONFIG             rlg-semantic-config
#   CHUNK-SIZE                               1600
#   CHUNK-OVERLAP                            200
#   EMBEDDING-BATCH-SIZE                     50
#   HQA-QUESTIONS-FIRST-CHUNK               8
#   HQA-QUESTIONS-OTHER-CHUNKS              5
#   TITLE-QUESTIONS-COUNT                    3
#   TITLE-QUESTIONS-MAX-WORDS               12
#   MAX-COLLISION-THRESHOLD                  3
#
# ── CONTAINER APPS JOB trigger ───────────────────────────────────
#
#   # Manual trigger — ADO pipeline after aria-scraper-job:
#   az containerapp job start \
#       --name aria-indexer-job \
#       --resource-group <rg>
#
# ── JOB RUN ORDER (full re-index) ──────────────────────────────────
#
#   Step  Job                      What it does
#   ────  ─────────────────────  ─────────────────────────────
#   1     aria-scraper-job         Scrape → Blob JSON
#   2     aria-indexer-job         --full --no-hqa → v4-baseline (~20 min)
#   3     aria-indexer-job         --full → v4 full HQA (~3.5 hrs)
#   4     DevOps (Key Vault)       AZURE-SEARCH-INDEX-NAME → rlg-faq-index-v4
#   5     ARIA server              Restart → picks up new index from Key Vault
#   6     aria-freshness-job       --mode report → verify hashes + URLs
#   7     aria-freshness-job       Enable nightly schedule (0 2 * * *)
#   8     Monitor                  Keep v3 live for 1-2 weeks as fallback
#
# ── IMPORTANT NOTES ─────────────────────────────────────────────────
#
#   - NEVER run --full on rlg-faq-index-v3 or v3-baseline.
#     These are the live fallback indexes. Wiping them removes
#     your rollback option. v4 targets only.
#   - HQA run (~3.5 hrs) consumes ~$0.91 in OpenAI token cost.
#     Run --pilot first to validate question quality on 100 chunks
#     before committing to the full run.
#   - EMBEDDING_DIMS=1536 is configured in Key Vault. The index
#     vector field dimension MUST match this value. Mismatch causes
#     upload failure. If you need to change dims, --full re-index
#     is mandatory (dimensions are immutable in AI Search).
#   - REDIS_URL in Key Vault — indexer clears Redis cache after
#     --full run automatically. If Redis unavailable, warning is
#     logged but index is still valid (cache expires via TTL).

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

v3.1.0 — June 2026 | Mukesh Kund
         Fix: load_dotenv() and core module import path

         ROOT CAUSE:
         - load_dotenv() with no arguments loads .env from the
           CURRENT WORKING DIRECTORY. If script is run from
           scraper/ subfolder instead of project root, .env is
           not found and all os.getenv() calls silently return
           hardcoded defaults (e.g. "rlg-faq-index" instead of
           "rlg-faq-index-v2"). This caused the existing index
           to be overwritten instead of creating a new one.
         - from core.cache import get_cache failed with
           "No module named core" when script run from scraper/
           because core/ is not in sys.path in that context.

         FIX:
         - load_dotenv() replaced with find_dotenv() which walks
           UP the directory tree until it finds .env — works
           correctly regardless of which directory you run from.
         - sys.path patched before core.cache import to add
           project root — ensures core/ is always importable.

v4.0.0 — June 2026 | Mukesh Kund
         HQA collision fixes — PRIMARY TOPIC constraint +
         BLOCKED_QUESTIONS + cross-chunk deduplication

         PROBLEM (identified via find_question_duplicates.py):
         - 22.6% collision rate across 33,960 HQA questions
         - 41 critical questions in >20 chunks each
         - Root causes:
             1. HQA prompt generated cross-topic questions from
                life-events and corporate pages that mention
                multiple products in passing.
             2. Off-topic personal finance questions (energy
                bills, budgeting) generated from cost-of-living
                pages with no Royal London product relevance.

         Fix 1 — HQA_SYSTEM_PROMPT [MODIFIED]:
         - Added PRIMARY TOPIC RULE: questions must be about
           what the chunk primarily covers, not topics mentioned
           in passing from other product/service areas.
         - Added CROSS-TOPIC RULE: if chunk covers multiple
           topics, generate questions only for the one that
           takes up most of the chunk content.
         - HQA_CORPORATE_PROMPT [NEW]: separate restricted
           prompt for corporate content_type chunks — generates
           company-level questions only (mission, values,
           mutuality, social impact) not product questions.

         Fix 2 — BLOCKED_QUESTIONS [NEW]:
         - 8 off-topic personal finance questions blocked
           globally from being generated in any chunk.
         - Applied in score_question() — score forced to 0.
         - TODO (Customer): confirm list with Royal London
           brand/marketing team. Current list = sensible
           technical defaults pending business sign-off.

         Fix 3 — deduplicate_questions_across_chunks() [NEW]:
         - Post-generation pass after ALL chunks are augmented.
         - Finds questions in > MAX_COLLISION_THRESHOLD chunks.
         - Keeps question only in single most relevant chunk:
             Priority 1: dedicated product page
                         (URL not containing life-events/
                          about-us/ cost-of-living/)
             Priority 2: chunk_index == 0 (first chunk)
             Priority 3: lowest chunk_index
         - Removes from all other chunks, updates
           augmented_questions field in-place.
         - Logs before/after collision stats.

         STRATEGY:
         - Targets rlg-faq-index-v2 for A/B comparison.
         - Set AZURE_SEARCH_INDEX_NAME=rlg-faq-index-v2 in .env
         - Both indexes remain live simultaneously.
         - Switch via env var to compare retrieval quality.

v5.0.0 — July 2026 | Mukesh Kund
         title_questions field — fixes broad-query retrieval gap
         Targets rlg-faq-index-v3 (v1 and v2 untouched, stay live)

         ROOT CAUSE (identified via compare_indexes.py 4-config
         comparison on rlg-faq-index-v2):
         - "What types of pensions does Royal London offer?"
           returned only workplace pension product pages, never
           the overview page at .../pension-basics/what-is-a-pension
           that actually answers the question.
         - Volume imbalance: 4 workplace product pages x 5 HQA x
           multiple chunks each = ~100 questions all competing in
           embedding + BM25 space, vs 1 overview page x 5 HQA = 5
           questions. The 5 always lose against the 100 regardless
           of relevance.

         FIX — title_questions field:
         - New field, generated ONLY for chunk_index == 0 (the
           first / most overview-like chunk of every page).
         - 3 broad ENTRY-POINT questions per page — the question
           a customer asks BEFORE knowing product-specific detail
           ("What types of pensions does RL offer?" naturally
           belongs to the overview page's title_questions, not to
           any single workplace-product page).
         - Listed FIRST in both the scoring profile and the
           semantic config content_fields — highest-priority
           signal Azure AI Search has for a query.

         DESIGN DECISION 1 — title_questions validation is
         DELIBERATELY LIGHTER than regular HQA validation:
         - score_question() / is_specific_enough() REJECT
           patterns like "what is a pension?" / "what types of X
           does royal london offer?" — correct for regular HQA
           (those questions add no retrieval value on a specific
           product chunk) but WRONG for title_questions, whose
           entire purpose is to BE that broad question on the
           overview chunk.
         - New is_valid_title_question() reuses only: grounding
           check (is_grounded — must still relate to real page
           content) + BLOCKED_QUESTIONS check + a word-count cap
           (<=12 words, per TITLE_QUESTIONS_PROMPT). It does NOT
           run the generic-pattern rejection — that rejection
           would defeat the feature it's meant to support.

         DESIGN DECISION 2 — title_questions are included in the
         EMBEDDING TEXT, not just the BM25/semantic index field:
         - Retrieval is hybrid: BM25 + vector (HNSW) + semantic
           reranker. The original spec only added title_questions
           as a searchable/semantic field, which improves BM25
           and the semantic reranker but leaves the VECTOR half
           of hybrid search with zero benefit — a broad query is
           an embedding-space problem as much as a keyword one.
         - build_embedding_texts() now includes title_questions
           (when present) ahead of augmented_questions for
           chunk_index == 0 chunks, matching the field priority
           used in the scoring profile and semantic config.
         - Without this, the root cause (embedding-space mismatch
           between broad customer questions and specific product
           chunks) would only be half-fixed.

         HQA_DEPLOYMENT [FIXED — was silently wrong]:
         - v4.0.0 and earlier read
           os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4o-mini")
           for HQA generation. This "worked" only because
           DEPLOYMENT_FAST happened to be gpt-4o-mini at the time.
         - DEPLOYMENT_FAST has since been repointed to gpt-4o for
           the main query pipeline (FCA disclaimer consistency —
           see generator.py). Left unchanged, HQA generation for
           this v3 re-index would have silently switched to
           gpt-4o — no error, just ~13x the cost for a task
           compare_hqa_models.py already proved doesn't need it
           (97% agreement between gpt-4o-mini and gpt-4.1).
         - FIX: HQA_DEPLOYMENT now reads its own dedicated
           AZURE_OPENAI_DEPLOYMENT_HQA env var, decoupled from
           DEPLOYMENT_FAST entirely. Used for BOTH regular HQA
           question generation and title_questions generation.

         VARIABLE HQA COUNT — generate_hqa_questions() [MODIFIED]:
         - New num_questions parameter (default 5).
         - chunk_index == 0: 8 HQA questions (was 5) — the first
           chunk carries more of a page's overview content, so
           more questions further help correct the volume
           imbalance described above.
         - chunk_index > 0: 5 HQA questions (unchanged).
         - HQA_SYSTEM_PROMPT / HQA_CORPORATE_PROMPT converted to
           .format()-able templates with a {num_questions}
           placeholder — the count is no longer hardcoded to "5"
           anywhere in the prompt text or the JSON array example,
           eliminating a copy-paste risk where the prompt says
           "exactly 5" while num_questions=8 is requested.
         - Fixed a related pre-existing bug in
           augment_chunks_with_hqa(): rejected_total was computed
           as `5 - len(questions)` unconditionally — silently
           wrong the moment question counts became variable.
           Now uses the actual num_questions requested per chunk.

         CROSS-FIELD DEDUPLICATION —
         deduplicate_questions_across_chunks() [MODIFIED]:
         - Previously scanned only augmented_questions for
           cross-chunk collisions. Now scans BOTH title_questions
           and augmented_questions together, so a broad question
           generated as a title_question on the overview page
           cannot also survive as a duplicate regular HQA question
           elsewhere — the exact kind of collision this whole
           feature exists to prevent.
         - Priority ordering (_dedup_priority) is unchanged and
           naturally favours the overview page's chunk_index == 0
           slot already, since overview pages typically sit under
           dedicated URL patterns (e.g. /pension-guides/).

         SCORING PROFILE — create_or_update_index() [NEW]:
         - ScoringProfile "rl-retrieval-profile":
             title_questions=5.0, title=4.0,
             augmented_questions=2.0, section=2.0,
             collection_name=1.5, content=1.0, description=1.0
         - Not set as the index's default profile — retriever.py
           (Step 8, separate change) requests it explicitly per
           query via scoring_profile="rl-retrieval-profile", so
           behaviour is opt-in and query-type-aware (only BROAD
           queries need the title_questions boost).

         SEMANTIC CONFIG — create_or_update_index() [MODIFIED]:
         - content_fields reordered: title_questions FIRST,
           then content, augmented_questions, description.

         load_dotenv FIX:
         - v4.0.0 called find_dotenv(usecwd=False) but never
           passed override=True to load_dotenv() — a real .env
           change could still lose to a stale OS environment
           variable from an earlier shell session. Now:
           load_dotenv(find_dotenv(usecwd=False), override=True)

         INDEX_NAME default changed to "rlg-faq-index-v3".
         rlg-faq-index and rlg-faq-index-v2 are NOT touched by
         this script and remain live for comparison.

v5.1.0 — July 2026 | Mukesh Kund
         --no-hqa baseline mode + --dry-run flag

         MOTIVATION:
         Before committing to production infrastructure (Azure
         Container Apps Job, Blob Storage, ADO pipeline), we
         need to validate that HQA + title_questions actually
         improves retrieval quality over a simpler baseline.
         Without a controlled comparison, the improvement is
         an assumption not a measurement.

         --no-hqa (BASELINE MODE):
         Skips ALL LLM calls — no HQA question generation,
         no title_questions generation. The page title is the
         only implicit retrieval signal (it is already prepended
         to content before chunking, so it contributes to both
         BM25 and the embedding naturally).

         This creates a clean A/B experiment:
           rlg-faq-index-v4          — full HQA + title_questions
           rlg-faq-index-v4-baseline — title as implicit signal only

         Run compare_indexes.py against both with the same 25
         queries used for v1 vs v2 comparison. This proves
         definitively whether HQA is worth the ~3.5hr / ~$0.91
         cost before building production infrastructure around it.

         Baseline differences from full HQA run:
         - augmented_questions = "" for all chunks
         - title_questions = "" for all chunks
         - build_embedding_texts() falls back to content-only
           (title already in content via chunk_pages() prepend)
         - No LLM API calls — run completes in ~15-20 minutes
         - INDEX_NAME defaults to rlg-faq-index-v4-baseline
           (set via AZURE_SEARCH_INDEX_NAME env var to override)
         - All other settings identical: same chunking, same
           embeddings, same schema, same scoring profile
           (fair comparison — only the data differs)

         PRODUCTION NOTE:
         This flag is for LOCAL VALIDATION only at this stage.
         Production infrastructure (Container Apps Job, Blob
         Storage, ADO pipeline, Terraform) will be designed
         AFTER the comparison validates the approach.
         See TODO list for the full production roadmap.

         --dry-run:
         Validates .env config, loads the scraped file, runs
         chunking, and reports what WOULD be indexed — without
         creating or modifying the index or uploading anything.
         Useful for:
         - Verifying a new scraped file is well-formed
         - Checking chunk counts before a full run
         - Testing in CI without Azure Search credentials
         - Confirming --no-hqa or --pilot settings look right
           before committing to a long run

v5.2.0 — July 2026 | Mukesh Kund
         content_hash: retrievable=True + MD5 → SHA-256

         TWO FIXES required by content_freshness.py nightly job:

         FIX 1 — content_hash retrievable=False → retrievable=True
         content_hash was marked retrievable=False (internal use
         only — correct intent). However content_freshness.py
         reads the stored hash back via select=["content_hash"]
         to compare against the live page hash. Azure AI Search
         returns None for non-retrievable fields, so
         fetch_current_hashes_from_index() always got empty results
         → every URL treated as changed on every nightly run
         → full re-scrape + re-index every night (wrong).
         Fix: retrievable=True. Field remains non-searchable,
         non-filterable, non-sortable — internal state only,
         not exposed to the API layer or frontend via retriever.py.

         FIX 2 — compute_content_hash(): MD5 → SHA-256
         scrape_approved_urls_updatedV3.py v4.4.0 uses SHA-256.
         The indexer was using MD5. content_freshness.py
         recomputes the hash from freshly scraped content and
         compares against the stored value. Mismatched algorithms
         means hashes never match even for identical content →
         every URL treated as changed every night (wrong).
         Fix: SHA-256 everywhere — scraper, indexer, freshness
         script all use the same algorithm and the same input
         (content after clean_content() external URL stripping).

         ACTION REQUIRED (DevOps / Mukesh):
         Run --full after deploying this version to refresh all
         stored hashes from MD5 → SHA-256. Only needed once.
         After that the nightly freshness job works correctly.

v5.3.0 — July 2026 | Mukesh Kund
         Renamed v3 → v4: new index targets with correct schema

         WHY A NEW FILE INSTEAD OF UPDATING v3:
         rlg-faq-index-v3 and rlg-faq-index-v3-baseline are live
         production indexes. They were built with MD5 hashes and
         content_hash retrievable=False — both wrong for the
         nightly content_freshness.py job. Wiping and rebuilding
         v3 would cause downtime and lose the existing comparison
         baseline. Instead, v4 is a clean build with the correct
         schema (SHA-256, retrievable=True) from day one.
         v3/v3-baseline remain live and untouched as fallback
         until v4 is validated in production.

         CHANGES vs v5.2.0:
         - INDEX_NAME default: rlg-faq-index-v3 → rlg-faq-index-v4
         - BASELINE_INDEX_NAME: rlg-faq-index-v3-baseline
                                → rlg-faq-index-v4-baseline
         - Module docstring updated to reference v4 targets
         - All v3 index references in comments updated to v4
         - No logic changes — schema, hashing, HQA, chunking
           all identical to v5.2.0

         PRODUCTION STEPS:
         1. Run scraper to produce fresh JSON:
            python scraper/scrape_approved_urls_updatedV4.py \\
               --file scraper/data/Approved_URLs.xlsx
         2. Build v4-baseline (fast, ~15-20 min, no HQA):
            python scraper/chunk_and_index_hqaV4.py --full --no-hqa
         3. Build v4 full HQA index (~3.5 hours):
            python scraper/chunk_and_index_hqaV4.py --full
         4. Update AZURE_SEARCH_INDEX_NAME=rlg-faq-index-v4 in
            server .env / Key Vault to point ARIA at v4
         5. Run content_freshness.py report mode to verify:
            python scraper/content_freshness.py --mode report
         6. Enable nightly freshness job (Container Apps Job)
         7. Keep v3 live until v4 validated in production

v5.4.0 — July 2026 | Mukesh Kund
         Atomic chunking for dropdown state pages.

         PROBLEM:
         chunk_pages() used RecursiveCharacterTextSplitter blindly
         on every page regardless of type. For standard pages this
         is correct. For dropdown state pages (one entry per policy
         option, scraped via Playwright) this is structurally wrong:

         If a dropdown page's per-option content grows large enough
         to split (>1600 chars), the splitter could place the policy
         context (in the title, prepended to chunk 0) in one chunk
         and the phone number / contact details in another. The LLM
         retrieves chunk 1 — gets a number with no policy name —
         and cannot correctly answer "what number for policy X?"

         The current data (20-100 words per option) avoids this by
         accident of size. Building on accidental safety is wrong
         for production. Future pages may have larger per-option
         content (T&Cs, eligibility criteria, full address blocks).

         FIX — Atomic chunking via dropdown_state field:
         Before splitting, chunk_pages() checks page.get("dropdown_state").
         If non-empty → this is a dropdown state page → produce
         exactly ONE chunk containing title + full content.
         No splitting, no overlap, no risk of separating policy
         context from contact details regardless of content size.

         WHY dropdown_state NOT URL pattern (#policy=):
         URL patterns are implementation details — they can change
         (today #policy=, tomorrow #tab= or #section= or no fragment
         at all). dropdown_state is set by the scraper at capture time
         and is the authoritative signal that this page entry represents
         a specific dropdown selection. URL-independent, scraper-version-
         independent, future-proof.

         EDGE CASES HANDLED:
         - Empty dropdown_state ("") → standard pages → normal chunking
         - dropdown_state set but content < 50 chars → skipped (existing guard)
         - dropdown_state set, very large content (future) → still 1 chunk
           (LLM context window handles individual chunks up to 8k tokens;
            a single option's content will never approach that limit)
         - HQA: atomic chunks still get augmented_questions generated
           (chunk_index=0, total_chunks=1 — HQA pipeline unchanged)

         SAME FIX applied to chunk_page() in content_freshness.py
         (delta indexing path) — both chunking functions must behave
         identically or freshness re-indexing produces different chunk
         structure than the full index run.

v5.5.0 — July 2026 | Mukesh Kund
         EMBEDDING_DIMS default 1024 → 1536. DevOps section added.

         EMBEDDING_DIMS CHANGE:
         Default value of AZURE_OPENAI_EMBEDDING_DIMENSIONS changed
         from 1024 → 1536. Rationale: Royal London FAQ content is
         domain-specific financial/insurance text with nuanced semantic
         differences between similar products (ISA vs pension vs
         protection). Higher dimensions = better semantic separation.
         No compromise on accuracy.

         Latency impact: ~5-10% slower vector search, ~10-15% slower
         embedding generation. Acceptable for a chat interface
         (~200-500ms total, negligible difference to user).

         ACTION REQUIRED (DevOps):
         - Update AZURE-OPENAI-EMBEDDING-DIMENSIONS in Key Vault → 1536
         - Run --full re-index after updating Key Vault secret
           (embedding dimension change requires full rebuild —
            Azure AI Search vector field dimensions are immutable)
         - Update AZURE_OPENAI_EMBEDDING_DIMENSIONS in content_freshness.py
           and embeddings.py to 1536 (already done in those files)

         PRODUCTION DEVOPS SECTION ADDED:
         Complete production deployment guide added to module docstring:
         - Dockerfile (python:3.11-slim, no playwright required)
         - Managed Identity RBAC roles table (5 resources)
         - Key Vault secrets list (12 secrets, correct names + values)
         - Container Apps Job trigger (manual, ADO pipeline)
         - 8-step job run order table (scraper → baseline → full HQA
           → Key Vault update → server restart → freshness verify
           → enable nightly → monitor)
         - Important notes (never touch v3, pilot before full,
           dim immutability, Redis TTL fallback)

         OTHER FIXES in this version:
         - v5.1.0 changelog: index names corrected from v3 to v4
           (v5.1.0 was written before v4 was created)
         - v5.3.0 production steps: scraper name corrected from
           scrape_approved_urls_updatedV3.py to V4

v5.6.0 — July 2026 | Mukesh Kund
         Env-var externalisation of hardcoded tuning constants +
         index name guard + import fix

         HARDCODED CONSTANTS → ENV VARS / KEY VAULT:
         Eight tuning constants were hardcoded in the script,
         meaning any change required a code edit and redeployment.
         All are now read from os.getenv() with safe defaults so
         they can be overridden via .env (dev) or Azure Key Vault
         (production) without touching code.

         Constants externalised:
           CHUNK_SIZE                 → CHUNK_SIZE               (default 1600)
           CHUNK_OVERLAP              → CHUNK_OVERLAP             (default 200)
           EMBEDDING_BATCH_SIZE       → EMBEDDING_BATCH_SIZE      (default 50)
           HQA_QUESTIONS_FIRST_CHUNK  → HQA_QUESTIONS_FIRST_CHUNK (default 8)
           HQA_QUESTIONS_OTHER_CHUNKS → HQA_QUESTIONS_OTHER_CHUNKS(default 5)
           TITLE_QUESTIONS_COUNT      → TITLE_QUESTIONS_COUNT     (default 3)
           TITLE_QUESTIONS_MAX_WORDS  → TITLE_QUESTIONS_MAX_WORDS (default 12)
           MAX_COLLISION_THRESHOLD    → MAX_COLLISION_THRESHOLD   (default 3)

         CHUNK_SIZE / CHUNK_OVERLAP SAFETY WARNING:
         If either is changed via env var, a WARNING is logged at
         startup. Changing chunk dimensions without running --full
         causes inconsistent chunk structure between existing and
         newly indexed pages. Always run --full after changing
         CHUNK_SIZE or CHUNK_OVERLAP.

         Key Vault secrets to add (DevOps action):
           CHUNK-SIZE                  1600
           CHUNK-OVERLAP               200
           EMBEDDING-BATCH-SIZE        50
           HQA-QUESTIONS-FIRST-CHUNK   8
           HQA-QUESTIONS-OTHER-CHUNKS  5
           TITLE-QUESTIONS-COUNT       3
           TITLE-QUESTIONS-MAX-WORDS   12
           MAX-COLLISION-THRESHOLD     3

         INDEX NAME GUARD — main() + run_pipeline() [NEW]:
         Prevents accidental overwrite of a wrong index if someone
         forgets to update env vars. Guard fires before any Azure
         call (including --dry-run). Logic: active_index must be
         one of the two values INDEX_NAME and BASELINE_INDEX_NAME
         resolve to — anything else aborts with sys.exit(1).
         Future-proof: when you move to V5 indexes, update the two
         constants (as you always would) and the guard automatically
         protects V4 with zero extra steps. No manual list to maintain.

         IMPORT FIX — main() [FIXED]:
         Line 3509: `import scraper.chunk_and_index_hqaV3 as _self`
         → `import scraper.chunk_and_index_hqaV4 as _self`
         Was importing V3 module (ModuleNotFoundError in production).

         PRINT STATEMENT FIXES:
         - Banner version string: v5.1.0 → v5.5.0 (was stale)
         - Pilot mode suggestion: chunk_and_index_hqaV3.py → V4
         - run_pipeline() docstring: rlg-faq-index-v3-baseline → v4-baseline

v5.7.0 — July 2026 | Mukesh Kund
         Content versioning and traceability — indexer side.

         NEW CONSTANT:
           PIPELINE_VERSION = "1.0.0"
             Bump when chunking, HQA, embedding, or index schema
             changes that affect the content or structure of what
             is uploaded to Azure AI Search.
             Start at 1.0.0 — v4 is the first index with versioning.

         5 NEW INDEX SCHEMA FIELDS (all SimpleField, non-searchable):
           pipeline_version  — which indexer logic built this chunk
           index_run_id      — UUID per pipeline execution; groups all
                               chunks from one run (filterable)
           indexed_at        — UTC ISO timestamp when chunk was uploaded
           scraper_version   — passed through from scraper JSON
           metadata_version  — passed through from scraper JSON

         All 5 fields: retrievable=True, filterable=True,
         searchable=False, sortable=False, facetable=False.
         REQUIRES --full reindex — new fields cannot be added to
         existing documents without a full rebuild.

         chunk_pages() UPDATED:
         index_run_id is generated once per pipeline run at
         the start of chunk_pages() and stamped on every chunk.
         indexed_at is set at chunk creation time (UTC ISO).
         scraper_version and metadata_version are passed through
         from the scraper JSON page dict (safe defaults "unknown"
         if scraper didn't produce them — backward compatible with
         old JSON files).

         run_pipeline() result dict UPDATED:
         run_id field added — callers can log which run produced
         the chunks in this execution.

         BUMPING RULES (documented here for reference):
           PIPELINE_VERSION bumps when:
             - Chunk schema changes (new fields)
             - Chunking logic changes (size, overlap, separators)
             - HQA prompt or generation logic changes
             - Embedding model or dimensions change
             - Scoring profile or semantic config changes
           Does NOT bump for: infrastructure changes (env vars,
           container config, Key Vault secrets).

v5.8.0 — July 2026 | Mukesh Kund
         scrape_run_id + refresh_count fields + versioning descriptions.

         NEW INDEX SCHEMA FIELDS:
           scrape_run_id (SimpleField, String, filterable, retrievable):
             UUID linking all chunks to the scraper run that produced
             the source page. Passed through from scraper JSON.
             Was previously produced by scraper but never indexed —
             now correctly stored so ops can query "all chunks from
             scrape run X". Default "unknown" for backward compat
             with pre-v4.6.0 scrape JSON files.

           refresh_count (SimpleField, Int32, filterable, sortable,
             retrievable):
             How many times this page has been refreshed by the
             nightly freshness job. Set to 0 by this indexer on
             every full run. content_freshness.py reads the existing
             value and increments by 1 on every delta re-index.
             Query: filter=refresh_count gt 0 shows everything
             freshness has ever touched.

         VERSIONING DESCRIPTION BLOCK:
         All versioning fields documented with their meaning,
         who sets them, and bumping rules — as code comments
         near the constants section. Prevents ambiguity when
         multiple developers work on the codebase.

         BUMPING RULES (see constants section for full detail):
           PIPELINE_VERSION — developer-bumped when chunking/HQA/
                              embedding/schema logic changes.
                              Requires --full reindex.
           scrape_run_id    — auto UUID per scraper run (from JSON)
           index_run_id     — auto UUID per indexer run
           refresh_count    — 0 on full run; auto-incremented by
                              freshness on delta re-index
           indexed_at       — auto timestamp per upload
           scraped_at       — from scraper JSON

v5.8.2 — July 2026 | Mukesh Kund
         GPT-5 compatibility: max_tokens → max_completion_tokens.

         generate_title_questions() [MODIFIED]:
         generate_hqa_questions()   [MODIFIED]:
         - GPT-5 models reject max_tokens with HTTP 400.
           Renamed to max_completion_tokens in both HQA call sites.
         - temperature and other params unchanged (fix separately
           if a temperature 400 error surfaces on GPT-5).

v5.8.1 — July 2026 | Mukesh Kund
         Self-import fix in main() [CRITICAL BUGFIX].

         PROBLEM:
         `import scraper.chunk_and_index_hqaV4 as _self` raises
         ModuleNotFoundError: No module named 'scraper' when the
         script is run as __main__ (python scraper/chunk_and_index_hqaV4.py
         from project root). The package import path only works when
         the module is imported by another module — not when run
         directly as a script entry point, even with __init__.py present.

         FIX:
         Replaced package import with sys.modules["__main__"] lookup.
         When a script is run directly, Python registers it as __main__
         in sys.modules. This always resolves correctly regardless of
         how the script is invoked (direct run, package import, or
         Container Apps Job entrypoint).

         Old: import scraper.chunk_and_index_hqaV4 as _self
         New: _self = sys.modules["__main__"]

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
from datetime import datetime, timezone
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
from azure.search.documents.indexes.models import ScoringProfile, TextWeights
from azure.search.documents.models import VectorizedQuery
from dotenv import load_dotenv, find_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter

# v5.0.0 FIX: v4.0.0 called find_dotenv(usecwd=False) but never
# passed override=True to load_dotenv() — a real .env change
# could still lose to a stale OS environment variable set in an
# earlier shell session. override=True ensures .env always wins.
_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path, override=True)
log = structlog.get_logger()

# v5.6.0: warn if chunk dimensions are overridden via env.
# Changing these without --full re-index causes inconsistent
# chunk structure between existing and newly indexed pages.
_chunk_size_overridden    = os.getenv("CHUNK_SIZE") is not None
_chunk_overlap_overridden = os.getenv("CHUNK_OVERLAP") is not None
if _chunk_size_overridden or _chunk_overlap_overridden:
    log.warning(
        "chunk_dimensions_overridden",
        chunk_size=os.getenv("CHUNK_SIZE", "1600"),
        chunk_overlap=os.getenv("CHUNK_OVERLAP", "200"),
        action_required="Run --full re-index or chunk structure will be inconsistent",
    )

# ── Versioning (v5.7.0 / v5.8.0) ────────────────────────────
#
# COMPLETE VERSIONING FIELD REFERENCE — ALL FIELDS ON EVERY CHUNK:
#
# PIPELINE_VERSION ("1.0.0" -> "1.1.0" -> ...)
#   Tracks WHICH CHUNKING/HQA/EMBEDDING LOGIC built this chunk.
#   Bump when: CHUNK_SIZE, CHUNK_OVERLAP, HQA prompts/generation/
#   validation logic, embedding model/dimensions, index schema,
#   or scoring profile changes. Requires --full reindex.
#   Never auto-increments. Developer-bumped only.
#   Stored as: pipeline_version (String) on every chunk.
#
# scraper_version (passed through from scraper JSON)
#   Tracks WHICH SCRAPING LOGIC produced the source page.
#   Set by scrape_approved_urls_updatedV4.py SCRAPER_VERSION.
#   Default "unknown" for pre-v4.6.0 scrape JSON files.
#   Stored as: scraper_version (String) on every chunk.
#
# metadata_version (passed through from scraper JSON)
#   Tracks WHICH METADATA EXTRACTION LOGIC produced the source page.
#   Set by scrape_approved_urls_updatedV4.py METADATA_VERSION.
#   Default "unknown" for pre-v4.6.0 scrape JSON files.
#   Stored as: metadata_version (String) on every chunk.
#
# scrape_run_id (passed through from scraper JSON, UUID)
#   Groups ALL CHUNKS whose source page came from ONE scraper run.
#   All 350 pages from one scraper invocation share one UUID.
#   Set by scrape_approved_urls_updatedV4.py at startup.
#   Default "unknown" for pre-v4.6.0 scrape JSON files.
#   Stored as: scrape_run_id (String) on every chunk.
#
# index_run_id (auto UUID per chunk_pages() call)
#   Groups ALL CHUNKS from ONE indexer execution together.
#   Generated once at start of chunk_pages(). New UUID per run.
#   Stored as: index_run_id (String) on every chunk.
#
# indexed_at (auto ISO timestamp per chunk_pages() call)
#   When this chunk was uploaded to Azure AI Search.
#   Distinct from scraped_at (when page was fetched).
#   Sortable — find the most recently indexed version.
#   Stored as: indexed_at (String, sortable) on every chunk.
#
# refresh_count (Int32, always 0 on full run)
#   How many times this page has been REFRESHED BY NIGHTLY FRESHNESS.
#   0 = indexed by this script, never touched by freshness.
#   N = refreshed N times by content_freshness.py nightly job.
#   content_freshness.py reads existing value and adds 1.
#   Stored as: refresh_count (Int32, sortable, filterable) on every chunk.
#
# scraped_at (from scraper JSON, ISO timestamp)
#   When the page was fetched from the Royal London website.
#   Set by scrape_approved_urls_updatedV4.py or content_freshness.py.
#   Stored as: scraped_at (String) on every chunk.
#
# BUMPING RULES SUMMARY:
#   Developer-bumped (manual — code logic changes only):
#     PIPELINE_VERSION, scraper_version, metadata_version
#   Auto-generated (no developer action needed):
#     scrape_run_id, index_run_id, indexed_at, scraped_at
#   Auto-incremented by freshness (reads existing + adds 1):
#     refresh_count
#
PIPELINE_VERSION = "1.0.0"

# ── Config ────────────────────────────────────────────────────
# SCRAPED_FILE is the last-resort fallback only — used when:
#   1. No --file argument passed, AND
#   2. find_latest_scraped_file() finds no matching JSON.
# Normal flow never reaches this constant.
# Update this if you need a guaranteed fallback file.
SCRAPED_FILE          = (
    "scraper/data/royal_london_faq_approved_20260623_111343.json"
)
# v5.6.0: externalised to env var / Key Vault.
# IMPORTANT: changing CHUNK_SIZE or CHUNK_OVERLAP requires --full
# re-index. Changing dimensions mid-run causes inconsistent chunk
# structure between existing and newly indexed pages.
# A warning is logged at startup if either is overridden via env.
CHUNK_SIZE            = int(os.getenv("CHUNK_SIZE", "1600"))
CHUNK_OVERLAP         = int(os.getenv("CHUNK_OVERLAP", "200"))
# v5.3.0: default changed to rlg-faq-index-v4.
# rlg-faq-index-v3 and rlg-faq-index-v3-baseline are NOT touched
# by this script — both remain live as fallback.
# Set AZURE_SEARCH_INDEX_NAME explicitly to override.
INDEX_NAME            = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v4")

# v5.3.0: baseline index name updated to v4-baseline.
# Targets rlg-faq-index-v4-baseline when --no-hqa flag is set.
# Separate from INDEX_NAME so both indexes can coexist for A/B
# comparison. Override via AZURE_SEARCH_BASELINE_INDEX_NAME env var.
BASELINE_INDEX_NAME   = os.getenv(
    "AZURE_SEARCH_BASELINE_INDEX_NAME",
    "rlg-faq-index-v4-baseline",
)
# v5.5.0: default changed 1024 → 1536 for improved semantic accuracy
# on Royal London domain-specific financial content.
# Requires re-index (--full) to rebuild vectors at new dimension.
# Update AZURE_OPENAI_EMBEDDING_DIMENSIONS in Key Vault to 1536.
EMBEDDING_DIMS        = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1536"))
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
SEARCH_ENDPOINT       = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT  = os.getenv(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "text-embedding-3-large",
)

# HQA model — gpt-4o-mini is sufficient for structured question
# generation (confirmed via compare_hqa_models.py: 97% agreement
# with gpt-4.1, 13.4x cheaper). Used for BOTH regular HQA
# question generation and title_questions generation.
#
# v5.0.0 FIX: was os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", ...).
# That "worked" only by coincidence — DEPLOYMENT_FAST happened to
# be gpt-4o-mini at the time. DEPLOYMENT_FAST has since been
# repointed to gpt-4o for the main query pipeline (FCA disclaimer
# consistency). Left unchanged, this script would have silently
# switched HQA generation to gpt-4o for the v3 re-index — no
# error, just ~13x the cost for a task that doesn't need it.
# HQA now reads its OWN dedicated env var, decoupled from FAST.
HQA_DEPLOYMENT        = os.getenv(
    "AZURE_OPENAI_DEPLOYMENT_HQA",
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
# Revert to 100 if/when upgraded to S1+ tier (via Key Vault update).
# v5.6.0: externalised to env var / Key Vault.
EMBEDDING_BATCH_SIZE  = int(os.getenv("EMBEDDING_BATCH_SIZE", "50"))
UPLOAD_BATCH_SIZE     = 100

# HQA batch size — how many chunks to generate questions for
# in a single gpt-4o-mini call. Keep small to stay under
# context limits and get focused per-chunk questions.
HQA_BATCH_SIZE        = 1   # one chunk per call for best quality

# ── v5.0.0: title_questions / variable HQA count ───────────────
# HQA_QUESTIONS_FIRST_CHUNK: questions generated for chunk_index
#   == 0 (the most overview-like content on any page). Raised
#   from 5 to 8 to help correct the volume imbalance that caused
#   broad queries to lose against product-specific pages.
# HQA_QUESTIONS_OTHER_CHUNKS: unchanged, all other chunk indices.
# TITLE_QUESTIONS_COUNT: broad ENTRY-POINT questions generated
#   ONLY for chunk_index == 0, stored in the separate
#   title_questions field (not augmented_questions).
# TITLE_QUESTIONS_MAX_WORDS: per TITLE_QUESTIONS_PROMPT — title
#   questions must stay under this word count.
# v5.6.0: all four externalised to env var / Key Vault so HQA
# tuning (question counts, word cap) can be adjusted without code
# changes or redeployment.
HQA_QUESTIONS_FIRST_CHUNK  = int(os.getenv("HQA_QUESTIONS_FIRST_CHUNK", "8"))
HQA_QUESTIONS_OTHER_CHUNKS = int(os.getenv("HQA_QUESTIONS_OTHER_CHUNKS", "5"))
TITLE_QUESTIONS_COUNT      = int(os.getenv("TITLE_QUESTIONS_COUNT", "3"))
TITLE_QUESTIONS_MAX_WORDS  = int(os.getenv("TITLE_QUESTIONS_MAX_WORDS", "12"))

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
    Compute SHA-256 hash of page content.
    Used by content_freshness.py to detect changed pages.
    Stored in content_hash index field (retrievable=True, v5.2.0).

    v5.2.0: MD5 -> SHA-256 to align with scraper v4.4.0 which
    uses SHA-256. All three scripts (scraper, indexer, freshness)
    must use the same algorithm and same input (post-clean_content)
    or hash comparison in the nightly job will always mismatch.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def chunk_pages(pages: list[dict]) -> list[dict]:
    """
    Split pages into chunks for indexing.
    Prepends title to each chunk for better embeddings.
    Respects markdown structure via separator hierarchy.
    Adds content_hash per chunk for freshness detection.

    v5.4.0 — ATOMIC CHUNKING FOR DROPDOWN STATE PAGES:
    Pages with a non-empty dropdown_state field are produced by the
    scraper's Playwright dropdown handler — one entry per policy option,
    containing ONLY that option's content (phone number, address, hours).
    These pages are NEVER split — they produce exactly 1 chunk regardless
    of content length. This guarantees the policy context (in title) and
    the contact details (in content) are always in the same chunk.

    Signal: page.get("dropdown_state") — non-empty string means this
    page is a dropdown state entry. Empty string = standard page.
    URL pattern (#policy=) is NOT used — it is an implementation detail
    that can change across scraper versions.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    # v5.7.0: one UUID per chunk_pages() call — links all chunks
    # from this pipeline run together in the index.
    _index_run_id  = str(uuid.uuid4())
    _indexed_at    = datetime.now(timezone.utc).isoformat()

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

        # v5.4.0 — ATOMIC CHUNKING: dropdown state pages get exactly
        # 1 chunk. The splitter is bypassed entirely for these pages.
        # dropdown_state is the authoritative signal — URL-independent.
        is_dropdown_state = bool(page.get("dropdown_state", ""))

        if is_dropdown_state:
            # Single atomic chunk — no splitting
            # chunk_index=0, total_chunks=1 so HQA pipeline is unaffected
            if len(content_with_title.strip()) >= 50:
                chunks.append({
                    "chunk_id":             str(uuid.uuid4()),
                    "content":              content_with_title.strip(),
                    "source_url":           url,
                    "title":                title,
                    "section":              section,
                    "audience":             audience,
                    "scraped_at":           page.get("scraped_at", ""),
                    "chunk_index":          0,
                    "total_chunks":         1,
                    "content_hash":         page_hash,
                    "augmented_questions":  "",
                    "title_questions":      "",
                    # ── Versioning fields (v5.7.0 / v5.8.0) ────
                    "pipeline_version":  PIPELINE_VERSION,
                    "index_run_id":      _index_run_id,
                    "indexed_at":        _indexed_at,
                    "scraper_version":   page.get("scraper_version", "unknown"),
                    "metadata_version":  page.get("metadata_version", "unknown"),
                    # scrape_run_id: UUID from scraper JSON — groups all chunks
                    # whose source page came from one scraper execution.
                    "scrape_run_id":     page.get("scrape_run_id", "unknown"),
                    # refresh_count: always 0 on full index run.
                    # content_freshness.py reads this value and increments by 1
                    # on every delta re-index of a changed page.
                    "refresh_count":     0,
                    # ── Enrichment fields (v3.0.0) ──────────────
                    "has_video":        page.get("has_video", False),
                    "content_type":     page.get("content_type", "article"),
                    "product_category": page.get("product_category", "general"),
                    "description":      page.get("description", ""),
                    "thumbnail_url":    page.get("thumbnail_url", ""),
                    "publish_date":     page.get("publish_date", ""),
                    "collection_name":  page.get("collection_name", ""),
                    "read_time_mins":   page.get("read_time_mins", "5"),
                })
                log.info(
                    "dropdown_atomic_chunk",
                    url=url,
                    dropdown_state=page.get("dropdown_state"),
                    chars=len(content_with_title),
                )
            continue  # Skip splitter for this page

        # Standard page — normal splitting
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
                # v5.0.0: title_questions populated later by
                # generate_title_questions() — ONLY for
                # chunk_index == 0 (i == 0 here). Empty string
                # default for all other chunks and as the
                # pre-augmentation default for chunk 0 too, so
                # the field is always present in every uploaded
                # document regardless of augmentation success.
                "title_questions":      "",

                # ── Versioning fields (v5.7.0 / v5.8.0) ────────
                # pipeline_version / index_run_id / indexed_at:
                #   generated by this indexer run.
                # scraper_version / metadata_version / scrape_run_id:
                #   passed through from scraper JSON — "unknown" if
                #   scraping from an older JSON without these fields
                #   (backward compatible with pre-v4.6.0 scrape files).
                # refresh_count:
                #   always 0 on full index run. content_freshness.py
                #   reads this and increments by 1 on each delta re-index.
                "pipeline_version":  PIPELINE_VERSION,
                "index_run_id":      _index_run_id,
                "indexed_at":        _indexed_at,
                "scraper_version":   page.get("scraper_version", "unknown"),
                "metadata_version":  page.get("metadata_version", "unknown"),
                "scrape_run_id":     page.get("scrape_run_id", "unknown"),
                "refresh_count":     0,

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
# ── Fix 2 (v4.0.0): BLOCKED_QUESTIONS ──────────────────────
# Questions blocked globally from being generated in any chunk.
# These are off-topic personal finance questions that appeared
# in 8-42 chunks due to cost-of-living / life-events pages
# mentioning money-saving content alongside product content.
#
# TODO (Customer sign-off required before next re-index):
# Confirm this list with Royal London brand/marketing team.
# Question to ask: "Should Aria answer general money-saving /
# budgeting questions, or only Royal London product questions?"
# Current list = sensible technical defaults pending decision.
#
# To add more blocked questions: add normalised lowercase string
# (no trailing punctuation) to this set. The check in
# score_question() normalises before comparing.
BLOCKED_QUESTIONS = {
    # Off-topic: personal finance / general money saving
    # Source: collision report June 2026 — appeared 8-42 chunks
    "how can i save money on my energy bills",
    "what state benefits can i get if im struggling",
    "how do i save money on my household bills",
    "how can i create a budget to save money",
    "how can i pay off my credit cards faster",
    "what tips do you have for monitoring my spending",
    "what tips do you have for building an emergency fund",
    "how can i find guides on saving and budgeting",
}

# Maximum number of chunks a question may appear in.
# Questions exceeding this threshold are deduplicated in
# deduplicate_questions_across_chunks() (Fix 3).
# Value of 3 allows same question on up to 3 different pages
# (acceptable — covers same topic from different angles)
# but prevents flooding across 20-100 chunks.
# v5.6.0: externalised to env var / Key Vault.
MAX_COLLISION_THRESHOLD = int(os.getenv("MAX_COLLISION_THRESHOLD", "3"))

# URL path segments that indicate a page is a dedicated
# product/topic page (NOT a generic/cross-topic page).
# Used in deduplication priority: chunks from these URLs
# get higher priority to keep a colliding question.
DEDICATED_PAGE_PATTERNS = [
    "/pensions/",
    "/insurance/",
    "/life-insurance/",
    "/income-protection/",
    "/critical-illness/",
    "/isa/",
    "/investments/",
    "/funeral/",
    "/pension-guides/",
    "/life-insurance-guides/",
    "/isa-guides/",
    "/existing-customers/",
]

# URL path segments that indicate a generic/cross-topic page.
# Chunks from these URLs get lower priority in deduplication
# — question is more likely kept on a dedicated product page.
GENERIC_PAGE_PATTERNS = [
    "/about-us/",
    "/our-purpose/",
    "/our-performance/",
    "/agm/",
    "/life-events/",
    "/cost-of-living/",
    "/planning-ahead/",
    "/money-guides/",
]


# ── Fix 1 (v4.0.0): Updated HQA prompts ─────────────────────
# Two prompts: standard (product/guide pages) and corporate.
# Previously one prompt for all content types — caused
# corporate pages to generate product questions from
# passing mentions, flooding retrieval with cross-topic
# collisions (103 chunks for "what types of life insurance
# does royal london offer" traced to corporate pages).
#
# v5.0.0: Converted to {num_questions}-parameterised TEMPLATES.
# Previously the count was hardcoded to "5" in three places
# (the instruction, the JSON array length, and the example
# array). Since chunk_index == 0 now requests 8 questions
# instead of 5 (HQA_QUESTIONS_FIRST_CHUNK), a hardcoded prompt
# would tell the model "generate exactly 5" while the code
# asks for 8 — a silent mismatch. .format(num_questions=...) is
# applied at call time in generate_hqa_questions(). The JSON
# example was changed from a fixed 5-item literal array to a
# generic "..." form so it stays correct for any count.

HQA_SYSTEM_PROMPT_TEMPLATE = """You are an expert at generating realistic customer search questions.

Given a chunk of text from Royal London's insurance and pension FAQ pages, generate exactly {num_questions} questions that:
1. A real Royal London customer would type into a search box or chatbot
2. Are ONLY answerable using the provided text — not general knowledge
3. Use natural customer language — not technical or legal jargon
4. Are SPECIFIC to this exact chunk — not generic insurance questions
5. Vary in phrasing to cover different ways a customer might ask the same thing

PRIMARY TOPIC RULE (v4.0.0 — most important rule):
Generate questions ONLY about the PRIMARY topic of this chunk.
The primary topic is what the majority of the chunk content discusses.
If a chunk about divorce mentions pensions in one sentence, do NOT generate pension questions.
If a chunk about budgeting mentions life insurance in passing, do NOT generate life insurance questions.
A question must be fully answerable from this chunk alone — not require other pages.

CROSS-TOPIC RULE (v4.0.0):
If this chunk covers multiple topics equally, generate questions for the FIRST topic only.
Do not generate questions that span across topics.

RULES:
- Include specific product names, numbers, or terms from the chunk
- Do NOT generate: "What is insurance?", "How do pensions work?" or any generic question
- Do NOT generate questions about topics mentioned only in passing
- Do NOT generate general money-saving or budgeting questions
  (e.g. "how can i save money on energy bills", "how to budget")
  unless this chunk is specifically and primarily about that topic
- Keep questions under 15 words each
- Questions must feel like real customer queries

Return ONLY a valid JSON array of exactly {num_questions} strings.
No explanation, no preamble, no markdown formatting.
Example format: ["question 1", "question 2", ...] — must contain exactly {num_questions} items"""


# Corporate page prompt — restricted to company-level questions.
# Used for chunks with content_type="corporate" only.
# Prevents corporate About-Us pages from generating product
# questions from passing mentions of pensions/insurance.
# v5.0.0: also converted to a {num_questions} template — see
# note above HQA_SYSTEM_PROMPT_TEMPLATE.
HQA_CORPORATE_PROMPT_TEMPLATE = """You are an expert at generating customer search questions about Royal London as a company.

Given a chunk of text from Royal London's corporate pages (About Us, Our Purpose, Social Impact, AGM etc),
generate exactly {num_questions} questions that a customer or stakeholder would ask about Royal London as an organisation.

ALLOWED question topics:
- Royal London's mission, values, and purpose
- How Royal London is owned and run (mutual ownership, profitshare)
- Royal London's social impact and charity partnerships
- Royal London's financial performance and results
- Royal London's history and heritage
- Who leads Royal London (Board, executives)
- AGM, governance, member rights

STRICTLY FORBIDDEN question topics (do NOT generate these):
- Specific products (pensions, life insurance, ISAs, income protection)
- How to make a claim
- Policy terms, premiums, or benefits
- Product pricing or eligibility
- Customer account questions

RULES:
- Questions must be about Royal London as a company, not its products
- Use natural language a stakeholder or curious customer would use
- Keep questions under 15 words each

Return ONLY a valid JSON array of exactly {num_questions} strings.
No explanation, no preamble, no markdown formatting."""


# ── v5.0.0 NEW: title_questions prompt ──────────────────────
# Generates broad ENTRY-POINT questions — the question a
# customer asks BEFORE knowing product-specific detail — for
# chunk_index == 0 of EVERY page. This is the counterpart to
# HQA_SYSTEM_PROMPT: HQA questions are deliberately SPECIFIC
# (see PRIMARY TOPIC RULE above), title_questions are
# deliberately BROAD. Both are needed — specific questions win
# for specific queries, title_questions win for broad queries
# like "What types of pensions does Royal London offer?".
#
# IMPORTANT: exactly 3 — do NOT copy this from the HQA prompt
# and forget to change "exactly 5" to "exactly 3". The count is
# fixed (not parameterised like the HQA templates above) because
# TITLE_QUESTIONS_COUNT is a permanent design constant, not a
# per-chunk-position variable.
TITLE_QUESTIONS_PROMPT = """You are an expert at generating broad ENTRY-POINT customer questions for Royal London's insurance and pension pages.

Given the first chunk of a page (its most overview-like content), generate exactly 3 questions that:
1. A customer would ask BEFORE knowing specific product detail — the natural "starting point" question for this topic
2. Are answerable using the provided text
3. Use natural customer language — not technical or legal jargon

Generate exactly these 3 questions, in this order:
Q1: The most natural "what is X" or "what are X" question for this page's topic
Q2: A "how does X work" or "what types of X" question for this page's topic
Q3: A Royal London specific question (e.g. "does Royal London offer X?")

RULES:
- Do NOT generate questions that are too specific — those are covered separately by product sub-pages, not this overview chunk
- Do NOT generate questions about topics mentioned only in passing
- Keep each question under 12 words
- Questions must feel like real, natural customer search queries

Return ONLY a valid JSON array of exactly 3 strings.
No explanation, no preamble, no markdown formatting.
Example format: ["question 1", "question 2", "question 3"]"""


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

    0 = reject — fails grounding, specificity, or blocked check
    1 = acceptable — passes all checks
    2 = good — passes all checks AND contains RL domain term

    Only questions scoring 1 or 2 are stored.
    Score 0 questions are discarded silently.

    v4.0.0: Added BLOCKED_QUESTIONS check (Fix 2).
    Normalises question before checking (lowercase, strip
    trailing punctuation) to catch near-matches.
    """
    # v4.0.0 Fix 2: Check BLOCKED_QUESTIONS first
    # Normalise: lowercase + strip trailing punctuation
    normalised = question.lower().strip()
    normalised = re.sub(r'[?!.,;:]+$', '', normalised).strip()
    if normalised in BLOCKED_QUESTIONS:
        log.debug(
            "hqa_question_blocked",
            question=question[:60],
            reason="in BLOCKED_QUESTIONS list",
        )
        return 0

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


# ── v5.0.0 NEW: title_questions validation ──────────────────
def is_valid_title_question(question: str, chunk_content: str) -> bool:
    """
    Validation for title_questions — DELIBERATELY LIGHTER than
    score_question() used for regular HQA questions.

    WHY NOT REUSE score_question() / is_specific_enough():
    is_specific_enough() explicitly REJECTS patterns like
    "what is a pension?" and "what types of X does royal london
    offer?" — correct for regular HQA (those add no retrieval
    value on a SPECIFIC product chunk) but exactly WRONG for
    title_questions, whose entire purpose is to BE that broad
    question on the page's overview chunk. Reusing
    is_specific_enough() here would reject the very questions
    this feature exists to generate.

    WHAT THIS STILL ENFORCES:
    - BLOCKED_QUESTIONS check — off-topic money/budgeting
      questions must not appear here either.
    - is_grounded() — the question must still relate to real
      content on this page, not be hallucinated.
    - Word count <= TITLE_QUESTIONS_MAX_WORDS (12), per
      TITLE_QUESTIONS_PROMPT's own instruction to the model —
      belt-and-braces in case the model doesn't comply exactly.

    WHAT THIS DELIBERATELY DOES NOT ENFORCE:
    - The generic-pattern rejection in is_specific_enough().
    """
    normalised = question.lower().strip()
    normalised = re.sub(r'[?!.,;:]+$', '', normalised).strip()

    if normalised in BLOCKED_QUESTIONS:
        log.debug(
            "title_question_blocked",
            question=question[:60],
            reason="in BLOCKED_QUESTIONS list",
        )
        return False

    if not is_grounded(question, chunk_content):
        return False

    word_count = len(question.split())
    if word_count == 0 or word_count > TITLE_QUESTIONS_MAX_WORDS:
        log.debug(
            "title_question_rejected",
            question=question[:60],
            reason="word_count",
            word_count=word_count,
            max_words=TITLE_QUESTIONS_MAX_WORDS,
        )
        return False

    return True


def generate_title_questions(
    chunk: dict,
    retry_count: int = 3,
) -> str:
    """
    v5.0.0 NEW — Generate 3 broad ENTRY-POINT questions for a
    page's first chunk (chunk_index == 0 ONLY).

    Process mirrors generate_hqa_questions() but:
    - Uses TITLE_QUESTIONS_PROMPT (fixed count of 3, not
      parameterised — see prompt docstring).
    - Uses is_valid_title_question() for validation, NOT
      score_question() — see that function's docstring for why.
    - Returns a NEWLINE-SEPARATED STRING (same format as
      augmented_questions), not a list. This is deliberate: it
      slots directly into chunk["title_questions"], into
      build_embedding_texts(), and into the uploaded document
      body with zero format conversion anywhere downstream.

    Returns empty string if chunk_index != 0, on total failure,
    or if the model's questions all fail validation. Never
    raises — failure here must not block the chunk from being
    indexed (it just loses the title_questions boost).

    Args:
        chunk:       The chunk dict (needs 'content', 'chunk_id',
                     'chunk_index' keys)
        retry_count: Max retries on API error or bad JSON
    """
    from openai import RateLimitError

    if chunk.get("chunk_index", -1) != 0:
        return ""

    client        = get_openai_client()
    chunk_content = chunk["content"]

    # Truncate very long chunks to stay within context limits —
    # same limit as generate_hqa_questions() for consistency.
    content_for_hqa = chunk_content[:2000]

    for attempt in range(retry_count):
        try:
            response = client.chat.completions.create(
                model=HQA_DEPLOYMENT,
                messages=[
                    {
                        "role":    "system",
                        "content": TITLE_QUESTIONS_PROMPT,
                    },
                    {
                        "role":    "user",
                        "content": (
                            f"Generate exactly {TITLE_QUESTIONS_COUNT} "
                            f"entry-point questions for this page's "
                            f"first chunk:\n\n{content_for_hqa}"
                        ),
                    },
                ],
                max_completion_tokens=200,
                temperature=0.3,
            )

            raw = response.choices[0].message.content or ""
            raw = raw.strip()

            if raw.startswith("```"):
                raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
                raw = raw.rstrip("`").strip()

            questions = json.loads(raw)

            if not isinstance(questions, list):
                log.warning(
                    "title_questions_invalid_format",
                    chunk_id=chunk["chunk_id"],
                    attempt=attempt + 1,
                )
                continue

            accepted = []
            for q in questions:
                if not isinstance(q, str) or not q.strip():
                    continue
                q = q.strip()
                if is_valid_title_question(q, chunk_content):
                    accepted.append(q)

            log.info(
                "title_questions_generated",
                chunk_id=chunk["chunk_id"][:8],
                generated=len(questions),
                accepted=len(accepted),
            )
            return "\n".join(accepted)

        except json.JSONDecodeError:
            log.warning(
                "title_questions_json_parse_failed",
                chunk_id=chunk["chunk_id"],
                attempt=attempt + 1,
            )
            if attempt < retry_count - 1:
                time.sleep(2)
            continue

        except RateLimitError as e:
            wait = 10 * (2 ** attempt)
            log.warning(
                "title_questions_rate_limit",
                chunk_id=chunk["chunk_id"],
                wait_seconds=wait,
                attempt=attempt + 1,
            )
            print(
                f"   ⚠️  Title questions rate limit. Waiting "
                f"{wait}s (attempt {attempt + 1}/{retry_count})..."
            )
            time.sleep(wait)
            continue

        except Exception as e:
            log.error(
                "title_questions_error",
                chunk_id=chunk["chunk_id"],
                error=str(e),
                attempt=attempt + 1,
            )
            if attempt < retry_count - 1:
                time.sleep(2)
            continue

    log.warning(
        "title_questions_failed_all_retries",
        chunk_id=chunk["chunk_id"],
        note="Chunk will be indexed without title_questions",
    )
    return ""


def generate_hqa_questions(
    chunk: dict,
    num_questions: int = HQA_QUESTIONS_OTHER_CHUNKS,
    retry_count: int = 3,
) -> list[str]:
    """
    Generate and validate hypothetical questions for a single chunk.

    Process:
    1. Call gpt-4o-mini with the HQA prompt template (formatted
       with num_questions) + chunk content
    2. Parse JSON response → list of num_questions questions
    3. Validate each question (grounding + specificity + score)
    4. Return only accepted questions (score >= 1)

    Returns between 0 and num_questions questions.
    Returns empty list on error (non-blocking — chunk still indexed
    without HQA questions, just with lower retrieval quality).

    Args:
        chunk:         The chunk dict (needs 'content' key)
        num_questions: v5.0.0 NEW — how many questions to request.
                       HQA_QUESTIONS_FIRST_CHUNK (8) for
                       chunk_index == 0, HQA_QUESTIONS_OTHER_CHUNKS
                       (5) otherwise. Caller (augment_chunks_with_hqa)
                       decides which to pass.
        retry_count:   Max retries on API error or bad JSON

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
    content_type  = chunk.get("content_type", "article")

    # v4.0.0 Fix 1: Use corporate prompt for corporate chunks.
    # Corporate pages (/about-us/, /our-purpose/, /agm/) were
    # generating product questions from passing mentions, causing
    # collisions. Corporate prompt restricts to company questions.
    # v5.0.0: templates formatted with the requested count — see
    # CHANGE LOG v5.0.0 for why this is no longer hardcoded to "5".
    prompt_template = (
        HQA_CORPORATE_PROMPT_TEMPLATE
        if content_type == "corporate"
        else HQA_SYSTEM_PROMPT_TEMPLATE
    )
    system_prompt = prompt_template.format(num_questions=num_questions)

    # Truncate very long chunks to stay within context limits
    # 2000 chars is sufficient for question generation
    content_for_hqa = chunk_content[:2000]

    # v5.0.0: max_tokens scales with num_questions — 300 was
    # tuned for 5 questions; 8 questions need more headroom or
    # responses risk truncating mid-JSON-array.
    max_tokens = int(num_questions * 55 + 50)

    for attempt in range(retry_count):
        try:
            response = client.chat.completions.create(
                model=HQA_DEPLOYMENT,
                messages=[
                    {
                        "role":    "system",
                        "content": system_prompt,
                    },
                    {
                        "role":    "user",
                        "content": (
                            f"Generate {num_questions} questions "
                            f"for this chunk:\n\n{content_for_hqa}"
                        ),
                    },
                ],
                max_completion_tokens=max_tokens,
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


def _is_dedicated_page(source_url: str) -> bool:
    """
    Returns True if URL matches a dedicated product/topic page.
    Used in deduplication priority — dedicated pages keep
    their questions when collision deduplication removes from
    generic/cross-topic pages.
    """
    url_lower = source_url.lower()
    return any(p in url_lower for p in DEDICATED_PAGE_PATTERNS)


def _is_generic_page(source_url: str) -> bool:
    """
    Returns True if URL matches a generic/cross-topic page.
    Generic pages get lower priority in deduplication —
    colliding questions are more likely removed from here
    than from dedicated product pages.
    """
    url_lower = source_url.lower()
    return any(p in url_lower for p in GENERIC_PAGE_PATTERNS)


def _dedup_priority(chunk: dict) -> tuple:
    """
    Compute deduplication priority for a chunk.
    Lower tuple value = higher priority = keeps the question.

    Priority order:
    1. Dedicated product page (not generic)
    2. Non-generic, non-dedicated (neutral)
    3. Generic cross-topic page
    Within each group:
    4. chunk_index == 0 (first chunk of page)
    5. chunk_index (lower = earlier = more definitional)
    """
    url        = chunk.get("source_url", "")
    idx        = chunk.get("chunk_index", 999)
    is_first   = 1 if idx == 0 else 2
    page_score = (
        0 if _is_dedicated_page(url) and not _is_generic_page(url)
        else 1 if not _is_generic_page(url)
        else 2
    )
    return (page_score, is_first, idx)


def deduplicate_questions_across_chunks(
    chunks: list[dict],
) -> list[dict]:
    """
    Fix 3 (v4.0.0): Post-generation cross-chunk deduplication.
    v5.0.0: Extended to cross-field deduplication — see below.

    After ALL chunks have HQA questions, scan for questions
    that appear in more than MAX_COLLISION_THRESHOLD chunks.
    For each over-threshold question, keep it only in the
    single most relevant chunk and remove from all others.

    PRIORITY for keeping (lower = higher priority):
        1. Dedicated product page (e.g. /pensions/, /insurance/)
        2. First chunk of page (chunk_index=0)
        3. Lowest chunk_index

    WHY post-generation (not during generation):
    We need ALL chunks to be generated before we can compare
    question frequency across the full index. Per-chunk
    generation doesn't have visibility into what other chunks
    will generate.

    v5.0.0 — CROSS-FIELD DEDUPLICATION:
    Previously this function scanned ONLY augmented_questions.
    It now scans title_questions AND augmented_questions
    TOGETHER as one collision pool. Without this, a broad
    question generated as a title_question on the overview page
    (e.g. "What types of pensions does Royal London offer?")
    could ALSO survive as a regular HQA question on some other
    product chunk — exactly the kind of duplicate-signal problem
    title_questions exists to eliminate, just re-introduced via
    a different field. _dedup_priority() already favours
    chunk_index == 0 on dedicated pages (where overview pages
    typically live, e.g. /pension-guides/), so in practice the
    overview page's title_questions slot usually wins the
    collision and the duplicate is removed from wherever else
    it appeared — whichever field that was.

    Normalisation:
    Compares normalised questions (lowercase, no trailing
    punctuation) — same as find_question_duplicates.py —
    to catch near-duplicates like "What is a pension?" vs
    "what is a pension".

    Args:
        chunks: List of all chunks with title_questions and
                augmented_questions set. Modified in-place.

    Returns:
        Same chunks list with deduplicated title_questions and
        augmented_questions fields.
    """
    def normalise(q: str) -> str:
        q = q.lower().strip()
        q = re.sub(r'[?!.,;:]+$', '', q)
        return ' '.join(q.split())

    # v5.0.0: both fields participate in the same collision pool.
    QUESTION_FIELDS = ("title_questions", "augmented_questions")

    print(f"\n🔄 Deduplicating questions across {len(chunks):,} chunks...")
    print(f"   Fields: {', '.join(QUESTION_FIELDS)}")
    print(f"   Threshold: >{MAX_COLLISION_THRESHOLD} chunks = collision")

    # Build question → list of (chunk_index_in_list, priority_tuple,
    # field_name, original_question)
    question_to_chunks: dict[str, list[tuple]] = {}

    for ci, chunk in enumerate(chunks):
        for field in QUESTION_FIELDS:
            raw = chunk.get(field, "") or ""
            questions = [q.strip() for q in raw.split("\n") if q.strip()]
            for q in questions:
                norm = normalise(q)
                if norm not in question_to_chunks:
                    question_to_chunks[norm] = []
                question_to_chunks[norm].append(
                    (ci, _dedup_priority(chunk), field, q)
                )

    # Find collisions
    collisions = {
        norm: entries
        for norm, entries in question_to_chunks.items()
        if len(entries) > MAX_COLLISION_THRESHOLD
    }

    if not collisions:
        print(f"   ✅ No collisions above threshold — nothing to remove")
        return chunks

    print(f"   Found {len(collisions):,} questions above threshold")

    # For each collision: find the best chunk to keep, remove from rest
    questions_removed = 0
    chunks_modified   = set()

    for norm_q, entries in collisions.items():
        # Sort by priority — lowest tuple = highest priority = KEEP
        sorted_entries = sorted(entries, key=lambda x: x[1])
        keep_chunk_idx = sorted_entries[0][0]

        # Remove from all other chunks — respecting WHICH field
        # each occurrence actually lives in.
        for ci, priority, field, original_q in sorted_entries[1:]:
            chunk = chunks[ci]
            raw   = chunk.get(field, "") or ""
            qs    = [q.strip() for q in raw.split("\n") if q.strip()]

            # Remove the colliding question (match normalised)
            new_qs = [
                q for q in qs
                if normalise(q) != norm_q
            ]

            if len(new_qs) < len(qs):
                chunk[field] = "\n".join(new_qs)
                questions_removed += 1
                chunks_modified.add(ci)

    log.info(
        "hqa_deduplication_complete",
        collisions_found=len(collisions),
        questions_removed=questions_removed,
        chunks_modified=len(chunks_modified),
        threshold=MAX_COLLISION_THRESHOLD,
        fields=list(QUESTION_FIELDS),
    )

    print(f"   ✅ Deduplication complete:")
    print(f"      Colliding questions:  {len(collisions):,}")
    print(f"      Questions removed:    {questions_removed:,}")
    print(f"      Chunks modified:      {len(chunks_modified):,}")

    return chunks


def augment_chunks_with_hqa(
    chunks: list[dict],
    pilot: bool = False,
    no_hqa: bool = False,
) -> list[dict]:
    """
    Generate HQA questions for all chunks (stored in
    'augmented_questions') and, for chunk_index == 0 only,
    title_questions (stored in 'title_questions').

    v5.0.0:
    - chunk_index == 0: HQA_QUESTIONS_FIRST_CHUNK (8) HQA
      questions + TITLE_QUESTIONS_COUNT (3) title questions.
    - chunk_index > 0:  HQA_QUESTIONS_OTHER_CHUNKS (5) HQA
      questions, title_questions = "" (not generated).

    v5.1.0 — no_hqa=True (baseline mode):
    - Skips ALL LLM calls entirely.
    - Sets augmented_questions = "" and title_questions = ""
      for every chunk.
    - The page title is the only retrieval signal — it is
      already prepended to content in chunk_pages(), so it
      contributes to both BM25 and the embedding naturally.
    - Run completes in seconds instead of ~3.5 hours.
    - Used to build rlg-faq-index-v4-baseline for A/B
      comparison against the full HQA index.

    pilot=True: process only first PILOT_CHUNK_LIMIT chunks.
    Used to validate question quality before full re-index.

    Returns the augmented chunk list. Chunks that fail HQA or
    title_questions generation are returned unchanged (empty
    string fields). The pipeline is non-blocking — generation
    failure never stops indexing.
    """
    # ── Baseline mode — skip all LLM calls ────────────────────
    if no_hqa:
        for chunk in chunks:
            chunk["augmented_questions"] = ""
            chunk["title_questions"]     = ""
        print(
            f"\n⏭️  Baseline mode (--no-hqa): skipping all LLM "
            f"calls for {len(chunks):,} chunks."
        )
        print(
            f"   augmented_questions = '' | "
            f"title_questions = '' for all chunks."
        )
        print(
            f"   Page title is the only implicit retrieval "
            f"signal (already in content via chunk_pages())."
        )
        return chunks
    chunks_to_augment = chunks[:PILOT_CHUNK_LIMIT] if pilot else chunks
    total             = len(chunks_to_augment)
    first_chunk_count = sum(
        1 for c in chunks_to_augment if c.get("chunk_index", -1) == 0
    )

    # Rough cost estimate — accounts for variable HQA count plus
    # title_questions generation on first_chunk_count chunks.
    # $0.000116 per question is the per-question cost figure
    # from compare_hqa_models.py's gpt-4o-mini pricing.
    est_questions = (
        (total - first_chunk_count) * HQA_QUESTIONS_OTHER_CHUNKS
        + first_chunk_count * (HQA_QUESTIONS_FIRST_CHUNK + TITLE_QUESTIONS_COUNT)
    )

    print(
        f"\n🧠 HQA: Generating questions for "
        f"{total:,} chunks"
        f"{' (PILOT MODE)' if pilot else ''}..."
    )
    print(
        f"   Model: {HQA_DEPLOYMENT} | "
        f"First-chunk pages: {first_chunk_count:,} "
        f"({HQA_QUESTIONS_FIRST_CHUNK} HQA + {TITLE_QUESTIONS_COUNT} title each) | "
        f"Other chunks: {HQA_QUESTIONS_OTHER_CHUNKS} HQA each"
    )
    print(
        f"   Est. questions: ~{est_questions:,} | "
        f"Est. cost: ~${est_questions * 0.000116:.2f} | "
        f"Est. time: ~{total // 30 + 1} min"
    )

    accepted_total       = 0
    accepted_title_total = 0
    rejected_total       = 0
    failed_chunks        = 0
    title_chunks_failed  = 0

    for i, chunk in enumerate(chunks_to_augment):
        is_first_chunk = chunk.get("chunk_index", -1) == 0
        num_questions = (
            HQA_QUESTIONS_FIRST_CHUNK
            if is_first_chunk
            else HQA_QUESTIONS_OTHER_CHUNKS
        )

        # ── title_questions — chunk_index == 0 only ───────────
        if is_first_chunk:
            title_qs_str = generate_title_questions(chunk)
            chunk["title_questions"] = title_qs_str
            if title_qs_str:
                accepted_title_total += len(title_qs_str.split("\n"))
            else:
                title_chunks_failed += 1
        else:
            chunk["title_questions"] = ""

        # ── regular HQA questions ──────────────────────────────
        questions = generate_hqa_questions(chunk, num_questions=num_questions)

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

        # v5.0.0 FIX: was hardcoded `5 - len(questions)` — silently
        # wrong once num_questions varies between 5 and 8. Now uses
        # the actual count requested for this specific chunk.
        rejected_total += (num_questions - len(questions))

        # Progress every 50 chunks
        if (i + 1) % 50 == 0 or (i + 1) == total:
            pct = round((i + 1) / total * 100)
            print(
                f"   [{pct:3d}%] {i + 1:,}/{total:,} chunks | "
                f"HQA accepted: {accepted_total:,} | "
                f"Title accepted: {accepted_title_total:,} | "
                f"Failed chunks: {failed_chunks}"
            )

        # Small sleep between chunks to respect rate limits
        # gpt-4o-mini is fast but we have 7,000+ chunks
        time.sleep(0.1)

    log.info(
        "hqa_augmentation_complete",
        total_chunks=total,
        first_chunk_count=first_chunk_count,
        accepted_hqa_questions=accepted_total,
        accepted_title_questions=accepted_title_total,
        rejected_hqa_questions=rejected_total,
        failed_hqa_chunks=failed_chunks,
        failed_title_chunks=title_chunks_failed,
        pilot=pilot,
    )

    print(f"\n   ✅ HQA complete:")
    print(f"      HQA questions accepted   : {accepted_total:,}")
    print(f"      HQA questions rejected   : {rejected_total:,}")
    print(f"      Title questions accepted : {accepted_title_total:,}")
    print(f"      Chunks without HQA       : {failed_chunks:,}")
    print(f"      First-chunks w/o title   : {title_chunks_failed:,}")

    # Pilot mode quality report
    if pilot:
        print(f"\n   📋 PILOT QUALITY REPORT — review before --full:")
        print(f"      HQA acceptance rate: "
              f"{accepted_total / max(accepted_total + rejected_total, 1) * 100:.1f}%")
        print(f"      Sample questions from first 3 chunks:")
        for chunk in chunks_to_augment[:3]:
            qs = chunk.get("augmented_questions", "")
            tqs = chunk.get("title_questions", "")
            if tqs:
                print(f"\n      Chunk: {chunk['title'][:50]} (chunk_index=0)")
                print(f"        Title questions:")
                for q in tqs.split("\n"):
                    print(f"          • {q}")
            if qs:
                if not tqs:
                    print(f"\n      Chunk: {chunk['title'][:50]}")
                print(f"        HQA questions:")
                for q in qs.split("\n")[:3]:
                    print(f"          • {q}")
        # Skip deduplication in pilot mode — not all chunks
        # are augmented so deduplication would be incomplete
        print(f"\n   ℹ️  Pilot mode: deduplication skipped")
        print(f"      Deduplication runs on full chunk set only")
        return chunks

    # v4.0.0 Fix 3 (v5.0.0: now cross-field) — cross-chunk
    # deduplication. Only runs after ALL chunks are augmented
    # (not in pilot mode). Removes questions appearing in
    # > MAX_COLLISION_THRESHOLD chunks across BOTH
    # title_questions and augmented_questions, keeping only the
    # most relevant occurrence.
    chunks = deduplicate_questions_across_chunks(chunks)

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

        # ── Title questions (v5.0.0 NEW) ─────────────────────
        # Stores gpt-4o-mini generated broad ENTRY-POINT
        # questions — ONLY populated for chunk_index == 0 (the
        # overview chunk of each page); "" for every other chunk.
        # Fixes the broad-query retrieval problem where specific
        # product pages (many chunks x many HQA questions each)
        # drowned out overview pages (one chunk x few questions)
        # in both BM25 and vector search.
        # Searchable: boosts BM25 — also given the highest weight
        # (5.0) in the "rl-retrieval-profile" scoring profile
        # below, and listed FIRST in the semantic config content
        # fields — the highest-priority signal available.
        # Not filterable/sortable/facetable: not used for
        # exact-match filtering, only for search relevance.
        # Retrievable: useful for debugging question quality.
        SearchableField(
            name="title_questions",
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
            facetable=True,    # v4.0.0: UI can facet/filter by audience
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
            filterable=True,   # v4.0.0: needed by content_freshness.py
            sortable=True,     # v4.0.0: sort by scrape date for freshness
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
            filterable=True,   # v4.0.0: needed by evaluate_index_quality.py
            sortable=True,     # v4.0.0: sort chunks in page order
            facetable=False,
            retrievable=True,
        ),
        SimpleField(
            name="total_chunks",
            type=SearchFieldDataType.Int32,
            searchable=False,
            filterable=True,   # v4.0.0: enables position-based queries
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Content hash (v2.0.0 NEW) ─────────────────────────
        # SHA-256 hash of page content (v5.2.0: MD5->SHA-256).
        # Computed post-clean_content(), pre-title-prepend.
        # Used by content_freshness.py nightly job to detect
        # changed pages without re-scraping everything.
        # retrievable=True (v5.2.0 FIX: was False).
        # content_freshness.py reads this via select=["content_hash"]
        # to compare against tonight's live scrape hash.
        # Was False -> Azure returned None -> every URL appeared
        # changed every night. Field is NOT exposed via retriever.py.
        SimpleField(
            name="content_hash",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=False,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),

        # ── Versioning fields (v5.7.0 NEW) ───────────────────
        # End-to-end traceability: every chunk can be traced back
        # to which pipeline run, scraper version, and metadata
        # extraction logic produced it. All filterable for ops
        # queries ("show me all chunks from run X").
        # Not searchable — version strings add no BM25 value.
        # Not exposed via retriever.py — internal/ops use only.
        SimpleField(
            name="pipeline_version",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),
        SimpleField(
            name="index_run_id",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),
        SimpleField(
            name="indexed_at",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=False,
            sortable=True,
            facetable=False,
            retrievable=True,
        ),
        SimpleField(
            name="scraper_version",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),
        SimpleField(
            name="metadata_version",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),
        # scrape_run_id: UUID from scraper JSON — groups all chunks
        # whose source page came from one scraper invocation.
        # Lets ops query "show everything from scrape run X".
        SimpleField(
            name="scrape_run_id",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=True,
            sortable=False,
            facetable=False,
            retrievable=True,
        ),
        # refresh_count: how many times this page has been refreshed
        # by the nightly content_freshness.py job.
        # 0 = full index run (this script), never delta'd.
        # N = refreshed N times by freshness. Int32 for sort/filter.
        # Use: filter=refresh_count gt 0 to find all freshness-touched chunks.
        SimpleField(
            name="refresh_count",
            type=SearchFieldDataType.Int32,
            searchable=False,
            filterable=True,
            sortable=True,
            facetable=False,
            retrievable=True,
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
        # filterable: UI can filter "show me only quick reads"
        # or "show me webinar-length content (>20 mins)".
        SimpleField(
            name="read_time_mins",
            type=SearchFieldDataType.String,
            searchable=False,
            filterable=True,   # v4.0.0: UI can filter by read time
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

    # ── Scoring profile (v5.0.0 NEW) ──────────────────────────
    # "rl-retrieval-profile" — text-weight boost for BM25 scoring.
    # title_questions gets the highest weight (5.0) — the
    # strongest signal for broad/entry-point queries. Not set as
    # the index's default profile: retriever.py requests it
    # explicitly per query via scoring_profile="rl-retrieval-
    # profile", so the boost is opt-in and only applied when
    # state.query_type == "BROAD" (classifier_node.py decides
    # this). Specific queries continue to use plain BM25 scoring
    # so title_questions doesn't distort results that are already
    # correctly matching a specific product chunk.
    scoring_profile = ScoringProfile(
        name="rl-retrieval-profile",
        text_weights=TextWeights(weights={
            "title_questions":     5.0,
            "title":               4.0,
            "augmented_questions": 2.0,
            "section":             2.0,
            "collection_name":     1.5,
            "content":             1.0,
            "description":         1.0,
        }),
    )

    # ── Semantic search configuration (v2.0.0) ────────────────
    # Created at index build time — no separate portal setup or
    # add_semantic_config.py script needed after this.
    #
    # title_field:     title — primary signal for the reranker
    # content_fields:  v5.0.0 — title_questions listed FIRST
    #                  (highest-priority signal for broad
    #                  queries), then content + augmented_questions
    #                  + description as before.
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
                        # v5.0.0 — title_questions FIRST. Broad
                        # entry-point questions are the strongest
                        # signal for the reranker on broad queries,
                        # so they take priority over chunk content.
                        SemanticField(field_name="title_questions"),
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
                scoring_profiles=[scoring_profile],
            )
        )
        log.info(
            "index_created",
            index=INDEX_NAME,
            semantic_config=SEMANTIC_CONFIG_NAME,
            scoring_profile="rl-retrieval-profile",
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

    v5.0.0: Also includes title_questions (when present — i.e.
    chunk_index == 0 with successful generation), ahead of
    augmented_questions, matching the field priority used in the
    scoring profile and semantic config.

    WHY THIS MATTERS (not just a nice-to-have):
    Retrieval is hybrid — BM25 + vector (HNSW) + semantic
    reranker. Adding title_questions only as a searchable/
    semantic index field (as originally specced) improves BM25
    and the semantic reranker, but leaves the VECTOR half of
    hybrid search with zero benefit from it. A broad query like
    "What types of pensions does Royal London offer?" is an
    embedding-space problem as much as a keyword one — the query
    embedding needs something in "question space" to be close
    to, not just declarative page content in "document space".
    Without title_questions in the embedded text, this root
    cause would only be half-fixed.

    Format (chunk_index == 0, both fields present):
        {content}

        Entry-point questions this page answers:
        {title question 1}
        {title question 2}
        {title question 3}

        Questions this answers:
        {hqa question 1}
        ...

    Format (all other chunks, or title_questions empty):
        {content}

        Questions this answers:
        {question 1}
        {question 2}
        ...

    If both are empty (generation failed or pilot mode), falls
    back to content-only embedding — same as v1.x behaviour.
    """
    texts = []
    for chunk in chunks:
        content        = chunk["content"]
        title_questions = (chunk.get("title_questions", "") or "").strip()
        questions       = (chunk.get("augmented_questions", "") or "").strip()

        parts = [content]
        if title_questions:
            parts.append(
                f"Entry-point questions this page answers:\n"
                f"{title_questions}"
            )
        if questions:
            parts.append(f"Questions this answers:\n{questions}")

        text = "\n\n".join(parts) if len(parts) > 1 else content
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
            "augmented_questions", "title_questions",
        ],
        top=3,
    )

    print("\n" + "=" * 55)
    print("🔍 Test Query: 'How do I make a claim?'")
    print("=" * 55)
    for i, result in enumerate(results, 1):
        qs = result.get("augmented_questions", "") or ""
        tqs = result.get("title_questions", "") or ""
        qs_preview = qs.split("\n")[0] if qs else "None"
        print(f"\n[{i}] Title:     {result.get('title', 'N/A')}")
        print(f"    Section:   {result.get('section', 'N/A')}")
        print(f"    URL:       {result.get('source_url', 'N/A')}")
        print(f"    Preview:   {result['content'][:120]}...")
        print(f"    HQA Q1:    {qs_preview}")
        if tqs:
            print(f"    Title Qs:  {tqs.split(chr(10))[0]}")

    # v5.0.0 — second test query: a BROAD query, specifically to
    # verify the root cause this re-index exists to fix. Uses the
    # "rl-retrieval-profile" scoring profile so title_questions
    # gets its 5.0 weight boost, same as retriever.py will apply
    # for state.query_type == "BROAD" queries in production.
    broad_query = "What types of pensions does Royal London offer?"
    broad_response = client.embeddings.create(
        input=[broad_query],
        model=EMBEDDING_DEPLOYMENT,
        dimensions=EMBEDDING_DIMS,
    )
    broad_embedding = broad_response.data[0].embedding

    broad_results = search_client.search(
        search_text=broad_query,
        vector_queries=[
            VectorizedQuery(
                vector=broad_embedding,
                k_nearest_neighbors=3,
                fields="embedding",
            )
        ],
        query_type="semantic",
        semantic_configuration_name=SEMANTIC_CONFIG_NAME,
        scoring_profile="rl-retrieval-profile",
        select=[
            "chunk_id", "title", "content",
            "source_url", "section", "title_questions",
        ],
        top=3,
    )

    print("\n" + "=" * 55)
    print(f"🔍 Broad Query Test: '{broad_query}'")
    print("   (verifies title_questions fix — expect the")
    print("    overview/pension-basics page in top results,")
    print("    not only workplace-pension product pages)")
    print("=" * 55)
    for i, result in enumerate(broad_results, 1):
        tqs = result.get("title_questions", "") or ""
        print(f"\n[{i}] Title:     {result.get('title', 'N/A')}")
        print(f"    URL:       {result.get('source_url', 'N/A')}")
        if tqs:
            print(f"    Title Qs:  {tqs.split(chr(10))[0]}")
        else:
            print(f"    Title Qs:  None (not chunk_index==0, or generation failed)")

    print("\n✅ Index verification complete!")


# ── File auto-detection ───────────────────────────────────────
def find_latest_scraped_file() -> str:
    """
    Auto-detect the most recently modified SCRAPER output JSON
    in scraper/data/. Used when --file is not specified.

    v2.0.0: Replaces hardcoded SCRAPED_FILE constant.
    v4.0.0 FIX: Now filters by filename prefix pattern
        "royal_london_faq_approved_*.json"
        instead of picking ANY *.json in scraper/data/.

    WHY THIS FIX WAS NEEDED:
    scraper/data/ accumulates multiple JSON files over time:
        royal_london_faq_approved_<ts>.json  ← scraper output ✅
        collision_report_<ts>.json           ← diagnostic output ❌
        quality_report_<ts>.json             ← evaluator output ❌
        hqa_model_comparison_<ts>.json       ← comparison output ❌
        approved_updates_<ts>.json           ← approval template ❌

    Before this fix, any of these could be picked as "latest"
    if created after the scraper run — causing chunk_pages() to
    fail with "AttributeError: str object has no attribute get"
    because these files have different JSON structures.

    Now only files matching the scraper output pattern are
    considered — completely safe regardless of what other JSON
    files accumulate in scraper/data/.

    Falls back to SCRAPED_FILE constant if no matching file found.
    """
    data_dir = Path("scraper/data")
    if not data_dir.exists():
        return SCRAPED_FILE

    # Only match scraper output files — pattern set by
    # scrape_approved_urls.py save_scraped_pages() function.
    # This intentionally excludes all diagnostic/report JSONs.
    SCRAPER_OUTPUT_PATTERN = "royal_london_faq_approved_*.json"

    matching_files = sorted(
        data_dir.glob(SCRAPER_OUTPUT_PATTERN),
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # Most recently modified first
    )

    if matching_files:
        latest = str(matching_files[0])
        log.info(
            "auto_detected_scraped_file",
            file=latest,
            pattern=SCRAPER_OUTPUT_PATTERN,
            candidates_found=len(matching_files),
        )
        return latest

    # No matching scraper output found — log clearly so user
    # knows why it fell back, not a silent failure
    log.warning(
        "no_scraped_file_found",
        pattern=SCRAPER_OUTPUT_PATTERN,
        data_dir=str(data_dir),
        note="Run scrape_approved_urls.py first, or pass --file explicitly",
    )
    return SCRAPED_FILE


# ── Programmatic entry point ──────────────────────────────────
def run_pipeline(
    mode: str = "new-only",
    scraped_file: str | None = None,
    pilot: bool = False,
    no_hqa: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Programmatic entry point for the indexing pipeline.
    Called by DevOps / Azure Container Apps Job trigger.

    Args:
        mode:         "full"     — delete and recreate index
                      "new-only" — only index new pages (default)
        scraped_file: Path to scraped JSON file. If None,
                      auto-detects latest file in scraper/data/.
        pilot:        If True, process first 100 chunks only
                      for HQA quality validation. Use before
                      running a full re-index for the first time.
        no_hqa:       v5.1.0 — If True, skip all LLM calls.
                      Builds rlg-faq-index-v4-baseline for A/B
                      comparison. ~15-20 minutes vs ~3.5 hours.
        dry_run:      v5.1.0 — If True, validate + chunk but do
                      NOT create/update index or upload anything.
                      Returns result with dry_run=True in dict.

    Returns:
        dict with keys:
            success         (bool)  — True if completed without error
            pages_indexed   (int)   — number of pages processed
            chunks_created  (int)   — number of chunks created
            chunks_uploaded (int)   — 0 if dry_run=True
            hqa_questions   (int)   — 0 if no_hqa=True
            title_questions (int)   — 0 if no_hqa=True
            cache_cleared   (bool)  — False if dry_run=True
            no_hqa          (bool)  — whether baseline mode was used
            dry_run         (bool)  — whether dry run was performed
            index_name      (str)   — actual index name used
            error           (str)   — error message if success=False

    TODO (DevOps — Sprint 2):
    Wrap in Azure Container Apps Job trigger:

        # ADO pipeline invocation:
        az containerapp job start \
            --name aria-indexer-job \
            --resource-group <rg> \
            --env-vars MODE=full NO_HQA=false DRY_RUN=false

        # In script entrypoint (reads env vars):
        no_hqa  = os.getenv("NO_HQA", "false").lower() == "true"
        dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
        result  = run_pipeline(
            mode=os.getenv("MODE", "new-only"),
            no_hqa=no_hqa,
            dry_run=dry_run,
        )

    See TODO list for full production infrastructure roadmap:
    Container Apps Job, Blob Storage, Checkpoint/Resume,
    ADO Pipeline YAML, Terraform, Application Insights.
    """
    import traceback

    # Resolve index name based on mode
    active_index = BASELINE_INDEX_NAME if no_hqa else INDEX_NAME

    # ── Index name guard (v5.6.0) ─────────────────────────────
    # Same guard as main() — protects against wrong env vars
    # when called programmatically from ADO pipeline / Container Job.
    CURRENT_TARGETS = {INDEX_NAME, BASELINE_INDEX_NAME}
    if active_index not in CURRENT_TARGETS:
        return {
            "success":         False,
            "pages_indexed":   0,
            "chunks_created":  0,
            "chunks_uploaded": 0,
            "hqa_questions":   0,
            "title_questions": 0,
            "cache_cleared":   False,
            "no_hqa":          no_hqa,
            "dry_run":         dry_run,
            "index_name":      active_index,
            "error": (
                f"ABORTED: '{active_index}' is not a recognised target. "
                f"Expected one of: {sorted(CURRENT_TARGETS)}. "
                f"Check AZURE_SEARCH_INDEX_NAME / AZURE_SEARCH_BASELINE_INDEX_NAME."
            ),
        }

    result = {
        "success":          False,
        "pages_indexed":    0,
        "chunks_created":   0,
        "chunks_uploaded":  0,
        "hqa_questions":    0,
        "title_questions":  0,
        "cache_cleared":    False,
        "no_hqa":           no_hqa,
        "dry_run":          dry_run,
        "index_name":       active_index,
        "pipeline_version": PIPELINE_VERSION,
        "run_id":           "",   # v5.7.0: set after chunk_pages() runs
        "error":            "",
    }

    try:
        fresh = (mode == "full")

        # ── Validate config ───────────────────────────────────
        if not AZURE_OPENAI_ENDPOINT:
            raise ValueError("AZURE_OPENAI_ENDPOINT not set in .env")
        if not SEARCH_ENDPOINT:
            raise ValueError("AZURE_SEARCH_ENDPOINT not set in .env")

        # ── Load pages ─────────────────────────────────────────
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
        # v5.7.0: capture index_run_id from first chunk so callers
        # can log/audit which run produced these chunks.
        if chunks:
            result["run_id"] = chunks[0].get("index_run_id", "")

        # ── Step 2: HQA augmentation (or baseline skip) ───────
        chunks = augment_chunks_with_hqa(
            chunks, pilot=pilot, no_hqa=no_hqa
        )
        if not no_hqa:
            result["hqa_questions"] = sum(
                len(c.get("augmented_questions", "").split("\n"))
                for c in chunks
                if c.get("augmented_questions", "").strip()
            )
            result["title_questions"] = sum(
                len(c.get("title_questions", "").split("\n"))
                for c in chunks
                if c.get("title_questions", "").strip()
            )

        # ── Dry run — stop here, don't touch the index ────────
        if dry_run:
            log.info(
                "dry_run_complete",
                pages=result["pages_indexed"],
                chunks=result["chunks_created"],
                no_hqa=no_hqa,
                index_name=active_index,
            )
            result["success"] = True
            return result

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
                import sys as _sys
                from pathlib import Path as _Path
                _project_root = str(
                    _Path(__file__).resolve().parent.parent
                )
                if _project_root not in _sys.path:
                    _sys.path.insert(0, _project_root)
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
        description=(
            "Chunk and index RLG FAQ pages with HQA.\n"
            "Use --no-hqa to build the baseline index for A/B comparison."
        )
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
        "--no-hqa",
        action="store_true",
        help=(
            "Baseline mode: skip ALL LLM calls (no HQA questions, "
            "no title_questions). Targets rlg-faq-index-v3-baseline "
            "for A/B comparison against the full HQA index. "
            "Completes in ~15-20 minutes (vs ~3.5 hours for full HQA)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate config, load pages, chunk — but do NOT "
            "create/update the index or upload anything. "
            "Shows what WOULD be indexed without touching Azure Search."
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
    no_hqa       = args.no_hqa
    dry_run      = args.dry_run
    scraped_file = args.file or find_latest_scraped_file()

    # ── Resolve index name ────────────────────────────────────
    # --no-hqa targets the baseline index automatically.
    # AZURE_SEARCH_INDEX_NAME env var overrides both defaults.
    active_index = BASELINE_INDEX_NAME if no_hqa else INDEX_NAME

    # ── Index name guard (v5.6.0) ─────────────────────────────
    # Prevent accidental overwrite of a wrong/protected index if
    # env vars are not updated before running. Only the two
    # current-version targets are permitted. When you move to V5
    # indexes, update INDEX_NAME and BASELINE_INDEX_NAME constants
    # — this guard automatically protects V4 with no extra steps.
    CURRENT_TARGETS = {INDEX_NAME, BASELINE_INDEX_NAME}
    if active_index not in CURRENT_TARGETS:
        print(f"\n❌ ABORTED — '{active_index}' is not a recognised target for this script version.")
        print(f"   Expected one of:")
        for _idx in sorted(CURRENT_TARGETS):
            print(f"     • {_idx}")
        print(f"\n   Check AZURE_SEARCH_INDEX_NAME / AZURE_SEARCH_BASELINE_INDEX_NAME in .env or Key Vault.")
        sys.exit(1)

    # Patch module-level INDEX_NAME so all downstream functions
    # (create_or_update_index, upload_chunks, verify_index etc)
    # use the correct index without needing to pass it everywhere.
    # v5.8.1 FIX: sys already imported globally — no local import needed.
    # sys.modules["__main__"] always resolves to the running script
    # regardless of invocation method (direct run or package import).
    _self = sys.modules["__main__"]
    _self.INDEX_NAME = active_index

    print(f"\n🚀 RLG Chunk and Index Pipeline v5.5.0")
    print("=" * 60)
    print(f"   Mode:      {'FULL (fresh index)' if fresh else 'NEW ONLY (append)'}")
    print(f"   Strategy:  {'⏭️  BASELINE — no HQA, title only' if no_hqa else '🧠 FULL HQA + title_questions'}")
    print(f"   Dry run:   {'✅ YES — index will NOT be modified' if dry_run else 'No'}")
    print(f"   HQA:       {'SKIPPED (--no-hqa)' if no_hqa else ('PILOT (100 chunks)' if pilot else 'FULL')}")
    print(f"   File:      {scraped_file}")
    print(f"   Index:     {active_index}")
    print(f"   Embed:     {EMBEDDING_DEPLOYMENT} ({EMBEDDING_DIMS}d)")
    if not no_hqa:
        print(f"   HQA mdl:   {HQA_DEPLOYMENT}")
        print(f"   HQA cnt:   {HQA_QUESTIONS_FIRST_CHUNK} (chunk_index=0) / {HQA_QUESTIONS_OTHER_CHUNKS} (other)")
        print(f"   Title Qs:  {TITLE_QUESTIONS_COUNT} (chunk_index=0 only)")
    print(f"   Semantic:  {SEMANTIC_CONFIG_NAME}")
    print(f"   Scoring:   rl-retrieval-profile")
    print(f"   Search:    {SEARCH_ENDPOINT}")
    print(f"   OpenAI:    {AZURE_OPENAI_ENDPOINT}")
    if no_hqa:
        print(f"\n   ℹ️  Baseline mode: augmented_questions and title_questions")
        print(f"      will be empty for all chunks. Page title is the only")
        print(f"      retrieval signal (already in content via chunking).")
        print(f"      Compare with {INDEX_NAME} using compare_indexes.py.")
    if dry_run:
        print(f"\n   ⚠️  DRY RUN: no index creation or upload will occur.")
    print("=" * 60)

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

    # ── Step 2: HQA augmentation (or baseline skip) ───────────
    if no_hqa:
        print(f"\n⏭️  Step 2/6: Skipping HQA (--no-hqa baseline mode)...")
    else:
        print(f"\n🧠 Step 2/6: HQA question augmentation...")
    chunks = augment_chunks_with_hqa(chunks, pilot=pilot, no_hqa=no_hqa)

    if pilot and not no_hqa:
        print(
            "\n⏸️  PILOT MODE: Review question quality above."
            f"\n   If satisfied, run:"
            f"\n     python scraper/chunk_and_index_hqaV4.py --full"
            f"\n   Exiting pilot run now (index not updated)."
        )
        return

    # ── Dry run — stop here ───────────────────────────────────
    if dry_run:
        hqa_count = sum(
            1 for c in chunks if c.get("augmented_questions", "").strip()
        )
        tq_count = sum(
            1 for c in chunks if c.get("title_questions", "").strip()
        )
        print(f"\n✅ DRY RUN COMPLETE — nothing was uploaded or indexed.")
        print(f"   Pages that would be indexed:  {len(pages_to_index):,}")
        print(f"   Chunks that would be created: {len(chunks):,}")
        print(f"   Chunks with HQA questions:    {hqa_count:,}")
        print(f"   Chunks with title_questions:  {tq_count:,}")
        print(f"   Target index:                 {active_index}")
        print(f"\n   Remove --dry-run to run for real.")
        return

    # ── Step 3: Create/update index ───────────────────────────
    print(
        f"\n🔧 Step 3/6: "
        f"{'Recreating' if fresh else 'Ensuring'} index..."
    )
    create_or_update_index(fresh=fresh)
    print(f"   Index '{active_index}' ready")
    print(f"   Semantic config '{SEMANTIC_CONFIG_NAME}' created")

    # ── Step 4: Build embedding texts ─────────────────────────
    label = "content only (baseline)" if no_hqa else "content + HQA"
    print(f"\n📝 Step 4/6: Building embedding texts ({label})...")
    embedding_texts = build_embedding_texts(chunks)
    hqa_count = sum(
        1 for c in chunks
        if c.get("augmented_questions", "").strip()
    )
    print(f"   {len(embedding_texts):,} texts built")
    if not no_hqa:
        print(f"   {hqa_count:,} chunks include HQA questions in embedding")

    # ── Step 5: Generate embeddings ───────────────────────────
    print(f"\n🔢 Step 5/6: Generating embeddings...")
    embeddings = get_embeddings(embedding_texts)
    print(f"   {len(embeddings):,} embeddings generated ({EMBEDDING_DIMS}d)")

    # ── Step 6: Upload ────────────────────────────────────────
    print(f"\n📤 Step 6/6: Uploading to '{active_index}'...")
    total = upload_chunks(chunks, embeddings)

    # ── Cache clear ───────────────────────────────────────────
    if fresh:
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _project_root = str(
                _Path(__file__).resolve().parent.parent
            )
            if _project_root not in _sys.path:
                _sys.path.insert(0, _project_root)
            from core.cache import get_cache
            cache = get_cache()
            cache.clear()
            log.info(
                "cache_cleared_post_reindex",
                reason="full_reindex_completed",
            )
            print("\n🗑️  Semantic cache cleared (full re-index)")
        except Exception as e:
            log.warning(
                "cache_clear_failed_post_reindex",
                error=str(e),
                note="Index valid. Cache expires via TTL.",
            )
            print(f"\n⚠️  Cache clear failed (non-fatal): {e}")

    # ── Summary ───────────────────────────────────────────────
    hqa_questions_total = sum(
        len(c.get("augmented_questions", "").split("\n"))
        for c in chunks
        if c.get("augmented_questions", "").strip()
    )
    title_questions_total = sum(
        len(c.get("title_questions", "").split("\n"))
        for c in chunks
        if c.get("title_questions", "").strip()
    )
    first_chunk_total = sum(
        1 for c in chunks if c.get("chunk_index", -1) == 0
    )
    print("\n" + "=" * 60)
    print("✅ INDEXING COMPLETE!")
    print("=" * 60)
    print(f"   Strategy:           {'Baseline (title only)' if no_hqa else 'Full HQA + title_questions'}")
    print(f"   Pages indexed:      {len(pages_to_index):,}")
    print(f"   Chunks created:     {len(chunks):,}")
    print(f"   Chunks uploaded:    {total:,}")
    print(f"   HQA questions:      {hqa_questions_total:,}{' (skipped)' if no_hqa else ''}")
    print(f"   Title questions:    {title_questions_total:,} ({first_chunk_total:,} first-chunks){' (skipped)' if no_hqa else ''}")
    print(f"   Index name:         {active_index}")
    print(f"   Semantic config:    {SEMANTIC_CONFIG_NAME}")
    print(f"   Scoring profile:    rl-retrieval-profile")
    print(f"   Embedding model:    {EMBEDDING_DEPLOYMENT}")
    if not no_hqa:
        print(f"   HQA model:          {HQA_DEPLOYMENT}")
    if fresh:
        print(f"   Cache cleared:      ✅ Yes (--full mode)")
    else:
        print(f"   Cache cleared:      ⏭️  Skipped (--new-only mode)")
    if no_hqa:
        print(
            f"\n   👉 Now run compare_indexes.py against"
            f"\n      {INDEX_NAME} (HQA) vs"
            f"\n      {active_index} (baseline)"
            f"\n      to measure whether HQA improves retrieval."
        )
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()