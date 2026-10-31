"""Excel reading + the validation rules (mirror of the agent's LLMResponse rules)."""
import json

import pytest

from src import templated_job as j


def test_read_excel_normalises_headers_and_skips_blank_rows(make_xlsx):
    path = make_xlsx([["Q1?", "A1."], [None, None], ["Q2?", "A2."]], header=["Question", " Answer ", None])
    rows = j.read_excel(path)
    assert [r["question"] for r in rows] == ["Q1?", "Q2?"]
    assert [r["_row"] for r in rows] == [2, 4]           # real Excel row numbers, blank row skipped
    assert rows[0]["answer"] == "A1."


def test_read_excel_header_only_and_empty_sheet(make_xlsx):
    assert j.read_excel(make_xlsx([])) == []


def test_read_excel_corrupt_file_raises_value_error(tmp_path):
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"this is not an excel file")
    with pytest.raises(ValueError, match="cannot read Excel file bad.xlsx"):
        j.read_excel(str(bad))


def test_happy_path_defaults_and_response_json_shape(make_xlsx, make_row, ok_url):
    entries, errors, warnings = j.validate(j.read_excel(make_xlsx([make_row()])))
    assert not errors and not warnings and len(entries) == 1
    d = json.loads(entries[0]["response_json"])
    # key order == LLMResponse field order
    assert list(d) == ["intent", "scenario", "answer", "advice_boundary", "citations", "read_more"]
    assert d["intent"] == "grounded" and d["scenario"] == "standard" and d["advice_boundary"] is False
    assert d["citations"] == [{"title": "Pensions", "url": ok_url}]
    assert d["read_more"] == [{"label": "Basics", "url": ok_url + "b/"}]
    assert entries[0]["source_urls"] == [ok_url, ok_url + "b/"]


def test_id_is_derived_from_normalised_question():
    assert j.make_id("What is a pension?") == j.make_id("  what IS a   pension ")
    assert j.make_id("What is a pension?") != j.make_id("What is a pension transfer?")
    assert j.make_id("x").startswith("q-")


def _errors(make_xlsx, make_row, **kw):
    return j.validate(j.read_excel(make_xlsx([make_row(**kw)])))[1]


@pytest.mark.parametrize("override, expected", [
    ({"answer": ""}, "answer is empty"),
    ({"question": ""}, "question is empty"),
    ({"answer": "x" * 251}, "over the 250"),
    ({"intent": "maybe"}, "intent"),
    ({"scenario": "nope"}, "scenario"),
    ({"advice_boundary": "perhaps"}, "advice_boundary must be true/false"),
    ({"citation_1_url": "http://www.royallondon.com/x"}, "https"),
    ({"citation_1_url": "/relative/path"}, "https"),
    ({"citation_1_url": "https://www.moneyhelper.org.uk/en"}, "not allowed"),
    ({"citation_1_url": "https://royallondon.com.evil.com/"}, "not allowed"),   # suffix trick
    ({"citation_1_title": ""}, "both title and url"),
    ({"cta_1_url": ""}, "both label and url"),
    ({"id": "bad id!"}, "invalid characters"),
])
def test_each_rule_rejects(make_xlsx, make_row, override, expected):
    errs = _errors(make_xlsx, make_row, **override)
    assert any(expected in e for e in errs), errs
    assert all(e.startswith("row 2:") for e in errs)       # points at the Excel row


def test_more_than_three_citations_rejected(make_xlsx, make_row, ok_url):
    errs = _errors(make_xlsx, make_row, citation_2_title="b", citation_2_url=ok_url, citation_3_title="c",
                   citation_3_url=ok_url, citation_4_title="d", citation_4_url=ok_url)
    assert any("at most 3" in e for e in errs)


def test_bare_domain_and_subdomain_are_allowed(make_xlsx, make_row):
    assert not _errors(make_xlsx, make_row, citation_1_url="https://royallondon.com/x")
    assert not _errors(make_xlsx, make_row, citation_1_url="https://www.royallondon.com/x")


@pytest.mark.parametrize("value, expected", [("true", True), ("Yes", True), ("1", True),
                                             ("false", False), ("N", False), ("0", False), ("", False)])
def test_advice_boundary_values(make_xlsx, make_row, value, expected):
    entries, errs, _ = j.validate(j.read_excel(make_xlsx([make_row(advice_boundary=value)])))
    assert not errs
    assert json.loads(entries[0]["response_json"])["advice_boundary"] is expected


def test_soft_limit_is_a_warning_not_an_error(make_xlsx, make_row):
    entries, errs, warns = j.validate(j.read_excel(make_xlsx([make_row(answer="x" * 200)])))
    assert len(entries) == 1 and not errs
    assert any("soft limit" in w for w in warns)


def test_duplicate_questions_and_ids_rejected(make_xlsx, make_row):
    _, errs, _ = j.validate(j.read_excel(make_xlsx([make_row(), make_row(question="what is a  PENSION ?")])))
    assert any("duplicate of row 2" in e for e in errs)
    _, errs, _ = j.validate(j.read_excel(make_xlsx([make_row(id="a1"), make_row(id="a1", question="Other?")])))
    assert any("already used in row 2" in e for e in errs)


def test_explicit_id_is_kept(make_xlsx, make_row):
    entries, errs, _ = j.validate(j.read_excel(make_xlsx([make_row(id="my-id_1")])))
    assert not errs and entries[0]["id"] == "my-id_1"


def test_inactive_rows_are_skipped_not_errors(make_xlsx, make_row):
    entries, errs, _ = j.validate(j.read_excel(make_xlsx([make_row(active="N", answer=""), make_row(question="Other?")])))
    assert not errs and [e["question"] for e in entries] == ["Other?"]


def test_only_inactive_rows_means_nothing_to_index(make_xlsx, make_row):
    assert j.validate(j.read_excel(make_xlsx([make_row(active="no")])))[1] == ["no active rows found"]


def test_every_problem_in_every_row_is_reported(make_xlsx, make_row):
    _, errs, _ = j.validate(j.read_excel(make_xlsx([make_row(answer=""), make_row(question="B?", intent="x")])))
    assert any(e.startswith("row 2:") for e in errs) and any(e.startswith("row 3:") for e in errs)


def test_sample_workbook_is_valid(tmp_path, monkeypatch):
    """make_sample_xlsx.py output (what product owners copy from) must pass validation."""
    import runpy
    monkeypatch.chdir(tmp_path)
    runpy.run_path(str(__import__("pathlib").Path(__file__).parents[3] / "make_sample_xlsx.py"))
    entries, errs, _ = j.validate(j.read_excel("sample_templated.xlsx"))
    assert len(entries) == 3 and not errs, errs


def test_minimum_citations_rule_when_configured(monkeypatch, make_xlsx, make_row):
    monkeypatch.setattr(j, "CITATION_MIN", 1)
    errs = _errors(make_xlsx, make_row, citation_1_title="", citation_1_url="")
    assert any("at least 1 citation required" in e for e in errs)
