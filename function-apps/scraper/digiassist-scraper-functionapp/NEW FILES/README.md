# digiassist-scraper-functionapp

Azure Function App that reads the **approved-URL Excel**, scrapes each page from the live site, and writes one scraped JSON (`output/scraped-data/`). That JSON landing is what triggers the Indexer.

## How it behaves

- Excel in, JSON out. The Excel is the full list of pages; one run scrapes all of them (~4 to 5 min for ~350 pages).
- Failure limit: more than 5% of URLs failing (`SCRAPER_MAX_FAILURE_RATIO`) marks the run **rejected**. The JSON goes to `output/scraper-rejected/` (the Indexer does not see it). A run at or below the limit is uploaded normally and the failed URLs are listed in `_failures.json`.
- `_failures.json` is uploaded **before** the main JSON, because the main JSON triggers the Indexer.
- No output at all (every URL failed) fails the run.
- One run at a time (internal lock; plan is also limited to one instance).
- Needs outbound HTTPS to `www.royallondon.com`. No Search or OpenAI access.
- Content validation (`src/scrape_validator.py`, called from `src/scraper_job.py`): after a successful scrape the JSON is checked for empty/short pages, a missing H1/title, and shrinkage or removal against the newest earlier JSON in `output/scraped-data/`. `SCRAPER_VALIDATION_MODE` = `warn` (default, log only), `block` (content ERRORs go to `output/scraper-rejected/`), `off`. A validator failure never fails a scrape.
- Script is `scrape_approved_urls_httpV1.py` v1.2.1 (v1.2.0 plus: keep the in-page `<header class="hero">` banner, H1 + strapline). The Function App adds the blob/queue hosting layer and validation in `src/scraper_job.py` and `src/scrape_validator.py`.

## Functions

| Function | Trigger | What it does |
|---|---|---|
| `scrape_on_excel` | Blob `input/approved-urls/{name}` (.xlsx only) | Scrape using that file |
| `scrape_on_request` | Queue `scrape-requests` | Scrape using a named file or the newest .xlsx. Body: any text (e.g. `run`) = newest .xlsx, or `{"excel": "x.xlsx"}` for a named file |

## App settings

| Setting | Example |
|---|---|
| `FUNCTIONS_WORKER_RUNTIME` | `python` |
| `DataStorage__blobServiceUri` | storage blob URL (Azure, managed identity) |
| `AzureWebJobsStorage` | host storage (Azure: identity-based settings) |

Optional tuning (defaults are fine): `SCRAPER_MAX_FAILURE_RATIO` (0.05), `SCRAPER_VALIDATION_MODE` (`warn`), `SCRAPER_INPUT_PREFIX` (`approved-urls/`), `SCRAPER_OUTPUT_PREFIX` (`scraped-data/`), `SCRAPER_REJECTED_PREFIX` (`scraper-rejected/`), `PIPELINE_RETRY_ATTEMPTS` (5).

---

# Step-by-step guide (VDI, PowerShell)

Run each block, check the result, then go to the next.

## 1. Get the code and create the environment

```powershell
cd C:\Users\MKund\Desktop\DigiAssist
git clone https://dev.azure.com/RLG-UK/Digital%20Assistant/_git/digiassist-scraper-functionapp
cd digiassist-scraper-functionapp
git checkout -b dev
code .
```

Copy all files into the folder (layout at the bottom). **Do not paste the script**: copy `scrape_approved_urls_httpV1.py` v1.2.1 (the version with the hero-banner fix; based on `digiassist-offlinescript`, branch `feature/scraper-v1.2.0`, commit `39c22d1`) into `src\`.

Check it is the right file:

```powershell
Get-FileHash src\scrape_approved_urls_httpV1.py -Algorithm SHA256
```

The hash must start with `d99c9cf111d39af2`. If your editor changed line endings it will differ; the LF-normalised hash must then start with `8aacf6cf47d40a03`. (v1.2.0 was `40f727b224c3bd73` / `727f4c60e3f0c812`.) If neither matches, stop and tell me.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-test.txt
pip list
```

Expected: no errors; `beautifulsoup4 4.14.3`, `markdownify 1.2.3`, `openpyxl 3.1.5`, `pytest` in the list.

## 2. Check every dependency exists in the feed (one time)

Copy `feedcheck.py` from the old repo into this folder (git-ignored via `feedcheck.*`), then:

```powershell
Get-Content requirements.txt | Where-Object { $_ -match '==' } | Set-Content reqs-check.txt
python feedcheck.py reqs-check.txt
```

Open `feedcheck.csv`. Every line must say OK. Packages new compared with the indexer set (most likely to be MISSING): `beautifulsoup4`, `soupsieve`, `markdownify`, `six`. Send the CSV (screenshot) if any line is MISSING; DevOps then needs an exception request.

## 3. Run the unit tests

```powershell
python -m pytest
```

Expected: `124 passed` (109 existing + 15 for content validation).

With coverage:

```powershell
python -m pytest --cov=src --cov-report=term-missing
```

