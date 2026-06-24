"""
HQA Model Comparison — gpt-4o-mini vs gpt-4.1
===============================================
Fetches representative chunks from Azure AI Search,
sends them to BOTH models with the same grading prompt,
and produces a side-by-side quality comparison report.

Purpose:
    Before committing to gpt-4o-mini for the full index
    quality evaluation, verify whether gpt-4.1 produces
    meaningfully better grading decisions on the same chunks.
    If difference is negligible → stick with gpt-4o-mini (~$0.50).
    If gpt-4.1 catches significantly more issues → switch (~$17).

Sampling strategy:
    Chunks are selected across THREE dimensions to give a
    representative picture of question quality across the index:

    Dimension 1 — Content type:
        webinar, guide, article, faq, tool, corporate
        (ensures we see quality across all page types)

    Dimension 2 — Chunk size:
        Short  (<300 words)  — transition/nav content risk
        Medium (300-700 words) — typical FAQ chunk
        Long   (>700 words)  — webinar transcript risk

    Dimension 3 — Chunk position:
        First chunk (chunk_index=0) — intro/header content
        Middle chunk — main body content
        Last chunk  — conclusion/CTA content

HOW TO RUN:
    python scraper/compare_hqa_models.py

    With custom sample size:
    python scraper/compare_hqa_models.py --samples 30

OUTPUT:
    scraper/data/hqa_model_comparison_<timestamp>.json
    scraper/data/hqa_model_comparison_<timestamp>.txt  (human readable)

REQUIRES:
    - .env with AZURE_SEARCH_ENDPOINT, AZURE_OPENAI_ENDPOINT
    - Index must exist with augmented_questions field populated
    - pip install azure-search-documents azure-identity openai
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
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from openai import AzureOpenAI
from dotenv import load_dotenv, find_dotenv

_dotenv_path = find_dotenv(usecwd=False)
load_dotenv(_dotenv_path)
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
INDEX_NAME            = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index")
DEPLOYMENT_FAST       = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4o-mini")
DEPLOYMENT_MAIN       = os.getenv("AZURE_OPENAI_DEPLOYMENT_MAIN", "gpt-4.1")
API_VERSION           = "2024-12-01-preview"

# Default sample size — covers all dimensions without being too expensive
DEFAULT_SAMPLE_SIZE   = 20

# Chunk size thresholds (word count)
SHORT_CHUNK_THRESHOLD  = 300
LONG_CHUNK_THRESHOLD   = 700

# ── Grading prompt (same for both models — fair comparison) ───
# Production-grade grading criteria covering 7 dimensions.
# Both models receive identical prompt — only model differs.
GRADING_SYSTEM_PROMPT = """You are a quality evaluator for a Royal London insurance and pension FAQ chatbot knowledge base.

Your task is to grade each question generated for a text chunk on 7 criteria.

GRADING CRITERIA (score each 1-5):

1. SPECIFICITY (1-5)
   5 = Only answerable using THIS exact chunk, not general knowledge
   3 = Partially answerable from this chunk but also from general knowledge
   1 = Could be answered without reading this chunk at all

2. CUSTOMER_RELEVANCE (1-5)
   5 = Exactly what a Royal London customer would ask when searching
   3 = Plausible customer question but slightly unnatural phrasing
   1 = Not how a customer would phrase this, or not customer-facing

3. CLARITY (1-5)
   5 = Perfectly clear, grammatically correct, unambiguous
   3 = Understandable but slightly awkward phrasing
   1 = Confusing, grammatically incorrect, or ambiguous

4. UNIQUENESS (1-5)
   5 = Distinct from all other questions in this set
   3 = Overlaps somewhat with another question but different angle
   1 = Near-duplicate of another question in this set

5. GROUNDEDNESS (1-5)
   5 = All key terms in the question appear in the chunk content
   3 = Most terms are grounded, one or two are extrapolated
   1 = Question mentions topics not present in the chunk

6. ANSWERABILITY (1-5)
   5 = The chunk content fully and directly answers this question
   3 = Chunk partially answers but customer would need more info
   1 = Chunk does not actually answer this question

