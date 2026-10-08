# PBI — Model Evaluation Framework

**Suggested Title:** Model Evaluation Framework
**Area:** Digital Assistant\Digital Assistant Team
**Iteration:** Digital Assistant\Backlog\Sprint [current]

---

## Description

Build an end-to-end evaluation framework for the Digital Assistance RAG pipeline, covering golden dataset creation through automated metrics reporting. Currently there is no systematic way to measure answer quality, retrieval accuracy, or regression risk when models, prompts, or chunking logic change. This framework will establish a repeatable evaluation loop: a curated golden Q&A dataset (grounded in the 297 approved URLs), automated scoring against retrieval and generation metrics (groundedness, relevance, faithfulness, latency), and a dashboard/report output styled on Azure AI Foundry evaluation metrics. This was previously deprioritised behind latency and structured-output work and is now being picked up.

## Acceptance Criteria

- Golden dataset of representative Q&A pairs exists, covering pensions, ISAs, equity release, and other in-scope product areas, with expected/ground-truth answers and source URLs.
- Evaluation script runs the golden dataset end-to-end through the pipeline (retrieval + generation) and produces scores per query.
- Metrics captured at minimum: retrieval accuracy/groundedness, answer relevance, faithfulness (no hallucination vs. source), and latency.
- Results are exportable/viewable in a dashboard or report format (Azure AI Foundry-style), not just raw console output.
- Framework can be re-run on demand to benchmark before/after changes (e.g. model swap, chunking change, prompt change) for regression comparison.
- Evaluation run against both v4 and v5 indexes supported (frozen v3 excluded — not an active target).

---

## Task Breakdown

### Task 1 — Golden Dataset Creation

**Description:** Curate a representative set of Q&A pairs across all in-scope product areas (pensions, ISAs, equity release, etc.), each with an expected answer and source URL for traceability. Reuse/extend existing `aria_sprint1_test_queries.xlsx` as a starting point where applicable.
**Acceptance Criteria:** Dataset stored in a structured, version-controlled format (e.g. JSON/CSV); each entry has question, expected answer, source URL, and product category; reviewed for coverage across approved URL topics.

### Task 2 — Retrieval Evaluation Metrics

**Description:** Implement scoring for retrieval quality — whether the correct chunk(s)/source URL are retrieved for a given golden question, and how relevant the retrieved context is.
**Acceptance Criteria:** Script computes retrieval hit-rate and relevance score per query against golden dataset; results logged per query, not just aggregate.

### Task 3 — Generation Evaluation Metrics

**Description:** Implement scoring for generated answer quality — faithfulness/groundedness against retrieved context (no hallucination), and relevance against the expected answer.
**Acceptance Criteria:** Script computes faithfulness and relevance/similarity score per query; flags answers that deviate from source content (potential hallucination).

### Task 4 — Latency & Performance Capture

**Description:** Capture end-to-end latency per query (retrieval + generation) during evaluation runs to track performance alongside quality.
**Acceptance Criteria:** Per-query and aggregate (p50/p95) latency captured and included in output report.

### Task 5 — Metrics Dashboard / Report Output

**Description:** Produce a consolidated, readable output (dashboard or generated report) summarising evaluation run results — styled on Azure AI Foundry evaluation metrics presentation.
**Acceptance Criteria:** Single report/dashboard artifact generated per run showing aggregate scores, per-category breakdown, and pass/fail or trend indicators; runs are comparable across time (before/after change).

### Task 6 — Regression/Benchmark Workflow

**Description:** Wire the framework so it can be run on-demand to benchmark a change (model swap, chunking update, prompt change) against a prior baseline run.
**Acceptance Criteria:** Two evaluation runs can be diffed/compared; regressions in key metrics are clearly surfaced.

---

_Note: Effort/Priority/Business Value to be set in ADO per team's standard sizing during sprint planning._

---

# PBI — Offline RAG Pipeline Scripts (Scrape, Index, Freshness)

**Suggested Title:** Offline RAG Pipeline Scripts – Development and Validation
**Area:** Digital Assistant\Digital Assistant Team
**Iteration:** Digital Assistant\Backlog\Sprint [current]

---

## Description

Build three standalone, production-grade offline scripts that refresh the Aria knowledge base from the 287 approved URLs: a scraper (Excel → JSON), a chunk/embed/index script (JSON → Azure AI Search), and a content freshness script (live pages vs. index, report and apply modes). The scripts are HTTP-only (no browser), have no cross-file imports, and are designed to be hosted later as the 3 Azure Function Apps. Every script needs retry, logging and exception handling, safety gates that prevent a bad run from damaging the index, and hash parity between scraper, indexer and freshness. They must also carry the B&M "Page Purpose" label (Information / Action / Directional / Reassurance / Engagement) through to the index, and handle thin JS-rendered pages (e.g. the pension planning calculator). The existing V5 scripts remain untouched, and the index must stay safe for other teams who consume it. Function App provisioning and deployment are out of scope and tracked in a separate PBI.

