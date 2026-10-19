# DevOps spec — Scraper Function App (DEV)
Status: DRAFT for Andy. Separate Function Apps approved by Andy (reasons: least-privilege identity, lean packages). Build order: Scraper, then Indexer, then Freshness. DEV only.

## 1. App
| Item | Value |
|---|---|
| Name (proposed) | rlg-dev-dig-uks-func-scraper-aia |
| Type / OS | Function App, Linux, **Python 3.11** (not 3.12: our libs are built for cp311) |
| Plan | shared App Service plan for the 3 apps (P1v3 preferred, P0v3 acceptable); fallback one plan per app |
| Always On | **On** (blob trigger polling + long runs) |
| Identity | system-assigned managed identity (own identity per app) |
| Network | VNet integration subnet rlg-dev-dig-uks-sn-aia-app; inbound public access disabled + private endpoint (same as agent app) |
| Deploy | zip, WEBSITE_RUN_FROM_PACKAGE=1, no Oryx; our ADO pipeline azure-pipelines-scraper-func.yml (manual) |

## 2. Roles for the app identity
| Scope | Role |
|---|---|
| Host storage rlgdevdiguksadigiaia | Storage Blob Data Owner, Storage Queue Data Contributor, Storage Table Data Contributor |
| Data storage rlgdevdigukssaaia | Storage Blob Data Contributor (reads input/approved-urls/, writes output/scraped-data/ and output/scraper-rejected/) |
No Azure Search / OpenAI / Key Vault rights needed.
Users (Mukesh): host acct Storage Queue Data Message Sender; data acct Storage Blob Data Contributor; Reader + Monitoring Reader on the app.

## 3. App settings
| Name | Value |
|---|---|
| FUNCTIONS_WORKER_RUNTIME | python |
| FUNCTIONS_EXTENSION_VERSION | ~4 |
| AzureWebJobsStorage__accountName / __blobServiceUri / __queueServiceUri / __tableServiceUri | host account rlgdevdiguksadigiaia (same pattern as agent app) |
| DataStorage__blobServiceUri | https://rlgdevdigukssaaia.blob.core.windows.net |
| DataStorage__queueServiceUri | https://rlgdevdigukssaaia.queue.core.windows.net (precaution; blob trigger on 2nd account) |
| APPLICATIONINSIGHTS_CONNECTION_STRING | existing App Insights rlg-dev-dig-uks-appin-aia |
| WEBSITE_RUN_FROM_PACKAGE / ENABLE_ORYX_BUILD / SCM_DO_BUILD_DURING_DEPLOYMENT | 1 / false / false (our pipeline also sets) |
Optional (defaults in script, set only to override): PIPELINE_RETRY_ATTEMPTS=5, PIPELINE_RETRY_BASE_SECONDS=2, SCRAPER_MAX_FAILURE_RATIO=0.05, SCRAPER_CLEANUP_LOSS_WARN_PCT=20.
Containers `input` and `output` already exist in rlgdevdigukssaaia and must persist. Queue `scrape-requests` is created by the host in the host account (needs the Queue role above).

## 4. Network
- Outbound TCP 443 from subnet rlg-dev-dig-uks-sn-aia-app to **royallondon.com** and **www.royallondon.com** (scraper fetches approved pages).
- App to storage private endpoints: host account blob + queue (+ table if agent app uses it), data account blob (+ queue if needed).
- Per-app prerequisites (as for agent app): private DNS A record `<app>.scm.azurewebsites.net` -> app SCM PE; hub firewall (10.196.0.4) allow ADO agent subnet 10.196.201.8/29 -> TCP/443 to the app SCM PE.
- QUESTION: extension bundle (Microsoft.Azure.Functions.ExtensionBundle, needed for blob/queue triggers) downloads at first start. Is outbound to the Functions CDN allowed, or must we package the extensions in the zip? Agent app is HTTP-only so this is untested.

## 5. Alerts (App Insights / Azure Monitor)
| Alert | Condition |
|---|---|
| Partial scrape | traces containing `SCRAPE_PARTIAL` (severity Error) |
| Function failure | failed executions / exceptions for scrape_on_excel, scrape_on_request |
| Long run | function duration > 30 min |
| Poison messages | `scrape-requests-poison` queue or `webjobs-blobtrigger-poison` count > 0 |

## 6. Our side (for info)
Triggers: blob on input/approved-urls/*.xlsx, queue scrape-requests (re-run latest Excel; message body is plain text, e.g. `run` or `{"excel":"file.xlsx"}`, NOT Base64, so portal/Storage Explorer 'Add message' works). host.json: functionTimeout -1, queue batchSize 1, maxDequeueCount 2, messageEncoding none. Run time ~4-5 min. Outputs: output/scraped-data/royal_london_faq_approved_httpV1_<UTC>.json (+ _failures.json). >5% failed URLs go to output/scraper-rejected/ instead, so indexing never starts on incomplete data.
Packages: markdownify 1.2.3 and langchain-text-splitters 1.1.2 requested for the rlg-pypi feed (requested 20 Oct).
Status: scraper.zip built and tested locally (blob + queue triggers, output upload). Committed to dev (function-apps/scraper). Pipeline file Pipelines/azure-pipelines-scraper-func.yml needs registering in ADO; first deploy waits for the app.