7. TONE (1-5)
   5 = Natural customer-facing language, warm and accessible
   3 = Acceptable but slightly formal or technical
   1 = Technical jargon, internal language, or inappropriate register

CONTEXT_POLLUTION_RISK (True/False):
   True = This question could retrieve WRONG chunks for customers
          (too generic, too similar to questions from other topics,
          or uses terms that appear in many unrelated pages)
   False = Question is specific enough to retrieve only relevant chunks

OVERALL_QUALITY (1-5):
   Overall assessment combining all criteria.
   5 = Production ready, will improve retrieval significantly
   4 = Good quality, minor issues
   3 = Acceptable, some concerns
   2 = Poor quality, likely to hurt retrieval
   1 = Should be removed, will cause context pollution

Return ONLY valid JSON in this exact format — no explanation, no preamble:
{
  "questions": [
    {
      "question": "exact question text",
      "scores": {
        "specificity": 4,
        "customer_relevance": 5,
        "clarity": 5,
        "uniqueness": 4,
        "groundedness": 5,
        "answerability": 4,
        "tone": 5
      },
      "context_pollution_risk": false,
      "overall_quality": 4,
      "keep": true,
      "rejection_reason": ""
    }
  ],
  "chunk_assessment": {
    "chunk_quality": "good",
    "issues": [],
    "recommendation": "keep_all"
  }
}

