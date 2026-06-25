"""
Aria — HQA Question Collision Diagnostic
==========================================
Scans ALL chunks in Azure AI Search index, finds HQA questions
that appear in multiple chunks (collisions), and produces a
detailed report showing the actual collision rate across the index.

PURPOSE:
    Before running evaluate_index_quality.py, understand how many
    HQA questions are duplicated across chunks. This tells us:
    - How severe the cross-chunk question collision problem is
    - Which question types collide most (generic vs specific)
    - Which content types / pages are most affected
    - Whether retrieval deduplication (by URL) is sufficient
    - What threshold to use for collision detection in the evaluator

WHAT IS A COLLISION:
    A question that appears in more than one chunk.
    The more chunks share a question, the more it floods retrieval
    results when a customer asks that exact question.

    Example:
        "What is a pension?" appears in 45 chunks
        → Customer asks "What is a pension?"
        → 45 chunks score very high in vector search
        → Top 5 all have this question but cover different topics
        → URL deduplication helps only if they're from different pages
        → If from same page: only 1 gets through (good)
        → If from different pages: all 5 could be pension-related
          but not the most relevant answer (bad)

COLLISION SEVERITY LEVELS:
    Low    (2-3 chunks)  — acceptable, likely same topic different pages
    Medium (4-10 chunks) — review recommended, may affect retrieval
    High   (11-20 chunks)— action needed, generic question
    Critical (>20 chunks) — must remove, causes serious context pollution

HOW TO RUN:
    python scraper/find_question_duplicates.py

OUTPUT:
    scraper/data/collision_report_<timestamp>.json  — full data
    scraper/data/collision_report_<timestamp>.txt   — human readable

REQUIRES:
    - .env with AZURE_SEARCH_ENDPOINT
    - Index populated with augmented_questions field
    - pip install azure-search-documents azure-identity python-dotenv

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════

v1.0.0 — June 2026 | Mukesh Kund
         Initial version.
         Full index scan, collision detection, severity grading,
         retrieval impact analysis, human-readable report.

═══════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import argparse
import textwrap
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

import structlog
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from dotenv import load_dotenv, find_dotenv

# ── dotenv: walk up directory tree to find .env ───────────────
# find_dotenv() works correctly regardless of which directory
# the script is run from (project root or scraper/ subfolder).
_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path)
log = structlog.get_logger()

# ── Config ─────────────────────────────────────────────────────
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
INDEX_NAME            = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index")

# TOP_K mirrors retriever.py — how many chunks reach the LLM
TOP_K = int(os.getenv("MAX_RETRIEVED_CHUNKS", "5"))

# Collision severity thresholds
# Tune these based on your index size and retrieval behaviour
SEVERITY_LOW      = 2    # 2-3 chunks: acceptable
SEVERITY_MEDIUM   = 4    # 4-10 chunks: review
SEVERITY_HIGH     = 11   # 11-20 chunks: action needed
SEVERITY_CRITICAL = 21   # >20 chunks: must remove

# Normalisation: lowercase + strip punctuation for comparison
# Catches near-duplicates like "What is a pension?" vs "what is a pension"
NORMALISE = True

# Batch size for paginated fetch
FETCH_BATCH_SIZE = 1000


# ── Client ─────────────────────────────────────────────────────
def get_search_client() -> SearchClient:
    """Get Azure Search client using DefaultAzureCredential."""
    if not AZURE_SEARCH_ENDPOINT:
        print("❌ AZURE_SEARCH_ENDPOINT not set in .env")
        sys.exit(1)
    return SearchClient(
        endpoint=AZURE_SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=DefaultAzureCredential(),
    )


# ── Fetch ───────────────────────────────────────────────────────
def fetch_all_chunks(client: SearchClient) -> list[dict]:
    """
    Fetch ALL chunks from index using pagination.
    Only retrieves fields needed for collision analysis —
    no content or embedding fields (keeps response small).
    Returns list of chunk dicts.
    """
    print(f"\n📥 Fetching all chunks from index '{INDEX_NAME}'...")
    print(f"   Paginating in batches of {FETCH_BATCH_SIZE}...")

    all_chunks = []
    skip       = 0

    while True:
        try:
            results = list(client.search(
                search_text="*",
                select=[
                    "chunk_id",
                    "title",
                    "source_url",
                    "section",
                    "content_type",
                    "product_category",
                    "chunk_index",
                    "total_chunks",
                    "augmented_questions",
                    "has_video",
                ],
                top=FETCH_BATCH_SIZE,
                skip=skip,
            ))

            if not results:
                break

            batch_with_hqa = 0
            for r in results:
                aq = (r.get("augmented_questions") or "").strip()
                all_chunks.append({
                    "chunk_id":            r["chunk_id"],
                    "title":               r.get("title", ""),
                    "source_url":          r.get("source_url", ""),
                    "section":             r.get("section", ""),
                    "content_type":        r.get("content_type", "article"),
                    "product_category":    r.get("product_category", "general"),
                    "chunk_index":         r.get("chunk_index", 0),
                    "total_chunks":        r.get("total_chunks", 1),
                    "augmented_questions": aq,
                    "has_video":           r.get("has_video", False),
                    "has_hqa":             bool(aq),
                })
                if aq:
                    batch_with_hqa += 1

            print(
                f"   skip={skip:>5}: {len(results):>4} fetched, "
                f"{batch_with_hqa} with HQA"
            )

            if len(results) < FETCH_BATCH_SIZE:
                break
            skip += FETCH_BATCH_SIZE

        except Exception as e:
            log.error("fetch_error", skip=skip, error=str(e))
            print(f"   ❌ Error at skip={skip}: {e}")
            break

    with_hqa = sum(1 for c in all_chunks if c["has_hqa"])
    print(f"\n   ✅ Fetched {len(all_chunks):,} total chunks")
    print(f"   ✅ {with_hqa:,} chunks have HQA questions")
    print(f"   ⚠️  {len(all_chunks) - with_hqa:,} chunks have NO HQA questions")
    return all_chunks


# ── Normalisation ───────────────────────────────────────────────
def normalise_question(q: str) -> str:
    """
    Normalise question for collision detection.
    Catches near-duplicates that differ only in:
    - Capitalisation: "What is a pension?" vs "what is a pension?"
    - Trailing punctuation: "What is a pension" vs "What is a pension?"
    - Extra whitespace
    """
    if not NORMALISE:
        return q
    import re
    q = q.lower().strip()
    q = re.sub(r'[?!.,;:]+$', '', q)  # strip trailing punctuation
    q = ' '.join(q.split())            # normalise whitespace
    return q


# ── Collision detection ─────────────────────────────────────────
def detect_collisions(chunks: list[dict]) -> dict:
    """
    Scan all chunks and build collision map.

    Returns dict:
        question_map:  normalised_question → list of chunk dicts
        collision_map: normalised_question → list of chunk dicts (len > 1)
        stats:         summary statistics
    """
    print(f"\n🔍 Scanning for question collisions...")

    # Map: normalised_question → [(chunk_dict, original_question)]
    question_map: dict[str, list[dict]] = defaultdict(list)

    total_questions  = 0
    chunks_with_hqa  = 0

    for chunk in chunks:
        aq = chunk["augmented_questions"]
        if not aq:
            continue

        chunks_with_hqa += 1
        questions = [q.strip() for q in aq.split("\n") if q.strip()]

        for original_q in questions:
            total_questions += 1
            norm_q = normalise_question(original_q)
            question_map[norm_q].append({
                "chunk_id":          chunk["chunk_id"],
                "title":             chunk["title"],
                "source_url":        chunk["source_url"],
                "section":           chunk["section"],
                "content_type":      chunk["content_type"],
                "product_category":  chunk["product_category"],
                "chunk_index":       chunk["chunk_index"],
                "total_chunks":      chunk["total_chunks"],
                "original_question": original_q,
            })

    # Filter to collisions only (question in > 1 chunk)
    collision_map = {
        q: chunks_list
        for q, chunks_list in question_map.items()
        if len(chunks_list) > 1
    }

    # Deduplicate within collision map
    # (same chunk_id can appear multiple times if question repeated)
    deduped_collision_map = {}
    for q, chunks_list in collision_map.items():
        seen_chunk_ids = set()
        deduped = []
        for entry in chunks_list:
            if entry["chunk_id"] not in seen_chunk_ids:
                seen_chunk_ids.add(entry["chunk_id"])
                deduped.append(entry)
        if len(deduped) > 1:
            deduped_collision_map[q] = deduped

    total_unique_questions  = len(question_map)
    total_colliding         = len(deduped_collision_map)
    collision_rate          = round(
        total_colliding / total_unique_questions * 100, 1
    ) if total_unique_questions else 0

    # Severity distribution
    severity_counts = {
        "low":      0,
        "medium":   0,
        "high":     0,
        "critical": 0,
    }
    for q, cl in deduped_collision_map.items():
        n = len(cl)
        if n >= SEVERITY_CRITICAL:
            severity_counts["critical"] += 1
        elif n >= SEVERITY_HIGH:
            severity_counts["high"] += 1
        elif n >= SEVERITY_MEDIUM:
            severity_counts["medium"] += 1
        else:
            severity_counts["low"] += 1

    # Chunks affected (appear in at least one collision)
    affected_chunk_ids = set()
    for chunks_list in deduped_collision_map.values():
        for entry in chunks_list:
            affected_chunk_ids.add(entry["chunk_id"])

    stats = {
        "total_chunks":              len(chunks),
        "chunks_with_hqa":           chunks_with_hqa,
        "total_questions_scanned":   total_questions,
        "unique_questions":          total_unique_questions,
        "colliding_questions":       total_colliding,
        "collision_rate_pct":        collision_rate,
        "chunks_affected":           len(affected_chunk_ids),
        "chunks_affected_pct":       round(
            len(affected_chunk_ids) / max(chunks_with_hqa, 1) * 100, 1
        ),
        "severity_distribution":     severity_counts,
        "top_k_context":             TOP_K,
    }

    print(f"   Total questions scanned:  {total_questions:,}")
    print(f"   Unique questions:         {total_unique_questions:,}")
    print(f"   Colliding questions:      {total_colliding:,} ({collision_rate}%)")
    print(f"   Chunks affected:          {len(affected_chunk_ids):,}")
    print(f"   Severity: critical={severity_counts['critical']} | "
          f"high={severity_counts['high']} | "
          f"medium={severity_counts['medium']} | "
          f"low={severity_counts['low']}")

    return {
        "question_map":       dict(question_map),
        "collision_map":      deduped_collision_map,
        "stats":              stats,
    }


# ── Retrieval impact analysis ───────────────────────────────────
def analyse_retrieval_impact(
    collision_map: dict,
    chunks: list[dict],
) -> dict:
    """
    Analyse how collisions affect actual retrieval for customers.

    Key question: when a customer asks a colliding question,
    how many of the TOP_K results will be from the same page
    (URL-deduplicated away) vs different pages (all survive)?

    Also analyses:
    - Which content types produce the most collisions
    - Which product categories are most affected
    - Cross-page vs within-page collision ratio
    - Most dangerous questions (high collision, cross-page)
    """
    # Build URL → content_type lookup from full chunk list
    url_to_type = {c["source_url"]: c["content_type"] for c in chunks}

    cross_page_collisions  = 0
    within_page_collisions = 0
    type_collision_counts  = defaultdict(int)
    cat_collision_counts   = defaultdict(int)
    dangerous_questions    = []  # high collision + cross-page

    for q, chunks_list in collision_map.items():
        urls = [entry["source_url"] for entry in chunks_list]
        unique_urls = set(urls)

        if len(unique_urls) > 1:
            cross_page_collisions += 1
            # Cross-page: URL deduplication in retriever DOES NOT help
            # These are the dangerous ones
            if len(chunks_list) >= SEVERITY_MEDIUM:
                content_types_involved = list(set(
                    entry["content_type"] for entry in chunks_list
                ))
                dangerous_questions.append({
                    "question":            q,
                    "chunk_count":         len(chunks_list),
                    "unique_url_count":    len(unique_urls),
                    "content_types":       content_types_involved,
                    "severity":            _severity_label(len(chunks_list)),
                    "retrieval_impact": (
                        f"Would flood {min(len(unique_urls), TOP_K)}/{TOP_K} "
                        f"retrieval slots with this question"
                    ),
                })
        else:
            within_page_collisions += 1
            # Within-page: URL deduplication handles this — only 1 chunk
            # from this URL will reach the LLM

        # Content type breakdown
        for entry in chunks_list:
            type_collision_counts[entry["content_type"]] += 1
            cat_collision_counts[entry["product_category"]] += 1

    # Sort dangerous questions by chunk count descending
    dangerous_questions.sort(key=lambda x: -x["chunk_count"])

    # Estimate: how many retrieval slots would be "wasted" by critical questions
    critical_slot_waste = sum(
        min(len(cl), TOP_K)
        for q, cl in collision_map.items()
        if len(cl) >= SEVERITY_CRITICAL
        and len(set(e["source_url"] for e in cl)) > 1
    )

    return {
        "cross_page_collisions":  cross_page_collisions,
        "within_page_collisions": within_page_collisions,
        "url_dedup_handles":      within_page_collisions,
        "url_dedup_misses":       cross_page_collisions,
        "type_collision_counts":  dict(type_collision_counts),
        "cat_collision_counts":   dict(cat_collision_counts),
        "dangerous_questions":    dangerous_questions[:50],  # top 50
        "critical_slot_waste":    critical_slot_waste,
    }


def _severity_label(count: int) -> str:
    if count >= SEVERITY_CRITICAL:
        return "critical"
    elif count >= SEVERITY_HIGH:
        return "high"
    elif count >= SEVERITY_MEDIUM:
        return "medium"
    else:
        return "low"


# ── Report ──────────────────────────────────────────────────────
def generate_text_report(
    stats: dict,
    impact: dict,
    collision_map: dict,
    output_path: str,
):
    """
    Generate human-readable collision report.

    Sections:
    1. Executive summary + verdict
    2. Retrieval impact analysis
    3. Critical collisions (>20 chunks) — must fix
    4. High collisions (11-20 chunks) — action needed
    5. Medium collisions (4-10 chunks) — review
    6. Content type breakdown
    7. Recommended actions
    """
    lines = []
    div   = "=" * 72
    sep   = "-" * 72

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines += [
        div,
        "  ARIA — HQA QUESTION COLLISION REPORT",
        f"  Generated: {ts}",
        f"  Index:     {INDEX_NAME}",
        f"  TOP_K:     {TOP_K} (chunks sent to LLM per query)",
        div, "",
    ]

    # ── 1. Executive summary ──────────────────────────────────
    sev = stats["severity_distribution"]
    lines += [
        "  1. EXECUTIVE SUMMARY",
        sep,
        f"  Total chunks in index:    {stats['total_chunks']:,}",
        f"  Chunks with HQA:          {stats['chunks_with_hqa']:,}",
        f"  Total questions scanned:  {stats['total_questions_scanned']:,}",
        f"  Unique questions:         {stats['unique_questions']:,}",
        "",
        f"  Colliding questions:      {stats['colliding_questions']:,}  "
        f"({stats['collision_rate_pct']}% of unique questions)",
        f"  Chunks affected:          {stats['chunks_affected']:,}  "
        f"({stats['chunks_affected_pct']}% of HQA chunks)",
        "",
        f"  Severity breakdown:",
        f"    🔴 Critical (>{SEVERITY_CRITICAL-1} chunks): {sev['critical']:>5}  questions — MUST REMOVE",
        f"    🟠 High    ({SEVERITY_HIGH}-{SEVERITY_CRITICAL-1} chunks): {sev['high']:>5}  questions — ACTION NEEDED",
        f"    🟡 Medium  ({SEVERITY_MEDIUM}-{SEVERITY_HIGH-1} chunks):  {sev['medium']:>5}  questions — REVIEW",
        f"    🟢 Low     (2-{SEVERITY_MEDIUM-1} chunks):   {sev['low']:>5}  questions — ACCEPTABLE",
        "",
    ]

    # Verdict
    cr   = stats["collision_rate_pct"]
    crit = sev["critical"]
    high = sev["high"]
    lines.append("  VERDICT:")
    if crit == 0 and high == 0 and cr < 5:
        lines.append("  ✅ EXCELLENT — collision rate is negligible, index is clean")
    elif crit == 0 and high <= 5 and cr < 15:
        lines.append("  ✅ GOOD — minor collisions, acceptable for production")
        lines.append("     Recommend removing critical/high questions in next re-index")
    elif crit <= 10 and cr < 30:
        lines.append("  ⚠️  MODERATE — collisions exist but retrieval still functional")
        lines.append("     Address critical questions before go-live")
    else:
        lines.append("  ❌ ACTION REQUIRED — significant collision rate affecting retrieval")
        lines.append("     Must address critical and high questions before go-live")

    # ── 2. Retrieval impact ───────────────────────────────────
    lines += [
        "",
        div,
        "  2. RETRIEVAL IMPACT ANALYSIS",
        sep,
        f"  Within-page collisions:   {impact['within_page_collisions']:,}",
        f"  → URL deduplication in retriever.py handles these ✅",
        f"    (same URL, only 1 chunk reaches LLM regardless)",
        "",
        f"  Cross-page collisions:    {impact['cross_page_collisions']:,}",
        f"  → URL deduplication DOES NOT help these ❌",
        f"    (different URLs, all {TOP_K} retrieval slots can be consumed)",
        "",
        f"  Estimated retrieval slots wasted by critical questions:",
        f"  → {impact['critical_slot_waste']} out of {TOP_K} slots per critical-question query",
        "",
        f"  Content types with most collision chunks:",
    ]
    for ct, count in sorted(
        impact["type_collision_counts"].items(), key=lambda x: -x[1]
    )[:8]:
        lines.append(f"    {ct:<20} {count:>5} collision occurrences")

    lines += ["", f"  Product categories with most collision chunks:"]
    for cat, count in sorted(
        impact["cat_collision_counts"].items(), key=lambda x: -x[1]
    )[:8]:
        lines.append(f"    {cat:<25} {count:>5} collision occurrences")

    # ── 3. Critical collisions ────────────────────────────────
    critical = {
        q: cl for q, cl in collision_map.items()
        if len(cl) >= SEVERITY_CRITICAL
    }
    lines += [
        "",
        div,
        f"  3. CRITICAL COLLISIONS — {len(critical)} questions (>{SEVERITY_CRITICAL-1} chunks each)",
        "     These MUST be removed — they flood retrieval and cause",
        "     context pollution across unrelated queries.",
        sep,
    ]
    if not critical:
        lines.append("  ✅ No critical collisions found!")
    else:
        for i, (q, cl) in enumerate(
            sorted(critical.items(), key=lambda x: -len(x[1])), 1
        ):
            unique_urls = set(e["source_url"] for e in cl)
            types       = set(e["content_type"] for e in cl)
            lines += [
                f"\n  [{i:03d}] \"{q}\"",
                f"        Appears in: {len(cl)} chunks | "
                f"{len(unique_urls)} unique URLs | "
                f"Types: {', '.join(sorted(types))}",
                f"        Sample URLs:",
            ]
            for url in sorted(unique_urls)[:3]:
                lines.append(f"          {url[:68]}")
            if len(unique_urls) > 3:
                lines.append(f"          ... and {len(unique_urls)-3} more URLs")

    # ── 4. High collisions ────────────────────────────────────
    high_cols = {
        q: cl for q, cl in collision_map.items()
        if SEVERITY_HIGH <= len(cl) < SEVERITY_CRITICAL
    }
    lines += [
        "",
        div,
        f"  4. HIGH COLLISIONS — {len(high_cols)} questions ({SEVERITY_HIGH}-{SEVERITY_CRITICAL-1} chunks each)",
        "     Action needed — review and remove from most chunks.",
        sep,
    ]
    if not high_cols:
        lines.append("  ✅ No high collisions found!")
    else:
        for i, (q, cl) in enumerate(
            sorted(high_cols.items(), key=lambda x: -len(x[1])), 1
        ):
            unique_urls = set(e["source_url"] for e in cl)
            types       = set(e["content_type"] for e in cl)
            lines.append(
                f"  [{i:03d}] \"{q[:65]}\""
                f"  →  {len(cl)} chunks | {len(unique_urls)} URLs"
            )

    # ── 5. Medium collisions ──────────────────────────────────
    medium_cols = {
        q: cl for q, cl in collision_map.items()
        if SEVERITY_MEDIUM <= len(cl) < SEVERITY_HIGH
    }
    lines += [
        "",
        div,
        f"  5. MEDIUM COLLISIONS — {len(medium_cols)} questions "
        f"({SEVERITY_MEDIUM}-{SEVERITY_HIGH-1} chunks each)",
        "     Review — acceptable if from different relevant pages.",
        sep,
    ]
    if not medium_cols:
        lines.append("  ✅ No medium collisions found!")
    else:
        for i, (q, cl) in enumerate(
            sorted(medium_cols.items(), key=lambda x: -len(x[1]))[:30], 1
        ):
            unique_urls = set(e["source_url"] for e in cl)
            lines.append(
                f"  [{i:03d}] \"{q[:60]}\"  "
                f"→  {len(cl)} chunks | {len(unique_urls)} URLs"
            )
        if len(medium_cols) > 30:
            lines.append(
                f"  ... and {len(medium_cols)-30} more medium collisions "
                f"(see JSON report for full list)"
            )

    # ── 6. Content type breakdown ─────────────────────────────
    lines += [
        "",
        div,
        "  6. CONTENT TYPE BREAKDOWN",
        sep,
        f"  {'Type':<20} {'Collision chunks':>18}  Notes",
        sep,
    ]
    for ct, count in sorted(
        impact["type_collision_counts"].items(), key=lambda x: -x[1]
    ):
        note = ""
        if ct == "webinar":
            note = "← transcript chunks share many topic questions"
        elif ct == "corporate":
            note = "← expected: similar content across pages"
        elif ct == "guide":
            note = "← largest group, most collision exposure"
        lines.append(f"  {ct:<20} {count:>18}  {note}")

    # ── 7. Recommended actions ────────────────────────────────
    lines += [
        "",
        div,
        "  7. RECOMMENDED ACTIONS",
        sep,
        "",
        "  IMMEDIATE (before go-live):",
    ]
    if sev["critical"] > 0:
        lines += [
            f"  🔴 Remove {sev['critical']} critical questions from ALL chunks.",
            f"     These are generic questions that appear in >{SEVERITY_CRITICAL-1} chunks.",
            f"     Add them to a BLOCKED_QUESTIONS list in chunk_and_index.py",
            f"     so they're never generated again on re-index.",
        ]
    if sev["high"] > 0:
        lines += [
            f"  🟠 Review {sev['high']} high-collision questions.",
            f"     Keep only in the 1-2 most relevant chunks per question.",
            f"     Remove from all others.",
        ]
    lines += [
        "",
        "  NEXT RE-INDEX:",
        "  🔧 Add cross-chunk uniqueness check to chunk_and_index.py:",
        "     After generating questions for all chunks, scan for",
        "     collisions and remove questions appearing in > 3 chunks.",
        "     This prevents new collisions from entering the index.",
        "",
        "  RETRIEVER ENHANCEMENT (future):",
        "  🔧 Add semantic diversity check in retriever.py:",
        "     If chunk A and chunk B have cosine similarity > 0.95,",
        "     keep only the higher-scoring one.",
        "     This catches content-level duplicates beyond question collisions.",
        "",
        div,
        "  END OF REPORT",
        div, "",
    ]

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"   📄 Text report: {output_path}")


# ── Main ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Find HQA question collisions across index chunks"
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help=f"Override TOP_K for impact analysis (default: {TOP_K} from env)",
    )
    args = parser.parse_args()

    top_k = args.top_k or TOP_K

    print("\n" + "=" * 72)
    print("  ARIA — HQA QUESTION COLLISION DIAGNOSTIC")
    print("=" * 72)
    print(f"  Index:  {INDEX_NAME}")
    print(f"  TOP_K:  {top_k}")
    print(f"  Normalise questions: {NORMALISE}")

    if not AZURE_SEARCH_ENDPOINT:
        print("❌ AZURE_SEARCH_ENDPOINT not set in .env")
        sys.exit(1)

    client = get_search_client()

    # ── Step 1: Fetch all chunks ──────────────────────────────
    chunks = fetch_all_chunks(client)
    if not chunks:
        print("❌ No chunks fetched. Is the index populated?")
        sys.exit(1)

    # ── Step 2: Detect collisions ─────────────────────────────
    detection  = detect_collisions(chunks)
    stats      = detection["stats"]
    collision_map = detection["collision_map"]

    # ── Step 3: Analyse retrieval impact ──────────────────────
    print(f"\n📊 Analysing retrieval impact...")
    impact = analyse_retrieval_impact(collision_map, chunks)

    print(
        f"   Cross-page (dangerous):  {impact['cross_page_collisions']:,}\n"
        f"   Within-page (safe):      {impact['within_page_collisions']:,}"
    )

    # ── Step 4: Save outputs ──────────────────────────────────
    ts         = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = Path("scraper/data")
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"collision_report_{ts}.json"
    txt_path  = output_dir / f"collision_report_{ts}.txt"

    # JSON — full collision data for programmatic use
    print(f"\n💾 Saving reports...")
    json_output = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "index":         INDEX_NAME,
        "top_k":         top_k,
        "normalised":    NORMALISE,
        "stats":         stats,
        "impact":        {
            k: v for k, v in impact.items()
            if k != "dangerous_questions"
        },
        "dangerous_questions": impact["dangerous_questions"],
        "collisions": {
            severity: {
                q: [
                    {
                        "chunk_id":       e["chunk_id"],
                        "title":          e["title"],
                        "source_url":     e["source_url"],
                        "content_type":   e["content_type"],
                        "original_q":     e["original_question"],
                    }
                    for e in cl
                ]
                for q, cl in collision_map.items()
                if _severity_label(len(cl)) == severity
            }
            for severity in ["critical", "high", "medium", "low"]
        },
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_output, f, ensure_ascii=False, indent=2)
    print(f"   💾 JSON report: {json_path}")

    # Text report
    generate_text_report(stats, impact, collision_map, str(txt_path))

    # ── Final summary ─────────────────────────────────────────
    sev = stats["severity_distribution"]
    print(f"\n{'='*72}")
    print(f"  SUMMARY")
    print(f"{'='*72}")
    print(f"  {stats['total_questions_scanned']:,} questions scanned across "
          f"{stats['chunks_with_hqa']:,} chunks")
    print(f"  {stats['colliding_questions']:,} colliding ({stats['collision_rate_pct']}%)")
    print(f"  🔴 Critical: {sev['critical']}  "
          f"🟠 High: {sev['high']}  "
          f"🟡 Medium: {sev['medium']}  "
          f"🟢 Low: {sev['low']}")
    print(f"\n  Review: {txt_path.name}")
    print(f"  Data:   {json_path.name}")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()