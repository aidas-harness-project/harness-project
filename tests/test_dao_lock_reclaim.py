"""Reclaiming a lock whose owner died without releasing it.

A holder that crashes leaves its lock file behind. `release_lock` runs in a
`finally`, which covers exceptions but not process death, and the lock record
carried only human-readable labels (`held_by`, `run_id`) -- several processes
legitimately share one, so a waiter could never ask "is that holder alive?".

Found for real on CASE_140: a `write-redacted-text` worker for DOC_002 died
holding the case-level `_revision_index.json` lock, and the DOC_001/DOC_003
workers -- whose own per-document work was completely independent -- blocked
behind it for P5's full 15-minute cap. The shared index is what turns one
dead worker into a case-wide stall.

The reclaim is deliberately conservative: it fires only when the owner is
PROVABLY gone (same machine, same boot, pid absent). Every uncertain case
keeps waiting, because waiting on a dead holder costs time while reclaiming a
live holder's lock corrupts the state the lock exists to protect.
"""
import json
import os
from pathlib import Path

import pytest

import dao


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


def _target(tmp_path: Path) -> Path:
    t = tmp_path / "shared_index.json"
    t.write_text("{}", encoding="utf-8")
    return t


def _dead_pid() -> int:
    """A pid that is not running. Allocated by spawning nothing: we take a pid
    that psutil confirms absent rather than trusting an arbitrary constant."""
    import psutil
    for candidate in range(90000, 100000):
        if not psutil.pid_exists(candidate):
            return candidate
    raise RuntimeError("no free pid found")


def test_lock_records_owner_identity(tmp_path):
    """held_by alone cannot answer 'is the holder alive'; the pid+boot can."""
    target = _target(tmp_path)
    assert dao.acquire_lock(target, "Claude", "RUN_1", "write") is None

    owner = dao.read_lock(target)["owner"]
    assert owner["pid"] == os.getpid()
    assert owner["machine"]
    assert owner["boot_id"], "boot_id is what makes a recycled pid safe to trust"


def test_blocking_acquire_reclaims_lock_of_dead_owner(tmp_path):
    """The CASE_140 deadlock: waiter must not block on a dead holder."""
    target = _target(tmp_path)
    dao.acquire_lock(target, "Claude", "RUN_1", "register revision for DOC_002")

    # Rewrite the holder as a process that is not running.
    lock_file = dao.lock_path(target)
    record = json.loads(lock_file.read_text(encoding="utf-8"))
    record["owner"]["pid"] = _dead_pid()
    lock_file.write_text(json.dumps(record), encoding="utf-8")

    # Cap is 0.05s here; without reclaim this returns the still-held lock.
    assert dao.acquire_lock_blocking(target, "Claude", "RUN_1", "write") is None
    assert dao.read_lock(target)["owner"]["pid"] == os.getpid()


def test_live_owner_lock_is_never_reclaimed(tmp_path):
    """The dangerous direction: a live holder must keep its lock."""
    target = _target(tmp_path)
    dao.acquire_lock(target, "OtherWorker", "RUN_1", "write")

    # Owner is this very process -- provably alive.
    existing = dao.acquire_lock_blocking(target, "Claude", "RUN_2", "write")
    assert existing is not None, "must not steal a lock from a running owner"
    assert existing["held_by"] == "OtherWorker"


def test_lock_from_different_boot_is_not_reclaimed(tmp_path):
    """A pid from a previous boot may collide with an unrelated live process."""
    target = _target(tmp_path)
    dao.acquire_lock(target, "Claude", "RUN_1", "write")

    lock_file = dao.lock_path(target)
    record = json.loads(lock_file.read_text(encoding="utf-8"))
    record["owner"]["boot_id"] = "1"  # some earlier boot
    record["owner"]["pid"] = _dead_pid()
    lock_file.write_text(json.dumps(record), encoding="utf-8")

    assert dao.acquire_lock_blocking(target, "Claude", "RUN_2", "write") is not None


