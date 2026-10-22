# Infra requirements: 4 Python Function Apps (Scraper, Indexer, Freshness, Templated-Questions) — DEV, new subscription

Owner: Mukesh Kund. Code: new repo `<repo name>`, branch `dev` (zips are pre-built and committed; no internet at runtime).
Everything below is the target state for the NEW subscription, including a NEW AI Search service and a NEW Azure OpenAI account (empty; our pipelines populate them). Names are proposals, change to your convention. TBC = not final.

## 1. Apps
| | Scraper | Indexer | Freshness | Templated-Questions (new, TBC) |
|---|---|---|---|---|
| Name (proposal) | rlg-dev-dig-uks-func-scraper-aia | rlg-dev-dig-uks-func-indexer-aia | rlg-dev-dig-uks-func-freshness-aia | rlg-dev-dig-uks-func-templated-aia |
| Runtime | Python 3.11, Linux | same | same | same |
| Plan | one shared Linux App Service plan, P1v3 (P0v3 acceptable); fallback one plan per app. Jobs run one at a time, not in parallel. | | | |
| Triggers | blob `input/approved-urls/*.xlsx`, queue `scrape-requests` | blob `output/scraped-data/*.json`, queues `index-requests`, `index-promote` | TIMER, once a day at a fixed time (schedule TBC) | blob `input/templated-questions/*.xlsx`, plus queues for build/promote (names TBC) |
| Typical run | ~4-5 min | ~8 min | ~4 min report, up to ~11 min with changes applied | short (Excel of questions + embeddings) |
| Timeout | functionTimeout -1 (long jobs; needs an App Service plan, not Consumption) | same | same | same |
| Identity | system-assigned managed identity per app (no keys or connection strings anywhere) | | | |
| Config | **Always On = ON** (needed for timer and blob triggers on a dedicated plan) | | | |
| Deploy | zip deploy from ADO (`az functionapp deploy --type zip`), `WEBSITE_RUN_FROM_PACKAGE=1`, `ENABLE_ORYX_BUILD=false`, `SCM_DO_BUILD_DURING_DEPLOYMENT=false` | | | |

## 2. Network (all apps)
- VNet with a subnet delegated to `Microsoft.Web/serverFarms` for VNet integration (/27 or larger), route all outbound through the VNet.
- Inbound public access DISABLED on each app; private endpoint (sites) + private DNS `privatelink.azurewebsites.net` and an A record for `<app>.scm.azurewebsites.net` (SCM/Kudu) so ADO can deploy. The timer-triggered Freshness app needs no other inbound access.
- Firewall/NSG: ADO agent subnet -> app SCM private endpoints on 443 (zip deploy).
- Private endpoints reachable from the integration subnet for: host storage (blob, queue, TABLE), data storage (blob; queue as a precaution), AI Search, Azure OpenAI, and Redis (Freshness, see section 4).
- No general internet egress needed (libraries are bundled in the zip). QUESTION: the blob/queue/timer triggers need the Functions extension bundle (`Microsoft.Azure.Functions.ExtensionBundle` [4.*, 5.0.0)). Is it pre-installed in the Linux image for your setup, or does it need egress to the Functions CDN? First deploy will show; if it fails we change packaging.
- **Scraper and Freshness only:** outbound HTTPS to the public Royal London website (`www.royallondon.com`) is required (they fetch the live pages). Please allow it through the firewall/NAT.

## 3. Storage accounts (2)
| | Host storage | Data storage |
|---|---|---|
| Purpose | Functions host state (`AzureWebJobsStorage`), queues, timer locks | Pipeline data: input Excel files, scraped JSON, freshness reports |
| Kind / perf | StorageV2, Standard | StorageV2, Standard |
| Redundancy | LRS (DEV) | LRS (DEV) |
| Hierarchical namespace | OFF (required) | OFF (simplest) |
| Min TLS / public access | TLS 1.2, public network access disabled | same |
| Shared key access | can be disabled (identity-based access only) | same |
| Private endpoints | blob, queue, TABLE | blob (+ queue) |
| Containers / queues | host creates `azure-webjobs-*` containers and the queues `scrape-requests`, `index-requests`, `index-promote` (+ poison queues, + templated queues, TBC) | containers `input` and `output` (must exist). Apps create the folders: `input/approved-urls/`, `input/templated-questions/`, `output/scraped-data/`, `output/scraper-rejected/`, and the Freshness paths for reports, run manifests and archives of deleted chunks (exact paths TBC) |
| Lifecycle | n/a | rule: delete blobs under `output/scraped-data/` older than 30 days (JSONs accumulate; a new app also re-processes old ones on first start). Keep Freshness archives longer (rollback evidence), e.g. 180 days (TBC). |

