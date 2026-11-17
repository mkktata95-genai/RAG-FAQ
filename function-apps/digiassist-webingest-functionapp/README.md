# digiassist-webingest-functionapp

One Azure Function App with four functions that read the **approved-URL Excel**, fetch pages from the live site and keep the main Azure AI Search index current:

- **Scraper**: Excel in, one scraped JSON out (`output/scraped-data/`). That JSON landing triggers the Indexer (separate app).
- **Freshness**: checks every approved page against the live site every day. Changed pages are re-chunked, re-embedded and replaced; new pages are added; dead, redirected or de-listed pages are removed.

Both scripts are unchanged (`scrape_approved_urls_httpV1.py` v1.2.1, `content_freshness_httpV1.py` v1.1.3). This repo only adds the hosting layer: `function_app.py`, `src/scraper_job.py`, `src/scrape_validator.py`, `src/freshness_job.py`.

## How it behaves

**Scraper**
- One run scrapes the whole Excel (~4 to 5 min for ~350 pages).
- More than 5% of URLs failing (`SCRAPER_MAX_FAILURE_RATIO`) marks the run **rejected**: the JSON goes to `output/scraper-rejected/` and the Indexer does not see it. At or below the limit the JSON is uploaded and failed URLs are listed in `_failures.json`.
- `_failures.json` is uploaded **before** the main JSON (the main JSON triggers the Indexer).
- Content validation (`scrape_validator.py`) compares the new JSON with the newest accepted JSON in `scraped-data/`. `SCRAPER_VALIDATION_MODE`: `warn` (default, log only), `block` (ERRORs route the run to `scraper-rejected/`), `off`.
- Needs outbound HTTPS to `www.royallondon.com`. No Search or OpenAI access for the scraper itself.

**Freshness**
- `report` mode: full scan, writes an Excel report, **no index writes**. `apply` mode also updates the index.
- Apply order: chunk and embed first (index untouched), archive a rollback copy, delete old chunks, upload new ones. Any failure before the delete leaves the index as it was.
- Only a confirmed 404/410 (checked twice) removes a page. Timeouts, 5xx, 401/403/429 are flagged, never deleted.
- Mass-removal protection: more than max(3, 10% of indexed pages) removals stops apply before any write. Override with `allow_mass_removal` in the queue message.
- Reads the **newest .xlsx in `input/approved-urls/`** (the same file the Scraper uses).
- Outputs go to `output/`: `freshness-reports/`, `freshness-manifests/`, `freshness-archive/`.
- Needs outbound HTTPS to `www.royallondon.com`.

The index is created and owned by the platform team. This app only writes documents (role: Search Index Data Contributor). It never creates, deletes or aliases an index.

## Functions

| Function | Trigger | What it does |
|---|---|---|
| `scrape_on_excel` | Blob `input/approved-urls/{name}` (.xlsx only) | Scrape using that file |
| `scrape_on_request` | Queue `scrape-requests` | Any text (e.g. `run`) = newest .xlsx, or `{"excel": "x.xlsx"}` |
| `freshness_daily` | Timer `%FRESHNESS_SCHEDULE%` (NCRONTAB, UTC) | Run in mode `FRESHNESS_TIMER_MODE` (default `report`) |
| `freshness_on_request` | Queue `freshness-requests` | `report`, `apply`, or JSON `{"mode":"apply","dry_run":true,"allow_mass_removal":true,"excel":"x.xlsx"}` (all optional, default report) |

## App settings

| Setting | Example |
|---|---|
| `FUNCTIONS_WORKER_RUNTIME` | `python` |
| `DataStorage__blobServiceUri` | storage blob URL (Azure, managed identity). Local: `DataStorage=UseDevelopmentStorage=true` |
| `AzureWebJobsStorage` | host storage (Azure: identity-based `AzureWebJobsStorage__*` settings) |
| `AZURE_SEARCH_ENDPOINT` | `https://<search>.search.windows.net` (freshness) |
| `AZURE_SEARCH_INDEX_NAME` | main index name, platform-created (freshness). **Local: a TEST index, never the live one** |
| `AZURE_OPENAI_ENDPOINT` | `https://<openai>.openai.azure.com` (freshness) |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `text-embedding-3-large` |
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | `1536` (must equal the index vector dimensions) |
| `FRESHNESS_SCHEDULE` | `0 0 3 * * *` (daily 03:00 UTC; time to agree) |
| `FRESHNESS_TIMER_MODE` | `report` (default) or `apply` |

