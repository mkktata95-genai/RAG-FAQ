# DigiAssist Function Apps: DEV Requirements (v3, two apps)

The four jobs run in **two** Function Apps, grouped by network profile. Status as of 17 Nov 2026. Items marked **TBC** need DevOps or code-owner confirmation.

## 1. Proposal

| App | Repo | Jobs inside | Network profile |
| --- | --- | --- | --- |
| **Web ingest** | `digiassist-webingest-functionapp` | Scraper + Freshness | Needs outbound internet to `royallondon.com` |
| **Indexing** | `digiassist-indexing-functionapp` | Indexer + Templated-Questions | Internal only (storage, Search, OpenAI) |

Why this grouping: only one app needs an internet firewall exception, and the other never leaves the private network. The scripts themselves are unchanged. Only the hosting layer (`function_app.py`) is merged.

App names (proposed, following the existing `rlg-dev-da-uks-` pattern): `rlg-dev-da-uks-func-webingest` for web ingest and `rlg-dev-da-uks-func-indexing` for indexing. **TBC:** DevOps to confirm the naming standard, and whether the four apps already created in DEV are reused or retired.

## 2. Common platform settings (both apps)

- Python **3.11**, Linux, shared App Service plan (P1v3), `functionTimeout` -1, **Always On = On**.
- System-assigned managed identity. No keys or connection strings.
- **Application Insights** (workspace-based), connected to each app. Needed for run logs, failure alerts and troubleshooting.
- `WEBSITE_RUN_FROM_PACKAGE=1`, `ENABLE_ORYX_BUILD=false`, `SCM_DO_BUILD_DURING_DEPLOYMENT=false`.
- Platform pipeline `pipelines/function-app.yml@templates` (tag v0.1.1), `pythonVersion` "3.11", one pipeline YAML per repo, one `appName` per repo.
- `host.json` (same in both): queue `batchSize` 1, `newBatchThreshold` 0, `maxDequeueCount` 2, `messageEncoding` none, blob `maxDegreeOfParallelism` 1.
- Our apps are data-plane only on Azure AI Search. Index creation, deletion, aliasing and promotion are owned by the platform team.
- **Index schema:** the platform creates both indexes from the schema JSON already shared with Roshan. Each run first checks that the index has every field the code needs (`REQUIRED_FIELDS` in `src/chunk_embed_index_funcV1.py` and `src/templated_job.py`) and stops if one is missing. The vector fields are 1536 dimensions in the new landing zone. The templated index needs `id`, `question`, `question_vector` and `response_json`.
- **One instance per app:** keep each app at a fixed instance count of 1, with no scale-out. The one-job-at-a-time lock lives inside the process, so two instances could run two syncs at once and overwrite each other.

## 3. Functions

| App | Function | Trigger | Typical run |
| --- | --- | --- | --- |
| Web ingest | `scrape_on_excel` | Blob `input/approved-urls/{name}` | 4 to 5 min |
| Web ingest | `scrape_on_request` | Queue `scrape-requests` | 4 to 5 min |
| Web ingest | `freshness_daily` | Timer `%FRESHNESS_SCHEDULE%` (21:00 UK time) | about 4 min report, up to 11 min with changes |
| Web ingest | `freshness_on_request` | Queue `freshness-requests` | same |
| Indexing | `index_on_scrape` | Blob `output/scraped-data/{name}` | about 8 min |
| Indexing | `index_on_request` | Queue `index-requests` | about 8 min |
| Indexing | `templated_on_upload` | Blob `input/templated-questions/{name}` | short |
| Indexing | `templated_on_request` | Queue `templated-requests` | short |

The blob triggers use the connection name `DataStorage`. The queue triggers use `AzureWebJobsStorage`.

`FRESHNESS_SCHEDULE` is an NCRONTAB expression. The daily run is at 21:00 UK time: set it to `0 0 21 * * *` and set `WEBSITE_TIME_ZONE` to `Europe/London` on the web ingest app, so the run stays at 21:00 UK time through the clock changes. Without the time zone setting it runs in UTC, which is 22:00 UK time in summer.

