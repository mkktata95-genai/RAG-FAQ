#!/usr/bin/env python3
"""
run_retrieval_eval.py  v1.0.0
Retrieval-only evaluation against Azure AI Search. No answer-generating LLM; the only model call
is query embedding. Scores on normalised source_url (never chunk_id).

Flow: load dataset -> scan index URLs (pre-flight: rows whose ground-truth URL is not in the index are
excluded as stale_ground_truth, not counted as misses) -> embed + search each query -> collapse chunks
to distinct URLs in rank order -> metrics -> runs/<run_id>/{report.json, per_query.csv, report.sha256, run.log}

Usage (VDI, .venv):
  python retrieval_eval\\run_retrieval_eval.py --limit 5            # smoke test
  python retrieval_eval\\run_retrieval_eval.py                      # full run on the synthetic dataset
  python retrieval_eval\\run_retrieval_eval.py --dataset <sme.json> # later: same code, SME dataset

Env (.env in repo root): AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_INDEX, AZURE_OPENAI_ENDPOINT,
  AZURE_OPENAI_EMBEDDING_DEPLOYMENT, AZURE_OPENAI_API_VERSION (optional). Auth: DefaultAzureCredential only.
  Needs role "Search Index Data Reader" on the search service.

Metrics (answerable rows only): success@k (hit for single/multi_any, all-pages-found for multi_required),
hit@k, recall@k, MRR, nDCG@10 (linear grade gains), k in 1/3/5/10, bootstrap 95% CIs, slices by
answer_type / category / variant / provenance, latency p50/p95. Unanswerable rows: false-positive
rate per variant (a row is a false positive if any chunk survives the reranker threshold) + top-1 scores.
Baseline mirrors the agent's search tool (values and behaviour; its code is not imported or modified):
hybrid + semantic (rlg-semantic-config), vector k=50, top 20, text-embedding-3-large with dimensions=1536; chunks are grouped by
URL (best reranker score), sorted by reranker score, groups with score >= 2.0 are kept ("confident"); if
none pass, the top group is still returned but flagged not confident. Single search per query (the agent
may search up to twice). raw_* metrics rank every group with no threshold, to separate ranking quality
from threshold effects.

Changelog (newest first)
  v1.0.0  Initial version. Rollback: git revert the commit.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import random
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from generate_synthetic_dataset import load_env, normalise_url  # same repo: reuse, don't duplicate

LOG = logging.getLogger("retrieval_eval")
HERE = Path(__file__).resolve().parent
KS = (1, 3, 5, 10)
ANSWER_TYPES = {"single", "multi_required", "multi_any", "unanswerable"}
METRICS = [f"success@{k}" for k in KS] + [f"hit@{k}" for k in KS] + [f"recall@{k}" for k in KS] + ["mrr", "ndcg@10", "raw_success@5", "raw_mrr"]
NO_RETRY = {400, 401, 403, 404}


# --------------------------------------------------------------------------- helpers
def with_retry(fn, what: str, attempts: int = 4):
    """Retry transient failures with backoff; auth/config errors (400/401/403/404) fail immediately."""
    last = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - logged, re-raised after the last attempt
            last = e
            if getattr(e, "status_code", None) in NO_RETRY:
                LOG.error("%s failed with non-retryable status %s: %s", what, e.status_code, e)
                raise
            wait = min(30, 2 ** i) + random.random()
            LOG.warning("%s failed (attempt %d/%d): %s; retry in %.1fs", what, i, attempts, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"{what} failed after {attempts} attempts: {last}") from last


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git_info() -> tuple[str, bool | None]:
    try:
        run = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, timeout=10, cwd=HERE).stdout.strip()  # noqa: E731
        return run("rev-parse", "HEAD") or "unknown", bool(run("status", "--porcelain"))
    except Exception:  # noqa: BLE001
        LOG.warning("git info unavailable", exc_info=True)
        return "unknown", None


# --------------------------------------------------------------------------- dataset
def load_dataset(path: str) -> tuple[list[dict], str]:
    raw = Path(path).read_bytes()
    rows, seen, out = json.loads(raw.decode("utf-8-sig")), set(), []
    for r in rows:
        qid, q, at = r.get("query_id"), (r.get("query") or "").strip(), r.get("answer_type")
        if not qid or not q or at not in ANSWER_TYPES or qid in seen:
            raise ValueError(f"Invalid or duplicate dataset row: {qid!r} (answer_type={at!r})")
        rel = {}
        for x in r.get("relevant_ids") or []:
            url, grade = (x, 2) if isinstance(x, str) else (x["url"], int(x.get("grade", 1)))
            rel[normalise_url(url)] = grade
        answerable = bool(r.get("answerable", at != "unanswerable"))
        if answerable != bool(rel):
            raise ValueError(f"{qid}: answerable={answerable} but {len(rel)} relevant_ids")
        seen.add(qid)
        out.append({**r, "query": q, "relevant": rel, "answerable": answerable})
    if not out:
        raise ValueError("Dataset is empty")
    return out, sha256_bytes(raw)


# --------------------------------------------------------------------------- azure
def make_clients(cfg: dict):
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from azure.search.documents import SearchClient

    cred = DefaultAzureCredential()
    sc = SearchClient(cfg["search_endpoint"], cfg["index"], cred)
    oai = None
    if cfg["mode"] != "keyword":
        from openai import AzureOpenAI

        oai = AzureOpenAI(
            azure_endpoint=cfg["aoai_endpoint"],
            azure_ad_token_provider=get_bearer_token_provider(cred, "https://cognitiveservices.azure.com/.default"),
            api_version=cfg["api_version"],
        )
    return sc, oai


def scan_index_urls(sc, page: int = 1000) -> tuple[set[str], int]:
    """All distinct normalised source_urls in the index (pre-flight). Returns (urls, docs_seen)."""
    urls, skip, seen = set(), 0, 0
    while True:
        batch = with_retry(
            lambda: list(sc.search(search_text="*", select=["source_url"], top=page, skip=skip)), "index url scan"
        )
        for d in batch:
            if d.get("source_url"):
                urls.add(normalise_url(d["source_url"]))
        seen += len(batch)
        if len(batch) < page:
            return urls, seen
        skip += page


def confirm_url(sc, u: str) -> bool:
    """Second look for a URL the scan missed (paging ties, trailing slash): exact filter lookup."""
    for v in (u, u + "/"):
        safe = v.replace("'", "''")
        hit = with_retry(
            lambda: list(sc.search(search_text="*", filter=f"source_url eq '{safe}'", select=["source_url"], top=1)),
            "url confirm",
        )
        if hit:
            return True
    return False


def make_search_fn(sc, oai, cfg: dict):
    from azure.search.documents.models import VectorizedQuery

    def search(query: str):
        t0 = time.perf_counter()
        vec = None
        if cfg["mode"] != "keyword":
            kw = {"model": cfg["embed_deployment"], "input": query}
            if cfg["embed_dims"]:
                kw["dimensions"] = cfg["embed_dims"]
            vec = with_retry(lambda: oai.embeddings.create(**kw).data[0].embedding, "embedding")
            if len(vec) != cfg["expected_dims"]:
                raise ValueError(f"Embedding has {len(vec)} dims, index expects {cfg['expected_dims']}")
        t1 = time.perf_counter()
        args = {"select": ["chunk_id", "source_url"], "top": cfg["top"], "filter": cfg["filter"] or None,
                "search_text": None if cfg["mode"] == "vector" else query}
        if vec:
            args["vector_queries"] = [VectorizedQuery(vector=vec, k_nearest_neighbors=cfg["knn"], fields="embedding")]
        if cfg["semantic"]:
            args.update(query_type="semantic", semantic_configuration_name=cfg["semantic_config"])
        res = with_retry(lambda: list(sc.search(**args)), "search")
        t2 = time.perf_counter()
        chunks = [{"chunk_id": d.get("chunk_id"), "url": normalise_url(d["source_url"]),
                   "score": d.get("@search.score"), "rerank": d.get("@search.reranker_score")}
                  for d in res if d.get("source_url")]
        return chunks, (t1 - t0) * 1000, (t2 - t1) * 1000

    return search


# --------------------------------------------------------------------------- scoring
def group_by_url(chunks: list[dict]) -> list[dict]:
    """One chunk per URL (highest reranker score; None counts as 0), sorted by reranker score descending.
    Ties keep first-seen order, as in the agent's search tool."""
    groups: dict[str, dict] = {}
    for c in chunks:
        best = groups.get(c["url"])
        if best is None or (c["rerank"] or 0) > (best["rerank"] or 0):
            groups[c["url"]] = c
    return sorted(groups.values(), key=lambda c: c["rerank"] or 0, reverse=True)