def test_lock_from_different_machine_is_not_reclaimed(tmp_path):
    """This process cannot interrogate another host's process table."""
    target = _target(tmp_path)
    dao.acquire_lock(target, "Claude", "RUN_1", "write")

    lock_file = dao.lock_path(target)
    record = json.loads(lock_file.read_text(encoding="utf-8"))
    record["owner"]["machine"] = "some-other-host"
    record["owner"]["pid"] = _dead_pid()
    lock_file.write_text(json.dumps(record), encoding="utf-8")

    assert dao.acquire_lock_blocking(target, "Claude", "RUN_2", "write") is not None


def test_legacy_lock_without_owner_is_not_reclaimed(tmp_path):
    """A lock written before this change has no owner record. Unknown means
    held: silently reclaiming it would break exclusion during a mixed-version
    rollout."""
    target = _target(tmp_path)
    dao.acquire_lock(target, "Claude", "RUN_1", "write")

    lock_file = dao.lock_path(target)
    record = json.loads(lock_file.read_text(encoding="utf-8"))
    del record["owner"]
    lock_file.write_text(json.dumps(record), encoding="utf-8")

    assert dao.acquire_lock_blocking(target, "Claude", "RUN_2", "write") is not None


def test_reclaim_aborts_when_lock_changed_underneath(tmp_path):
    """Between deciding 'dead' and unlinking, another waiter may have already
    reclaimed and re-acquired. Removing whatever is there NOW would delete a
    live lock, so the removal is guarded by the exact bytes examined."""
    target = _target(tmp_path)
    dao.acquire_lock(target, "Claude", "RUN_1", "write")
    lock_file = dao.lock_path(target)

    stale = json.loads(lock_file.read_text(encoding="utf-8"))
    stale["owner"] = dict(stale["owner"], pid=_dead_pid())

    # On disk sits a DIFFERENT, live lock -- not the record being judged.
    live = json.loads(lock_file.read_text(encoding="utf-8"))
    live["held_by"] = "NewOwner"
    lock_file.write_text(json.dumps(live), encoding="utf-8")

    assert dao._reclaim_if_dead(target, stale) is False
    assert dao.read_lock(target)["held_by"] == "NewOwner"


def test_release_survives_windows_permission_error(tmp_path, monkeypatch):
    """The defect that CAUSED the CASE_140 deadlock.

    On Windows, `unlink` raises PermissionError (WinError 32) while any other
    process holds the file open -- which `read_lock` does on every poll. The
    original `except FileNotFoundError` did not cover it, so the exception
    escaped from `_register_revision`'s `finally`, killed a writer that had
    ALREADY committed, and left its lock behind to stall everyone else.
    """
    target = _target(tmp_path)
    dao.acquire_lock(target, "Claude", "RUN_1", "write")

    calls = {"n": 0}
    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **kw):
        # Blocked while a concurrent reader holds a handle, then succeeds.
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "in use by another process")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(dao, "_RELEASE_RETRY_DELAY_SECONDS", 0.001)

    dao.release_lock(target)  # must not raise
    assert dao.read_lock(target) is None, "lock must be gone after release"


def test_release_never_raises_when_unlink_stays_blocked(tmp_path, monkeypatch):
    """Releasing is cleanup on an already-committed write. If the file truly
    cannot be removed, the caller must still succeed -- the leftover lock is
    the dead-owner reclaim's job, not a reason to fail completed work."""
    target = _target(tmp_path)
    dao.acquire_lock(target, "Claude", "RUN_1", "write")

    def always_blocked(self, *a, **kw):
        raise PermissionError(32, "in use by another process")

    monkeypatch.setattr(Path, "unlink", always_blocked)
    monkeypatch.setattr(dao, "_RELEASE_RETRY_DELAY_SECONDS", 0.001)

    dao.release_lock(target)  # must not raise


def test_reclaim_disabled_without_psutil(tmp_path, monkeypatch):
    """No psutil means no way to prove death -- fall back to waiting rather
    than guessing."""
    target = _target(tmp_path)
    dao.acquire_lock(target, "Claude", "RUN_1", "write")

    lock_file = dao.lock_path(target)
    record = json.loads(lock_file.read_text(encoding="utf-8"))
    record["owner"]["pid"] = _dead_pid()
    lock_file.write_text(json.dumps(record), encoding="utf-8")

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def no_psutil(name, *a, **kw):
        if name == "psutil":
            raise ImportError("psutil unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", no_psutil)
    assert dao.acquire_lock_blocking(target, "Claude", "RUN_2", "write") is not None