## 4. Storage

|  | Host storage (`rlgdevdaukssa001`) | Data storage (**TBC**: same account or separate) |
| --- | --- | --- |
| Purpose | Functions host state, queues, timer locks | Input Excel, scraped JSON, freshness reports |
| Public network access | Disabled, TLS 1.2 | Same |
| Shared key access | Disabled (identity only) | Same |
| Private endpoints | blob, queue, table | blob (+ queue if used) |
| Containers | Host-created `azure-webjobs-*` | `input`, `output` (must exist) |
| Queues | `scrape-requests`, `freshness-requests`, `index-requests`, `templated-requests` (+ poison queues) | None |

Folders (created by the apps):

| Container | Folder | Written by | Read by |
| --- | --- | --- | --- |
| input | `approved-urls/` | Users | Scraper |
| input | `templated-questions/` | Users | Templated |
| output | `scraped-data/` | Scraper | Indexer |
| output | `scraper-rejected/` | Scraper (block mode) | Operators |
| output | freshness reports, run manifests, deleted-chunk archives | Freshness | Operators (paths **TBC**) |

Lifecycle: delete blobs under `output/scraped-data/` older than 30 days. Keep freshness archives longer (for example 180 days, **TBC**).

## 5. Networking

Both storage accounts have public access disabled, so **each of the two apps** needs:

