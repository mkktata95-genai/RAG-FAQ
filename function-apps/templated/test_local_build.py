"""local_build.py: the local runner (check / dry-run / sync / query / eval)."""
import json
from types import SimpleNamespace as NS

import pytest

import local_build as lb
from src import templated_job as j


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """The runner reads ./local.settings.json: never let a developer's real file into a test."""
    monkeypatch.chdir(tmp_path)


def test_summarise_reports_separable_and_threshold_position():
    rows = [{"category": "exact", "query": "q1", "similarity": 0.99, "wrong": None},
            {"category": "paraphrase", "query": "q2", "similarity": 0.90, "wrong": None},
            {"category": "near-miss", "query": "q3", "similarity": 0.60, "wrong": None},
            {"category": "unrelated", "query": "q4", "similarity": 0.30, "wrong": None}]
    text = "\n".join(lb.summarise(rows, 0.75))
    assert "separable" in text and "sits inside the gap" in text and "PROBLEM" not in text


def test_summarise_flags_overlap_and_problems():
    rows = [{"category": "paraphrase", "query": "a", "similarity": 0.70, "wrong": "missed"},
            {"category": "near-miss", "query": "b", "similarity": 0.80, "wrong": "false HIT"}]
    text = "\n".join(lb.summarise(rows, 0.75))
    assert "OVERLAPPING" in text and "NOT inside the gap" in text and text.count("PROBLEM") == 2


def test_run_eval_classifies_hits_misses_and_wrong_matches(tmp_path, capsys):
    csv_path = tmp_path / "e.csv"
    csv_path.write_text(
        "query,category,expected_question\n"
        "What is a pension?,exact,What is a pension?\n"
        "Whats a pension,paraphrase,What is a pension?\n"
        "pension money invested,paraphrase,Are pensions invested?\n"
        "weather,unrelated,\n"
        "pension tax,near-miss,\n", encoding="utf-8")
    answers = {
        "What is a pension?": {"id": j.make_id("What is a pension?"), "question": "What is a pension?", "similarity": 0.99},
        "Whats a pension": {"id": j.make_id("What is a pension?"), "question": "What is a pension?", "similarity": 0.9},
        "pension money invested": {"id": j.make_id("What is a pension?"), "question": "What is a pension?", "similarity": 0.8},  # wrong question
        "weather": {"id": "x", "question": "What is a pension?", "similarity": 0.2},
        "pension tax": {"id": "y", "question": "Are pensions taxed?", "similarity": 0.8},                                           # false hit
    }
    lines = lb.run_eval(str(csv_path), "idx", 0.75, embed=lambda t: t, search=lambda idx, qs: [answers[q] for q in qs])
    text = "\n".join(lines)
    assert "matched the wrong question" in text and "false HIT" in text
    assert "HIT " in capsys.readouterr().out


def test_run_eval_handles_empty_result_and_miss(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("query,category,expected_question\nq,exact,q\nz,paraphrase,\n", encoding="utf-8")
    res = [None, {"id": "a", "question": "other", "similarity": 0.1}]
    text = "\n".join(lb.run_eval(str(p), "i", 0.75, embed=lambda t: t, search=lambda i, q: res))
    assert "no result" in text and "missed" in text


def test_check_ok_and_failing_workbooks(make_xlsx, make_row, capsys):
    assert lb.main([make_xlsx([make_row()]), "--check"]) == 0
    assert "valid rows: 1" in capsys.readouterr().out
    assert lb.main([make_xlsx([make_row(answer="")]), "--check"]) == 1
    assert "answer is empty" in capsys.readouterr().out


def test_missing_settings_exit_code_2(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)                                    # no local.settings.json here
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    assert lb.main(["--query", "x"]) == 2
    assert "Missing settings: AZURE_OPENAI_ENDPOINT" in capsys.readouterr().err


def test_sync_path_passes_flags_to_sync_workbook(monkeypatch, make_xlsx, make_row, capsys):
    seen = {}
    monkeypatch.setattr(j, "sync_workbook", lambda box, name, **kw: seen.update(name=name, **kw) or {"ok": True})
    assert lb.main([make_xlsx([make_row()]), "--dry-run", "--allow-mass-removal"]) == 0
    assert seen["dry_run"] and seen["allow_mass_removal"] and seen["name"].endswith(".xlsx")


def test_errors_become_exit_code_1(monkeypatch, make_xlsx, make_row):
    def boom(*a, **k):
        raise RuntimeError("index does not exist")
    monkeypatch.setattr(j, "sync_workbook", boom)
    assert lb.main([make_xlsx([make_row()])]) == 1


def test_query_hit_and_miss(monkeypatch, capsys):
    monkeypatch.setattr(j, "azure_embed", lambda t: [[0.0] * 4])
    monkeypatch.setattr(lb, "nearest", lambda i, v: [{"id": "q-1", "question": "What is a pension?",
                                                      "response_json": "{}", "similarity": 0.9}])
    assert lb.main(["--query", "pension"]) == 0 and "HIT  similarity=0.9000" in capsys.readouterr().out
    monkeypatch.setattr(lb, "nearest", lambda i, v: [{"id": "q-1", "question": "What is a pension?",
                                                      "response_json": "{}", "similarity": 0.4}])
    assert lb.main(["--query", "weather"]) == 0 and "miss similarity=0.4000" in capsys.readouterr().out
    monkeypatch.setattr(lb, "nearest", lambda i, v: [None])
    assert lb.main(["--query", "x"]) == 1


def test_eval_path(monkeypatch, tmp_path, capsys):
    p = tmp_path / "e.csv"
    p.write_text("query,category,expected_question\nq,unrelated,\n", encoding="utf-8")
    monkeypatch.setattr(j, "azure_embed", lambda t: t)
    monkeypatch.setattr(lb, "nearest", lambda i, v: [{"id": "a", "question": "z", "similarity": 0.1} for _ in v])
    assert lb.main(["--eval", str(p)]) == 0 and "threshold = 0.75" in capsys.readouterr().out


def test_no_arguments_prints_help(capsys):
    assert lb.main([]) == 0 and "usage" in capsys.readouterr().out.lower()


def test_load_settings_does_not_override_existing_env(monkeypatch, tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"Values": {"A_NEW": "1", "AZURE_SEARCH_ENDPOINT": "https://other"}}), encoding="utf-8")
    monkeypatch.delenv("A_NEW", raising=False)
    lb.load_settings(str(f))
    assert __import__("os").environ["A_NEW"] == "1"
    assert __import__("os").environ["AZURE_SEARCH_ENDPOINT"] == "https://search.test.invalid"   # env wins
    lb.load_settings(str(tmp_path / "missing.json"))                                              # silently ignored


def test_nearest_computes_cosine_similarity(monkeypatch):
    class C:
        def __init__(self, *a, **k): pass
        def search(self, **kw):
            return [{"id": "q-1", "question": "Q", "response_json": "{}", "@search.score": 0.8}]
    monkeypatch.setattr("azure.search.documents.SearchClient", C)
    out = lb.nearest("idx", [[0.0] * 4])
    assert out[0]["similarity"] == pytest.approx(2 - 1 / 0.8)
