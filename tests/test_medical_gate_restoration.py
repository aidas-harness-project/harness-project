"""The medical-appropriateness clearance gate, and the boundary it applies on.

Merge 3569d50 discarded parent2's `dao.py` wholesale (known-gaps item 48),
which kept `tools/medical_repository.py` and `tools/medical_review_ledger.py`
on disk while removing every DAO entry point they are reached through. The
adjudication logic was never lost; its callers were. The visible effect was
nothing at all: 53 cases on disk, zero medical artifacts, 9 of them with
`claim_analysis` recorded `passed` through a gate that was not running.

`medical_review_adopted` is the tell. Before this restoration it was written
by three call sites and read by none -- the same "flag nobody reads" shape as
the `raw_page_text` classification flag (2026-08-05). A gate that is defined
but never consulted fails silently and looks healthy, so the tests here drive
`_update_run_state` and `cmd_finalize_stage` themselves rather than calling
`_medical_clearance_required` directly. Reintroducing the defect (deleting the
call in `_update_run_state` while leaving the function defined) must fail these.

The gate is scoped FORWARD by user decision (2026-08-14): pre-existing runs are
stamped `never_evaluated` and left alone, because retro-failing completed runs
whose evaluation results are already recorded would rewrite history rather than
fix it.
"""
import json

import pytest

import dao
import stage_dependencies


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


@pytest.fixture
def no_stage_dependencies(monkeypatch):
    """Neutralize the dependency layer so ONLY the medical gate can refuse.

    Without this a claim_analysis transition is refused for an unmet
    prerequisite first, and the test would pass whether or not the medical
    gate exists -- proving nothing.
    """
    monkeypatch.setattr(
        stage_dependencies, "check_dependencies", lambda *a, **k: [])


