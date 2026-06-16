"""
Add Semantic Configuration to rlg-faq-index
=============================================
Reads index_backup.json, replaces "semantic": null
with the rlg-semantic-config configuration, then
PUTs the updated definition back to Azure AI Search.

HOW TO RUN:
    python add_semantic_config.py

REQUIREMENTS:
    - index_backup.json must be in the same folder
    - az CLI logged in (az account show must work)
    - pip install requests
"""

import json
import subprocess
import sys
import os

# ── Config ────────────────────────────────────────────────────
SEARCH_ENDPOINT = "https://rlg-dev-dit-uks-srch-aisplatform.search.windows.net"
INDEX_NAME      = "rlg-faq-index"
API_VERSION     = "2023-11-01"
BACKUP_FILE     = "index_backup.json"

# ── Semantic config to inject ─────────────────────────────────
SEMANTIC_CONFIG = {
    "defaultConfiguration": "rlg-semantic-config",
    "configurations": [
        {
            "name": "rlg-semantic-config",
            "prioritizedFields": {
                "titleField": {
                    "fieldName": "title"
                },
                "prioritizedContentFields": [
                    {"fieldName": "content"}
                ],
                "prioritizedKeywordsFields": [
                    {"fieldName": "section"}
                ]
            }
        }
    ]
}


def get_token() -> str:
    """Get Azure access token for Search using az CLI."""
    print("Getting Azure access token...", end="", flush=True)
    result = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", "https://search.azure.com",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"\n❌ Failed to get token: {result.stderr}")
        sys.exit(1)
    token = result.stdout.strip()
    print(" done")
    return token


def load_backup() -> dict:
    """Load index_backup.json."""
    if not os.path.exists(BACKUP_FILE):
        print(f"❌ {BACKUP_FILE} not found in current directory.")
        print(f"   Run this script from: C:\\Users\\MKund\\Desktop\\RAG\\")
        sys.exit(1)
    print(f"Loading {BACKUP_FILE}...", end="", flush=True)
    with open(BACKUP_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f" done ({len(str(data))} chars)")
    return data


def clean_for_put(index_def: dict) -> dict:
    """
    Remove read-only OData fields that Azure rejects on PUT.
    Azure returns @odata.context, @odata.etag etc. in GET
    but rejects them if sent back in PUT.
    """
    keys_to_remove = [
        "@odata.context",
        "@odata.etag",
        "@odata.type",
    ]
    cleaned = {k: v for k, v in index_def.items()
               if k not in keys_to_remove}
    return cleaned


def inject_semantic(index_def: dict) -> dict:
    """Replace 'semantic': null with our config."""
    print("Injecting semantic configuration...", end="", flush=True)

    if "semantic" not in index_def:
        print("\n❌ 'semantic' key not found in index definition.")
        print("   Expected at line 202 based on your screenshots.")
        sys.exit(1)

    index_def["semantic"] = SEMANTIC_CONFIG
    print(" done")
    return index_def


def push_to_azure(index_def: dict, token: str):
    """PUT the updated index definition back to Azure."""
    import urllib.request

    url = (f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}"
           f"?api-version={API_VERSION}&allowIndexDowntime=false")

    body = json.dumps(index_def, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=body,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )

    print(f"Pushing updated index to Azure...", end="", flush=True)
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            response_body = resp.read().decode("utf-8")
            print(f" done (HTTP {status})")
            return status, response_body
    except urllib.error.HTTPError as e:
        status = e.code
        error_body = e.read().decode("utf-8")
        print(f"\n❌ HTTP {status}")
        try:
            error_json = json.loads(error_body)
            print(f"   Error: {json.dumps(error_json, indent=2)}")
        except Exception:
            print(f"   Response: {error_body[:500]}")
        return status, error_body


def verify(token: str):
    """GET the index back and confirm semantic config is present."""
    import urllib.request

    url = (f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}"
           f"?api-version={API_VERSION}")
    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={"Authorization": f"Bearer {token}"}
    )
    print("Verifying semantic config in Azure...", end="", flush=True)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            semantic = data.get("semantic")
            if semantic and semantic.get("configurations"):
                configs = semantic["configurations"]
                print(f" ✅ CONFIRMED — {len(configs)} config(s) found:")
                for c in configs:
                    print(f"   - {c['name']}")
                    fields = c.get("prioritizedFields", {})
                    print(f"     title   : {fields.get('titleField', {}).get('fieldName', '?')}")
                    content = [f['fieldName'] for f in fields.get('prioritizedContentFields', [])]
                    print(f"     content : {content}")
                    keywords = [f['fieldName'] for f in fields.get('prioritizedKeywordsFields', [])]
                    print(f"     keywords: {keywords}")
            else:
                print(" ❌ semantic config not found after PUT")
    except Exception as e:
        print(f"\n⚠️  Verify failed: {e}")


def main():
    print()
    print("=" * 60)
    print("  Add Semantic Config → rlg-faq-index")
    print("=" * 60)
    print()

    # 1. Load backup
    index_def = load_backup()

    # 2. Show current semantic value
    current = index_def.get("semantic")
    print(f"Current 'semantic' value: {current}")

    if current and current != "null" and isinstance(current, dict):
        configs = current.get("configurations", [])
        if any(c.get("name") == "rlg-semantic-config" for c in configs):
            print("✅ rlg-semantic-config already exists — nothing to do.")
            return

    # 3. Clean read-only fields
    index_def = clean_for_put(index_def)

    # 4. Inject semantic config
    index_def = inject_semantic(index_def)

    # 5. Save modified version locally as backup
    modified_path = "index_with_semantic.json"
    with open(modified_path, "w", encoding="utf-8") as f:
        json.dump(index_def, f, indent=2, ensure_ascii=False)
    print(f"Modified index saved to {modified_path} (local backup)")

    # 6. Get token
    token = get_token()

    # 7. Push to Azure
    status, body = push_to_azure(index_def, token)

    if status in (200, 201, 204):
        print()
        print("✅ Index updated successfully!")
        print()
        # 8. Verify
        verify(token)
        print()
        print("=" * 60)
        print("  NEXT STEPS")
        print("=" * 60)
        print()
        print("  1. Add to your .env file:")
        print("     AZURE_SEARCH_SEMANTIC_CONFIG=rlg-semantic-config")
        print("     SEMANTIC_MIN_SCORE=0.5")
        print()
        print("  2. Replace retriever.py with the updated version")
        print("     (already delivered)")
        print()
        print("  3. Restart server:")
        print("     uvicorn server:app --reload")
        print()
        print("  4. Test: 'What types of pensions does Royal London offer?'")
        print("     Expected: personal pensions, workplace pensions, State Pension")
        print()
    else:
        print()
        print(f"❌ Push failed with HTTP {status}")
        print("   The modified index is saved in index_with_semantic.json")
        print("   Share the error above and we'll debug it.")


if __name__ == "__main__":
    main()