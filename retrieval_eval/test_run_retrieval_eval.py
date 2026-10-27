"""Smoke checks for run_retrieval_eval.py: metrics maths, dedupe, stale-row exclusion, reranker threshold, outputs.
Run: python retrieval_eval/test_run_retrieval_eval.py   (no Azure needed)"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_retrieval_eval as r  # noqa: E402

A, B, C, D = (f"https://www.royallondon.com/p/{x}" for x in "abcd")


def close(x, y):
    return abs(x - y) < 1e-9


def row(qid, q, at, rel, variant="plain", ans=True):
    return {"query_id": qid, "query": q, "answer_type": at, "relevant": rel, "answerable": ans,
            "category": "c", "variant": variant, "provenance": "synthetic"}


def test_metrics():
    m = r.score_row({A: 2}, [B, A, C], "single")                       # relevant at rank 2
    assert m["first_rel_rank"] == 2 and close(m["mrr"], 0.5)
    assert m["hit@1"] == 0 and m["hit@3"] == 1 and m["success@3"] == 1
    m = r.score_row({A: 2, B: 2}, [A, C, D], "multi_required")         # only one of two pages found
    assert close(m["recall@3"], 0.5) and m["all@3"] == 0 and m["success@3"] == 0 and m["hit@3"] == 1
    m = r.score_row({A: 2, B: 2}, [C, B, A], "multi_required")
    assert m["success@3"] == 1 and m["success@1"] == 0
    m = r.score_row({A: 2, B: 1}, [A, B], "multi_any")                 # ideal order -> nDCG 1
    assert close(m["ndcg@10"], 1.0)
    m = r.score_row({A: 2, B: 1}, [B, A], "multi_any")                 # swapped -> below 1
    assert m["ndcg@10"] < 1.0 and m["success@1"] == 1
    assert r.score_row({A: 2}, [B, C], "single")["mrr"] == 0.0


def test_grouping():
    chunks = [{"url": A, "rerank": 1.0}, {"url": A, "rerank": 3.0}, {"url": B, "rerank": 2.0}, {"url": C, "rerank": None}]
    g = r.group_by_url(chunks)
    assert [c["url"] for c in g] == [A, B, C] and g[0]["rerank"] == 3.0     # best chunk per URL, sorted desc, None = 0
    g = r.group_by_url([{"url": B, "rerank": None}, {"url": A, "rerank": None}])
    assert [c["url"] for c in g] == [B, A]                                  # ties keep first-seen order


def fake_factory(canned):
    """canned: query -> list of (url, rerank_score)."""
    def fake_search(q):
        if q == "boom":
            raise RuntimeError("simulated outage")
        return [{"url": u, "score": 1.0, "rerank": s} for u, s in canned[q]], 5.0, 20.0
    return fake_search


def test_pipeline_and_outputs():
    cfg = {"semantic": True, "reranker_threshold": None, "bootstrap": 200, "seed": 1}
    rows = [
        row("S1", "q1", "single", {A: 2}),
        row("S2", "q2", "single", {B: 2}),
        row("M1", "q3", "multi_required", {A: 2, C: 2}),
        row("STALE", "q4", "single", {"https://www.royallondon.com/gone": 2}),
        row("U1", "q5", "unanswerable", {}, "out_of_scope", False),
        row("BOOM", "boom", "single", {A: 2}),
    ]
    canned = {"q1": [(A, 3.5), (B, 1.0)], "q2": [(C, 3.5), (D, 1.0)], "q3": [(A, 3.5), (D, 3.0), (C, 2.5)], "q5": [(D, 0.5)]}
    res = r.evaluate(rows, fake_factory(canned), {A, B, C, D}, cfg, confirm_fn=lambda u: False)
    st = {x["query_id"]: x["status"] for x in res}
    assert st == {"S1": "ok", "S2": "ok", "M1": "ok", "STALE": "stale_ground_truth", "U1": "ok", "BOOM": "error"}, st
    s = r.summarise(res, cfg)
    assert s["counts"] == {"total": 6, "scored_answerable": 3, "scored_unanswerable": 1, "stale_ground_truth": 1, "errors": 1}
    assert close(s["overall"]["success@1"], 1 / 3)      # S1 hit; S2 miss; M1 needs both pages, only one in top 1
    assert close(s["overall"]["success@3"], 2 / 3)      # S1 hit; S2 miss; M1 has both pages in top 3
    with tempfile.TemporaryDirectory() as td:
        digest = r.write_outputs(Path(td) / "run", {"run_id": "t", "summary": s}, res)
        assert len(digest) == 64 and (Path(td) / "run" / "per_query.csv").exists()
        assert json.loads((Path(td) / "run" / "report.json").read_text(encoding="utf-8"))["run_id"] == "t"


def test_reranker_threshold():
    cfg = {"semantic": True, "reranker_threshold": 2.0, "bootstrap": 200, "seed": 1}
    rows = [
        row("S1", "q1", "single", {B: 2}),                                    # B scores below threshold while A passes -> miss
        row("S2", "q2", "single", {A: 2}),                                    # A passes -> hit
        row("S3", "q5", "single", {D: 2}),                                    # nothing passes -> top group still returned -> hit, not confident
        row("U1", "q3", "unanswerable", {}, "out_of_scope", False),           # nothing passes -> not confident -> no false positive
        row("U2", "q4", "unanswerable", {}, "in_domain_gap", False),          # a group passes -> confident -> false positive
    ]
    canned = {"q1": [(A, 3.5), (B, 1.0)], "q2": [(A, 3.5)], "q3": [(D, 0.5)], "q4": [(C, 3.0)], "q5": [(D, 0.5)]}
    res = r.evaluate(rows, fake_factory(canned), {A, B, C, D}, cfg)
    by_id = {x["query_id"]: x for x in res}
    assert by_id["S3"]["ranked_urls"] == [D] and by_id["S3"]["is_confident"] is False
    assert by_id["S1"]["ranked_urls"] == [A] and by_id["S1"]["raw_success@5"] == 1.0   # raw ranking is fine, threshold hides B
    s = r.summarise(res, cfg)
    assert close(s["overall"]["success@5"], 2 / 3)
    assert s["overall"]["raw_success@5"] == 1.0
    assert s["unanswerable"]["out_of_scope"]["fp_rate"] == 0.0
    assert s["unanswerable"]["in_domain_gap"]["fp_rate"] == 1.0
    assert close(s["answerable_confident_rate"], 2 / 3)
    print("all checks passed")


if __name__ == "__main__":
    test_metrics()
    test_grouping()
    test_pipeline_and_outputs()
    test_reranker_threshold()