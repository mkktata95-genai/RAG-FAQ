"""
Content Safety Diagnostic Script
=================================
Standalone script to verify Azure Content Safety connectivity
and configuration independently of the ARIA pipeline.

USAGE (from VDI):
    (.venv) PS C:\\Users\\MKund\\Desktop\\RAG> python check_content_safety.py

WHAT IT CHECKS:
    Step 1 — Environment: CONTENT_SAFETY_ENDPOINT set in .env
    Step 2 — TCP connectivity: raw socket connect to the endpoint host
    Step 3 — DNS resolution: resolves the hostname
    Step 4 — Azure credential: DefaultAzureCredential works
    Step 5 — Client creation: ContentSafetyClient instantiates
    Step 6 — API call (safe text): analyze "hello" with 10s timeout
    Step 7 — API call (borderline text): verify severity scoring works
    Step 8 — Latency report: how long each step took

Each step prints PASS / FAIL / WARN with details.
Script exits with code 0 if all critical steps pass, 1 otherwise.
"""

import os
import sys
import socket
import time
from urllib.parse import urlparse
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(usecwd=True), override=True)

# ── Colour helpers ────────────────────────────────────────────
def green(s):  return f"\033[92m{s}\033[0m"
def red(s):    return f"\033[91m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"

PASS = green("PASS")
FAIL = red("FAIL")
WARN = yellow("WARN")

results = []   # (step, status, detail)
critical_failed = False


def step(num: int, name: str, status: str, detail: str, latency_ms: float | None = None):
    lat = f"  [{latency_ms:.0f}ms]" if latency_ms is not None else ""
    print(f"  Step {num}: {status}  {bold(name)}{lat}")
    if detail:
        print(f"           {detail}")
    results.append((num, name, status, detail))


print()
print(bold("=" * 60))
print(bold("  ARIA — Content Safety Diagnostic"))
print(bold("=" * 60))
print()

# ── Step 1: Endpoint configured ───────────────────────────────
t = time.time()
endpoint = os.getenv("CONTENT_SAFETY_ENDPOINT", "").strip()
latency = (time.time() - t) * 1000

if not endpoint:
    step(1, "Endpoint configured", FAIL,
         "CONTENT_SAFETY_ENDPOINT not set in .env\n"
         "           Add: CONTENT_SAFETY_ENDPOINT=https://<resource>.cognitiveservices.azure.com/")
    critical_failed = True
else:
    step(1, "Endpoint configured", PASS, f"Endpoint: {endpoint}", latency)

# ── Step 2: DNS resolution ────────────────────────────────────
if endpoint:
    t = time.time()
    parsed = urlparse(endpoint)
    hostname = parsed.hostname or ""
    try:
        ip = socket.gethostbyname(hostname)
        latency = (time.time() - t) * 1000
        step(2, "DNS resolution", PASS, f"{hostname} → {ip}", latency)
    except socket.gaierror as e:
        latency = (time.time() - t) * 1000
        step(2, "DNS resolution", FAIL,
             f"Cannot resolve {hostname}: {e}\n"
             "           Check VDI network / DNS settings", latency)
        critical_failed = True
else:
    step(2, "DNS resolution", WARN, "Skipped — no endpoint set")

# ── Step 3: TCP connectivity ──────────────────────────────────
if endpoint and not critical_failed:
    t = time.time()
    parsed = urlparse(endpoint)
    hostname = parsed.hostname or ""
    port = parsed.port or 443
    try:
        sock = socket.create_connection((hostname, port), timeout=8)
        sock.close()
        latency = (time.time() - t) * 1000
        step(3, "TCP connectivity", PASS,
             f"Connected to {hostname}:{port}", latency)
    except socket.timeout:
        latency = (time.time() - t) * 1000
        step(3, "TCP connectivity", FAIL,
             f"TCP timeout connecting to {hostname}:{port}\n"
             "           Likely causes:\n"
             "           - Private endpoint not accessible from VDI\n"
             "           - Firewall/NSG blocking port 443\n"
             "           - Wrong endpoint URL (public vs private)\n"
             "           Action: Check with Andy — VNet peering / private endpoint config", latency)
        critical_failed = True
    except ConnectionRefusedError as e:
        latency = (time.time() - t) * 1000
        step(3, "TCP connectivity", FAIL,
             f"Connection refused: {e}", latency)
        critical_failed = True
    except Exception as e:
        latency = (time.time() - t) * 1000
        step(3, "TCP connectivity", FAIL, str(e), latency)
        critical_failed = True
else:
    step(3, "TCP connectivity", WARN, "Skipped — previous step failed")

# ── Step 4: Azure credential ──────────────────────────────────
if not critical_failed:
    t = time.time()
    try:
        from azure.identity import DefaultAzureCredential
        credential = DefaultAzureCredential()
        # Force token acquisition to validate credential
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        latency = (time.time() - t) * 1000
        step(4, "Azure credential", PASS,
             f"DefaultAzureCredential token acquired (expires: {token.expires_on})", latency)
    except Exception as e:
        latency = (time.time() - t) * 1000
        step(4, "Azure credential", FAIL,
             f"Credential error: {e}\n"
             "           Ensure az login is active on VDI or Managed Identity is configured", latency)
        critical_failed = True