## Acceptance Criteria

- Three scripts run standalone from the command line on the VDI, configured via `.env`, with no cross-file imports.
- `content_hash` is identical across scraper, indexer and freshness for the same page.
- Transient failures retry with exponential backoff and jitter, and every attempt is logged. Fatal errors exit 1 and partial runs exit 2.
- Safety gates are in place: scrape failure ratio above 5% fails the run, a shrink gate stops an under-sized rebuild, a mass-removal breaker limits removals, and index scan completeness, upload verification and post-load count checks pass.
- `Page Purpose` flows from Excel to JSON, chunks and index, and is filterable and facetable.
- Thin pages with a label other than Information fall back to the meta description and are logged. Information pages still fail visibly.
- Validation on the 25-URL sample index passes, including freshness apply timing and `refresh_count` increments.
- Full-scale run on 287 unique URLs gives a scrape with 0 failed, a clean `--full` index build and a freshness report with 0 changed.
- Offline tests pass and pyflakes is clean on all three scripts.

---

## Task Breakdown

### Task 1 — Scraper (Excel → JSON)

**Description:** Build the HTTP-only scraper that reads the approved-URL Excel, extracts main content, metadata and dropdown/tab entries, and writes scraper JSON. De-duplicate pages on `dropdown_url` / `source_url`.
**Acceptance Criteria:** Produces valid JSON for all approved URLs; duplicate pages dropped with a warning; dropdown pages emit one entry per dropdown.

### Task 2 — Chunk, Embed and Index Script

**Description:** Build the script that chunks the scraper JSON, generates embeddings, and loads the Azure AI Search index, with deterministic chunk IDs and `--full` and incremental modes.
**Acceptance Criteria:** Index schema attributes (searchable / filterable / sortable / facetable) reviewed against consumers; `--full` rebuild completes; chunk counts verified after load.

### Task 3 — Content Freshness Script

**Description:** Build the freshness script that compares live pages with the index. Report mode lists changed / new / removed pages. Apply mode refreshes changed chunks and increments `refresh_count`.
**Acceptance Criteria:** Report shows 0 changes on an unchanged site; apply updates only the changed URLs; `refresh_count` increments correctly across rounds.

### Task 4 — Retry, Logging and Exception Handling

**Description:** Add production-grade resilience to all three scripts: retry with backoff and jitter for transient errors, structured logging of every attempt, exit codes (1 fatal, 2 partial), atomic file saves, and the safety gates listed above.
**Acceptance Criteria:** No silent failures; each gate triggers on a simulated failure; offline resilience tests pass for all three scripts.

### Task 5 — Freshness Apply Optimisation

**Description:** Replace the per-URL full-index scan in apply mode with a single pre-write index snapshot. Chunk and embed before deleting old chunks, and verify deletes and uploads.
**Acceptance Criteria:** Apply of 10 changed URLs is measurably faster than the 201s baseline. A failed delete skips the upload for that URL.

### Task 6 — Page Purpose Field

**Description:** Read the "Page Purpose" column from the Excel like title and category, pass it through JSON and chunks, and add the `page_purpose` index field (filterable, facetable). Add a coverage check script that compares Excel URLs with the scrape output.
**Acceptance Criteria:** All rows carry the label; a missing column logs an error without crashing; a facet query on `page_purpose` returns the expected counts.

### Task 7 — Thin-Page Fallback

**Description:** For pages with too little text (JS-rendered tools such as the pension planning calculator), use the meta description when the label is not Information. Apply the same rule in the scraper and freshness.
**Acceptance Criteria:** Scraper and freshness hashes are equal for fallback pages; Information pages and pages without a description still fail; each fallback is logged.

### Task 8 — Offline Tests

**Description:** Write offline tests for resilience behaviour, `page_purpose` propagation, and the thin-page fallback using the saved calculator HTML.
**Acceptance Criteria:** All tests pass; pyflakes is clean on all three scripts.

### Task 9 — Validation on the 25-URL Sample Index

**Description:** Run the scripts end to end on the VDI against a 25-URL sample index, covering scrape, `--full` build, freshness report, and apply rounds.
**Acceptance Criteria:** Freshness apply timing recorded (201s → 59.7s); `refresh_count` increments as expected; results documented.

### Task 10 — Full-Scale Validation (287 URLs)

**Description:** Run the scraper, `--full` index build, `page_purpose` facet query and freshness report on all 287 approved URLs into a new index.
**Acceptance Criteria:** Scrape reports 0 failed; index build completes with verified counts; freshness report shows 0 changed / new / removed.

### Task 11 — Documentation

**Description:** Document the script design, run commands, safety gates, validation timings and open items in `state.md` and the architecture doc.
**Acceptance Criteria:** Documents reflect final behaviour, including the 287-URL scope and the scraper and freshness timings.
