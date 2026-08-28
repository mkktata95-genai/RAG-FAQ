"""
generate_answers_from_po_prompt.py — Answer Generation (PO-supplied prompt)

═══════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════
Fills in expected_answer for an EXISTING golden dataset CSV (e.g. the
~292-question output of build_golden_dataset_from_urls.py) using the
exact extraction prompt supplied by the PO — no new questions are
generated, only answers for questions that already exist.

Standalone. Does NOT import anything from the RLG pipeline or from any
other script in this project — self-contained per project convention.

═══════════════════════════════════════════════════════════════
PRACTICAL ADAPTATION — WORTH FLAGGING TO THE PO
═══════════════════════════════════════════════════════════════
The PO's prompt is written as if the model can browse the given URL
directly ("Base your answer exclusively on the content available at
the given URL"). A plain LLM API call cannot fetch a live URL. This
script satisfies that instruction by fetching the REAL page content
from the approved-content search index (same content the RLG chatbot
itself was built from — same source of truth, same source_url filter
already used elsewhere in this project) and injecting it into the
prompt alongside the URL. Every other instruction in the PO's prompt
is preserved VERBATIM — nothing about the source-restriction, accuracy,
missing-information handling, format, or output rules was changed.

═══════════════════════════════════════════════════════════════
AUTOMATIC FLAGS (from the PO's own prompt rules, not invented here)
═══════════════════════════════════════════════════════════════
The PO's prompt defines two specific behaviors that are worth catching
programmatically rather than only eyeballing 292 rows by hand:
  - "no_info_found"   — model returned the exact fallback sentence the
                         prompt specifies for unanswerable questions.
                         Worth a decision: is the question bad (should
                         be dropped/reworded), or is this a legitimate
                         signal the page genuinely doesn't cover it?
  - "exceeds_250_chars" — model's answer ran over the prompt's stated
                         limit. Never silently truncated (could cut a
                         sentence mid-thought and misrepresent the
                         source) — flagged for manual review instead.

USAGE
    python generate_answers_from_po_prompt.py --input golden_dataset_full_internal.csv --dry-run --limit 5
    python generate_answers_from_po_prompt.py --input golden_dataset_full_internal.csv --out-prefix golden_dataset_po_answers

REQUIRES
    .env with AZURE_SEARCH_ENDPOINT and AZURE_OPENAI_ENDPOINT set
    pip install azure-search-documents azure-identity openai python-dotenv
    DefaultAzureCredential auth (RLG policy — no API keys)

INPUT CSV must have at minimum: id, question, source_url columns
(matches the schema already used by build_golden_dataset_seed.py /
build_golden_dataset_from_urls.py output).

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════
v1.0.0 — Aug 2026 | Mukesh Kund
         Initial version. Standalone, no imports from pipeline or other
         project scripts. Uses PO's exact prompt text, adapted only to
         inject real page content in place of live URL browsing.
         ROLLBACK: n/a — new standalone script.
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv, find_dotenv
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from openai import AzureOpenAI

load_dotenv(find_dotenv(usecwd=False))

# ═══════════════════════════════════════════════════════════════
# TWEAKABLE CONFIG
# ═══════════════════════════════════════════════════════════════

AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
SEARCH_INDEX_NAME     = os.getenv("AZURE_SEARCH_INDEX_NAME", "rlg-faq-index-v5")

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

ANSWER_MODEL = os.getenv("GOLDEN_ANSWER_MODEL", "gpt-5.6-luna")

CHAR_LIMIT = 250  # from the PO's prompt — used only to flag, never to truncate
NO_INFO_FALLBACK = "The provided source does not contain information to answer this question."

REQUEST_SLEEP_SECONDS = 0.5
MAX_RETRIES = 2
MAX_OUTPUT_TOKENS = 500  # PO's prompt caps answers at 250 chars — generous headroom for GPT-5 reasoning tokens

# ═══════════════════════════════════════════════════════════════
# PO'S PROMPT — verbatim, only the Input section adapted to inject
# real fetched content alongside the URL (see module docstring)
# ═══════════════════════════════════════════════════════════════

ANSWER_PROMPT = """You are a precise information extraction assistant. Your task is to answer a question using ONLY the content found at the provided URL.

## Instructions

1. **Source Restriction**: Base your answer exclusively on the content available at the given URL. Do not use prior knowledge, assumptions, or external information.

2. **Accuracy Requirements**:
   - Only state facts explicitly supported by the page content.
   - Preserve specific details exactly as presented (names, dates, numbers, quotes, statistics).
   - Do not infer, extrapolate, or generalize beyond what the text states.

3. **Handling Missing Information**:
   - If the URL content does not contain the answer, respond exactly: "The provided source does not contain information to answer this question."
   - If the content only partially addresses the question, answer the part you can and clearly note what is not covered.

4. **Format**:
   - Be concise: Maximum of 250 characters.
   - Lead with the direct answer, then add essential supporting detail only if needed.
   - Use plain, factual language with no speculation, opinion, or filler.

5. **Output**: Respond with ONLY the answer text. No preamble, no restating the question, no citations of the URL, no explanations of your process.

## Input
URL: {url}
PAGE CONTENT (fetched from the URL above — treat this as the exact content of that page):
{content}
Question: {question}

