"""dao.py's write-contract: lock acquire/release, the
atomic-write-then-validate-fail rollback known-gaps.md item 4 named
explicitly, and acquire_lock_blocking's wait-for-clear behavior (item 7 --
every lock in the DAO now blocks instead of failing fast, per P5's
already-documented 30s/15min cadence, now owned by the DAO itself).
"""
import json
import threading
import time

import pytest

import dao


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    """Every test in this file gets a tiny poll interval and cap -- nothing
    here should ever wait anywhere close to real P5 timing (30s/15min)."""
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


def _write_data_file(tmp_path, obj):
    p = tmp_path / "data.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


VALID_COVERAGE_RESULT = {
    "case_id": "CASE_009", "run_id": "RUN_20260712_001", "component": "claim-analysis",
    "status": "success", "created_at": "2026-07-12T10:00:00+09:00",
    "model_info": {"model_name": "x", "prompt_version": "y"},
    "coverages": [{
        "coverage_name": "a", "standardized_coverage_name": "b", "applicable": True,
        "confidence": 0.9, "evidence_references": [{"document_id": "DOC_001", "page": 1, "quote": "q"}],
        "review_required": False,
    }],
}


def test_write_contract_success_writes_file_and_releases_lock(isolated_dao, make_args, tmp_path):
    data_file = _write_data_file(tmp_path, VALID_COVERAGE_RESULT)
    args = make_args(filename="coverage_result.json", data_file=data_file, schema_name="coverage_result.schema.json")

    rc = dao.cmd_write_contract(args)

    target = isolated_dao / "outputs" / "CASE_009" / "coverage_result.json"
    assert rc == 0
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["coverages"][0]["coverage_name"] == "a"
    assert not target.with_name(target.name + ".lock").exists(), "lock must be released after a successful write"


def test_write_contract_rejects_when_already_locked(isolated_dao, make_args, tmp_path):
    target = isolated_dao / "outputs" / "CASE_009" / "coverage_result.json"
    dao.acquire_lock(target, "someone-else", "RUN_OTHER", "holding for test")

    data_file = _write_data_file(tmp_path, VALID_COVERAGE_RESULT)
    args = make_args(filename="coverage_result.json", data_file=data_file, schema_name="coverage_result.schema.json")
    rc = dao.cmd_write_contract(args)

    assert rc == 1
    assert not target.exists(), "a locked target must not be written"
    lock = dao.read_lock(target)
    assert lock["held_by"] == "someone-else", "the other holder's lock must survive the rejected attempt"


def test_write_contract_unknown_schema_name_fails_cleanly(isolated_dao, make_args, tmp_path):
    data_file = _write_data_file(tmp_path, VALID_COVERAGE_RESULT)
    args = make_args(filename="coverage_result.json", data_file=data_file, schema_name="not_a_real_schema.schema.json")

    rc = dao.cmd_write_contract(args)

    target = isolated_dao / "outputs" / "CASE_009" / "coverage_result.json"
    assert rc == 1
    assert not target.exists()
    assert not target.with_name(target.name + ".lock").exists(), "lock must be released even when the schema name is bad"


def test_write_contract_schema_validation_failure_writes_nothing_and_releases_lock(isolated_dao, make_args, tmp_path):
    invalid = dict(VALID_COVERAGE_RESULT)
    invalid["coverages"] = [{"coverage_name": "a"}]  # missing every other required field
    data_file = _write_data_file(tmp_path, invalid)
    args = make_args(filename="coverage_result.json", data_file=data_file, schema_name="coverage_result.schema.json")

    rc = dao.cmd_write_contract(args)

    target = isolated_dao / "outputs" / "CASE_009" / "coverage_result.json"
    assert rc == 1
    assert not target.exists(), "atomic-write-then-validate-fail: nothing should land on disk"
    assert not target.with_name(target.name + ".lock").exists(), "lock must not be left behind on validation failure"


def test_write_contract_second_write_after_first_release_succeeds(isolated_dao, make_args, tmp_path):
    """The lock is per-write, not permanent -- a clean write leaves the door
    open for the next legitimate write (e.g. a retry after a fix)."""
    data_file = _write_data_file(tmp_path, VALID_COVERAGE_RESULT)
    args = make_args(filename="coverage_result.json", data_file=data_file, schema_name="coverage_result.schema.json")

    assert dao.cmd_write_contract(args) == 0
    assert dao.cmd_write_contract(args) == 0


# ---------------------------------------------- acquire_lock_blocking itself --

def test_acquire_lock_blocking_waits_then_succeeds_once_released(isolated_dao, monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 2.0)
    target = isolated_dao / "outputs" / "CASE_009" / "thing.json"
    dao.acquire_lock(target, "someone-else", "RUN_OTHER", "holding briefly")

    def release_soon():
        time.sleep(0.06)
        dao.release_lock(target)
    threading.Thread(target=release_soon).start()

    result = dao.acquire_lock_blocking(target, "me", "RUN_MINE", "waiting my turn")

    assert result is None, "must eventually succeed once the other holder releases"
    lock = dao.read_lock(target)
    assert lock["held_by"] == "me", "the lock now held is mine, acquired fresh after the wait"


def test_acquire_lock_blocking_gives_up_after_max_wait(isolated_dao, monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.03)
    target = isolated_dao / "outputs" / "CASE_009" / "thing.json"
    dao.acquire_lock(target, "someone-else", "RUN_OTHER", "holding forever")

    result = dao.acquire_lock_blocking(target, "me", "RUN_MINE", "waiting")

    assert result is not None
    assert result["held_by"] == "someone-else", "reports the still-current holder, for the caller to surface to a human"


# --------------------------------- read-modify-write commands: lock coverage --

def test_add_conflict_entry_requires_held_by_and_run_id_and_locks(isolated_dao, make_args, tmp_path):
    sources_file = tmp_path / "sources.json"
    sources_file.write_text(json.dumps([
        {"document_id": "DOC_001", "value": "a", "quote": "q1"},
        {"document_id": "DOC_002", "value": "b", "quote": "q2"},
    ]), encoding="utf-8")
    target = dao.conflict_ledger_path("CASE_009")
    dao.acquire_lock(target, "someone-else", "RUN_OTHER", "holding")

    rc = dao.cmd_add_conflict_entry(make_args(stage="claim_analysis", topic="t", sources_file=str(sources_file)))

    assert rc == 1, "must wait, then report the still-held lock rather than silently proceeding"
    ledger = dao.load_conflict_ledger("CASE_009")
    assert ledger["conflicts"] == [], "nothing written while locked"


def test_set_ledger_status_locks_across_the_whole_operation(isolated_dao, make_args):
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), {
        "case_id": "CASE_009", "source_dir": "x", "created_at": dao.now_iso(), "updated_at": dao.now_iso(),
        "files": [{"file_name": "a.pdf", "classification": "raw", "review_status": "pending",
                   "reviewed_by": None, "reviewed_at": None, "rejection_reason": None}],
    })
    dao.acquire_lock(dao.source_ledger_path("CASE_009"), "someone-else", "RUN_OTHER", "holding")

    rc = dao.cmd_set_ledger_status(make_args(file_name="a.pdf", status="approved", reviewer="human"))

    assert rc == 1
    ledger = dao.load_json(dao.source_ledger_path("CASE_009"))
    assert ledger["files"][0]["review_status"] == "pending", "unchanged while locked"
