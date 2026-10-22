"""Local runner for the templated-questions builder (no Function App needed).

  python local_build.py sample_templated.xlsx --check            validate only, no Azure
  python local_build.py sample_templated.xlsx                    build a NEW index (not live)
  python local_build.py --promote [index_name] [--force]         point alias at it
  python local_build.py --query "What is a pension?" [--index X] nearest match + hit/miss (threshold 0.75)

Settings come from local.settings.json ("Values") or the environment. Needs `az login`.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
try:
    os.environ.update({k: str(v) for k, v in json.loads(Path("local.settings.json").read_text())["Values"].items() if k not in os.environ})
except FileNotFoundError:
    pass
sys.path.insert(0, str(Path(__file__).parent / "libs"))
from src import templated_job as j  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("xlsx", nargs="?")
ap.add_argument("--check", action="store_true")
ap.add_argument("--promote", nargs="?", const="", metavar="INDEX")
ap.add_argument("--force", action="store_true")
ap.add_argument("--query")
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
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.models import VectorizedQuery
    vec = j.azure_embed([a.query])[0]
    c = SearchClient(os.environ["AZURE_SEARCH_ENDPOINT"], a.index, DefaultAzureCredential())
    for r in c.search(search_text=None, top=1, select=["id", "question", "response_json"],
                      vector_queries=[VectorizedQuery(vector=vec, k_nearest_neighbors=1, fields="question_vector", exhaustive=True)]):
        sim = 2 - 1 / r["@search.score"]  # same formula as the agent (cosine)
        print(f"{'HIT ' if sim >= a.threshold else 'miss'} similarity={sim:.4f} id={r['id']} question={r['question']!r}")
        print(r["response_json"])
else:
    ap.print_help()
