"""
curate_golden_dataset_subset.py — Golden Dataset Curation

═══════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════
Reduces the ~900-row build_golden_dataset_seed.py output down to a
smaller, high-quality, category-balanced subset for Product Owner
triage. Rationale: full SME review of ~900 questions is not feasible
on SME bandwidth. This script does the mechanical filtering (exclude
off-topic categories, dedupe near-identical phrasing, drop low-signal
generic questions, cap per-category) so the PO only has to make fast
keep/discard/add judgement calls on a manageable set — not write ~900
answers cold.

FILTERING STAGES (in order)
  1. Category exclusion — drop categories that aren't actual chatbot
     product-area content (e.g. internal IT/security topics, corporate
     project pages). Edit EXCLUDE_CATEGORIES / INCLUDE_CATEGORIES below
     to match your real category values before running.
  2. Low-signal question filter — drops questions below a minimum
     word count (proxy for "too generic to be a useful test case",
     e.g. "What is the Help and Support section?").
  3. Fuzzy dedup — the seed script's dedup was exact-normalise only;
     this adds a similarity-ratio pass to catch near-identical
     rephrasings the exact-match dedup missed.
  4. Stratified sampling with per-category cap — takes up to
     max_per_category from each remaining category, in original
     (already-diverse) order, until target_count is reached or every
     category is exhausted.

OUTPUT
  CSV with all seed columns plus two new ones for PO triage:
    - po_decision  (blank — PO fills keep / discard)
    - po_notes     (blank — PO fills free-text, e.g. "add citation to Y instead")

USAGE
    python curate_golden_dataset_subset.py --in golden_dataset_seed.csv --out golden_dataset_curated.csv
    python curate_golden_dataset_subset.py --in golden_dataset_seed.csv --out golden_dataset_curated.csv --target-count 150 --max-per-category 15

═══════════════════════════════════════════════════════════════
CHANGE LOG
═══════════════════════════════════════════════════════════════
v1.0.0 — Aug 2026 | Mukesh Kund
         Initial version.
         ROLLBACK: n/a — new standalone script.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from difflib import SequenceMatcher

# ═══════════════════════════════════════════════════════════════
# EDIT THESE to match the real product_category values in your seed
# CSV before running. Whichever list you populate wins; leave the
# other empty. INCLUDE_CATEGORIES (whitelist) is safer/stricter —
# recommended once you've seen the actual category values on VDI.
# ═══════════════════════════════════════════════════════════════
INCLUDE_CATEGORIES: set[str] = set()  # e.g. {"pensions", "isa", "equity_release", "protection", "bereavement"}
EXCLUDE_CATEGORIES: set[str] = {"corporate", "general"}  # placeholder defaults — review against real values

MIN_QUESTION_WORDS = 5
FUZZY_DEDUP_THRESHOLD = 0.85  # 0-1, higher = stricter (only drops near-identical phrasing)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def category_filter(rows: list[dict]) -> list[dict]:
    if INCLUDE_CATEGORIES:
        kept = [r for r in rows if r.get("product_category", "").strip() in INCLUDE_CATEGORIES]
    else:
        kept = [r for r in rows if r.get("product_category", "").strip() not in EXCLUDE_CATEGORIES]
    print(f"[category_filter] {len(rows)} -> {len(kept)}")
    return kept


def low_signal_filter(rows: list[dict], min_words: int) -> list[dict]:
    kept = [r for r in rows if len(_norm(r["question"]).split()) >= min_words]
    print(f"[low_signal_filter] {len(rows)} -> {len(kept)}")
    return kept


def fuzzy_dedup(rows: list[dict], threshold: float) -> list[dict]:
    """O(n^2) similarity check within each category only (keeps it fast
    and is the right scope anyway — cross-category duplicates are rare
    and not the problem this is solving)."""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r.get("product_category", "")].append(r)

    kept = []
    dropped = 0
    for cat, cat_rows in by_cat.items():
        survivors: list[dict] = []
        for r in cat_rows:
            q_norm = _norm(r["question"])
            is_dup = any(
                SequenceMatcher(None, q_norm, _norm(s["question"])).ratio() >= threshold
                for s in survivors
            )
            if is_dup:
                dropped += 1
                continue
            survivors.append(r)
        kept.extend(survivors)
    print(f"[fuzzy_dedup] dropped {dropped}, {len(rows)} -> {len(kept)}")
    return kept


def stratified_sample(rows: list[dict], target_count: int, max_per_category: int) -> list[dict]:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r.get("product_category", "")].append(r)

    selected = []
    # Round-robin across categories so early categories don't exhaust
    # the target_count budget before later ones get a look-in.
    cat_iters = {cat: iter(cat_rows[:max_per_category]) for cat, cat_rows in by_cat.items()}
    active = set(cat_iters.keys())
    while active and len(selected) < target_count:
        for cat in list(active):
            if len(selected) >= target_count:
                break
            try:
                selected.append(next(cat_iters[cat]))
            except StopIteration:
                active.discard(cat)

    print(f"[stratified_sample] {len(rows)} -> {len(selected)} "
          f"(target={target_count}, max_per_category={max_per_category})")
    print("  Category breakdown:")
    breakdown: dict[str, int] = defaultdict(int)
    for r in selected:
        breakdown[r.get("product_category", "")] += 1
    for cat, n in sorted(breakdown.items(), key=lambda x: -x[1]):
        print(f"    {cat}: {n}")
    return selected


def main():
    parser = argparse.ArgumentParser(description="Curate golden dataset seed CSV down to a PO-triageable subset")
    parser.add_argument("--in", dest="in_path", required=True, help="Path to seed CSV (build_golden_dataset_seed.py output)")
    parser.add_argument("--out", dest="out_path", default="golden_dataset_curated.csv")
    parser.add_argument("--target-count", type=int, default=150)
    parser.add_argument("--max-per-category", type=int, default=20,
                         help="Cap per category before stratified sampling, to prevent one large category dominating")
    parser.add_argument("--min-question-words", type=int, default=MIN_QUESTION_WORDS)
    parser.add_argument("--fuzzy-threshold", type=float, default=FUZZY_DEDUP_THRESHOLD)
    args = parser.parse_args()

    with open(args.in_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"Loaded {len(rows)} rows from {args.in_path}")

    rows = category_filter(rows)
    rows = low_signal_filter(rows, args.min_question_words)
    rows = fuzzy_dedup(rows, args.fuzzy_threshold)
    rows = stratified_sample(rows, args.target_count, args.max_per_category)

    fieldnames = list(rows[0].keys()) + ["po_decision", "po_notes"] if rows else []
    with open(args.out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            r["po_decision"] = ""
            r["po_notes"] = ""
            writer.writerow(r)

    print(f"\nOutput: {args.out_path} ({len(rows)} rows)")
    print("NEXT STEP: review INCLUDE_CATEGORIES/EXCLUDE_CATEGORIES at the top of this "
          "script against your actual category values before treating this as final — "
          "defaults are placeholders.")


if __name__ == "__main__":
    main()