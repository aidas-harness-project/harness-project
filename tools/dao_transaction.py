"""Multi-file fail-closed transactions for the DAO (Part 11J commit a).

Every guardrail the DAO enforces is per-file: one lock, one validate, one
atomic write. That is enough while a change touches one file. It is not enough
for a source-text revision, which has to move three things at once -- the
processed text every offset is expressed against, the run-state that records
which stages were validated against it, and the pointer naming the current
revision -- and must never be observed half-done.

Two failures this module exists to remove.

**Fail-open cascades.** `_invalidate_dependents` returns `[]` both when it
invalidated nothing and when it COULD NOT invalidate (lock contention, a
run-state that would fail schema validation). Its three callers discarded that
return value and reported success, so a rewritten source text could leave
downstream stages sitting `passed` against bytes that no longer existed, with
exit code 0 and a WARNING nobody reads. The write was already durable by then,
so the caller could not have undone it even had it noticed. The ordering was
backwards: the irreversible step ran first, the step that can fail ran second.

**Crash windows.** Writing the text, then flipping the pointer, then
invalidating run-state leaves a crash between the pointer flip and the
invalidation showing new text alongside stale `passed` stages -- the exact
state the cascade exists to prevent, reached by interruption instead of by
omission.

The ordering rule here is the fix for both: **every step that can fail runs
before any step that cannot be undone, and the observable pointer flips last.**
A crash therefore lands on either the old text with conservatively invalidated
stages (safe -- over-invalidation costs a rerun) or the new text with those
same stages already invalidated (safe). It can never produce new text with a
stale pass.

Recovery deliberately does NOT finish an interrupted transaction. A journal
records intent, and an interrupted transaction is rolled back toward the
pre-transaction pointer, leaving the conservative invalidation standing. A
machine completing a half-finished transition it does not understand is how a
recovery path becomes a corruption path; making the stage rerun is cheap, and
the invalidation that survives is the honest record that it must.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class TransactionAborted(Exception):
    """Raised when a step that can fail did fail. Nothing durable has been
    changed at the point this is raised -- that is the invariant the ordering
    exists to provide, not merely a convention."""


# --- global lock order -----------------------------------------------------
# Deadlock is only possible when two holders take the same locks in different
# orders. Every multi-file DAO operation acquires locks in exactly this order
# and releases in reverse. The order runs coarse -> fine: run-state governs the
# whole case, a revision file governs one version of one document.
#
# `acquire_ordered` enforces this rather than trusting call sites to remember
# it, because a lock order that lives only in a comment is one refactor away
# from being violated silently.
LOCK_ORDER = (
    "semantic_index",
    "run_state",
    "document_manifest",
    "revision_index",
    "current_pointer",
    "revision_file",
)

_ORDER_INDEX = {name: index for index, name in enumerate(LOCK_ORDER)}


def check_lock_order(kinds) -> list[str]:
    """Return errors if `kinds` is not a strictly increasing LOCK_ORDER
    sequence. Duplicates are rejected too: taking the same lock kind twice in
    one transaction means two different files are competing for one slot, and
    the order between them is undefined."""
    errors = []
    unknown = [k for k in kinds if k not in _ORDER_INDEX]
    if unknown:
        errors.append(
            f"unknown lock kind(s) {unknown} -- every lock a transaction takes "
            f"must have a declared position in {list(LOCK_ORDER)}")
        return errors
    positions = [_ORDER_INDEX[k] for k in kinds]
    if positions != sorted(positions):
        errors.append(
            f"lock order violation: {list(kinds)} is not in the global order "
            f"{list(LOCK_ORDER)} -- acquiring out of order is how two "
            "concurrent DAO operations deadlock")
    if len(set(positions)) != len(positions):
        errors.append(
            f"lock order violation: {list(kinds)} takes the same lock kind "
            "more than once, which leaves the order between those files "
            "undefined")
    return errors


# --- journal ---------------------------------------------------------------

def journal_path(case_dir: Path) -> Path:
    return case_dir / "_transaction_journal.json"


def write_journal(case_dir: Path, entry: dict) -> Path:
    """Record intent durably BEFORE the first irreversible step.

    fsync'd deliberately: a journal that is still in the OS page cache when the
    machine dies records nothing, and a recovery path that cannot tell "no
    transaction was running" from "a transaction was running and I lost the
    record" is worse than no journal at all -- it would report clean.
    """
    target = journal_path(case_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(entry, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def read_journal(case_dir: Path):
    target = journal_path(case_dir)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A torn journal still means a transaction was interrupted. Reporting
        # "no transaction" here would be the one answer that is certainly
        # wrong, so surface it as an unreadable-but-present journal.
        return {"status": "unreadable"}


def clear_journal(case_dir: Path) -> None:
    target = journal_path(case_dir)
    if target.exists():
        target.unlink()


def pending_journal_errors(case_dir: Path) -> list[str]:
    """Blockers for starting new work while an interrupted transaction stands.

    Not auto-repaired here: recovery rolls back toward the pre-transaction
    pointer and leaves the conservative invalidation in place, so the affected
    stage reruns. Silently resuming someone else's half-finished multi-file
    transition is how a recovery path becomes a corruption path.
    """
    entry = read_journal(case_dir)
    if entry is None:
        return []
    if entry.get("status") == "committed":
        return []
    return [
        "an interrupted DAO transaction is recorded in "
        f"{journal_path(case_dir).name} (operation="
        f"{entry.get('operation', 'unknown')!r}, status="
        f"{entry.get('status', 'unknown')!r}) -- the affected stage must be "
        "rerun; the transaction is not resumed automatically"
    ]