def _seed_run_state(case_id, *, legacy=False, adopted=False):
    """A run state on disk. `legacy=True` omits medical_gate_status, which is
    exactly how every pre-restoration file on disk looks."""
    state = {
        "run_state_version": "run_state.v0.3",
        "case_id": case_id,
        "run_id": "RUN_20260814_001",
        "created_at": "2026-08-14T00:00:00+09:00",
        "updated_at": "2026-08-14T00:00:00+09:00",
        "stages": [],
        "human_input_status": [],
        "medical_review_adopted": adopted,
    }
    if not legacy:
        state["medical_gate_status"] = "enforced"
    path = dao.run_state_path(case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


# --- the boundary --------------------------------------------------------

def test_new_run_state_is_born_enforced(isolated_dao):
    state = dao.load_run_state("CASE_9001")
    assert state["medical_gate_status"] == "enforced"
    assert dao._medical_gate_applies(state) is True


def test_pre_existing_run_state_is_stamped_never_evaluated(isolated_dao):
    _seed_run_state("CASE_0900", legacy=True)
    state = dao.load_run_state("CASE_0900")
    # Stamped so the record says the question was never asked -- otherwise
    # `medical_review_adopted: false` reads as "asked and not required".
    assert state["medical_gate_status"] == "never_evaluated"
    assert dao._medical_gate_applies(state) is False


def test_never_evaluated_is_not_silently_upgraded(isolated_dao):
    """A stamped legacy run must stay ungated across reloads, or the forward
    scoping decays into retroactive enforcement one save at a time."""
    _seed_run_state("CASE_0900", legacy=True)
    state = dao.load_run_state("CASE_0900")
    dao.save_run_state("CASE_0900", state)
    assert dao.load_run_state("CASE_0900")["medical_gate_status"] == \
        "never_evaluated"


# --- what the gate covers ------------------------------------------------

def test_claim_analysis_is_gated_even_when_not_adopted(isolated_dao):
    """The medical variables are an INPUT to claim_analysis, so "no medical
    review yet" is the condition the gate exists to catch -- not an exemption.
    This is the case that covers all 9 already-passed cases."""
    state = _seed_run_state("CASE_9001", adopted=False)
    assert dao._medical_clearance_required(state, "claim_analysis", "passed")


def test_post_medical_stages_gate_only_once_adopted(isolated_dao):
    not_adopted = _seed_run_state("CASE_9001", adopted=False)
    adopted = _seed_run_state("CASE_9002", adopted=True)
    assert not dao._medical_clearance_required(
        not_adopted, "screening_report", "in_progress")
    assert dao._medical_clearance_required(
        adopted, "screening_report", "in_progress")


def test_unrelated_stage_is_never_gated(isolated_dao):
    state = _seed_run_state("CASE_9001", adopted=True)
    assert not dao._medical_clearance_required(
        state, "document_processing", "passed")


# --- the wiring (this is the part that was missing) ----------------------

def test_update_run_state_refuses_a_gated_transition(
    isolated_dao, no_stage_dependencies, capsys
):
    """Drives the real transition path. Deleting the
    _require_transition_medical_clearance call from _update_run_state while
    leaving the function defined must fail this test -- that is precisely the
    state the repository was in for 53 cases."""
    _seed_run_state("CASE_9001")
    result = dao._update_run_state(
        "CASE_9001", "RUN_20260814_001", "claim_analysis", "passed",
        "tester", backup_path="snapshot", finalize=True)
    assert result is None
    assert "REFUSED" in capsys.readouterr().out


def test_update_run_state_allows_a_pre_existing_run(
    isolated_dao, no_stage_dependencies
):
    _seed_run_state("CASE_0900", legacy=True)
    dao.load_run_state("CASE_0900")  # stamp it
    result = dao._update_run_state(
        "CASE_0900", "RUN_20260814_001", "claim_analysis", "passed",
        "tester", backup_path="snapshot", finalize=True)
    assert result is not None


def test_finalize_stage_command_itself_enforces_the_gate(
    isolated_dao, no_stage_dependencies, monkeypatch, capsys
):
    """cmd_finalize_stage, not the helper underneath it.

    Every other test here would still pass if the gate were computed and then
    never consulted by the command an orchestrator actually calls.

    The two monkeypatches matter. cmd_finalize_stage refuses a bare case for
    unrelated reasons first (an unverified policy layer, a missing manifest),
    so asserting only `== 1` passes whether or not the medical gate runs --
    verified: with the gate call deleted this test still returned 1. The
    assertion is therefore on the medical REASON, and the earlier blockers are
    cleared so that reason is reachable at all.
    """
    monkeypatch.setattr(dao, "_policy_layer_scheme_blockers", lambda *a, **k: [])
    monkeypatch.setattr(dao, "_policy_completion_blockers", lambda *a, **k: [])
    _seed_run_state("CASE_9001")
    args = type("A", (), {
        "case_id": "CASE_9001", "stage": "claim_analysis",
        "run_id": "RUN_20260814_001", "held_by": "tester",
        "backup_path": "snapshot", "status": "passed",
        "dep_check": "hard", "note": None,
    })()
    assert dao.cmd_finalize_stage(args) == 1
    out = capsys.readouterr().out
    assert "medical review ledger" in out, (
        f"refused, but not by the medical gate -- got: {out!r}")


# --- the restored DAO surface -------------------------------------------

@pytest.mark.parametrize("name", [
    "cmd_write_medical_variables",
    "cmd_read_medical_variables",
    "cmd_check_medical_reviews_clear",
    "cmd_transition_medical_review",
    "cmd_open_medical_review_item",
    "cmd_record_medical_referral_decision",
    "cmd_provide_medical_review_information",
    "cmd_reconcile_medical_review_waits",
    "cmd_read_medical_review_ledger",
    "cmd_read_medical_review_outcomes",
    "medical_variables_path",
    "medical_review_ledger_path",
    "_load_medical_revision",
    "acquire_owned_lock",
    "release_owned_lock",
    "atomic_create_bytes_in_directory",
    "ensure_conflict_ledger",
])
def test_restored_dao_symbol_exists(name):
    """The surviving medical modules call these back into the DAO. Each one
    missing turns a whole module into dead code, which is what happened."""
    assert hasattr(dao, name)


def test_medical_cli_subcommands_are_registered():
    """Present in argparse, not merely defined -- a command with no parser
    entry is unreachable from the orchestrator."""
    parser = dao.build_parser()
    registered = set()
    for action in parser._actions:
        if hasattr(action, "choices") and action.choices:
            registered.update(action.choices)
    for name in (
        "write-medical-variables", "read-medical-variables",
        "check-medical-reviews-clear", "transition-medical-review",
        "open-medical-review-item", "record-medical-referral-decision",
        "provide-medical-review-information",
        "reconcile-medical-review-waits", "read-medical-review-ledger",
        "read-medical-review-outcomes",
    ):
        assert name in registered, f"{name} is not reachable from the CLI"


# --- the ownership guards -----------------------------------------------

@pytest.mark.parametrize("filename", [
    "_medical_review_ledger.json",
    "_medical_variable_revisions/" + "a" * 64 + ".json",
])
def test_in_process_contract_read_refuses_medical_state(isolated_dao, filename):
    """read_contract_data is the in-process path pipeline tools import. A
    guard on the CLI alone would be the "guard on one path, nothing on the
    other" shape this repo has closed three times already."""
    with pytest.raises(ValueError, match="purpose-built medical DAO command"):
        dao.read_contract_data("CASE_9001", filename)


def test_traversal_still_exits_hard_not_softly(isolated_dao):
    """The medical guard must not downgrade a path-safety violation into an
    ordinary `return 1`. Caught as a real regression while building this."""
    args = type("A", (), {
        "case_id": "CASE_9001", "filename": "../../secret.txt"})()
    with pytest.raises(SystemExit):
        dao.cmd_read_contract(args)
