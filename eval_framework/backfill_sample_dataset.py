"""
backfill_sample_dataset.py — one-off helper, NOT part of the framework.

Pulls the real citation URL each question actually got from your last
RLG pipeline run's eval_report.json, and writes them into
sample_golden_dataset.json as source_url (clearing the old fabricated
chunk_id/source_url guesses). This gives the 3-query demo genuine
ground truth instead of invented URLs — needed for hit_rate/precision/
recall to mean anything.

Run once, from eval_framework/, after you have a successful
eval_report.json from the real pipeline (the run that showed
citations=1 / citations=3 / citations=2 in your terminal logs):

    python backfill_sample_dataset.py --source eval_report.json

NOTE: takes the FIRST citation per case as the "known relevant" one.
If a question's answer legitimately drew on multiple valid sources,
this only captures one — same "single known-relevant chunk" limitation
discussed earlier for golden dataset ground truth in general. Fine for
a demo; revisit if doing this for the real dataset at scale.
"""

import argparse
import json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Path to a previous eval_report.json with real citations")
    parser.add_argument("--dataset", default="sample_golden_dataset.json")
    args = parser.parse_args()

    with open(args.source, encoding="utf-8") as f:
        report = json.load(f)

    with open(args.dataset, encoding="utf-8") as f:
        dataset = json.load(f)

    by_id = {c["id"]: c for c in report["cases"]}

    updated = 0
    for entry in dataset:
        # sample dataset uses "SMOKE-001" style ids — match by question
        # text instead, since report case ids may differ (e.g. SMOKE-001
        # vs GOLD-001) depending on how load_dataset() assigned them.
        match = next((c for c in report["cases"] if c["question"] == entry["question"]), None)
        if not match:
            print(f"[skip] no matching case found for: {entry['question']!r}")
            continue

        raw_citations = match.get("citations")
        if not raw_citations:
            print(f"[skip] no citations recorded for: {entry['question']!r} — "
                  f"re-run eval with this case included, or check eval_report.json structure")
            continue

        entry["source_url"] = raw_citations[0]
        entry["chunk_id"] = ""  # clear the old fabricated placeholder
        updated += 1
        print(f"[updated] {entry['id']}: source_url -> {raw_citations[0]}")

    with open(args.dataset, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2)

    print(f"\n{updated} entries updated in {args.dataset}")


if __name__ == "__main__":
    main()
