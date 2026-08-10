"""dao.py's write-contract: lock acquire/release, the
atomic-write-then-validate-fail rollback known-gaps.md item 4 named
explicitly, and acquire_lock_blocking's wait-for-clear behavior (item 7 --
every lock in the DAO now blocks instead of failing fast, per P5's
already-documented 30s/15min cadence, now owned by the DAO itself -- with the
poll interval overridable for parallel batches of short commits).
"""
import json
import os
from pathlib import Path
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
    assert dao.read_lock(target) is None, "lock must be released after a successful write"


def test_write_contract_rejects_ancestor_swap_into_medical_revision_namespace(
    isolated_dao,
    make_args,
    monkeypatch,
    tmp_path,
):
    case_dir = dao.case_dir("CASE_009")
    revision_directory = case_dir / "_medical_variable_revisions"
    revision_directory.mkdir()
    digest = "b" * 64
    protected = revision_directory / f"{digest}.json"
    escaped_lock = protected.with_name(protected.name + ".lock")
    protected.write_bytes(b"immutable revision")
    safe_alias = case_dir / "safe-alias"
    safe_alias.mkdir()
    repository = dao.sys.modules["medical_repository"]
    real_guard = repository.require_generic_target_allowed
    real_read_text = dao.Path.read_text
    observed_escaped_lock = []

    def swap_after_generic_guard(dao_module, case_id, filename):
        real_guard(dao_module, case_id, filename)
        safe_alias.rmdir()
        safe_alias.symlink_to(
            revision_directory.name,
            target_is_directory=True,
        )

    def observe_lock_before_input_read(path, *args, **kwargs):
        if path == Path(data_file):
            observed_escaped_lock.append(escaped_lock.exists())
        return real_read_text(path, *args, **kwargs)

    data_file = _write_data_file(tmp_path, VALID_COVERAGE_RESULT)

    monkeypatch.setattr(
        repository,
        "require_generic_target_allowed",
        swap_after_generic_guard,
    )
    monkeypatch.setattr(dao.Path, "read_text", observe_lock_before_input_read)
    result = dao.cmd_write_contract(make_args(
        case_id="CASE_009",
        filename=f"safe-alias/{digest}.json",
        data_file=data_file,
        schema_name="coverage_result.schema.json",
    ))

    assert result == 1
    assert protected.read_bytes() == b"immutable revision"
    assert not any(observed_escaped_lock)


def test_write_contract_rejects_parent_replacement_after_lock(
    isolated_dao,
    make_args,
    monkeypatch,
    tmp_path,
):
    case_dir = dao.case_dir("CASE_009")
    parent = case_dir / "safe-parent"
    displaced = case_dir / "displaced-parent"
    parent.mkdir()
    data_file = _write_data_file(tmp_path, VALID_COVERAGE_RESULT)
    real_read_text = dao.Path.read_text
    swapped = False

    def replace_parent_during_input_read(path, *args, **kwargs):
        nonlocal swapped
        if path == Path(data_file) and not swapped:
            parent.rename(displaced)
            parent.mkdir()
            swapped = True
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(dao.Path, "read_text", replace_parent_during_input_read)
    result = dao.cmd_write_contract(make_args(
        case_id="CASE_009",
        filename="safe-parent/coverage_result.json",
        data_file=data_file,
        schema_name="coverage_result.schema.json",
    ))

    assert swapped
    assert result == 1
    assert not (parent / "coverage_result.json").exists()
    assert not (displaced / "coverage_result.json").exists()
    assert dao.read_lock(displaced / "coverage_result.json") is None


def test_anchored_lock_release_preserves_replacement_inode(isolated_dao):
    case_dir = dao.case_dir("CASE_009")
    target = case_dir / "nested" / "thing.json"
    owned_lock, existing = dao.acquire_lock_beneath(
        case_dir,
        "nested/thing.json",
        "original-holder",
        "RUN_001",
        "test replacement ownership",
    )
    assert existing is None
    assert owned_lock is not None
    lock_file = target.with_name(target.name + ".lock")
    lock_file.unlink()
    lock_file.write_text("foreign replacement", encoding="utf-8")

    dao.release_lock_beneath(owned_lock)

    assert lock_file.read_text(encoding="utf-8") == "foreign replacement"


def test_anchored_lock_fifo_collision_returns_without_blocking(
    isolated_dao,
    monkeypatch,
):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO is unavailable on this platform")
    case_dir = dao.case_dir("CASE_009")
    parent = case_dir / "nested"
    parent.mkdir()
    os.mkfifo(parent / "thing.json.lock")
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)

    started = time.monotonic()
    owned_lock, existing = dao.acquire_lock_beneath_blocking(
        case_dir,
        "nested/thing.json",
        "new-holder",
        "RUN_002",
        "test FIFO collision",
    )

    assert time.monotonic() - started < 1
    assert owned_lock is None
    assert existing == {
        "held_by": "unknown",
        "run_id": "unknown",
        "purpose": "unsafe lock path",
    }


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
    assert dao.read_lock(target) is None, "lock must be released even when the schema name is bad"