def score_row(rel: dict[str, int], ranked: list[str], answer_type: str) -> dict:
    n = len(rel)
    first = next((i for i, u in enumerate(ranked, 1) if u in rel), None)
    m = {"first_rel_rank": first, "mrr": 1.0 / first if first else 0.0}
    for k in KS:
        found = len(set(ranked[:k]) & rel.keys())
        m[f"hit@{k}"], m[f"recall@{k}"], m[f"all@{k}"] = float(found > 0), found / n, float(found == n)
        m[f"success@{k}"] = m[f"all@{k}"] if answer_type == "multi_required" else m[f"hit@{k}"]
    dcg = sum(rel.get(u, 0) / math.log2(i + 1) for i, u in enumerate(ranked[:10], 1))
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(sorted(rel.values(), reverse=True)[:10], 1))
    m["ndcg@10"] = dcg / idcg if idcg else 0.0
    return m


def evaluate(rows: list[dict], search_fn, index_urls: set[str], cfg: dict, confirm_fn=None) -> list[dict]:
    results = []
    for r in rows:
        res = {k: r.get(k) for k in ("query_id", "answer_type", "category", "variant", "provenance", "query")}
        res["status"] = "ok"
        missing = [u for u in r["relevant"] if u not in index_urls]
        if missing and confirm_fn:
            missing = [u for u in missing if not confirm_fn(u)]
        if missing:
            LOG.warning("%s: ground-truth URL(s) not in index, excluded: %s", r["query_id"], missing)
            res.update(status="stale_ground_truth", missing=missing)
            results.append(res)
            continue
        try:
            chunks, emb_ms, srch_ms = search_fn(r["query"])
        except Exception as e:  # noqa: BLE001 - recorded per query, never silent
            LOG.exception("%s: query failed", r["query_id"])
            res.update(status="error", error=f"{type(e).__name__}: {e}"[:300])
            results.append(res)
            continue
        thr = cfg.get("reranker_threshold")
        groups = group_by_url(chunks)
        strong = [g for g in groups if thr is None or (g["rerank"] or 0) >= thr]
        confident = bool(strong)
        selected = strong if confident else groups[:1]  # agent falls back to the top group, flagged not confident
        urls = [g["url"] for g in selected]
        top = groups[0] if groups else None
        top1 = None
        if top:
            top1 = top["rerank"] if cfg["semantic"] and top["rerank"] is not None else top["score"]
        res.update(ranked_urls=urls[:10], top1=top1, embed_ms=emb_ms, search_ms=srch_ms, n_chunks=len(chunks),
                   n_groups=len(groups), n_selected=len(selected), is_confident=confident)
        if r["answerable"]:
            res.update(score_row(r["relevant"], urls, r["answer_type"]))
            raw = score_row(r["relevant"], [g["url"] for g in groups], r["answer_type"])
            res.update({"raw_success@5": raw["success@5"], "raw_mrr": raw["mrr"]})
            res["relevant"] = list(r["relevant"])
        results.append(res)
        LOG.info("%s [%s] first_rel_rank=%s top1=%s", r["query_id"], r["answer_type"], res.get("first_rel_rank"), top1)
    return results


