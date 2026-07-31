# ADO PBI Details — Expanded for Audit

---

## 607365 — Scrape Targeted URLs

**Description**
Build the initial scraping capability to extract content from the set of Royal London Group web pages approved by the customer for use as ARIA's knowledge source. Scraping runs via crawl4ai/Playwright in CDP mode against system Chrome on the VDI (required due to corporate SSL restrictions). Output is structured JSON per page, forming the raw input for downstream chunking and indexing. Only URLs explicitly approved and shared by the customer are in scope — no discovery or crawling beyond the approved list.

**Acceptance Criteria**
- Scraper processes only URLs present in the customer-approved URL list (Excel source in Azure Blob Storage).
- No URLs outside the approved list are scraped or included in output.
- Each scraped page produces a valid JSON output with page content, title, and source URL preserved.
- Scrape run tested on a subset before full run, per team convention.
- Output validated for completeness (no empty/failed pages) before handoff to chunking.

---

## 609569 — AI Search - Chunking

**Description**
Implement the chunking pipeline that takes the scraped page JSON and splits it into retrieval-sized chunks, then stores those chunks in Azure AI Search (index) and Azure Blob Storage. Chunking at this stage uses `RecursiveCharacterTextSplitter` over the full page text; this is the baseline pipeline prior to element-aware routing (tables/prose separation handled in a later PBI).

**Acceptance Criteria**
- Scraped JSON content is chunked and each chunk is written to both Azure AI Search index and Blob Storage.
- Chunks are retrievable via Azure AI Search query (spot-checked).
- No data loss between source JSON and stored chunks (chunk count/content sanity-checked).
- Pipeline runs without errors end-to-end on the scraped dataset.

---

## 609570 — AI Search - Re-indexing

**Description**
Re-index the newly scraped/updated dataset into Azure AI Search so it serves as the up-to-date knowledgebase for the ARIA chatbot. Triggered after a fresh scrape/chunk cycle, replacing or updating the existing index content with the latest approved-URL data.

**Acceptance Criteria**
- Final scraped JSON is re-indexed into Azure AI Search without manual intervention beyond running the pipeline.
- Re-indexed content is accessible and retrievable via code (API/SDK query against the index).
- Index reflects the latest scrape — no stale/orphaned entries from prior versions remain unaddressed.
- Retrieval spot-check confirms indexed content matches source JSON.

---

## 609568 — AI Search - Re-scraping the Targeted URLs

**Description**
Re-run the scraping process against the customer-approved URL list to refresh content ahead of re-indexing (609570). Ensures all approved URLs are valid, reachable, and fully captured — this is the data-freshness step that feeds the re-index cycle.

**Acceptance Criteria**
- Every URL scraped is approved and valid (present in and matching the current approved URL list).
- No external/non-approved URLs are scraped.
- All approved URLs are scraped without missing content — final JSON output includes all approved URLs and their full page content.
- Output JSON validated as complete before being passed to re-indexing.

---

## 633945 — Element-Aware Chunking *(current sprint)*

**Description**
Extend `chunk_and_index_hqaV4.py` to route content by element type instead of applying a single flat text splitter to entire pages. Element type is determined using output from `analyse_page_structure.py`. Tables are chunked atomically (~30 rows per chunk via `TABLE_ROWS_PER_CHUNK`), while prose and headers continue through the existing `RecursiveCharacterTextSplitter` (1600/200 chunk size/overlap). This addresses the root cause of near-duplicate chunk proliferation on `fund-changes`, `historic-fund-changes`, and webinar pages, where flat splitting was producing hundreds of near-identical chunks.

**Acceptance Criteria**
- Chunking pipeline routes by element type (table vs. prose/header) based on `analyse_page_structure.py` output.
- Tables are chunked atomically at ~30 rows per chunk (`TABLE_ROWS_PER_CHUNK=30`); no table row is split mid-chunk.
- Prose and headers continue to use `RecursiveCharacterTextSplitter` with existing 1600/200 settings.
- Applied to target pages: `fund-changes`, `historic-fund-changes`, and webinar pages.
- Duplicate chunk count measurably reduced, verified via `audit_duplicates.py`.
- AST check (`ast.parse`) and schema check (`audit_schema_types.py`) pass on all modified files.
- `PIPELINE_VERSION` bumped and changelog entry added with rollback notes.
- No changes made to frozen v3 indexes.

---

## 633957 — Content Safety Integration Testing *(current sprint)*

**Description**
The Azure Content Safety endpoint has been newly deployed under the Azure resource group in the VDI environment and is currently untested end-to-end. This task verifies that `input_safety.py` and `output_safety.py` correctly call the live deployed endpoint (not just local regex-based rule checks), and confirms all three safety invariants (terminal illness cache bypass, PII canonical rewrite, recommendation trigger cache bypass) hold when exercised against the live endpoint rather than mocked/local logic.

**Acceptance Criteria**
- `input_safety.py` and `output_safety.py` successfully call the live Azure Content Safety endpoint (confirmed via request/response, not local regex fallback).
- All three safety invariants (terminal illness cache bypass, PII canonical rewrite, recommendation trigger cache bypass) verified to hold end-to-end against the live endpoint.
- Integration test suite passes against the deployed endpoint.
- No regression in existing local safety unit tests.
- RBAC access (`Cognitive Services User` role) confirmed working for the calling identity.

---
*Redis integration testing remains explicitly deferred to a later sprint and is not covered by 633957.*
