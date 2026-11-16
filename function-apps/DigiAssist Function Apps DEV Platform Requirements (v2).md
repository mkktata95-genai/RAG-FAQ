# DigiAssist Function Apps: DEV Platform Requirements (v2)

Supersedes the earlier spec shared on 22/10. Status as of 16 Nov 2026. Items marked **TBC** need DevOps or code-owner confirmation.

## 1. Summary of changes from v1

- Added outbound networking requirements (VNet integration and private DNS). This is the cause of the current 403 `AuthorizationFailure`.
- Added the full app settings list.
- Removed `index-promote` queue. The platform team owns the index, and our apps are data-plane only.
- Removed Search Service Contributor from the indexer.
- Python runtime confirmed as 3.11 (pipeline `pythonVersion` changed to 3.11).

## 2. Storage

|  | Host storage (`rlgdevdaukssa001`) | Data storage (**TBC**: same account or separate) |
| --- | --- | --- |
| Purpose | Functions host state (`AzureWebJobsStorage`), queues, timer locks | Pipeline data: input Excel, scraped JSON, freshness reports |
| Kind | StorageV2, Standard, LRS (DEV) | Same |
| Hierarchical namespace | Off | Off |
| Public network access | Disabled, TLS 1.2 | Same |
| Shared key access | Disabled (identity-based only) | Same |
| Private endpoints | blob, queue, table | blob (+ queue if used) |
| Containers | Host-created `azure-webjobs-*` | `input`, `output` (must exist) |
| Queues | `scrape-requests`, `index-requests`, `freshness-requests` (+ poison queues, templated queues TBC) | None |

Folders (created by the apps):

| Container | Folder | Used by |
| --- | --- | --- |
| input | `approved-urls/` | Scraper blob trigger |
| input | `templated-questions/` | Templated app blob trigger |
| output | `scraped-data/` | Scraper writes JSON; indexer triggers on it |
| output | `scraper-rejected/` | Scraper validator (block mode) |
| output | freshness reports, run manifests, deleted-chunk archives | Freshness (exact paths **TBC**) |

Lifecycle: delete blobs under `output/scraped-data/` older than 30 days. Keep freshness archives longer (e.g. 180 days, **TBC**).

## 3. Function Apps

Common to all apps: Python 3.11, Linux, shared App Service plan (P1v3), `functionTimeout` -1, system-assigned managed identity, **Always On = On**, `WEBSITE_RUN_FROM_PACKAGE=1`, `ENABLE_ORYX_BUILD=false`, `SCM_DO_BUILD_DURING_DEPLOYMENT=false`. Jobs run one at a time.

| App | Triggers | Typical run |
| --- | --- | --- |
| Scraper | Blob `input/approved-urls/*`; queue `scrape-requests` | 4 to 5 min |
| Indexer (`rlg-dev-da-uks-func-indexer`) | Blob `output/scraped-data/{name}`; queue `index-requests` | about 8 min |
| Freshness | Timer (`%FRESHNESS_SCHEDULE%`, time TBC); queue `freshness-requests` | about 4 min report, up to 11 min with changes |
| Templated-Questions | Blob `input/templated-questions/*.xlsx`; build queues (names TBC) | short |

Open decision: scraper and freshness may be merged into one app (`webingest`). If agreed, the Scraper and Freshness rows and roles merge into a single app.

## 4. Networking (required)

Both storage accounts have public access disabled, so each app needs:

1. **VNet integration** (outbound) on a delegated subnet in `rlg-dev-aia-uks-vnet-core`, with a route to the storage private endpoints.
2. **Private DNS zones** linked to that VNet: `privatelink.blob.core.windows.net`, `privatelink.queue.core.windows.net`, `privatelink.table.core.windows.net`.
3. Private connectivity to Azure AI Search and Azure OpenAI (private endpoints or allowed network path), **TBC**.
4. **Freshness and Scraper only:** outbound internet access to `royallondon.com`. This needs a firewall or NAT exception. **TBC** which app and subnet carries it.
5. Inbound private endpoint per app already exists (indexer: `rlg-dev-da-uks-ple-func-indexer`).

Current symptom: indexer app shows `Azure.Storage.Blobs 403 AuthorizationFailure` (16 Nov 2026, 07:23 UTC). Points 1 and 2 are the expected fix.

## 5. App settings

Identity-based storage settings (host) are already present: `AzureWebJobsStorage__accountName`, `__blobServiceUri`, `__queueServiceUri`, `__tableServiceUri`.

### Indexer

| Setting | Value |
| --- | --- |
| `DataStorage__blobServiceUri` | Blob endpoint of the data storage account (holds `output`) |
| `AZURE_SEARCH_ENDPOINT` | Dev Search endpoint (**required**) |
| `AZURE_SEARCH_INDEX_NAME` | Platform-created index name (**required**) |
| `AZURE_OPENAI_ENDPOINT` | Dev Azure OpenAI endpoint (**required**) |
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | Must equal the index vector field dimensions (**required**) |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Deployment name of `text-embedding-3-large` |
| `AZURE_SEARCH_SEMANTIC_CONFIG` | Semantic configuration name on the index |

The indexer fails at start-up of a run if any of the four required settings is missing.

### Scraper and Freshness

| Setting | Value |
| --- | --- |
| `DataStorage__blobServiceUri` | Blob endpoint of the data storage account |
| `FRESHNESS_SCHEDULE` | CRON expression for the daily run (Freshness, time TBC) |
| Search and OpenAI settings | Same names as the indexer (Freshness only) |

Full list for each app will be confirmed against its `local.settings.json.example` and shared separately.

The platform template settings (`CONTENT_SAFETY_ENDPOINT`, `REDIS_HOST`, `FOUNDRY_AZURE_OPENAI_ENDPOINT`, `OTEL_CONNECTION_STRING`, `SEARCH_ENDPOINT`) are not read by our code. Keep or remove as platform policy requires.

## 6. RBAC (system-assigned identity per app)

| Scope | Scraper | Indexer | Freshness | Templated |
| --- | --- | --- | --- | --- |
| Host storage | Blob Data Owner, Queue Data Contributor, Table Data Contributor | Same | Same | Same |
| Data storage | Blob Data Contributor | Blob Data Reader | Blob Data Contributor | Blob Data Reader (**TBC**: Contributor if it writes) |
| AI Search | None | Search Index Data Contributor | Search Index Data Contributor | Search Index Data Contributor |
| Azure OpenAI | None | Cognitive Services OpenAI User | Cognitive Services OpenAI User | Cognitive Services OpenAI User |
| Redis | None | None | TBC | None |

No app needs control-plane Search roles. Index creation, deletion, aliasing and promotion are owned by the platform team. Our apps only read and write documents in the existing index.

## 7. Open items for DevOps

1. Fix indexer 403 (sections 4 and 5), then confirm the Functions list and blob trigger work.
2. Confirm which storage account is the data account.
3. Supply Search endpoint, index name, OpenAI endpoint, embedding deployment, and dimensions.
4. Confirm outbound egress approach for `royallondon.com`.
5. Confirm app count: four separate apps, or scraper and freshness merged.
6. Enable Always On on all apps.
7. Confirm freshness schedule time and exact report paths.