chunk_quality values: "excellent" | "good" | "acceptable" | "poor"
recommendation values: "keep_all" | "keep_with_filtering" | "regenerate" | "remove_chunk"
issues examples: ["transition_content", "too_generic", "duplicate_overlap", "context_pollution"]
keep: true if overall_quality >= 3, false if overall_quality <= 2
rejection_reason: empty string if keep=true, brief reason if keep=false"""


# ── Clients ───────────────────────────────────────────────────
def get_clients() -> tuple[SearchClient, AzureOpenAI]:
    """Get Azure Search and OpenAI clients."""
    credential     = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential,
        "https://cognitiveservices.azure.com/.default",
    )
    search_client = SearchClient(
        endpoint=AZURE_SEARCH_ENDPOINT,
        index_name=INDEX_NAME,
        credential=credential,
    )
    openai_client = AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_ad_token_provider=token_provider,
        api_version=API_VERSION,
    )
    return search_client, openai_client


# ── Sampling strategy ─────────────────────────────────────────
def fetch_representative_chunks(
    search_client: SearchClient,
    target_count: int = DEFAULT_SAMPLE_SIZE,
) -> list[dict]:
    """
    Fetch representative chunks across all three sampling dimensions:

    Dimension 1 — Content type (webinar/guide/article/faq/tool/corporate)
    Dimension 2 — Chunk size (short/medium/long by word count)
    Dimension 3 — Chunk position (first/middle/last within page)

    Only fetches chunks that HAVE augmented_questions — chunks without
    HQA questions can't be graded.

    Strategy:
    - Allocate slots proportionally across content types
    - Within each content type, ensure mix of sizes and positions
    - Prioritise: webinar (longest/most complex) and guide (largest group)
    """
    print(f"\n📊 Fetching representative sample ({target_count} chunks)...")

    # Content types to sample — weight by importance for grading
    # Webinars: long transcripts, highest context pollution risk
    # Guides: largest group, most customer-facing
    # Others: smaller groups, proportional representation
    type_weights = {
        "webinar":   0.25,  # highest pollution risk — oversample
        "guide":     0.30,  # largest group
        "article":   0.20,
        "corporate": 0.10,  # lowest priority
        "faq":       0.10,
        "tool":      0.05,
    }

    chunks_by_type  = defaultdict(list)
    all_chunks      = []
    fetched         = 0
    max_fetch       = min(target_count * 10, 500)  # fetch pool to sample from

    # Fetch a pool of chunks with augmented_questions
    try:
        results = search_client.search(
            search_text="*",
            select=[
                "chunk_id", "content", "title", "source_url",
                "section", "content_type", "product_category",
                "chunk_index", "total_chunks", "augmented_questions",
                "has_video",
            ],
            filter="augmented_questions ne ''",
            top=max_fetch,
        )
        for r in results:
            aq = r.get("augmented_questions", "") or ""
            if not aq.strip():
                continue
            chunk = {
                "chunk_id":           r["chunk_id"],
                "content":            r["content"],
                "title":              r.get("title", ""),
                "source_url":         r.get("source_url", ""),
                "section":            r.get("section", ""),
                "content_type":       r.get("content_type", "article"),
                "product_category":   r.get("product_category", "general"),
                "chunk_index":        r.get("chunk_index", 0),
                "total_chunks":       r.get("total_chunks", 1),
                "augmented_questions": aq,
                "has_video":          r.get("has_video", False),
                "word_count":         len(r["content"].split()),
            }
            # Classify size
            wc = chunk["word_count"]
            if wc < SHORT_CHUNK_THRESHOLD:
                chunk["size_category"] = "short"
            elif wc > LONG_CHUNK_THRESHOLD:
                chunk["size_category"] = "long"
            else:
                chunk["size_category"] = "medium"

            # Classify position
            idx   = chunk["chunk_index"]
            total = chunk["total_chunks"]
            if idx == 0:
                chunk["position"] = "first"
            elif total > 1 and idx == total - 1:
                chunk["position"] = "last"
            else:
                chunk["position"] = "middle"

            chunks_by_type[chunk["content_type"]].append(chunk)
            all_chunks.append(chunk)
            fetched += 1

    except Exception as e:
        log.error("fetch_chunks_error", error=str(e))
        print(f"   ❌ Error fetching chunks: {e}")
        return []

    print(f"   Fetched {fetched} chunks with HQA questions")
    print(f"   Content type distribution:")
    for ct, chunks in sorted(chunks_by_type.items()):
        print(f"     {ct:<15} {len(chunks):>4} chunks")

    # ── Select representative sample ──────────────────────────
    selected = []

    for content_type, weight in type_weights.items():
        type_chunks = chunks_by_type.get(content_type, [])
        if not type_chunks:
            continue

        # Slots for this content type
        slots = max(1, round(target_count * weight))
        slots = min(slots, len(type_chunks))

        # Within this type, select across size and position dimensions
        # Priority: long chunks (highest pollution risk), first/last positions
        size_priority  = {"long": 0, "medium": 1, "short": 2}
        pos_priority   = {"first": 0, "last": 1, "middle": 2}

        sorted_chunks = sorted(
            type_chunks,
            key=lambda c: (
                size_priority.get(c["size_category"], 2),
                pos_priority.get(c["position"], 2),
            )
        )

        # Take from start (highest priority) and spread through list
        if len(sorted_chunks) <= slots:
            selected.extend(sorted_chunks)
        else:
            # Take evenly spaced + first + last for variety
            step    = len(sorted_chunks) // slots
            indices = list(range(0, len(sorted_chunks), step))[:slots]
            for i in indices:
                selected.append(sorted_chunks[i])

    # Deduplicate by chunk_id
    seen     = set()
    deduped  = []
    for c in selected:
        if c["chunk_id"] not in seen:
            seen.add(c["chunk_id"])
            deduped.append(c)

    # Trim to target
    deduped = deduped[:target_count]

    print(f"\n   Selected {len(deduped)} representative chunks:")
    size_counts = defaultdict(int)
    pos_counts  = defaultdict(int)
    type_counts = defaultdict(int)
    for c in deduped:
        size_counts[c["size_category"]] += 1
        pos_counts[c["position"]]       += 1
        type_counts[c["content_type"]]  += 1

    print(f"   By size:     " + " | ".join(f"{k}: {v}" for k, v in size_counts.items()))
    print(f"   By position: " + " | ".join(f"{k}: {v}" for k, v in pos_counts.items()))
    print(f"   By type:     " + " | ".join(f"{k}: {v}" for k, v in type_counts.items()))

    return deduped


# ── Model grading ─────────────────────────────────────────────
def grade_chunk_questions(
    openai_client: AzureOpenAI,
    chunk: dict,
    model: str,
    max_retries: int = 3,
) -> dict | None:
    """
    Send chunk content + HQA questions to model for grading.
    Returns parsed grading result or None on failure.
    """
    import re

    questions = chunk["augmented_questions"].split("\n")
    questions = [q.strip() for q in questions if q.strip()]

    user_prompt = (
        f"CHUNK CONTENT:\n{chunk['content'][:2000]}\n\n"
        f"CHUNK METADATA:\n"
        f"  Title: {chunk['title']}\n"
        f"  Content type: {chunk['content_type']}\n"
        f"  Product category: {chunk['product_category']}\n"
        f"  Chunk position: {chunk['position']} "
        f"({chunk['chunk_index']+1} of {chunk['total_chunks']})\n"
        f"  Word count: {chunk['word_count']}\n\n"
        f"HQA QUESTIONS TO GRADE:\n"
        + "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    )

    for attempt in range(max_retries):
        try:
            response = openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": GRADING_SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                max_tokens=1500,
                temperature=0.1,  # Low temp for consistent grading
            )

            raw = response.choices[0].message.content or ""
            raw = raw.strip()

            # Strip markdown fences if present
            raw = re.sub(r"```(?:json)?\n?", "", raw).strip()
            raw = raw.rstrip("`").strip()

            result = json.loads(raw)

            # Add token usage for cost tracking
            result["_token_usage"] = {
                "input":  response.usage.prompt_tokens,
                "output": response.usage.completion_tokens,
                "model":  model,
            }
            return result

        except json.JSONDecodeError as e:
            log.warning(
                "grading_json_error",
                model=model,
                chunk_id=chunk["chunk_id"][:8],
                attempt=attempt + 1,
                error=str(e),
            )
            if attempt < max_retries - 1:
                time.sleep(2)

        except Exception as e:
            if "rate" in str(e).lower() or "429" in str(e):
                wait = 10 * (2 ** attempt)
                print(f"   ⚠️  Rate limit ({model}). Waiting {wait}s...")
                time.sleep(wait)
            else:
                log.error(
                    "grading_error",
                    model=model,
                    chunk_id=chunk["chunk_id"][:8],
                    error=str(e),
                )
                if attempt < max_retries - 1:
                    time.sleep(2)

    return None


# ── Comparison analysis ───────────────────────────────────────
def compute_model_agreement(
    fast_result: dict,
    main_result: dict,
) -> dict:
    """
    Compare grading decisions between two models on same chunk.

    Agreement metrics:
    - overall_agreement: both models agree on keep/reject per question
    - score_delta: average absolute difference in overall_quality scores
    - disagreements: questions where models disagree on keep/reject
    - chunk_recommendation_match: both agree on chunk-level recommendation
    """
    if not fast_result or not main_result:
        return {"error": "One or both models failed to grade"}

    fast_qs = {
        q["question"]: q
        for q in fast_result.get("questions", [])
    }
    main_qs = {
        q["question"]: q
        for q in main_result.get("questions", [])
    }

    agreements    = 0
    disagreements = []
    score_deltas  = []
    common_qs     = set(fast_qs.keys()) & set(main_qs.keys())

    for q_text in common_qs:
        fq = fast_qs[q_text]
        mq = main_qs[q_text]

        fast_keep = fq.get("keep", True)
        main_keep = mq.get("keep", True)
        fast_score = fq.get("overall_quality", 3)
        main_score = mq.get("overall_quality", 3)

        score_deltas.append(abs(fast_score - main_score))

        if fast_keep == main_keep:
            agreements += 1
        else:
            disagreements.append({
                "question":         q_text,
                "fast_keep":        fast_keep,
                "fast_score":       fast_score,
                "main_keep":        main_keep,
                "main_score":       main_score,
                "fast_reason":      fq.get("rejection_reason", ""),
                "main_reason":      mq.get("rejection_reason", ""),
            })

    total           = len(common_qs) if common_qs else 1
    agreement_pct   = round(agreements / total * 100, 1)
    avg_score_delta = round(sum(score_deltas) / len(score_deltas), 2) if score_deltas else 0

    fast_rec = fast_result.get("chunk_assessment", {}).get("recommendation", "")
    main_rec = main_result.get("chunk_assessment", {}).get("recommendation", "")

    return {
        "overall_agreement_pct":         agreement_pct,
        "avg_score_delta":               avg_score_delta,
        "questions_compared":            total,
        "disagreements":                 disagreements,
        "chunk_recommendation_match":    fast_rec == main_rec,
        "fast_recommendation":           fast_rec,
        "main_recommendation":           main_rec,
    }


def compute_cost_estimate(results: list[dict]) -> dict:
    """Calculate token usage and cost for both models."""
    fast_input  = sum(
        r.get("fast_result", {}).get("_token_usage", {}).get("input", 0)
        for r in results if r.get("fast_result")
    )
    fast_output = sum(
        r.get("fast_result", {}).get("_token_usage", {}).get("output", 0)
        for r in results if r.get("fast_result")
    )
    main_input  = sum(
        r.get("main_result", {}).get("_token_usage", {}).get("input", 0)
        for r in results if r.get("main_result")
    )
    main_output = sum(
        r.get("main_result", {}).get("_token_usage", {}).get("output", 0)
        for r in results if r.get("main_result")
    )

    # Azure OpenAI pricing (approximate, USD per 1K tokens)
    FAST_IN, FAST_OUT = 0.00015, 0.00060
    MAIN_IN, MAIN_OUT = 0.002,   0.008

    sample_size    = len(results)
    full_index     = 6827

    fast_sample_cost = (fast_input * FAST_IN + fast_output * FAST_OUT) / 1000
    main_sample_cost = (main_input * MAIN_IN + main_output * MAIN_OUT) / 1000

    scale_factor     = full_index / sample_size if sample_size else 1
    fast_full_cost   = fast_sample_cost * scale_factor
    main_full_cost   = main_sample_cost * scale_factor

    return {
        "sample_size":       sample_size,
        "fast_model": {
            "sample_tokens":  fast_input + fast_output,
            "sample_cost_usd": round(fast_sample_cost, 4),
            "full_index_cost_usd": round(fast_full_cost, 2),
        },
        "main_model": {
            "sample_tokens":  main_input + main_output,
            "sample_cost_usd": round(main_sample_cost, 4),
            "full_index_cost_usd": round(main_full_cost, 2),
        },
        "cost_multiplier": round(main_full_cost / fast_full_cost, 1) if fast_full_cost else 0,
    }


# ── Report generation ─────────────────────────────────────────
def generate_text_report(
    comparison_results: list[dict],
    summary: dict,
    output_path: str,
):
    """Generate human-readable comparison report."""
    lines = []
    div   = "=" * 72

    lines.append(div)
    lines.append("  HQA MODEL COMPARISON REPORT")
    lines.append(f"  {DEPLOYMENT_FAST} vs {DEPLOYMENT_MAIN}")
    lines.append(f"  Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"  Index: {INDEX_NAME}")
    lines.append(div)

    # ── Summary ───────────────────────────────────────────────
    lines.append("\n  OVERALL SUMMARY")
    lines.append("-" * 72)
    lines.append(f"  Chunks compared:           {summary['chunks_compared']}")
    lines.append(f"  Questions compared:        {summary['total_questions_compared']}")
    lines.append(f"  Avg agreement rate:        {summary['avg_agreement_pct']}%")
    lines.append(f"  Avg score delta:           {summary['avg_score_delta']} / 5.0")
    lines.append(f"  Chunk rec. match rate:     {summary['chunk_rec_match_pct']}%")
    lines.append(f"  Chunks with disagreement:  {summary['chunks_with_disagreement']}")

    lines.append("\n  COST COMPARISON")
    lines.append("-" * 72)
    cost = summary["cost_estimate"]
    lines.append(f"  Sample size: {cost['sample_size']} chunks")
    lines.append(
        f"  {DEPLOYMENT_FAST:<20} "
        f"sample: ${cost['fast_model']['sample_cost_usd']:.4f}  |  "
        f"full index: ${cost['fast_model']['full_index_cost_usd']:.2f}"
    )
    lines.append(
        f"  {DEPLOYMENT_MAIN:<20} "
        f"sample: ${cost['main_model']['sample_cost_usd']:.4f}  |  "
        f"full index: ${cost['main_model']['full_index_cost_usd']:.2f}"
    )
    lines.append(
        f"  Cost multiplier: {cost['cost_multiplier']}x "
        f"(gpt-4.1 is {cost['cost_multiplier']}x more expensive)"
    )

    # ── Recommendation ────────────────────────────────────────
    lines.append("\n  MODEL RECOMMENDATION")
    lines.append("-" * 72)
    avg_agree  = summary["avg_agreement_pct"]
    avg_delta  = summary["avg_score_delta"]
    multiplier = cost["cost_multiplier"]

    if avg_agree >= 90 and avg_delta <= 0.5:
        rec = f"✅ USE {DEPLOYMENT_FAST}"
        reason = (
            f"Agreement rate {avg_agree}% with avg score delta {avg_delta} "
            f"indicates models produce equivalent grading quality. "
            f"No justification for {multiplier}x cost increase."
        )
    elif avg_agree >= 75 and avg_delta <= 1.0:
        rec = f"⚠️  BORDERLINE — review disagreements below"
        reason = (
            f"Moderate agreement {avg_agree}% with delta {avg_delta}. "
            f"Check disagreement cases — if {DEPLOYMENT_MAIN} catches "
            f"important issues {DEPLOYMENT_FAST} misses, consider upgrading."
        )
    else:
        rec = f"🔴 CONSIDER {DEPLOYMENT_MAIN}"
        reason = (
            f"Low agreement {avg_agree}% with high delta {avg_delta} "
            f"suggests meaningful quality difference. "
            f"Extra cost may be justified."
        )

    lines.append(f"  {rec}")
    lines.append(f"  {reason}")

    # ── Per-chunk results ─────────────────────────────────────
    lines.append("\n\n" + div)
    lines.append("  PER-CHUNK COMPARISON")
    lines.append(div)

    for i, result in enumerate(comparison_results, 1):
        chunk   = result["chunk"]
        agree   = result["agreement"]
        fast_r  = result.get("fast_result")
        main_r  = result.get("main_result")

        lines.append(
            f"\n[{i:02d}] {chunk['title'][:55]}"
        )
        lines.append(
            f"     Type: {chunk['content_type']:<12} "
            f"Size: {chunk['size_category']:<8} "
            f"Position: {chunk['position']:<8} "
            f"Words: {chunk['word_count']}"
        )
        lines.append(
            f"     URL: {chunk['source_url'][:60]}"
        )

        if "error" in agree:
            lines.append(f"     ❌ Error: {agree['error']}")
            continue

        lines.append(
            f"     Agreement: {agree['overall_agreement_pct']}%  |  "
            f"Score delta: {agree['avg_score_delta']}  |  "
            f"Rec match: {'✅' if agree['chunk_recommendation_match'] else '❌'}"
        )

        if fast_r:
            fast_keep  = sum(1 for q in fast_r.get("questions", []) if q.get("keep"))
            fast_total = len(fast_r.get("questions", []))
            fast_rec   = fast_r.get("chunk_assessment", {}).get("recommendation", "?")
            lines.append(
                f"     {DEPLOYMENT_FAST:<20} keep {fast_keep}/{fast_total} | "
                f"rec: {fast_rec}"
            )

        if main_r:
            main_keep  = sum(1 for q in main_r.get("questions", []) if q.get("keep"))
            main_total = len(main_r.get("questions", []))
            main_rec   = main_r.get("chunk_assessment", {}).get("recommendation", "?")
            lines.append(
                f"     {DEPLOYMENT_MAIN:<20} keep {main_keep}/{main_total} | "
                f"rec: {main_rec}"
            )

        # Show questions with scores
        questions = chunk["augmented_questions"].split("\n")
        questions = [q.strip() for q in questions if q.strip()]
        fast_qs   = {
            q["question"]: q
            for q in (fast_r.get("questions", []) if fast_r else [])
        }
        main_qs   = {
            q["question"]: q
            for q in (main_r.get("questions", []) if main_r else [])
        }

        lines.append(f"\n     Questions:")
        for q in questions:
            fq    = fast_qs.get(q, {})
            mq    = main_qs.get(q, {})
            fkeep = "✅" if fq.get("keep", True) else "❌"
            mkeep = "✅" if mq.get("keep", True) else "❌"
            fscore = fq.get("overall_quality", "?")
            mscore = mq.get("overall_quality", "?")
            match  = "=" if fq.get("keep", True) == mq.get("keep", True) else "≠"

            wrapped = textwrap.wrap(q, width=50)
            lines.append(
                f"     {match} [{fkeep}{fscore} vs {mkeep}{mscore}] "
                f"{wrapped[0]}"
            )
            for extra in wrapped[1:]:
                lines.append(f"                          {extra}")

            # Show disagreement detail
            if fq.get("keep", True) != mq.get("keep", True):
                if fq.get("rejection_reason"):
                    lines.append(
                        f"         {DEPLOYMENT_FAST}: {fq['rejection_reason']}"
                    )
                if mq.get("rejection_reason"):
                    lines.append(
                        f"         {DEPLOYMENT_MAIN}: {mq['rejection_reason']}"
                    )

        lines.append("-" * 72)

    # ── Disagreement summary ──────────────────────────────────
    all_disagreements = []
    for r in comparison_results:
        for d in r.get("agreement", {}).get("disagreements", []):
            all_disagreements.append({
                "chunk_title": r["chunk"]["title"],
                **d,
            })

    if all_disagreements:
        lines.append(f"\n\n  ALL DISAGREEMENTS ({len(all_disagreements)} total)")
        lines.append("-" * 72)
        for d in all_disagreements:
            lines.append(f"\n  Chunk: {d['chunk_title'][:55]}")
            lines.append(f"  Q: {d['question'][:65]}")
            lines.append(
                f"     {DEPLOYMENT_FAST}: {'KEEP' if d['fast_keep'] else 'REJECT'} "
                f"(score {d['fast_score']}) {d.get('fast_reason','')}"
            )
            lines.append(
                f"     {DEPLOYMENT_MAIN}: {'KEEP' if d['main_keep'] else 'REJECT'} "
                f"(score {d['main_score']}) {d.get('main_reason','')}"
            )

    lines.append(f"\n{div}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n   📄 Text report: {output_path}")


# ── Main ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Compare HQA grading quality between gpt-4o-mini and gpt-4.1"
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help=f"Number of chunks to compare (default: {DEFAULT_SAMPLE_SIZE})",
    )
    args = parser.parse_args()

    print("\n" + "=" * 72)
    print("  HQA MODEL COMPARISON")
    print(f"  {DEPLOYMENT_FAST} vs {DEPLOYMENT_MAIN}")
    print("=" * 72)
    print(f"  Index:   {INDEX_NAME}")
    print(f"  Samples: {args.samples} chunks")
    print(f"  Est. cost: ~${args.samples * 0.002:.2f} total both models")

    # Validate config
    if not AZURE_OPENAI_ENDPOINT:
        print("❌ AZURE_OPENAI_ENDPOINT not set in .env")
        sys.exit(1)
    if not AZURE_SEARCH_ENDPOINT:
        print("❌ AZURE_SEARCH_ENDPOINT not set in .env")
        sys.exit(1)

    search_client, openai_client = get_clients()

    # Fetch representative chunks
    chunks = fetch_representative_chunks(search_client, args.samples)
    if not chunks:
        print("❌ No chunks fetched. Is the index populated?")
        sys.exit(1)

    # Grade each chunk with both models
    print(f"\n🔍 Grading {len(chunks)} chunks with both models...")
    print(f"   This will take ~{len(chunks) * 5 // 60 + 1} minutes\n")

    comparison_results = []
    total_fast_tokens  = 0
    total_main_tokens  = 0

    for i, chunk in enumerate(chunks, 1):
        short_title = chunk["title"][:40]
        print(
            f"  [{i:02d}/{len(chunks)}] {short_title:<40} "
            f"({chunk['content_type']}, {chunk['size_category']}, "
            f"{chunk['position']})",
            end="", flush=True,
        )

        # Grade with fast model
        fast_result = grade_chunk_questions(
            openai_client, chunk, DEPLOYMENT_FAST
        )
        time.sleep(0.5)  # avoid rate limit between models

        # Grade with main model
        main_result = grade_chunk_questions(
            openai_client, chunk, DEPLOYMENT_MAIN
        )
        time.sleep(0.5)

        # Compare
        agreement = compute_model_agreement(fast_result, main_result)
        agree_pct = agreement.get("overall_agreement_pct", 0)
        match_sym = "✅" if agree_pct >= 80 else "⚠️ " if agree_pct >= 60 else "❌"

        print(f" → {match_sym} {agree_pct}% agree")

        comparison_results.append({
            "chunk":       chunk,
            "fast_result": fast_result,
            "main_result": main_result,
            "agreement":   agreement,
        })

        # Small delay between chunks to respect rate limits
        if i < len(chunks):
            time.sleep(1.0)

    # ── Compute summary ───────────────────────────────────────
    valid_results = [
        r for r in comparison_results
        if "error" not in r["agreement"]
    ]

    agree_pcts     = [r["agreement"]["overall_agreement_pct"] for r in valid_results]
    score_deltas   = [r["agreement"]["avg_score_delta"] for r in valid_results]
    rec_matches    = [r["agreement"]["chunk_recommendation_match"] for r in valid_results]
    disagreements  = [
        r for r in valid_results
        if r["agreement"]["disagreements"]
    ]

    total_qs = sum(
        r["agreement"]["questions_compared"]
        for r in valid_results
    )

    cost_estimate = compute_cost_estimate(comparison_results)

    summary = {
        "generated_at":               datetime.now(timezone.utc).isoformat(),
        "index":                      INDEX_NAME,
        "fast_model":                 DEPLOYMENT_FAST,
        "main_model":                 DEPLOYMENT_MAIN,
        "chunks_compared":            len(valid_results),
        "total_questions_compared":   total_qs,
        "avg_agreement_pct":          round(sum(agree_pcts) / len(agree_pcts), 1) if agree_pcts else 0,
        "avg_score_delta":            round(sum(score_deltas) / len(score_deltas), 2) if score_deltas else 0,
        "chunk_rec_match_pct":        round(sum(rec_matches) / len(rec_matches) * 100, 1) if rec_matches else 0,
        "chunks_with_disagreement":   len(disagreements),
        "cost_estimate":              cost_estimate,
    }

    # Print summary
    print("\n" + "=" * 72)
    print("  RESULTS SUMMARY")
    print("=" * 72)
    print(f"  Chunks compared:       {summary['chunks_compared']}")
    print(f"  Questions compared:    {summary['total_questions_compared']}")
    print(f"  Avg agreement rate:    {summary['avg_agreement_pct']}%")
    print(f"  Avg score delta:       {summary['avg_score_delta']} / 5.0")
    print(f"  Rec match rate:        {summary['chunk_rec_match_pct']}%")
    print(f"  Disagreements:         {len(disagreements)} chunks")

    cost = cost_estimate
    print(f"\n  Cost (full index):")
    print(f"    {DEPLOYMENT_FAST}: ${cost['fast_model']['full_index_cost_usd']:.2f}")
    print(f"    {DEPLOYMENT_MAIN}: ${cost['main_model']['full_index_cost_usd']:.2f}")
    print(f"    Multiplier: {cost['cost_multiplier']}x")

    # Save outputs
    timestamp  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = Path("scraper/data")
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"hqa_model_comparison_{timestamp}.json"
    txt_path  = output_dir / f"hqa_model_comparison_{timestamp}.txt"

    # Save JSON (remove chunk content to keep file size manageable)
    json_output = {
        "summary": summary,
        "results": [
            {
                "chunk_id":       r["chunk"]["chunk_id"],
                "chunk_title":    r["chunk"]["title"],
                "content_type":   r["chunk"]["content_type"],
                "size_category":  r["chunk"]["size_category"],
                "position":       r["chunk"]["position"],
                "word_count":     r["chunk"]["word_count"],
                "questions":      r["chunk"]["augmented_questions"].split("\n"),
                "fast_result":    {
                    k: v for k, v in (r.get("fast_result") or {}).items()
                    if k != "_token_usage"
                },
                "main_result":    {
                    k: v for k, v in (r.get("main_result") or {}).items()
                    if k != "_token_usage"
                },
                "agreement":      r["agreement"],
            }
            for r in comparison_results
        ],
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_output, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 JSON report: {json_path}")

    # Save text report
    generate_text_report(comparison_results, summary, str(txt_path))

    print("\n  ✅ Comparison complete!")
    print(f"  Review the text report for model recommendation.")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    main()