def test_write_contract_schema_validation_failure_writes_nothing_and_releases_lock(isolated_dao, make_args, tmp_path):
    invalid = dict(VALID_COVERAGE_RESULT)
    invalid["coverages"] = [{"coverage_name": "a"}]  # missing every other required field
    data_file = _write_data_file(tmp_path, invalid)
    args = make_args(filename="coverage_result.json", data_file=data_file, schema_name="coverage_result.schema.json")

    rc = dao.cmd_write_contract(args)

    target = isolated_dao / "outputs" / "CASE_009" / "coverage_result.json"
    assert rc == 1
    assert not target.exists(), "atomic-write-then-validate-fail: nothing should land on disk"
    assert dao.read_lock(target) is None, "lock must not remain held on validation failure"


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
    acquired = threading.Event()

    def hold_briefly():
        assert dao.acquire_lock(
            target, "someone-else", "RUN_OTHER", "holding briefly"
        ) is None
        acquired.set()
        time.sleep(0.06)
        dao.release_lock(target)

    holder = threading.Thread(target=hold_briefly)
    holder.start()
    assert acquired.wait(timeout=1)

    result = dao.acquire_lock_blocking(target, "me", "RUN_MINE", "waiting my turn")
    holder.join(timeout=1)

    assert result is None, "must eventually succeed once the other holder releases"
    lock = dao.read_lock(target)
    assert lock["held_by"] == "me", "the lock now held is mine, acquired fresh after the wait"


def test_lock_path_replacement_cannot_create_second_generic_owner(isolated_dao):
    target = isolated_dao / "outputs" / "CASE_009" / "thing.json"
    assert dao.acquire_lock(target, "first", "RUN_001", "original owner") is None
    dao.lock_path(target).unlink()

    existing = dao.acquire_lock(target, "second", "RUN_002", "replacement owner")

    assert existing is not None
    dao.release_lock(target)
    assert dao.acquire_lock(target, "second", "RUN_002", "after release") is None
    dao.release_lock(target)


def test_lock_path_replacement_cannot_create_second_owned_owner(isolated_dao):
    target = isolated_dao / "outputs" / "CASE_009" / "thing.json"
    first, existing = dao.acquire_owned_lock(target, "first", "RUN_001", "original owner")
    assert first is not None and existing is None
    dao.lock_path(target).unlink()

    second, existing = dao.acquire_owned_lock(target, "second", "RUN_002", "replacement owner")

    assert second is None and existing is not None
    dao.release_owned_lock(first)
    second, existing = dao.acquire_owned_lock(target, "second", "RUN_002", "after release")
    assert second is not None and existing is None
    dao.release_owned_lock(second)


def test_lock_path_replacement_cannot_create_second_anchored_owner(isolated_dao):
    case_dir = dao.case_dir("CASE_009")
    first, existing = dao.acquire_lock_beneath(
        case_dir, "nested/thing.json", "first", "RUN_001", "original owner"
    )
    assert first is not None and existing is None
    target = case_dir / "nested" / "thing.json"
    dao.lock_path(target).unlink()

    second, existing = dao.acquire_lock_beneath(
        case_dir, "nested/thing.json", "second", "RUN_002", "replacement owner"
    )

    assert second is None and existing is not None
    dao.release_lock_beneath(first)
    second, existing = dao.acquire_lock_beneath(
        case_dir, "nested/thing.json", "second", "RUN_002", "after release"
    )
    assert second is not None and existing is None
    dao.release_lock_beneath(second)


def test_unrelated_thread_cannot_release_generic_lock(isolated_dao):
    target = isolated_dao / "outputs" / "CASE_009" / "thing.json"
    assert dao.acquire_lock(target, "first", "RUN_001", "thread owner") is None

    releaser = threading.Thread(target=dao.release_lock, args=(target,))
    releaser.start()
    releaser.join(timeout=1)

    assert dao.acquire_lock(target, "second", "RUN_002", "must remain blocked") is not None
    dao.release_lock(target)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_fork_child_cannot_release_parent_generic_lock(isolated_dao):
    target = isolated_dao / "outputs" / "CASE_009" / "thing.json"
    assert dao.acquire_lock(target, "parent", "RUN_001", "parent owner") is None

    child = os.fork()
    if child == 0:
        dao.release_lock(target)
        os._exit(0)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0

    assert dao.acquire_lock(target, "second", "RUN_002", "must remain blocked") is not None
    dao.release_lock(target)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
