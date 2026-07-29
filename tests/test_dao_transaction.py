"""Part 11J commit a: fail-closed multi-file transactions.

Two defects are under test, both of the same shape -- an irreversible step
running before a step that can fail.

`_invalidate_dependents` returned `[]` both when there was nothing to
invalidate and when it COULD NOT invalidate (lock contention, a run-state that
would not validate). All four callers discarded that value, and three of them
reported success. So rewriting a document's processed text could leave
downstream stages sitting `passed` against bytes that no longer existed, at
exit code 0, behind a WARNING no one reads. By the time the warning printed,
the write was durable and the caller could not have undone it.

The ordering is now inverted: validate and invalidate first, flip the
observable pointer last. The tests below inject failures at each step and
assert the invariant that makes the ordering worth having -- **there is no
interruption point that yields new text alongside a stale `passed`.**
"""
import json

import pytest

import dao
import dao_transaction as tx


SOURCE_V1 = "<<<PAGE page=1>>>\n제1조(목적) 이 약관은 보험금을 지급합니다.\n"
SOURCE_V2 = "<<<PAGE page=1>>>\n제1조(목적) 이 약관은 보험금을 지급하지 않습니다.\n"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _stage(name, status="passed"):
    entry = {"stage_name": name, "status": status, "attempt_count": 1}
    if status == "passed":
        entry["backup_path"] = f"outputs/CASE_030/_backups/step_01_{name}"
    return entry


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    """The real DAO waits 900s for a contended lock before giving up -- correct
    for a pipeline where the other holder is usually mid-write, useless in a
    test that deliberately holds one. Only the WAIT is shortened; the
    contention path under test is unchanged."""
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


@pytest.fixture
def case(isolated_dao):
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    processed = dao.processed_dir("CASE_030", "DOC_005")
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text(SOURCE_V1, encoding="utf-8")
    _write_json(out / "_run_state.json", {
        "case_id": "CASE_030", "run_id": "RUN_20260724_001",
        "stages": [_stage("document_processing"),
                   _stage("policy_clause_processing"),
                   _stage("claim_analysis")],
    })
    return out


def _revise(make_args, isolated_dao, text=SOURCE_V2):
    new_text = isolated_dao / "new.md"
    new_text.write_text(text, encoding="utf-8")
    return dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(new_text),
        held_by="document-pipeline", run_id="RUN_20260724_001"))


def _current_text():
    return (dao.processed_dir("CASE_030", "DOC_005")
            / "redacted_text.md").read_text(encoding="utf-8")


def _statuses():
    return {s["stage_name"]: s["status"]
            for s in dao.load_run_state("CASE_030")["stages"]}


# --- lock order ------------------------------------------------------------

def test_declared_lock_order_is_enforced_not_merely_documented():
    assert tx.check_lock_order(["run_state", "current_pointer"]) == []
    # Reversed: the deadlock shape.
    errors = tx.check_lock_order(["current_pointer", "run_state"])
    assert any("lock order violation" in e for e in errors), errors
    # Same kind twice leaves the order between those two files undefined.
    errors = tx.check_lock_order(["run_state", "run_state"])
    assert any("more than once" in e for e in errors), errors
    # A lock with no declared position cannot be ordered against the others.
    errors = tx.check_lock_order(["run_state", "mystery"])
    assert any("unknown lock kind" in e for e in errors), errors


# --- the happy path preserves the ordering guarantee -----------------------

def test_revision_invalidates_downstream_before_switching(case, isolated_dao,
                                                          make_args):
    assert _revise(make_args, isolated_dao) == 0
    assert _current_text() == SOURCE_V2
    statuses = _statuses()
    assert statuses["document_processing"] == "passed"   # the writer itself
    assert statuses["policy_clause_processing"] == "failed"
    assert statuses["claim_analysis"] == "failed"
    # The superseded revision is retained, so the UIDs computed from it stay
    # recomputable for audit rather than becoming unverifiable claims.
    import hashlib
    old = hashlib.sha256(SOURCE_V1.encode("utf-8")).hexdigest()
    new = hashlib.sha256(SOURCE_V2.encode("utf-8")).hexdigest()
    assert dao.revision_file_path("CASE_030", "DOC_005", new).exists()
    assert not tx.read_journal(dao.case_dir("CASE_030"))


