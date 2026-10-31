"""sync_entries: plan -> embed -> upsert -> delete stale -> verify, against an in-memory fake index."""
import pytest

from src import templated_job as j


def test_first_run_adds_everything(fake_ops, entries, embed4):
    ops = fake_ops()
    r = j.sync_entries(entries(3), ops, embed=embed4)
    assert (r["added"], r["updated"], r["deleted"], r["rows"]) == (3, 0, 0, 3)
    assert set(ops.docs) == {"q1", "q2", "q3"} and r["success"] and not r["dry_run"]
    assert r["index_name"] == "test-templated-index"


def test_written_document_shape(fake_ops, entries, embed4):
    ops = fake_ops()
    j.sync_entries(entries(1), ops, embed=embed4)
    doc = ops.docs["q1"]
    assert set(doc) == {"id", "question", "question_vector", "response_json", "source_urls", "created_at"}
    assert doc["question_vector"] == [0.1, 0.2, 0.3, 0.4] and doc["created_at"].endswith("+00:00")


def test_changed_rows_are_updated_and_missing_ids_deleted(fake_ops, entries, embed4):
    ops = fake_ops(existing=["q1", "q2", "gone"])
    r = j.sync_entries(entries(3), ops, embed=embed4)            # q1..q3 wanted
    assert (r["added"], r["updated"], r["deleted"]) == (1, 2, 1)
    assert set(ops.docs) == {"q1", "q2", "q3"}                   # 'gone' removed


def test_delete_happens_only_after_upload(fake_ops, entries, embed4):
    ops = fake_ops(existing=["q1", "old"])
    j.sync_entries(entries(1), ops, embed=embed4)
    assert ops.calls.index("upload") < ops.calls.index("delete")
    assert ops.calls[:2] == ["probe", "scan"]                    # preflight and read-only plan come first


def test_failed_upload_deletes_nothing(fake_ops, entries, embed4):
    ops = fake_ops(existing=["q1", "old"])
    ops.fail_upload = True
    with pytest.raises(RuntimeError, match="boom"):
        j.sync_entries(entries(1), ops, embed=embed4)
    assert "delete" not in ops.calls and "old" in ops.docs


def test_rerun_is_idempotent(fake_ops, entries, embed4):
    ops = fake_ops()
    j.sync_entries(entries(3), ops, embed=embed4)
    r = j.sync_entries(entries(3), ops, embed=embed4)
    assert (r["added"], r["updated"], r["deleted"]) == (0, 3, 0) and len(ops.docs) == 3


def test_no_delete_call_when_nothing_is_stale(fake_ops, entries, embed4):
    ops = fake_ops(existing=["q1"])
    j.sync_entries(entries(2), ops, embed=embed4)
    assert "delete" not in ops.calls


# ── circuit breaker ───────────────────────────────────────────
def test_mass_removal_is_refused_before_any_write(fake_ops, entries, embed4):
    ops = fake_ops(existing=[f"q{i}" for i in range(1, 11)])     # 10 in index
    with pytest.raises(RuntimeError, match="refusing to delete 8 of 10"):
        j.sync_entries(entries(2), ops, embed=embed4)            # Excel has only 2 -> 8 deletions
    assert ops.calls == ["probe", "scan"] and len(ops.docs) == 10 and embed4.calls == []


def test_mass_removal_allowed_with_flag(fake_ops, entries, embed4):
    ops = fake_ops(existing=[f"q{i}" for i in range(1, 11)])
    r = j.sync_entries(entries(2), ops, embed=embed4, allow_mass_removal=True)
    assert r["deleted"] == 8 and set(ops.docs) == {"q1", "q2"}


def test_small_removals_are_not_blocked(fake_ops, entries, embed4):
    ops = fake_ops(existing=["q1", "a", "b"])                    # 2 stale < 3 minimum
    assert j.sync_entries(entries(1), ops, embed=embed4)["deleted"] == 2


def test_removals_under_the_ratio_are_not_blocked(fake_ops, entries, embed4):
    ops = fake_ops(existing=[f"q{i}" for i in range(1, 11)] + ["x", "y", "z"])   # 13 in index, 3 stale = 23%
    assert j.sync_entries(entries(10), ops, embed=embed4)["deleted"] == 3


# ── dry run ───────────────────────────────────────────────────
def test_dry_run_reports_the_plan_and_writes_nothing(fake_ops, entries, embed4):
    ops = fake_ops(existing=["q1", "old"])
    r = j.sync_entries(entries(2), ops, embed=embed4, dry_run=True)
    assert r["dry_run"] and (r["added"], r["updated"], r["deleted"]) == (1, 1, 1)
    assert ops.calls == ["probe", "scan"] and embed4.calls == [] and set(ops.docs) == {"q1", "old"}


def test_dry_run_still_applies_the_circuit_breaker(fake_ops, entries, embed4):
    ops = fake_ops(existing=[f"q{i}" for i in range(1, 11)])
    with pytest.raises(RuntimeError, match="refusing"):
        j.sync_entries(entries(1), ops, embed=embed4, dry_run=True)


# ── safety checks ─────────────────────────────────────────────
def test_preflight_failure_stops_everything(fake_ops, entries, embed4):
    ops = fake_ops()
    ops.fail_probe = RuntimeError("index does not exist")
    with pytest.raises(RuntimeError, match="does not exist"):
        j.sync_entries(entries(1), ops, embed=embed4)
    assert ops.calls == ["probe"] and embed4.calls == []


@pytest.mark.parametrize("bad", [lambda t: [[0.1] * 4], lambda t: [[0.1] * 3 for _ in t]])
def test_wrong_embedding_count_or_size_is_caught_before_upload(fake_ops, entries, bad):
    ops = fake_ops()
    with pytest.raises(RuntimeError, match="embedding result mismatch"):
        j.sync_entries(entries(2), ops, embed=bad)
    assert "upload" not in ops.calls


def test_document_count_lag_is_tolerated(fake_ops, entries, embed4):
    ops = fake_ops()
    ops.count_lag = 2                                            # portal/API count catches up on the 3rd poll
    assert j.sync_entries(entries(2), ops, embed=embed4)["success"]
    assert ops.calls.count("count") == 3


def test_persistent_count_mismatch_raises(fake_ops, entries, embed4):
    ops = fake_ops()
    ops.count_lag = 99
    with pytest.raises(RuntimeError, match="re-running the same file is safe"):
        j.sync_entries(entries(2), ops, embed=embed4)
    assert ops.calls.count("count") == j.VERIFY_ATTEMPTS