@pytest.mark.parametrize("family", ["generic", "owned", "anchored"])
def test_fork_during_kernel_token_handoff_does_not_leak_ownership(
    tmp_path: Path,
    monkeypatch,
    family: str,
):
    target = tmp_path / f"{family}.json"
    child_ready_read, child_ready_write = os.pipe()
    child_exit_read, child_exit_write = os.pipe()
    child_pid: int | None = None
    original_try_kernel_lock = dao._try_kernel_lock

    def fork_after_kernel_bind(key: str):
        nonlocal child_pid
        token = original_try_kernel_lock(key)
        if token is None:
            return None
        child_pid = os.fork()
        if child_pid == 0:
            os.close(child_ready_read)
            os.close(child_exit_write)
            os.write(child_ready_write, b"1")
            os.read(child_exit_read, 1)
            os._exit(0)
        os.close(child_ready_write)
        os.close(child_exit_read)
        assert os.read(child_ready_read, 1) == b"1"
        return token

    def acquire():
        if family == "generic":
            existing = dao.acquire_lock(
                target, "holder", "RUN_HANDOFF", "fork handoff"
            )
            return target if existing is None else None
        if family == "owned":
            owner, _ = dao.acquire_owned_lock(
                target, "holder", "RUN_HANDOFF", "fork handoff"
            )
            return owner
        owner, _ = dao.acquire_lock_beneath(
            tmp_path,
            target.name,
            "holder",
            "RUN_HANDOFF",
            "fork handoff",
        )
        return owner

    def release(owner) -> None:
        if family == "generic":
            dao.release_lock(owner)
        elif family == "owned":
            dao.release_owned_lock(owner)
        else:
            dao.release_lock_beneath(owner)

    monkeypatch.setattr(dao, "_try_kernel_lock", fork_after_kernel_bind)
    first_owner = acquire()
    assert first_owner is not None
    monkeypatch.setattr(dao, "_try_kernel_lock", original_try_kernel_lock)
    release(first_owner)

    second_owner = None
    try:
        second_owner = acquire()
        assert second_owner is not None
    finally:
        if second_owner is not None:
            release(second_owner)
        os.write(child_exit_write, b"1")
        if child_pid is not None:
            os.waitpid(child_pid, 0)
        os.close(child_ready_read)
        os.close(child_exit_write)


def test_generic_lock_release_never_unlinks_the_lock_path(
    isolated_dao, monkeypatch
):
    target = isolated_dao / "outputs" / "CASE_009" / "thing.json"
    assert dao.acquire_lock(target, "owner", "RUN_001", "synthetic") is None
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda *_args, **_kwargs: pytest.fail(
            "generic lock release must never unlink a pathname"
        ),
    )

    dao.release_lock(target)


def test_release_lock_tolerates_a_concurrent_release(isolated_dao):
    """The exact CASE_907 crash: two workers release the same lock and the
    loser hit FileNotFoundError between exists() and unlink(), failing a
    caller whose write had already committed."""
    target = isolated_dao / "outputs" / "CASE_009" / "thing.json"
    dao.acquire_lock(target, "worker-a", "RUN_A", "committing")

    dao.release_lock(target)
    dao.release_lock(target)  # must not raise

    assert dao.read_lock(target) is None


def test_release_lock_removes_a_held_lock(isolated_dao):
    target = isolated_dao / "outputs" / "CASE_009" / "thing.json"
    dao.acquire_lock(target, "worker-a", "RUN_A", "committing")
    assert dao.read_lock(target) is not None

    dao.release_lock(target)

    assert dao.read_lock(target) is None, "release must still actually release"


# ------------------------------------------------- poll-interval resolution --
#
# The interval became configurable because a parallel batch of short commits
# (analyze-policy-polarity appending one receipt) waits 30s per contended
# commit for work that takes milliseconds. A bad value must degrade to the
# default, never spin (0/negative) and never hard-fail the run.

def test_lock_poll_interval_defaults_when_unset():
    assert dao._lock_poll_interval({}) == 30.0


def test_lock_poll_interval_reads_env_override():
    assert dao._lock_poll_interval({dao.LOCK_POLL_INTERVAL_ENV: "0.5"}) == 0.5


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "", "   "])
def test_lock_poll_interval_rejects_values_that_would_spin_or_crash(raw):
    assert dao._lock_poll_interval({dao.LOCK_POLL_INTERVAL_ENV: raw}) == 30.0, (
        "a zero/negative interval would busy-spin and a malformed one must not "
        "turn every lock wait into a configuration failure"
    )


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
    entries = [{"file_name": "a.pdf", "classification": "raw", "review_status": "pending",
                "reviewed_by": None, "reviewed_at": None, "rejection_reason": None}]
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), {
        "ledger_version": "source_ledger.v0.4",
        "case_id": "CASE_009", "source_dir": "x", "created_at": dao.now_iso(), "updated_at": dao.now_iso(),
        "files": entries,
        "history_boundary": dao.make_history_boundary(entries, mode="native"),
        "operations": [],
    })
    dao.acquire_lock(dao.source_ledger_path("CASE_009"), "someone-else", "RUN_OTHER", "holding")

    rc = dao.cmd_set_ledger_status(make_args(file_name="a.pdf", status="approved", reviewer="human"))

    assert rc == 1
    ledger = dao.load_json(dao.source_ledger_path("CASE_009"))
    assert ledger["files"][0]["review_status"] == "pending", "unchanged while locked"