def test_identical_bytes_are_a_no_op(case, isolated_dao, make_args):
    """Nothing downstream was derived from different text, so nothing is
    invalidated. A cascade that fires on every write trains everyone to
    ignore it."""
    assert _revise(make_args, isolated_dao, text=SOURCE_V1) == 0
    assert _statuses()["policy_clause_processing"] == "passed"


# --- fault injection -------------------------------------------------------

def test_contended_run_state_aborts_the_whole_revision(case, isolated_dao,
                                                       make_args):
    """The defect this part exists to remove. Previously the text was written
    first and the cascade attempted afterwards, so a contended run-state
    produced new text with stale `passed` stages at exit code 0."""
    dao.acquire_lock(dao.run_state_path("CASE_030"), "someone-else",
                     "RUN_OTHER", "holding run-state")
    try:
        assert _revise(make_args, isolated_dao) == 1
    finally:
        dao.release_lock(dao.run_state_path("CASE_030"))
    # Nothing moved: old text, stages still passed.
    assert _current_text() == SOURCE_V1
    assert _statuses()["policy_clause_processing"] == "passed"


def test_a_failed_cascade_never_leaves_new_text_behind(
        case, isolated_dao, make_args, monkeypatch):
    """Inject a cascade failure at the step that runs BEFORE the pointer flip
    and assert the pointer did not move."""
    def boom(*args, **kwargs):
        raise dao.CascadeFailed("injected: run-state would be invalid")
    monkeypatch.setattr(dao, "_invalidate_dependents", boom)

    assert _revise(make_args, isolated_dao) == 1
    assert _current_text() == SOURCE_V1
    assert _statuses()["policy_clause_processing"] == "passed"
    # The journal is cleared on a clean abort -- an abort is not an
    # interrupted transaction, and leaving one behind would block later work
    # for a failure that changed nothing.
    assert not tx.read_journal(dao.case_dir("CASE_030"))


def test_an_invalid_prospective_run_state_aborts_instead_of_warning(
        case, isolated_dao, make_args):
    """A run-state that cannot be made schema-valid used to print WARNING and
    return [], which the caller discarded. It must now abort the revision."""
    state = dao.load_run_state("CASE_030")
    state["stages"].append({"stage_name": "claim_analysis",
                            "status": "not_a_real_status",
                            "attempt_count": 1})
    _write_json(dao.run_state_path("CASE_030"), state)

    assert _revise(make_args, isolated_dao) == 1
    assert _current_text() == SOURCE_V1


def test_strict_cascade_raises_where_the_lenient_one_returned_empty(case):
    dao.acquire_lock(dao.run_state_path("CASE_030"), "someone-else",
                     "RUN_OTHER", "holding run-state")
    try:
        # Lenient: indistinguishable from "nothing to invalidate".
        assert dao._invalidate_dependents(
            "CASE_030", "document_processing", "r", "me", "RUN") == []
        # Strict: the caller is forced to notice.
        with pytest.raises(dao.CascadeFailed):
            dao._invalidate_dependents(
                "CASE_030", "document_processing", "r", "me", "RUN",
                strict=True)
    finally:
        dao.release_lock(dao.run_state_path("CASE_030"))


# --- journal / recovery ----------------------------------------------------

def test_an_interrupted_transaction_blocks_new_work_and_is_not_auto_resumed(
        case, isolated_dao, make_args):
    """Recovery deliberately does not finish someone else's half-done
    multi-file transition. It blocks, so the stage reruns."""
    tx.write_journal(dao.case_dir("CASE_030"), {
        "operation": "revise_source_text", "case_id": "CASE_030",
        "document_id": "DOC_005", "status": "invalidating",
        "started_at": dao.now_iso(),
    })
    assert _revise(make_args, isolated_dao) == 1
    assert _current_text() == SOURCE_V1


def test_a_torn_journal_is_never_read_as_no_transaction(case):
    """The one answer that is certainly wrong is 'nothing was running'."""
    tx.journal_path(dao.case_dir("CASE_030")).write_text(
        "{not valid json", encoding="utf-8")
    assert tx.read_journal(dao.case_dir("CASE_030")) == {"status": "unreadable"}
    assert tx.pending_journal_errors(dao.case_dir("CASE_030"))


def test_a_committed_journal_does_not_block(case):
    tx.write_journal(dao.case_dir("CASE_030"), {
        "operation": "revise_source_text", "status": "committed"})
    assert tx.pending_journal_errors(dao.case_dir("CASE_030")) == []