1. **VNet integration** (outbound) on a delegated subnet in `rlg-dev-aia-uks-vnet-core`, with a route to the storage private endpoints.
2. **Private DNS zones** linked to that VNet: `privatelink.blob.core.windows.net`, `privatelink.queue.core.windows.net`, `privatelink.table.core.windows.net`.
3. Private connectivity to Azure AI Search and Azure OpenAI (**TBC**: private endpoints or allowed path).
4. An inbound private endpoint per app (the indexer's already exists as `rlg-dev-da-uks-ple-func-indexer`).

**Web ingest only:** outbound internet access to `royallondon.com` (scraper and freshness both fetch pages). This needs a firewall or NAT exception on the web ingest app's subnet. The Indexing app needs no internet access.

## 6. App settings

Host storage identity settings (`AzureWebJobsStorage__accountName`, `__blobServiceUri`, `__queueServiceUri`, `__tableServiceUri`) are already present on the existing apps. Add the same to any new app.

Both apps also need `APPLICATIONINSIGHTS_CONNECTION_STRING` (the Application Insights connection string). **TBC:** key-based or identity-based ingestion.

### Indexing app

| Setting | Value |
| --- | --- |
| `DataStorage__blobServiceUri` | Blob endpoint of the data storage account (holds `input` and `output`) |
| `AZURE_SEARCH_ENDPOINT` | Dev Search endpoint (required) |
| `AZURE_SEARCH_INDEX_NAME` | Platform-created FAQ index, `rlg-faq-index` (required, exact name TBC) |
| `AZURE_SEARCH_TEMPLATED_INDEX_NAME` | Platform-created templated index, `rlg-faq-templated-index` (required, exact name TBC) |
| `AZURE_SEARCH_SEMANTIC_CONFIG` | Semantic config name on the FAQ index |
| `AZURE_OPENAI_ENDPOINT` | Dev Azure OpenAI endpoint (required) |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Deployment name of `text-embedding-3-large` |
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | **1536**. Shared by both jobs, so both indexes must use 1536 vector dimensions |

The indexer and templated jobs fail at the start of a run if a required setting is missing.

### Web ingest app

| Setting | Value |
| --- | --- |
| `DataStorage__blobServiceUri` | Blob endpoint of the data storage account |
| `FRESHNESS_SCHEDULE` | `0 0 21 * * *` (daily at 21:00 UK time; needs `WEBSITE_TIME_ZONE` = `Europe/London` on this app) |
| FRESHNESS\_TIMER\_MODE | Optional. report (default, no index writes) or apply. Keep report until the owner approves apply. |
| `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX_NAME`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | Same names and values as the Indexing app, FAQ index only. Freshness reads that index and re-embeds changed chunks. It does not use the templated index. Dimensions 1536. |
| Scraper and freshness tuning settings | Optional, all have defaults. Full list is in the repo's `local.settings.json.example` |

The platform template settings (`CONTENT_SAFETY_ENDPOINT`, `REDIS_HOST`, `FOUNDRY_AZURE_OPENAI_ENDPOINT`, `OTEL_CONNECTION_STRING`, `SEARCH_ENDPOINT`) are not read by our code.

## 7. RBAC (system-assigned identity per app)

| Scope | Web ingest | Indexing |
| --- | --- | --- |
| Host storage | Blob Data Owner, Queue Data Contributor, Table Data Contributor | Same |
| Data storage | Blob Data Contributor (scraper writes JSON, freshness writes reports) | Blob Data Reader (indexer reads `output`, templated reads `input`; **TBC**: Contributor if templated writes anything) |
| AI Search | Search Index Data Contributor (freshness updates the FAQ index) | Search Index Data Contributor |
| Azure OpenAI | Cognitive Services OpenAI User (freshness) | Cognitive Services OpenAI User |

Search roles are service-wide, so one assignment covers both indexes. No control-plane Search roles are needed.

If Application Insights accepts identity-based ingestion only, each app's identity also needs Monitoring Metrics Publisher on that resource (**TBC**).

People (not the apps' identities): users who upload the input Excel files need Storage Blob Data Contributor on the `input` container. Operators who read reports and `scraper-rejected/` need Storage Blob Data Reader on `output` (**TBC**).

**Access for testers:** an individual signed in with Entra ID needs the following to test both apps end to end.

| Task | Role | Scope |
| --- | --- | --- |
| Upload input Excel files | Storage Blob Data Contributor | Data storage, `input` container |
| Read scraped JSON, reports and `scraper-rejected/` | Storage Blob Data Reader | Data storage, `output` container |
| Send test messages to the four `*-requests` queues | Storage Queue Data Message Sender | Host storage account |
| See the storage accounts and queues in the portal | Reader | Both storage accounts |
| Check chunks in the indexes (counts, test queries) | Search Index Data Reader, plus Reader to see it in the portal | AI Search service |
| See function runs and logs | Reader on each Function App, Monitoring Reader and Log Analytics Reader | Both Function Apps, Application Insights and its workspace |

Network: both storage accounts have public access disabled, so the tester must work from inside the VNet (for example the VDI) or over a private path with private DNS resolving the storage endpoints.

Sign-in: shared key access is off, so the portal, Storage Explorer and the Azure CLI must use Microsoft Entra authorization (CLI: `--auth-mode login`).

## 8. Runtime behaviour to be aware of

- Jobs in one app share one process and one plan, so a scrape and a freshness run, or an index run and a templated run, can overlap in time. Memory on the shared P1v3 plan should be sized for this (**TBC**).
- The two apps are independent: a failed deployment or a stuck job in one does not stop the other.
- Index writes: the FAQ index and the templated index are separate, so the indexer and templated jobs do not contend for the same documents.

## 9. Open items for DevOps

1. Confirm the app names and which already-created apps are reused or retired.
2. Fix the 403 `AuthorizationFailure` on storage (VNet integration and private DNS, section 5).
3. Confirm which storage account is the data account.
4. Supply the Search endpoint, both index names, the OpenAI endpoint and the embedding deployment name.
5. Confirm the egress approach for `royallondon.com` on the web ingest app.
6. Enable Always On on both apps.
7. Create Application Insights, set its connection string on both apps, and confirm the apps can reach its ingestion endpoint.
8. Confirm the report paths.
9. Point each pipeline YAML at the correct `appName` (one per repo).
