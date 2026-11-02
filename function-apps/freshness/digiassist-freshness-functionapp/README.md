# digiassist-freshness-functionapp

Azure Function App that checks every approved page against the live site every day, and keeps the main Azure AI Search index current: changed pages are re-chunked, re-embedded and replaced; new pages are added; dead, redirected or de-listed pages are removed.

## How it behaves

- **report** mode: full scan, writes an Excel report, **no index writes**. **apply** mode: also updates the index.
- Order in apply mode: chunk and embed first (index untouched), then archive a rollback copy, then delete old chunks, then upload new ones. Any failure before the delete leaves the index as it was.
- Only a confirmed 404/410 (checked twice) removes a page. Timeouts, 5xx, 401/403/429 are flagged, never deleted.
- Mass-removal protection: more than max(3, 10% of indexed pages) removals stops apply before any write. Override with `allow_mass_removal` in the queue message.
- The index is created and owned by the platform team. This app only writes documents (role: Search Index Data Contributor).
- Outputs go to blob `output/`: `freshness-reports/`, `freshness-manifests/`, `freshness-archive/` (rollback copies of deleted chunks). A failed archive upload fails the run.
- Reads the **newest .xlsx in `input/approved-urls/`** (the same file the Scraper uses).
- Needs outbound HTTPS to `www.royallondon.com`.
- Script version 1.1.3 (see "Script changes" below).

## Functions

| Function | Trigger | What it does |
|---|---|---|
| `freshness_daily` | Timer `%FRESHNESS_SCHEDULE%` (NCRONTAB, UTC) | Run in mode `FRESHNESS_TIMER_MODE` (default `report`) |
| `freshness_on_request` | Queue `freshness-requests` | Body: `report`, `apply`, or JSON `{"mode":"apply","dry_run":true,"allow_mass_removal":true,"excel":"x.xlsx"}` (all optional; default = report). Invalid mode fails the message. |

## App settings

| Setting | Example |
|---|---|
| `FUNCTIONS_WORKER_RUNTIME` | `python` |
| `AZURE_SEARCH_ENDPOINT` | `https://<search>.search.windows.net` |
| `AZURE_SEARCH_INDEX_NAME` | main index name (platform-created) |
| `AZURE_OPENAI_ENDPOINT` | `https://<openai>.openai.azure.com` |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `text-embedding-3-large` (default) |
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | `1536` (default; must equal the index vector dimensions) |
| `FRESHNESS_SCHEDULE` | `0 0 3 * * *` (daily 03:00 UTC; time to agree) |
| `FRESHNESS_TIMER_MODE` | `report` (default) or `apply` |
| `DataStorage__blobServiceUri` | storage blob URL (Azure, managed identity) |
| `AzureWebJobsStorage` | host storage (Azure: identity-based settings) |

Optional tuning: `FRESHNESS_MAX_REMOVAL_RATIO` (0.10), `FRESHNESS_WORK_DIR` (writable folder, default a temp folder), `PIPELINE_RETRY_ATTEMPTS` (5).

## Script changes (v1.1.3)

The script `content_freshness_httpV1.py` has two approved changes on top of v1.1.1:

1. `scraped_at` falls back to the index time when empty (the index field is a DateTimeOffset and rejects an empty string).
2. Embedding deployment and dimensions come from `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` / `AZURE_OPENAI_EMBEDDING_DIMENSIONS`; the old fixed values are the defaults.

The repo already contains the patched file. If you instead start from your own v1.1.1 copy, apply `tools\patch_freshness_v113.py` (step 1).

---

# Step-by-step guide (VDI, PowerShell)

Run each block, check the result, then go to the next.

## 1. Get the code and create the environment

```powershell
cd C:\Users\MKund\Desktop\DigiAssist
git clone https://dev.azure.com/RLG-UK/Digital%20Assistant/_git/digiassist-freshness-functionapp
cd digiassist-freshness-functionapp
git checkout -b dev
code .
```

Copy all files into the folder (layout at the bottom).

**Script file.** Use the supplied `src\content_freshness_httpV1.py` (already v1.1.3). Check it:

```powershell
Get-FileHash src\content_freshness_httpV1.py -Algorithm SHA256
```

The hash must start with `43d46529865f479c` (or, if your editor changed line endings, the LF-normalised hash must). Different hash: stop and tell me.

If you prefer to start from your own copy (old repo `digiassist-offlinescript`, branch `feature/freshness-app`, commit `ac11f6d`), copy it to `src\` and run the patch (never committed, keep it outside the repo or in `tools\`):

```powershell
python tools\patch_freshness_v113.py src\content_freshness_httpV1.py
```

Expected: it prints `sha256 (LF-normalised) 43d46529865f479c`. If a replacement does not match, it stops without changing the file.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-test.txt
pip list
```

Expected: no errors; `azure-search-documents`, `openai`, `aiohttp`, `markdownify 1.2.3`, `pytest` in the list.

## 2. Check every dependency exists in the feed (one time)

Copy `feedcheck.py` from the old repo into this folder (git-ignored via `feedcheck.*`), then:

```powershell
Get-Content requirements.txt | Where-Object { $_ -match '==' } | Set-Content reqs-check.txt
python feedcheck.py reqs-check.txt
```