Expected: `scraper_job.py 100%`, `scrape_validator.py` 99% (only the `__main__` line), `scrape_approved_urls_httpV1.py` about 86%, total about 87% or slightly higher. The uncovered lines are the CLI `main()`, rare error branches and some dropdown edge cases.

To enforce a threshold (the pipeline can use the same flag):

```powershell
python -m pytest --cov=src --cov-fail-under=80
```

What the tests cover: URL logic (section, content type, audience, normalisation), HTML cleaning and markdown, page metadata, dropdown detection, Excel loading (header names, dead status, duplicates), fetch and retry, dedup and atomic save, full runs on fixture HTML (success, failure limit, all failed, too-short page, HTTP error), the wrapper (upload order, rejected path, queue parsing, lock) and content validation (each check, `warn`/`block`/`off` modes, baseline lookup, validator-failure safety). No test touches the network or Azure.

## 4. Run the Function App locally (Azurite)

```powershell
Copy-Item local.settings.json.example local.settings.json
```

1. In VS Code start Azurite (Blob, Queue and Table show as running in the status bar).
2. Make sure no other `func` host is running on port 7071.

```powershell
func start
```

Expected: both functions listed (`scrape_on_excel`, `scrape_on_request`). In a second terminal, use a small Excel (2 to 3 URLs) to keep it quick:

```powershell
.\.venv\Scripts\Activate.ps1
python local_upload.py approved_small.xlsx
```

Expected in the `func` log: scraping lines, then the JSON uploaded to `output/scraped-data/`. The local run needs internet for royallondon.com. Queue test (needs `pip install azure-storage-queue`, local only):

```powershell
python local_upload.py --queue
```

## 5. Build the deployable zip

```powershell
Remove-Item -Recurse -Force libs -ErrorAction SilentlyContinue
.\funcbuild.ps1
```

Expected: `Build complete: scraper.zip`. It installs only `requirements.txt` with `--no-deps` as Linux Python 3.11 wheels, so a package missing from the feed fails loudly.

## 6. Commit and push to `dev`

```powershell
git branch --show-current
```

Must print `dev`. Then stage by name (never `git add .`):

```powershell
git add .gitignore README.md host.json function_app.py requirements.in requirements.txt lock.txt requirements-test.txt pytest.ini funcbuild.ps1 local_upload.py local.settings.json.example scraper.zip
git add src/scrape_approved_urls_httpV1.py src/scraper_job.py src/scrape_validator.py Pipelines/azure-pipelines-scraper-func.yml Pipelines/env/dev.yml tests
git status --short
```

Check that `local.settings.json`, `libs/`, `.venv/`, `*.xlsx` and `feedcheck.*` are NOT listed. Then:

```powershell
git commit -m "Scraper function app: Excel to scraped JSON, unit tests, pinned requirements"
git push -u origin dev
git log --oneline -3
```

## 7. Deploy and operate

- Register the pipeline from `Pipelines/azure-pipelines-scraper-func.yml` (manual trigger). It deploys `scraper.zip` and checks that `scrape_on_excel` and `scrape_on_request` are registered.
- Run a scrape: upload the approved-URL Excel to `input/approved-urls/`, or send queue message `run` (or `{"excel": "x.xlsx"}`) to `scrape-requests`.
- Result: `output/scraped-data/royal_london_faq_approved_httpV1_<time>.json` (Indexer starts by itself) and, if any URL failed, `..._failures.json` next to it.
- Rejected run: files are in `output/scraper-rejected/`; read the `_failures.json` there, fix the Excel or wait for the site, run again.
- Log markers: `scraper_pipeline_complete` (success), `scrape_failure_ratio_exceeded` (rejected), `scrape_failures_written` (some URLs failed), `SCRAPE_VALIDATION` (content check issues; `errors=`/`warnings=` summary line). Not yet confirmed to arrive as App Insights traces: check after the first deployed run with `traces | where message contains "SCRAPE_"`.

## Repo layout

```
function_app.py            Function App (2 functions)
host.json
src/scrape_approved_urls_httpV1.py   scraper script v1.2.1
src/scraper_job.py         hosting layer: blob/queue, upload order, rejected path, lock, validation hook
src/scrape_validator.py    content validation (standard library only)
requirements.in            direct dependencies
requirements.txt           full pinned set (direct + transitive)
lock.txt                   resolver output kept for reference
requirements-test.txt      pytest, pytest-mock, pytest-cov
pytest.ini
funcbuild.ps1              builds scraper.zip
local_upload.py            Azurite helper
local.settings.json.example
scraper.zip                deployable package (committed)
Pipelines/azure-pipelines-scraper-func.yml
Pipelines/env/dev.yml      DEV variables (values to be confirmed for the new subscription)
tests/conftest.py
tests/unit/wrapper/test_scraper_job.py
tests/unit/wrapper/test_scrape_validator.py
tests/unit/script/test_url_logic.py
tests/unit/script/test_content_cleaning.py
tests/unit/script/test_metadata_and_dropdowns.py
tests/unit/script/test_load_fetch_run.py
```
