# DigiAssist Input Data Repo and Upload Pipeline

Nov 20, 2026 · @Mukesh

The approved-URLs and templated-questions Excel files live in a separate repo, and a pipeline uploads them to blob storage. The upload is what starts the scrape or the templated sync, so no one has to push files by hand in higher environments. DEV only for now.

## Why a separate repo

- The Excel files are input data, not app code. The Function App zip contains code only.
- Every change to the approved list has an author, a reviewer and a date.
- The same process runs in every environment. Only the storage account and service connection change.

## Repo layout

```
digiassist-inputdata/   (branch: dev)
  approved-urls/approved_urls.xlsx
  templated-questions/templated_questions.xlsx
  azure-pipelines.yml
  .gitignore
```

File names are fixed. Each upload overwrites the same blob, and the overwrite is what fires the function. Older `.xlsx` files left in the blob folder would be picked up as "newest", so remove them.

## How it flows

1. Someone commits a changed Excel file to `dev`.
2. The pipeline uploads only the changed file to `input/approved-urls/` or `input/templated-questions/`.
3. `scrape_on_excel` or `templated_on_upload` fires on the new blob version.
4. The scraper writes JSON to `output/scraped-data/`, which fires `index_on_scrape`. Templated syncs its own index directly.

Nothing runs on a schedule except the freshness timer (21:00 UK time). An unchanged file is not re-uploaded, so it does not re-trigger anything.

## Pipeline

```yaml
trigger:
  branches:
    include: [dev]
  paths:
    include:
      - approved-urls/*
      - templated-questions/*

parameters:
  - name: forceUpload
    displayName: Upload both files even if unchanged
    type: boolean
    default: false

pool:
  name: <self-hosted-pool-inside-vnet>

variables:
  azureServiceConnection: '<service-connection>'
  dataStorageAccount: '<data-storage-account>'

steps:
  - checkout: self
    fetchDepth: 2
  - task: AzureCLI@2
    displayName: Upload changed input files to blob
    inputs:
      azureSubscription: $(azureServiceConnection)
      scriptType: bash
      scriptLocation: inlineScript
      inlineScript: |
        set -euo pipefail
        changed=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || git ls-files)
        for f in approved-urls/approved_urls.xlsx templated-questions/templated_questions.xlsx; do
          if [ "${{ parameters.forceUpload }}" = "True" ] || echo "$changed" | grep -qx "$f"; then
            az storage blob upload --auth-mode login \
              --account-name "$(dataStorageAccount)" \
              --container-name input --name "$f" --file "$f" --overwrite
            echo "uploaded $f"
          else
            echo "unchanged $f, skipped"
          fi
        done
```

## What DevOps provides

| Item | Detail |
| --- | --- |
| Agent pool | Self-hosted, inside the VNet. Storage has public access disabled, so a Microsoft-hosted agent cannot reach it. |
| Service connection | One per environment, with Storage Blob Data Contributor on the `input` container. |
| Data storage account | The account name for the `dataStorageAccount` variable. |
| Pipeline review | Review and own the YAML, as for the Function App pipelines. |

## Higher environments

- One stage per environment after DEV, each with an approval gate.
- Branch policy with a reviewer on the approved list.
- Remove people's manual Contributor role on `input`, so the repo and blob cannot disagree.

## Open items

1. Who owns the approved-URLs list and approves changes (TBC).
2. Who owns the templated-questions Excel (TBC with the product owners).
3. Agent pool name, service connection name and storage account name from DevOps.
4. Repo name and whether DevOps wants the pipeline in this repo or a shared template.