Open `feedcheck.csv`. Every line must say OK. Most likely to be MISSING (new compared with the indexer set): the `aiohttp` family (`aiohttp`, `aiosignal`, `frozenlist`, `multidict`, `yarl`, `propcache`, `aiohappyeyeballs`, `attrs`), `beautifulsoup4`, `soupsieve`, `markdownify`, `six`, `openpyxl`, `et-xmlfile`. Send the CSV (screenshot) if any line is MISSING; DevOps then needs an exception request.

## 3. Run the unit tests

```powershell
python -m pytest
```

Expected: `149 passed`.

With coverage:

```powershell
python -m pytest --cov=src --cov-report=term-missing
```

Expected: `freshness_job.py 100%`, `content_freshness_httpV1.py` about 83%, total about 85%. The uncovered lines are the CLI `main()`, rare error branches and some dropdown edge cases.

To enforce a threshold (the pipeline can use the same flag):

```powershell
python -m pytest --cov=src --cov-fail-under=80
```

What the tests cover: message parsing and Excel choice, outputs upload (reports, manifests, rollback archive), failure handling, hash parity, chunking including the `scraped_at` fallback, embedding batching and settings, upload and delete with retries, index scan completeness, health classification, and **complete runs** (unchanged, changed, new, dead, de-listed, mass-removal breaker with and without override, delete failure, upload failure, dry run). No test calls Azure or the live site.

## 4. Run the Function App locally (Azurite)

```powershell
Copy-Item local.settings.json.example local.settings.json
# edit local.settings.json: set AZURE_SEARCH_INDEX_NAME to a TEST index, not the live one
```

Prerequisites: `az login`; you have Search Index Data Contributor and Cognitive Services OpenAI User.

1. In VS Code start Azurite (Blob, Queue and Table show as running).
2. Make sure no other `func` host is running on port 7071.
3. Upload the approved-URL Excel and start the host:

```powershell
python local_upload.py approved_small.xlsx
func start
```

Expected: both functions listed (`freshness_daily`, `freshness_on_request`). Report run (no index writes):

```powershell
python local_upload.py --run report
python local_upload.py --outputs
```

Expected in the `func` log: `FRESHNESS_DONE mode=report ...`; `--outputs` lists the report and manifest. Try `apply` only against a test index: `python local_upload.py --run apply --dry-run` first, then `python local_upload.py --run apply`.

## 5. Build the deployable zip

```powershell
Remove-Item -Recurse -Force libs -ErrorAction SilentlyContinue
.\funcbuild.ps1
```

Expected: `Build complete: freshness.zip`. It installs only `requirements.txt` with `--no-deps` as Linux Python 3.11 wheels, so a package missing from the feed fails loudly.

## 6. Commit and push to `dev`

```powershell
git branch --show-current
```

Must print `dev`. Then stage by name (never `git add .`):

```powershell
git add .gitignore README.md host.json function_app.py requirements.in requirements.txt lock.txt requirements-test.txt pytest.ini funcbuild.ps1 local_upload.py local.settings.json.example freshness.zip
git add src/content_freshness_httpV1.py src/freshness_job.py Pipelines/azure-pipelines-freshness-func.yml Pipelines/env/dev.yml tests
git status --short
```

Check that `local.settings.json`, `libs/`, `.venv/`, `*.xlsx` and `feedcheck.*` are NOT listed. Then:

```powershell
git commit -m "Freshness function app: daily index refresh, unit tests, pinned requirements, script v1.1.3"
git push -u origin dev
git log --oneline -3
```

## 7. Deploy and operate

- Register the pipeline from `Pipelines/azure-pipelines-freshness-func.yml` (manual trigger). It deploys `freshness.zip` and checks that `freshness_daily` and `freshness_on_request` are registered.
- First rollout: keep `FRESHNESS_TIMER_MODE=report`, read the report in `output/freshness-reports/`, then switch to `apply` when it looks right (or send an `apply` queue message once).
- Preview an apply without writing: `{"mode":"apply","dry_run":true}`.
- Intentional large removal: `{"mode":"apply","allow_mass_removal":true}`.
- Rollback of a bad refresh: the deleted chunks are in `output/freshness-archive/deleted_<hash>_<time>.json`; or re-run the Indexer from an earlier scraped JSON.
- Log markers: `FRESHNESS_DONE` (success), `FRESHNESS_ERRORS` (script reported errors), `FRESHNESS_FAILED` (exception), `FRESHNESS_UPLOAD_FAILED`, `removal_circuit_breaker_tripped`.

## Repo layout

```
function_app.py            Function App (2 functions)
host.json
src/content_freshness_httpV1.py   freshness script v1.1.3
src/freshness_job.py       hosting layer: Excel choice, run, outputs upload, locking
requirements.in            direct dependencies
requirements.txt           full pinned set (direct + transitive)
lock.txt                   resolver output kept for reference
requirements-test.txt      pytest, pytest-mock, pytest-cov
pytest.ini
funcbuild.ps1              builds freshness.zip
local_upload.py            Azurite helper
local.settings.json.example
freshness.zip              deployable package (committed)
Pipelines/azure-pipelines-freshness-func.yml
Pipelines/env/dev.yml      DEV variables (values to be confirmed for the new subscription)
tests/conftest.py
tests/fakes.py
tests/unit/wrapper/test_freshness_job.py
tests/unit/script/test_hash_chunk_embed.py
tests/unit/script/test_index_and_health.py
tests/unit/script/test_run_freshness_job.py
tests/unit/script/test_load_and_tables.py
tests/unit/script/test_url_logic_parity.py
tests/unit/script/test_metadata_and_dropdowns_parity.py
```