## Answer
"""


def _build_create_kwargs(model: str, max_tokens: int) -> dict:
    """Model-family-compatible kwargs, mirroring generator.py/
    classifier_node.py's own GPT-4 vs GPT-5 handling. GPT-5 family
    needs max_completion_tokens and rejects a custom temperature.
    Standalone copy per this project's no-pipeline-dependency
    convention for these scripts."""
    is_gpt4 = "gpt-4" in model.lower()
    return {"max_tokens": max_tokens} if is_gpt4 else {"max_completion_tokens": max_tokens}


def get_search_client() -> SearchClient:
    if not AZURE_SEARCH_ENDPOINT:
        raise SystemExit("AZURE_SEARCH_ENDPOINT not set in .env")
    return SearchClient(
        endpoint=AZURE_SEARCH_ENDPOINT,
        index_name=SEARCH_INDEX_NAME,
        credential=DefaultAzureCredential(),
    )


def get_openai_client() -> AzureOpenAI:
    if not AZURE_OPENAI_ENDPOINT:
        raise SystemExit("AZURE_OPENAI_ENDPOINT not set in .env")
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_ad_token_provider=token_provider,
        api_version=AZURE_OPENAI_API_VERSION,
    )


def fetch_base_page_content(search_client: SearchClient, url: str) -> str:
    """Same base-page-only retrieval used elsewhere in this project —
    excludes dropdown #state= chunk variants, concatenates by chunk_index."""
    results = list(search_client.search(
        search_text="*",
        filter=f"source_url eq '{url}'",
        select=["chunk_index", "content"],
        top=200,
    ))
    if not results:
        return ""
    results.sort(key=lambda r: r.get("chunk_index", 0))
    return "\n\n".join(r.get("content", "") for r in results if r.get("content"))


def generate_answer(client: AzureOpenAI, url: str, content: str, question: str) -> Optional[str]:
    prompt = ANSWER_PROMPT.format(url=url, content=content[:12000], question=question)
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=ANSWER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                **_build_create_kwargs(ANSWER_MODEL, MAX_OUTPUT_TOKENS),
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise ValueError("empty response")
            return text
        except Exception as exc:
            if attempt == MAX_RETRIES:
                print(f"    [answer generation failed after {MAX_RETRIES + 1} attempts]: {exc}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


@dataclass
class ResultRow:
    row: dict  # original input row, preserved as-is
    generated_answer: str
    char_count: int
    flag: str  # "" | "no_info_found" | "exceeds_250_chars" | "no_content" | "generation_failed"


def process_row(search_client, openai_client, row: dict, dry_run: bool) -> ResultRow:
    url = row.get("source_url", "").strip()
    question = row.get("question", "").strip()

    if not url or not question:
        return ResultRow(row=row, generated_answer="", char_count=0, flag="missing_url_or_question")

    content = fetch_base_page_content(search_client, url)
    if not content or len(content.split()) < 10:
        return ResultRow(row=row, generated_answer="", char_count=0, flag="no_content")

    if dry_run:
        return ResultRow(row=row, generated_answer="[DRY RUN — no LLM call]", char_count=0, flag="")

    answer = generate_answer(openai_client, url, content, question)
    if answer is None:
        return ResultRow(row=row, generated_answer="", char_count=0, flag="generation_failed")

    flag = ""
    if answer.strip() == NO_INFO_FALLBACK:
        flag = "no_info_found"
    elif len(answer) > CHAR_LIMIT:
        flag = "exceeds_250_chars"

    return ResultRow(row=row, generated_answer=answer, char_count=len(answer), flag=flag)


def main():
    parser = argparse.ArgumentParser(description="Fill in expected_answer for an existing golden dataset CSV using the PO's extraction prompt")
    parser.add_argument("--input", required=True, help="Path to existing golden dataset CSV (must have id, question, source_url columns)")
    parser.add_argument("--out-prefix", default="golden_dataset_po_answers")
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM calls — just verify content retrieval works")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N rows (for testing)")
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[:args.limit]
    print(f"Loaded {len(rows)} row(s) from {args.input}"
          f"{' (dry run — no LLM calls)' if args.dry_run else ''}")

    search_client = get_search_client()
    openai_client = None if args.dry_run else get_openai_client()

    results: list[ResultRow] = []
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {row.get('question', '')[:70]}")
        result = process_row(search_client, openai_client, row, args.dry_run)
        results.append(result)
        status = result.flag if result.flag else "ok"
        print(f"    -> {status}" + (f" ({result.char_count} chars)" if result.char_count else ""))
        if not args.dry_run:
            time.sleep(REQUEST_SLEEP_SECONDS)

    ok = [r for r in results if r.flag == ""]
    no_info = [r for r in results if r.flag == "no_info_found"]
    over_limit = [r for r in results if r.flag == "exceeds_250_chars"]
    no_content = [r for r in results if r.flag == "no_content"]
    failed = [r for r in results if r.flag == "generation_failed"]

    out_path = f"{args.out_prefix}.csv"
    original_fieldnames = list(rows[0].keys()) if rows else []
    fieldnames = original_fieldnames + ["generated_answer", "char_count", "flag"] if "expected_answer" not in original_fieldnames else \
                 [f for f in original_fieldnames if f != "expected_answer"] + ["generated_answer", "char_count", "flag"]

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            out_row = {k: v for k, v in r.row.items() if k in fieldnames}
            out_row["generated_answer"] = r.generated_answer
            out_row["char_count"] = r.char_count
            out_row["flag"] = r.flag
            writer.writerow(out_row)

    print(f"\n{'='*60}")
    print(f"Total rows: {len(results)}")
    print(f"  OK: {len(ok)}")
    print(f"  No info found (needs decision — bad question or valid refusal?): {len(no_info)}")
    print(f"  Exceeds 250 chars (flagged, not truncated): {len(over_limit)}")
    print(f"  No page content found: {len(no_content)}")
    print(f"  Generation failed: {len(failed)}")
    print(f"\nOutput: {out_path}")


if __name__ == "__main__":
    main()