else:
    step(4, "Azure credential", WARN, "Skipped — previous step failed")

# ── Step 5: Client creation ───────────────────────────────────
client = None
if not critical_failed:
    t = time.time()
    try:
        from azure.ai.contentsafety import ContentSafetyClient
        client = ContentSafetyClient(
            endpoint=endpoint,
            credential=credential,
        )
        latency = (time.time() - t) * 1000
        step(5, "Client creation", PASS, "ContentSafetyClient instantiated", latency)
    except Exception as e:
        latency = (time.time() - t) * 1000
        step(5, "Client creation", FAIL, str(e), latency)
        critical_failed = True
else:
    step(5, "Client creation", WARN, "Skipped — previous step failed")

# ── Step 6: API call — safe text ─────────────────────────────
if client and not critical_failed:
    t = time.time()
    try:
        from azure.ai.contentsafety.models import AnalyzeTextOptions, TextCategory
        request = AnalyzeTextOptions(
            text="Hello, can you help me with my pension?",
            categories=[
                TextCategory.HATE,
                TextCategory.VIOLENCE,
                TextCategory.SEXUAL,
                TextCategory.SELF_HARM,
            ],
        )
        response = client.analyze_text(request, timeout=10)
        latency = (time.time() - t) * 1000
        scores = {r.category: r.severity for r in response.categories_analysis}
        step(6, "API call — safe text", PASS,
             f"Scores: {scores}  (expected all 0)", latency)
    except Exception as e:
        latency = (time.time() - t) * 1000
        step(6, "API call — safe text", FAIL,
             f"API error: {e}", latency)
        critical_failed = True
else:
    step(6, "API call — safe text", WARN, "Skipped — previous step failed")

# ── Step 7: API call — verify scoring works ───────────────────
if client and not critical_failed:
    t = time.time()
    try:
        from azure.ai.contentsafety.models import AnalyzeTextOptions, TextCategory
        request = AnalyzeTextOptions(
            text="I want to hurt someone",
            categories=[TextCategory.VIOLENCE],
        )
        response = client.analyze_text(request, timeout=10)
        latency = (time.time() - t) * 1000
        scores = {r.category: r.severity for r in response.categories_analysis}
        violence_score = scores.get("Violence", 0)
        if violence_score >= 2:
            step(7, "API call — severity scoring", PASS,
                 f"Violence score={violence_score} (≥2 as expected — blocking works)", latency)
        else:
            step(7, "API call — severity scoring", WARN,
                 f"Violence score={violence_score} — lower than expected. "
                 f"Verify BLOCK_THRESHOLD=2 with RLG compliance team", latency)
    except Exception as e:
        latency = (time.time() - t) * 1000
        step(7, "API call — severity scoring", FAIL,
             f"API error: {e}", latency)
else:
    step(7, "API call — severity scoring", WARN, "Skipped — previous step failed")

# ── Summary ───────────────────────────────────────────────────
print()
print(bold("=" * 60))
print(bold("  SUMMARY"))
print(bold("=" * 60))

passed  = sum(1 for _, _, s, _ in results if s == PASS)
failed  = sum(1 for _, _, s, _ in results if s == FAIL)
warned  = sum(1 for _, _, s, _ in results if s == WARN)

print(f"  {green(f'{passed} passed')}  |  {red(f'{failed} failed')}  |  {yellow(f'{warned} skipped/warned')}")
print()

if critical_failed:
    print(red("  ✗ Content Safety NOT reachable — pipeline running without Layer 5 safety."))
    print(red("  ✗ Check the FAIL steps above and resolve before production deployment."))
    print()
    # Specific guidance based on what failed
    for num, name, status, detail in results:
        if status == FAIL:
            if "TCP" in name:
                print(yellow("  ACTION: TCP failure detected — raise with Andy:"))
                print(yellow("          1. Confirm Content Safety resource is in same RG as other Azure resources"))
                print(yellow("          2. Check if private endpoint is configured — VDI may need VNet access"))
                print(yellow("          3. Try public endpoint URL directly if private endpoint is the issue"))
                print(yellow("          4. Check NSG rules on VDI subnet — port 443 outbound must be allowed"))
            elif "DNS" in name:
                print(yellow("  ACTION: DNS failure — VDI cannot resolve the endpoint hostname"))
                print(yellow("          Check VDI DNS settings or use the IP address directly"))
            elif "credential" in name.lower():
                print(yellow("  ACTION: Run 'az login' on VDI or check Managed Identity assignment"))
    print()
    sys.exit(1)
else:
    print(green("  ✓ Content Safety fully operational — pipeline Layer 5 is active."))
    print()
    sys.exit(0)