## 4. AI Search, Azure OpenAI, Redis
- **AI Search** (new service, uks): semantic ranker ENABLED (`rlg-semantic-config` is used), public access disabled + private endpoint. Please tell us the SKU/replicas. It starts EMPTY. Indexer builds timestamped indexes (`rlg-faq-index-<timestamp>`, 1536-dim vectors) and a manual promote points the alias `rlg-faq-live` at one. The Templated-Questions app builds a SECOND index on the same service (`rlg-faq-templated-<timestamp>`, alias `rlg-faq-templated-live`).
- **Azure OpenAI** (new account): embedding deployment named `text-embedding-3-large` (we call it with dimensions=1536) with enough TPM for ~600 embeddings per build. Freshness may also need a small chat deployment (e.g. `gpt-4o-mini`) for generated question text (TBC, depends on the script version). Private endpoint reachable from the integration subnet.
- **Redis (TBC):** Freshness invalidates cached answers for changed pages, so it must reach the query service's Redis cache. QUESTION: is Redis also being created in the new subscription? If yes: private endpoint, Entra ID (managed identity) access for the Freshness app, or give us the secret reference you prefer.
- QUESTION: does the agent/query service also move to the new subscription? The query side must read the alias `rlg-faq-live` (and the templated alias) instead of an index name; one-time config change, owner TBC.

## 5. RBAC (app managed identities; use roles, not keys)
| Scope | Scraper | Indexer | Freshness | Templated-Questions |
|---|---|---|---|---|
| Host storage | Blob Data Owner, Queue Data Contributor, Table Data Contributor | same | same | same |
| Data storage | Blob Data Contributor | Blob Data Reader | Blob Data Contributor (writes reports/archives) | Blob Data Reader |
| AI Search | - | Search Service Contributor + Search Index Data Contributor | Search Index Data Contributor (reads and updates the live index, and removes entries for discarded URLs from the templated index; the role is service-wide, so one assignment covers both indexes) | Search Service Contributor + Search Index Data Contributor |
| Azure OpenAI | - | Cognitive Services OpenAI User | Cognitive Services OpenAI User | Cognitive Services OpenAI User |
| Redis | - | - | data access to invalidate keys (TBC) | - |

Users (Mukesh): host storage `Storage Queue Data Message Sender` (to send run/promote messages), data storage `Storage Blob Data Contributor`, Reader + Monitoring Reader on the apps and App Insights.

## 6. App settings (per app; storage uses identity-based connections)
Common: `FUNCTIONS_WORKER_RUNTIME=python`, `AzureWebJobsStorage__accountName`, `AzureWebJobsStorage__blobServiceUri`, `AzureWebJobsStorage__queueServiceUri`, `AzureWebJobsStorage__tableServiceUri` (host account), `DataStorage__blobServiceUri`, `DataStorage__queueServiceUri` (data account), `APPLICATIONINSIGHTS_CONNECTION_STRING` (or the identity-based equivalent), `WEBSITE_RUN_FROM_PACKAGE=1`, `ENABLE_ORYX_BUILD=false`, `SCM_DO_BUILD_DURING_DEPLOYMENT=false`.
Indexer extra: `AZURE_SEARCH_ENDPOINT`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large`, `AZURE_OPENAI_EMBEDDING_DIMENSIONS=1536`, `AZURE_SEARCH_SEMANTIC_CONFIG=rlg-semantic-config`, `SEARCH_ALIAS_NAME=rlg-faq-live`.
Freshness extra: same Search and OpenAI settings, plus the templated alias name `rlg-faq-templated-live`, a schedule setting (NCRONTAB, e.g. `0 0 2 * * *`) and `WEBSITE_TIME_ZONE=Europe/London`; the run time of day is TBC (Mukesh to confirm), Redis settings TBC.
Templated-Questions extra: Search and OpenAI settings as the Indexer, `SEARCH_ALIAS_NAME=rlg-faq-templated-live` (rest TBC).

## 7. Monitoring
- One Log Analytics workspace + Application Insights (workspace-based) for all apps; diagnostic settings on the apps.
- Alerts: function failures > 0 (all apps); poison queues (`scrape-requests-poison`, `index-requests-poison`, `index-promote-poison`, `webjobs-blobtrigger-poison`, templated poison queues) count > 0; scraper log `SCRAPE_PARTIAL` or `SCRAPE_REJECTED`; indexer `PROMOTE_REFUSED`; run duration > 30 min; Freshness timer did not run in 26 h.
- Audit traces (info): `INDEX_BUILT`, `INDEX_PROMOTED`.

## 8. Azure DevOps
- Pipelines (manual trigger) registered from the NEW repo: `Pipelines/azure-pipelines-scraper-func.yml`, `azure-pipelines-indexer-func.yml` (freshness and templated to follow). Variables per app in `Pipelines/env/dev.yml`.
- A service connection with permission to deploy to the 4 apps (Website Contributor on each app is enough) and an agent pool that can reach the app SCM private endpoints.

## 9. Data to carry over / first steps
- Upload the approved-URLs Excel to `input/approved-urls/` in the new data storage (the scraper's input).
- Nothing else to migrate: indexes are rebuilt by the pipelines.
- Please send us the final resource names, endpoints and subscription/resource group when ready, and confirm the open questions: extension bundle, shared plan, Redis in the new subscription, OpenAI region/capacity.
