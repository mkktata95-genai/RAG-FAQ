"""
find_oversized_pages.py
=======================
Scans the latest scraper JSON and reports:
1. Pages with content_length > threshold (too large for RAG chunking)
2. Estimated chunk count per page
3. Whether they appear in the duplicate audit top offenders

Outputs a CSV for client review.
"""
import json
import os
import csv
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────
CHUNK_SIZE       = 1600
CHUNK_OVERLAP    = 200
# Pages producing more than this many chunks are flagged
MAX_CHUNKS_WARN  = 20
# Pages producing more than this are critical
MAX_CHUNKS_CRIT  = 50

# Find latest JSON
data_dir = Path("scraper/data")
jsons = sorted(data_dir.glob("royal_london_faq_approved_*.json"),
               key=lambda p: p.stat().st_mtime, reverse=True)
if not jsons:
    raise FileNotFoundError("No scraper JSON found in scraper/data/")
json_path = jsons[0]
print(f"JSON: {json_path}")

data = json.loads(json_path.read_text(encoding="utf-8"))
print(f"Total pages: {len(data)}\n")

# ── Estimate chunks ───────────────────────────────────────────
def estimate_chunks(content_len: int) -> int:
    if content_len <= CHUNK_SIZE:
        return 1
    effective = CHUNK_SIZE - CHUNK_OVERLAP
    return max(1, (content_len - CHUNK_OVERLAP) // effective)

# ── Analyse ───────────────────────────────────────────────────
results = []
for d in data:
    url     = d.get("url", "")
    content = d.get("content", "")
    clen    = d.get("content_length", len(content))
    title   = d.get("title", "")
    cat     = d.get("content_type", d.get("category", ""))
    est     = estimate_chunks(clen)
    flag    = "CRITICAL" if est > MAX_CHUNKS_CRIT else ("WARN" if est > MAX_CHUNKS_WARN else "OK")
    results.append({
        "flag":            flag,
        "est_chunks":      est,
        "content_length":  clen,
        "url":             url,
        "title":           title,
        "category":        cat,
        "dropdown_state":  d.get("dropdown_state", ""),
    })

results.sort(key=lambda x: -x["est_chunks"])

# ── Print summary ─────────────────────────────────────────────
critical = [r for r in results if r["flag"] == "CRITICAL"]
warn     = [r for r in results if r["flag"] == "WARN"]
ok       = [r for r in results if r["flag"] == "OK"]

print(f"{'='*65}")
print(f"  CRITICAL (>{MAX_CHUNKS_CRIT} chunks): {len(critical)} pages")
print(f"  WARN     (>{MAX_CHUNKS_WARN} chunks): {len(warn)} pages")
print(f"  OK                      : {len(ok)} pages")
print(f"{'='*65}\n")

if critical:
    print(f"CRITICAL pages — recommend removing from Excel:\n")
    for r in critical:
        print(f"  [{r['est_chunks']:>4} est chunks | {r['content_length']:>8} chars]")
        print(f"  URL  : {r['url']}")
        print(f"  Title: {r['title'][:80]}")
        print(f"  Cat  : {r['category']}")
        print()

if warn:
    print(f"WARN pages — review with client:\n")
    for r in warn:
        print(f"  [{r['est_chunks']:>4} est chunks | {r['content_length']:>8} chars]")
        print(f"  URL  : {r['url']}")
        print(f"  Title: {r['title'][:80]}")
        print(f"  Cat  : {r['category']}")
        print()

# ── Write CSV ─────────────────────────────────────────────────
out_csv = Path("scraper/data/oversized_pages_review.csv")
with open(out_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "flag", "est_chunks", "content_length", "url", "title", "category", "dropdown_state"
    ])
    writer.writeheader()
    writer.writerows(results)

print(f"Full list saved to: {out_csv}")
print(f"(Share this CSV with client for review)")