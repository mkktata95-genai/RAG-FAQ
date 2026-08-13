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
*Note: Effort/Priority/Business Value to be set in ADO per team's standard sizing during sprint planning.*
