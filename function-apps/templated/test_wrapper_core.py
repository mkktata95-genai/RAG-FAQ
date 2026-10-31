"""Blob/queue helpers and sync_workbook (download -> validate -> sync). No Azure."""
import json
from unittest import mock

import pytest

from src import templated_job as j


def xlsx_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# ── names / messages ──────────────────────────────────────────
@pytest.mark.parametrize("name, expected", [
    ("templated-questions/a.xlsx", True), ("templated-questions/A.XLSX", True),
    ("templated-questions/~$a.xlsx", False), ("templated-questions/.hidden.xlsx", False),
    ("templated-questions/a.csv", False), ("templated-questions/a.xls", False),
])
def test_is_excel(name, expected):
    assert j.is_excel(name) is expected


def test_latest_excel_picks_newest_and_skips_temp_files(fake_container):
    box = fake_container({"templated-questions/a.xlsx": b"", "templated-questions/b.xlsx": b"",
                          "templated-questions/~$c.xlsx": b"", "other/z.xlsx": b""})
    assert j.latest_excel(box) == "templated-questions/~$c.xlsx".replace("~$c", "b")  # newest valid = b


def test_latest_excel_none_found(fake_container):
    with pytest.raises(FileNotFoundError, match="No .xlsx found"):
        j.latest_excel(fake_container({"templated-questions/readme.txt": b""}))


@pytest.mark.parametrize("body, expected", [
    ('{"file": "x.xlsx"}', ("templated-questions/x.xlsx", False, False)),
    ('{"file": "x.xlsx", "allow_mass_removal": true, "dry_run": true}', ("templated-questions/x.xlsx", True, True)),
    ('{"file": null}', (None, False, False)),
    ('{}', (None, False, False)),
    ("sync", (None, False, False)),
    ("sync dry_run", (None, False, True)),
    ("ALLOW_MASS_REMOVAL", (None, True, False)),
    ("[1, 2]", (None, False, False)),
])
def test_parse_build_message(body, expected):
    assert j.parse_build_message(body) == expected


# ── sync_workbook ─────────────────────────────────────────────
def test_sync_workbook_end_to_end(fake_container, fake_ops, make_xlsx, make_row, embed4):
    path = make_xlsx([make_row(), make_row(question="Other?")])
    box, ops = fake_container({"templated-questions/a.xlsx": xlsx_bytes(path)}), fake_ops()
    r = j.sync_workbook(box, "templated-questions/a.xlsx", embed=embed4, ops_factory=lambda: ops)
    assert r["rows"] == 2 and r["added"] == 2 and len(ops.docs) == 2 and r["warnings"] == []


def test_rejected_excel_raises_and_never_touches_azure(fake_container, make_xlsx, make_row, caplog):
    path = make_xlsx([make_row(answer=""), make_row(question="B?", intent="x")])
    box = fake_container({"templated-questions/a.xlsx": xlsx_bytes(path)})
    factory, embed = mock.Mock(), mock.Mock()
    with caplog.at_level("ERROR", logger="templated_job"), pytest.raises(ValueError, match="rejected, 2 problem"):
        j.sync_workbook(box, "templated-questions/a.xlsx", embed=embed, ops_factory=factory)
    factory.assert_not_called()
    embed.assert_not_called()
    assert caplog.text.count("TEMPLATED_REJECTED") == 2           # every problem is in the log


def test_warnings_are_logged_and_returned(fake_container, fake_ops, make_xlsx, make_row, embed4, caplog):
    path = make_xlsx([make_row(answer="x" * 200)])
    box = fake_container({"templated-questions/a.xlsx": xlsx_bytes(path)})
    with caplog.at_level("WARNING", logger="templated_job"):
        r = j.sync_workbook(box, "templated-questions/a.xlsx", embed=embed4, ops_factory=lambda: fake_ops())
    assert len(r["warnings"]) == 1 and "TEMPLATED_WARN" in caplog.text


def test_flags_are_passed_through(fake_container, fake_ops, make_xlsx, make_row, embed4):
    path = make_xlsx([make_row()])
    box = fake_container({"templated-questions/a.xlsx": xlsx_bytes(path)})
    ops = fake_ops(existing=[f"old{i}" for i in range(10)])
    with pytest.raises(RuntimeError, match="refusing"):
        j.sync_workbook(box, "templated-questions/a.xlsx", embed=embed4, ops_factory=lambda: ops)
    r = j.sync_workbook(box, "templated-questions/a.xlsx", allow_mass_removal=True, embed=embed4, ops_factory=lambda: ops)
    assert r["deleted"] == 10
    ops2 = fake_ops()
    r = j.sync_workbook(box, "templated-questions/a.xlsx", dry_run=True, embed=embed4, ops_factory=lambda: ops2)
    assert r["dry_run"] and ops2.docs == {}


def test_synced_log_line_names_the_counts(fake_container, fake_ops, make_xlsx, make_row, embed4, caplog):
    box = fake_container({"templated-questions/a.xlsx": xlsx_bytes(make_xlsx([make_row()]))})
    with caplog.at_level("INFO", logger="templated_job"):
        j.sync_workbook(box, "templated-questions/a.xlsx", embed=embed4, ops_factory=lambda: fake_ops())
    assert "TEMPLATED_SYNCED index=test-templated-index" in caplog.text and "added=1" in caplog.text


def test_missing_app_setting_is_reported_before_anything_else(monkeypatch, fake_container):
    monkeypatch.delenv("AZURE_SEARCH_TEMPLATED_INDEX_NAME")
    with pytest.raises(RuntimeError, match="Missing required app settings: AZURE_SEARCH_TEMPLATED_INDEX_NAME"):
        j.sync_workbook(fake_container({}), "templated-questions/a.xlsx")


def test_corrupt_upload_is_a_clean_error(fake_container):
    box = fake_container({"templated-questions/a.xlsx": b"not an excel file"})
    with pytest.raises(ValueError, match="cannot read Excel file"):
        j.sync_workbook(box, "templated-questions/a.xlsx", ops_factory=mock.Mock())


def test_missing_blob_raises(fake_container):
    with pytest.raises(KeyError):
        j.sync_workbook(fake_container({}), "templated-questions/nope.xlsx", ops_factory=mock.Mock())