def aggregate(rows: list[dict]) -> dict:
    return {"n": len(rows), **{m: mean([r[m] for r in rows]) for m in METRICS}}


def by(rows: list[dict], key: str) -> dict:
    g = defaultdict(list)
    for r in rows:
        g[str(r.get(key) or "")].append(r)
    return {k: aggregate(v) for k, v in sorted(g.items())}


def bootstrap_ci(values: list[float], iters: int, seed: int):
    if len(values) < 2 or iters < 100:
        return None
    rng, n = random.Random(seed), len(values)
    ms = sorted(sum(rng.choices(values, k=n)) / n for _ in range(iters))
    return [ms[int(0.025 * iters)], ms[int(0.975 * iters) - 1]]


def summarise(results: list[dict], cfg: dict) -> dict:
    ok = [r for r in results if r["status"] == "ok"]
    ans = [r for r in ok if r["answer_type"] != "unanswerable"]
    unans = [r for r in ok if r["answer_type"] == "unanswerable"]
    ug = defaultdict(list)
    for r in unans:
        ug[r.get("variant") or "unknown"].append(r)
    unanswerable = {k: {"n": len(v), "fp_rate": mean([float(r["is_confident"]) for r in v]),
                        "top1_scores": [r["top1"] for r in v if r.get("top1") is not None]}
                    for k, v in sorted(ug.items())}
    lat = lambda key: [r[key] for r in ok if key in r]  # noqa: E731
    tot = [r["embed_ms"] + r["search_ms"] for r in ok if "embed_ms" in r]
    return {
        "counts": {"total": len(results), "scored_answerable": len(ans), "scored_unanswerable": len(unans),
                   "stale_ground_truth": sum(r["status"] == "stale_ground_truth" for r in results),
                   "errors": sum(r["status"] == "error" for r in results)},
        "overall": aggregate(ans) if ans else None,
        "overall_ci95": {m: bootstrap_ci([r[m] for r in ans], cfg["bootstrap"], cfg["seed"])
                         for m in ("success@1", "success@3", "success@5", "success@10", "mrr", "ndcg@10")} if ans else None,
        "by_answer_type": by(ans, "answer_type"), "by_category": by(ans, "category"),
        "by_variant": by(ans, "variant"), "by_provenance": by(ans, "provenance"),
        "unanswerable": unanswerable,
        "answerable_top1_scores": [r["top1"] for r in ans if r.get("top1") is not None],
        "answerable_confident_rate": mean([float(r["is_confident"]) for r in ans]) if ans else None,
        "latency_ms": {"embed_p50": pct(lat("embed_ms"), 50), "embed_p95": pct(lat("embed_ms"), 95),
                       "search_p50": pct(lat("search_ms"), 50), "search_p95": pct(lat("search_ms"), 95),
                       "total_p50": pct(tot, 50), "total_p95": pct(tot, 95)},
        "stale_rows": [{"query_id": r["query_id"], "missing": r["missing"]} for r in results if r["status"] == "stale_ground_truth"],
        "error_rows": [{"query_id": r["query_id"], "error": r["error"]} for r in results if r["status"] == "error"],
    }


