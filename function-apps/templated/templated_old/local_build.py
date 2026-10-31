"""Local runner for the templated-questions builder (no Function App needed).

  python local_build.py sample_templated.xlsx --check            validate only, no Azure
  python local_build.py sample_templated.xlsx                    build a NEW index (not live)
  python local_build.py --promote [index_name] [--force]         point alias at it
  python local_build.py --query "What is a pension?" [--index X] nearest match + hit/miss (threshold 0.75)
  python local_build.py --eval eval_sample.csv [--index X]       many queries at once + threshold report

--eval CSV columns: query, category (exact | paraphrase | near-miss | unrelated), expected_question
(expected_question = the stored question a paraphrase should match; blank for near-miss/unrelated).

Settings come from local.settings.json ("Values") or the environment. Needs `az login`.
"""
import argparse
import csv
import json
import logging
import os
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).parent / "libs"))
from src import templated_job as j  # noqa: E402


def nearest(index: str, vectors: list[list[float]]) -> list[dict]:
    """Top-1 match per vector, same query shape and score formula as the agent (cosine: 2 - 1/score)."""
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.models import VectorizedQuery
    c = SearchClient(os.environ["AZURE_SEARCH_ENDPOINT"], index, DefaultAzureCredential())
    out = []
    for vec in vectors:
        best = None
        for r in c.search(search_text=None, top=1, select=["id", "question", "response_json"],
                          vector_queries=[VectorizedQuery(vector=vec, k_nearest_neighbors=1, fields="question_vector", exhaustive=True)]):
            best = {"id": r["id"], "question": r["question"], "response_json": r["response_json"],
                    "similarity": 2 - 1 / r["@search.score"]}
        out.append(best)
    return out


def summarise(rows: list[dict], threshold: float) -> list[str]:
    """rows: {category, query, similarity (or None), hit_expected: bool|None}. Returns report lines."""
    lines, by = [], {}
    for r in rows:
        by.setdefault(r["category"], []).append(r)
    lines.append(f"threshold = {threshold}")
    lines.append(f"{'category':<11} {'n':>3} {'min':>7} {'mean':>7} {'max':>7}  hits(sim>=threshold)")
    for cat, items in by.items():
        s = [r["similarity"] for r in items if r["similarity"] is not None]
        if not s:
            continue
        lines.append(f"{cat:<11} {len(items):>3} {min(s):>7.4f} {statistics.mean(s):>7.4f} {max(s):>7.4f}  "
                     f"{sum(x >= threshold for x in s)}/{len(s)}")
    pos = [r["similarity"] for r in by.get("paraphrase", []) + by.get("exact", []) if r["similarity"] is not None]
    neg = [r["similarity"] for r in by.get("near-miss", []) + by.get("unrelated", []) if r["similarity"] is not None]
    if pos and neg:
        low, high = min(pos), max(neg)
        lines.append(f"lowest wanted match = {low:.4f}, highest unwanted = {high:.4f} -> "
                     f"{'separable' if low > high else 'OVERLAPPING'} (gap {low - high:+.4f})")
        lines.append(f"threshold {'sits inside the gap' if high < threshold <= low else 'is NOT inside the gap'}")
    wrong = [r for r in rows if r.get("wrong")]
    for r in wrong:
        lines.append(f"PROBLEM [{r['category']}] {r['query']!r}: {r['wrong']}")
    return lines


def run_eval(path: str, index: str, threshold: float, embed=j.azure_embed, search=nearest) -> list[str]:
    cases = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    matches = search(index, embed([c["query"] for c in cases]))
    rows = []
    for c, m in zip(cases, matches):
        sim = m["similarity"] if m else None
        hit = sim is not None and sim >= threshold
        want = j.make_id(c["expected_question"]) if c.get("expected_question", "").strip() else None
        wrong = None
        if c["category"] in ("near-miss", "unrelated") and hit:
            wrong = f"false HIT on {m['question']!r} at {sim:.4f}"
        elif c["category"] in ("exact", "paraphrase") and not hit:
            wrong = f"missed (best {m['question']!r} at {sim:.4f})" if m else "no result"
        elif c["category"] in ("exact", "paraphrase") and want and m["id"] != want:
            wrong = f"matched the wrong question {m['question']!r}"
        rows.append({"category": c["category"], "query": c["query"], "similarity": sim, "wrong": wrong,
                     "best": m["question"] if m else "-"})
        print(f"{c['category']:<11} {'HIT ' if hit else 'miss'} {sim if sim is None else format(sim, '.4f'):>7}  "
              f"{c['query']!r} -> {rows[-1]['best']!r}{'  <-- ' + wrong if wrong else ''}")
    print()
    return summarise(rows, threshold)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        os.environ.update({k: str(v) for k, v in json.loads(Path("local.settings.json").read_text())["Values"].items() if k not in os.environ})
    except FileNotFoundError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", nargs="?")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--promote", nargs="?", const="", metavar="INDEX")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--eval", metavar="CSV")
    ap.add_argument("--index", default=j.ALIAS_NAME)
    ap.add_argument("--threshold", type=float, default=0.75)
    a = ap.parse_args()

    if a.xlsx and a.check:
        entries, errors, warnings = j.validate(j.read_excel(a.xlsx))
        print(f"valid rows: {len(entries)}  errors: {len(errors)}  warnings: {len(warnings)}")
        for m in errors + warnings:
            print(" -", m)
        sys.exit(1 if errors else 0)

    missing = [k for k in j.REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing settings: {', '.join(missing)}")

    if a.xlsx:
        box = NS(download_blob=lambda n: NS(readall=lambda: Path(a.xlsx).read_bytes()))
        print(j.build_index(box, Path(a.xlsx).name))
    elif a.promote is not None:
        print(j.promote(j.AzureSearchOps(), a.promote or None, a.force))
    elif a.query:
        m = nearest(a.index, j.azure_embed([a.query]))[0]
        if not m:
            sys.exit("index is empty")
        if m["similarity"] >= a.threshold:
            print(f"HIT  similarity={m['similarity']:.4f} id={m['id']} question={m['question']!r}")
            print(m["response_json"])
        else:
            print(f"miss similarity={m['similarity']:.4f} (below {a.threshold}); nearest stored question: {m['question']!r}")
            print("no stored answer is served on a miss: the agent falls back to the normal RAG")
    elif a.eval:
        print("\n".join(run_eval(a.eval, a.index, a.threshold)))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()