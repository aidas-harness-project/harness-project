"""dao.py's _run_state.json operations: update-run-state,
get-last-passed-stage -- the resume-from-interruption mechanism
(harness-guardrails P7/P10) reads through get_last_passed_stage, not by
re-deriving progress from scattered output files.
"""
import dao


def test_get_last_passed_stage_none_when_no_run_yet(isolated_dao, make_args):
    rc = dao.cmd_get_last_passed_stage(make_args())
    assert rc == 0  # prints NONE, but that's not a failure -- a fresh case simply hasn't run yet


def test_update_run_state_creates_new_stage_entry(isolated_dao, make_args, run_id):
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="document_processing", status="in_progress"))

    state = dao.load_run_state("CASE_009")
    entry = state["stages"][0]
    assert entry["stage_name"] == "document_processing"
    assert entry["status"] == "in_progress"
    assert entry["started_at"] is not None
    assert entry["attempt_count"] == 1


def test_attempt_count_increments_on_each_in_progress(isolated_dao, make_args, run_id):
    args = make_args(run_id=run_id, stage="claim_analysis", status="in_progress")
    dao.cmd_update_run_state(args)
    dao.cmd_update_run_state(args)  # simulates a P9 retry
    dao.cmd_update_run_state(args)

    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["attempt_count"] == 3


def test_started_at_does_not_reset_across_retries(isolated_dao, make_args, run_id):
    args = make_args(run_id=run_id, stage="claim_analysis", status="in_progress")
    dao.cmd_update_run_state(args)
    first_started = dao.load_run_state("CASE_009")["stages"][0]["started_at"]
    dao.cmd_update_run_state(args)
    second_started = dao.load_run_state("CASE_009")["stages"][0]["started_at"]

    assert first_started == second_started, "a retry must not look like a fresh start"


def test_get_last_passed_stage_returns_the_latest_passed_only(isolated_dao, make_args, run_id):
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="intake", status="passed"))
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="document_processing", status="passed"))
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="policy_clause_processing", status="in_progress"))

    state = dao.load_run_state("CASE_009")
    passed = [s["stage_name"] for s in state["stages"] if s["status"] == "passed"]
    assert passed == ["intake", "document_processing"]


def test_get_last_passed_stage_prints_the_actual_stage_name(isolated_dao, make_args, run_id, capsys):
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="intake", status="passed"))
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="document_processing", status="passed"))
    capsys.readouterr()  # discard the two "OK: ..." lines above

    dao.cmd_get_last_passed_stage(make_args())

    assert capsys.readouterr().out.strip() == "document_processing"


def test_failed_stage_does_not_count_as_passed(isolated_dao, make_args, run_id):
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="document_processing", status="failed"))

    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["status"] == "failed"
    assert state["stages"][0]["completed_at"] is not None
    passed = [s["stage_name"] for s in state["stages"] if s["status"] == "passed"]
    assert passed == []


def test_schema_invalid_state_is_rejected_and_not_written(isolated_dao, run_id):
    """Regression: _update_run_state used to build+save whatever it was
    given with zero schema enforcement (found via a real fork_case.py
    smoke test -- validate_output.py had been silently unable to check
    _run_state.json at all due to a separate schema_name_for() bug).
    Calling the module function directly bypasses cmd_update_run_state's
    argparse choices= restriction, the way a bug in this file's own future
    edits could."""
    result = dao._update_run_state("CASE_009", run_id, "some-stage", "not_a_real_status", "tester")

    assert result is None, "schema failure returns None, same sentinel as a lock failure"
    state = dao.load_run_state("CASE_009")
    assert state["stages"] == [], "nothing should have been written"


def test_non_canonical_stage_name_is_rejected_and_not_written(isolated_dao, run_id):
    """run_state.schema.json v0.2: stage_name is an enum of canonical
    names. CASE_021's first full end-to-end run showed free-form names
    silently forking one stage into several entries (document-pipeline vs
    document_processing, critic vs critic_v1) whenever a tool and an agent
    picked different spellings -- breaking get_last_passed_stage's resume
    logic. A valid status with a drifted stage name must now be rejected
    outright, not persisted as a fresh parallel stage."""
    result = dao._update_run_state("CASE_009", run_id, "document-pipeline", "passed", "tester")

    assert result is None, "schema failure returns None, same sentinel as a lock failure"
    state = dao.load_run_state("CASE_009")
    assert state["stages"] == [], "the drifted stage name must not have been written"