# --------------------------------------------------------------------------- output
CSV_COLS = ["query_id", "answer_type", "category", "variant", "provenance", "status", "query", "first_rel_rank",
            "success@1", "success@3", "success@5", "success@10", "mrr", "ndcg@10", "recall@5", "raw_success@5", "raw_mrr",
            "is_confident", "top1", "n_groups", "n_selected", "embed_ms", "search_ms", "ranked_urls", "relevant",
            "missing", "error"]


def write_outputs(run_dir: Path, report: dict, results: list[dict]) -> str:
    run_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8")
    (run_dir / "report.json").write_bytes(body)
    digest = sha256_bytes(body)
    (run_dir / "report.sha256").write_text(f"{digest}  report.json\n", encoding="utf-8")
    with (run_dir / "per_query.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({**r, **{k: json.dumps(r[k]) for k in ("ranked_urls", "relevant", "missing") if k in r}})
    return digest


def print_summary(report: dict) -> None:
    s, c = report["summary"], report["summary"]["counts"]
    print(f"\n=== Retrieval eval {report['run_id']} | index {report['index']['name']} ({report['index']['doc_count']} chunks) ===")
    print(f"rows: {c}")
    o, ci = s["overall"], s["overall_ci95"]
    if o:
        for m in ("success@1", "success@3", "success@5", "success@10", "mrr", "ndcg@10", "raw_success@5", "raw_mrr"):
            band = f"  CI95 {ci[m][0]:.2f}-{ci[m][1]:.2f}" if ci and ci.get(m) else ""
            print(f"  {m:<11} {o[m]:.3f}{band}")
        print("  by answer_type (n, success@5, mrr):")
        for k, v in s["by_answer_type"].items():
            print(f"    {k:<15} n={v['n']:<3} success@5={v['success@5']:.2f} mrr={v['mrr']:.2f}")
    for k, v in s["unanswerable"].items():
        sc = [round(x, 2) for x in v["top1_scores"]]
        print(f"  unanswerable/{k}: top1 scores {sc} fp_rate={v['fp_rate']}")
    print(f"  answerable confident rate (>=1 URL at/above threshold): {s['answerable_confident_rate']}")
    print(f"  latency ms: {s['latency_ms']}")
    print(f"  report: {report['run_dir']}\\report.json  sha256={report['report_sha256_note']}\n")


# --------------------------------------------------------------------------- main
def build_cfg(a) -> dict:
    cfg = {
        "search_endpoint": a.search_endpoint or os.environ.get("AZURE_SEARCH_ENDPOINT", ""),
        "index": a.index or os.environ.get("AZURE_SEARCH_INDEX", ""),
        "aoai_endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
        "embed_deployment": a.embed_deployment or os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", ""),
        "api_version": a.api_version or os.environ.get("AZURE_OPENAI_API_VERSION", "2025-03-01-preview"),
        "mode": a.mode, "semantic": (not a.no_semantic) and a.mode != "vector",
        "semantic_config": a.semantic_config, "top": a.top, "knn": a.knn, "filter": a.filter,
        "embed_dims": a.embed_dims, "expected_dims": a.expected_dims,
        "reranker_threshold": a.reranker_threshold if a.reranker_threshold and a.reranker_threshold > 0 else None,
        "bootstrap": a.bootstrap, "seed": a.seed, "limit": a.limit,
    }
    if cfg["reranker_threshold"] is not None and not cfg["semantic"]:
        LOG.warning("--reranker-threshold needs the semantic reranker; ignored")
        cfg["reranker_threshold"] = None
    names = {"search_endpoint": "AZURE_SEARCH_ENDPOINT", "index": "AZURE_SEARCH_INDEX",
             "aoai_endpoint": "AZURE_OPENAI_ENDPOINT", "embed_deployment": "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"}
    need = [k for k in names if not cfg[k] and not (cfg["mode"] == "keyword" and k in ("aoai_endpoint", "embed_deployment"))]
    if need:
        raise ValueError("Missing config: " + ", ".join(names[k] for k in need) + " (set in .env or pass flags)")
    return cfg


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=str(HERE / "datasets" / "synthetic_dataset_v1.json"))
    ap.add_argument("--out", default=str(HERE / "runs"))
    ap.add_argument("--limit", type=int, default=0, help="Only the first N dataset rows (smoke test)")
    ap.add_argument("--mode", choices=["hybrid", "vector", "keyword"], default="hybrid")
    ap.add_argument("--no-semantic", action="store_true", help="Disable the semantic reranker")
    ap.add_argument("--semantic-config", default="rlg-semantic-config")
    ap.add_argument("--top", type=int, default=20, help="Chunks returned per query (baseline 20)")
    ap.add_argument("--knn", type=int, default=50, help="Vector k_nearest_neighbors (baseline 50)")
    ap.add_argument("--filter", default="", help="OData filter, e.g. \"page_purpose eq 'Information'\"")
    ap.add_argument("--embed-dims", type=int, default=1536,
                    help="dimensions= passed to the embedding call, as the agent does (baseline 1536; 0 = omit)")
    ap.add_argument("--expected-dims", type=int, default=1536)
    ap.add_argument("--reranker-threshold", type=float, default=2.0,
                    help="Keep URL groups with reranker score >= this; if none pass the top group is returned "
                         "as not confident (baseline 2.0; 0 = off)")
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--index", default="")
    ap.add_argument("--search-endpoint", default="")
    ap.add_argument("--embed-deployment", default="")
    ap.add_argument("--api-version", default="")
    a = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    run_dir = Path(a.out) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(run_dir / "run.log", encoding="utf-8")])
    for noisy in ("azure", "httpx", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cfg = build_cfg(a)
        rows, ds_sha = load_dataset(a.dataset)
        if a.limit:
            rows = rows[: a.limit]
        LOG.info("Run %s | dataset %s (%d rows, sha %s...) | index %s | mode=%s semantic=%s",
                 run_id, a.dataset, len(rows), ds_sha[:10], cfg["index"], cfg["mode"], cfg["semantic"])
        sc, oai = make_clients(cfg)
        doc_count = with_retry(sc.get_document_count, "document count")
        index_urls, seen = scan_index_urls(sc)
        LOG.info("Index: %d chunks reported, %d scanned, %d distinct URLs", doc_count, seen, len(index_urls))
        if seen != doc_count:
            LOG.warning("Scan saw %d docs but index reports %d; missing URLs are re-checked with filters", seen, doc_count)
        results = evaluate(rows, make_search_fn(sc, oai, cfg), index_urls, cfg, lambda u: confirm_url(sc, u))
        sha, dirty = git_info()
        report = {
            "run_id": run_id, "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_sha": sha, "git_dirty": dirty, "run_dir": str(run_dir),
            "config": {k: v for k, v in cfg.items()}, "config_hash": sha256_bytes(json.dumps(cfg, sort_keys=True).encode())[:16],
            "dataset": {"path": a.dataset, "sha256": ds_sha, "rows_run": len(rows)},
            "index": {"name": cfg["index"], "doc_count": doc_count, "distinct_urls": len(index_urls)},
            "summary": summarise(results, cfg), "report_sha256_note": "see report.sha256",
        }
        digest = write_outputs(run_dir, report, results)
        report["report_sha256_note"] = digest[:16] + "..."
        print_summary(report)
        c = report["summary"]["counts"]
        if c["stale_ground_truth"]:
            LOG.warning("%d row(s) excluded as stale_ground_truth; see report.json", c["stale_ground_truth"])
        return 2 if c["errors"] else 0
    except Exception:
        LOG.exception("Fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())