Optional tuning (defaults are fine): `SCRAPER_MAX_FAILURE_RATIO` (0.05), `SCRAPER_VALIDATION_MODE` (warn), `SCRAPER_INPUT_PREFIX` (`approved-urls/`), `SCRAPER_OUTPUT_PREFIX` (`scraped-data/`), `SCRAPER_REJECTED_PREFIX` (`scraper-rejected/`), `FRESHNESS_MAX_REMOVAL_RATIO` (0.10), `FRESHNESS_WORK_DIR`, `PIPELINE_RETRY_ATTEMPTS` (5).

Roles (managed identity): host storage Blob Data Owner + Queue Data Contributor + Table Data Contributor; data storage Blob Data Contributor; Search Index Data Contributor; Cognitive Services OpenAI User.

---

# Step-by-step guide (VDI, PowerShell)

Run each block, check the result, then go to the next.

## 1. Get the code and create the environment

```powershell
cd C:\Users\MKund\Desktop\DigiAssist
git clone https://dev.azure.com/RLG-UK/Digital%20Assistant/_git/digiassist-webingest-functionapp
cd digiassist-webingest-functionapp
git checkout -b dev
code .
```

Copy all files into the folder (layout at the bottom). Do not paste the scripts: copy them as supplied.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-test.txt
pip list
```

Expected: no errors; `azure-search-documents`, `openai`, `markdownify 1.2.3`, `openpyxl 3.1.5`, `pytest` in the list. The runtime is Python **3.11**, so use 3.11 for the venv.

## 2. Check every dependency exists in the feed (one time)

Copy `feedcheck.py` from the old repo into this folder (git-ignored via `feedcheck.*`), then:

```powershell
Get-Content requirements.txt | Where-Object { $_ -match '==' } | Set-Content reqs-check.txt
python feedcheck.py reqs-check.txt
```

Open `feedcheck.csv`. Every line must say OK. Send the CSV (screenshot) if any line is MISSING; DevOps then needs an exception request.

## 3. Run the unit tests

```powershell
python -m pytest
```

Expected: `277 passed` (scraper 126, freshness 150, app registration 1).

With coverage:

```powershell
python -m pytest --cov=src --cov-report=term-missing
python -m pytest --cov=src --cov-fail-under=80
```

Run one suite only: `python -m pytest tests/scraper` or `python -m pytest tests/freshness`.

What the tests cover: URL logic, HTML cleaning, metadata, dropdowns, Excel loading, fetch and retry, full scrape runs, the validator and the wrapper (upload order, rejected path, queue parsing, lock); freshness hashing, chunking, embedding, upload and delete with retries, index scan, health classification and complete runs (unchanged, changed, new, dead, de-listed, breaker, dry run); and a check that exactly four functions are registered. No test calls Azure or the live site.

## 4. Run the Function App locally (Azurite)

```powershell
Copy-Item local.settings.json.example local.settings.json
# edit local.settings.json: set AZURE_SEARCH_INDEX_NAME to a TEST index, not the live one
```

Prerequisites: `az login`; you have Search Index Data Contributor and Cognitive Services OpenAI User; internet access to royallondon.com.

1. In VS Code start Azurite (Blob, Queue and Table show as running).
2. Make sure no other `func` host is running on port 7071.

```powershell
func start
```

Expected: four functions listed (`scrape_on_excel`, `scrape_on_request`, `freshness_daily`, `freshness_on_request`). In a second terminal use a small Excel (2 to 3 URLs):

```powershell
.\.venv\Scripts\Activate.ps1
python local_upload.py approved_small.xlsx
```

Expected in the `func` log: scraping lines, then the JSON uploaded to `output/scraped-data/`.

Queue and freshness tests (need `pip install azure-storage-queue`, local only):

```powershell
python local_upload.py --queue
python local_upload.py --run report
python local_upload.py --outputs
python local_upload.py --run apply --dry-run
```

Expected: `FRESHNESS_DONE mode=report ...`; `--outputs` lists the report and manifest. Try a real `apply` only against a test index.

## 5. Build the deployable zip (optional local check)

The platform pipeline builds and deploys the app, so a local zip is only a check that every package exists as a Linux wheel.

```powershell
Remove-Item -Recurse -Force libs -ErrorAction SilentlyContinue
.\funcbuild.ps1 -PythonVersion 3.11
```

Expected: `Build complete: webingest.zip`. It installs only `requirements.txt` with `--no-deps`, so a missing package fails loudly. The script default is 3.12, so always pass `-PythonVersion 3.11` (or change the default in `funcbuild.ps1`). Do not commit the zip.

## 6. Commit and push to `dev`

```powershell
git branch --show-current
```

Must print `dev`. Then stage by name (never `git add .`):

```powershell
git add .gitignore host.json function_app.py requirements.in requirements.txt requirements-test.txt pytest.ini funcbuild.ps1 local_upload.py local.settings.json.example
git add src/scrape_approved_urls_httpV1.py src/scrape_validator.py src/scraper_job.py src/content_freshness_httpV1.py src/freshness_job.py Pipelines/azure-pipelines-func-webingest-platform.yml tests
git status --short
```

Check that `local.settings.json`, `libs/`, `.venv/`, `*.xlsx`, `*.zip` and `feedcheck.*` are NOT listed. Then:

```powershell
git commit -m "Web ingest function app: scraper + freshness in one app, unit tests, pinned requirements"
git push -u origin dev
git log --oneline -3
```

## 7. Deploy and operate

- Pipeline: copy an existing platform YAML to `Pipelines/azure-pipelines-func-webingest-platform.yml` and set `appName` to the real Function App name (agreed with DevOps). It uses `pythonVersion: "3.11"`.
- Run a scrape: upload the approved-URL Excel to `input/approved-urls/`, or send queue message `run` to `scrape-requests`.
- Result: `output/scraped-data/royal_london_faq_approved_httpV1_<time>.json` (the Indexer starts by itself) and, if any URL failed, `..._failures.json` next to it.
- Rejected run: files are in `output/scraper-rejected/`; read the `_failures.json`, fix the Excel or wait for the site, run again.
- First freshness rollout: keep `FRESHNESS_TIMER_MODE=report`, read the report in `output/freshness-reports/`, then switch to `apply` (or send one `apply` message).
- Preview an apply: `{"mode":"apply","dry_run":true}`. Intentional large removal: `{"mode":"apply","allow_mass_removal":true}`.
- Rollback of a bad refresh: deleted chunks are in `output/freshness-archive/deleted_<hash>_<time>.json`, or re-run the Indexer from an earlier scraped JSON.
- Log markers: `scraper_pipeline_complete`, `scrape_failure_ratio_exceeded`, `scrape_failures_written`, `SCRAPE_VALIDATION`, `FRESHNESS_DONE`, `FRESHNESS_ERRORS`, `FRESHNESS_FAILED`, `FRESHNESS_UPLOAD_FAILED`, `removal_circuit_breaker_tripped`.

## Repo layout

```
function_app.py            Function App (4 functions)
host.json
src/scrape_approved_urls_httpV1.py   scraper script v1.2.1 (unchanged)
src/scrape_validator.py    post-scrape content validation
src/scraper_job.py         hosting layer: blob/queue, upload order, rejected path, lock
src/content_freshness_httpV1.py      freshness script v1.1.3
src/freshness_job.py       hosting layer: Excel choice, run, outputs upload, locking
requirements.in            direct dependencies
requirements.txt           full pinned set (direct + transitive)
requirements-test.txt      pytest, pytest-mock, pytest-cov
pytest.ini
funcbuild.ps1              local zip build check
local_upload.py            Azurite helper
local.settings.json.example
Pipelines/azure-pipelines-func-webingest-platform.yml   (add; appName from DevOps)
tests/test_function_app.py
tests/fakes.py             shared fakes
tests/scraper/             conftest + unit/script (4 files) + unit/wrapper (2 files)
tests/freshness/           conftest + unit/script (6 files) + unit/wrapper (1 file)
```
