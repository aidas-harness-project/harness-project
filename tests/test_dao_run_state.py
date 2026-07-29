"""dao.py's _run_state.json operations: update-run-state,
get-last-passed-stage -- the resume-from-interruption mechanism
(harness-guardrails P7/P10) reads through get_last_passed_stage, not by
re-deriving progress from scattered output files.
"""
import json
from pathlib import Path

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
    # document_processing has no hard prereq when no intake entry exists, so it
    # can go in_progress on a fresh case (mirrors run_checkpoint1's real use).
    args = make_args(run_id=run_id, stage="document_processing", status="in_progress")
    dao.cmd_update_run_state(args)
    dao.cmd_update_run_state(args)  # simulates a P9 retry
    dao.cmd_update_run_state(args)

    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["attempt_count"] == 3


def test_started_at_does_not_reset_across_retries(isolated_dao, make_args, run_id):
    args = make_args(run_id=run_id, stage="document_processing", status="in_progress")
    dao.cmd_update_run_state(args)
    first_started = dao.load_run_state("CASE_009")["stages"][0]["started_at"]
    dao.cmd_update_run_state(args)
    second_started = dao.load_run_state("CASE_009")["stages"][0]["started_at"]

    assert first_started == second_started, "a retry must not look like a fresh start"


def _finalize(make_args, run_id, stage):
    """Pass a stage the only legitimate way now -- finalize-stage (snapshot +
    passed, atomic). update-run-state can no longer set 'passed' directly."""
    return dao.cmd_snapshot_backup(make_args(run_id=run_id, stage=stage))


def test_get_last_passed_stage_returns_the_latest_passed_only(isolated_dao, make_args, run_id):
    _finalize(make_args, run_id, "intake")
    _finalize(make_args, run_id, "document_segmentation")
    _finalize(make_args, run_id, "document_processing")
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="policy_clause_processing", status="in_progress"))

    state = dao.load_run_state("CASE_009")
    passed = [s["stage_name"] for s in state["stages"] if s["status"] == "passed"]
    assert passed == ["intake", "document_segmentation", "document_processing"]


def test_get_last_passed_stage_prints_the_actual_stage_name(isolated_dao, make_args, run_id, capsys):
    _finalize(make_args, run_id, "intake")
    _finalize(make_args, run_id, "document_segmentation")
    _finalize(make_args, run_id, "document_processing")
    capsys.readouterr()  # discard the "OK: ..." lines above

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
    # in_progress (not passed) so this exercises the stage_name enum check
    # rather than the earlier direct-passed refusal.
    result = dao._update_run_state("CASE_009", run_id, "document-pipeline", "in_progress", "tester")

    assert result is None, "schema failure returns None, same sentinel as a lock failure"
    state = dao.load_run_state("CASE_009")
    assert state["stages"] == [], "the drifted stage name must not have been written"


# --------------------------------------------------------------------------
# Part 1: stage dependency graph + atomic passed-with-snapshot finalization
# --------------------------------------------------------------------------

def _passed_stage(make_args, run_id, stage):
    return dao.cmd_snapshot_backup(make_args(run_id=run_id, stage=stage))


def test_policy_start_refused_while_document_processing_in_progress(isolated_dao, make_args, run_id):
    """The CASE_030 shape: document_processing is in_progress (and there is no
    indexing entry), yet policy_clause_processing is asked to start. It must be
    refused -- policy_clause_processing requires document_processing=passed."""
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="document_processing", status="in_progress"))

    rc = dao.cmd_update_run_state(make_args(run_id=run_id, stage="policy_clause_processing", status="in_progress"))

    assert rc == 1, "policy_clause_processing must not start while document_processing is in_progress"
    state = dao.load_run_state("CASE_009")
    names = [s["stage_name"] for s in state["stages"]]
    assert "policy_clause_processing" not in names, "the refused stage must not have been recorded at all"


def test_policy_start_allowed_after_document_processing_passed(isolated_dao, make_args, run_id):
    """Once document_processing is finalized (passed + snapshot), the dependent
    policy_clause_processing may start."""
    _passed_stage(make_args, run_id, "document_processing")

    rc = dao.cmd_update_run_state(make_args(run_id=run_id, stage="policy_clause_processing", status="in_progress"))

    assert rc == 0
    state = dao.load_run_state("CASE_009")
    entry = next(s for s in state["stages"] if s["stage_name"] == "policy_clause_processing")
    assert entry["status"] == "in_progress"


def test_optional_indexing_may_be_skipped_but_a_normal_stage_may_not(isolated_dao, make_args, run_id):
    """A run may record the optional indexing adapter as 'skipped'. A
    non-skippable stage (policy_clause_processing) may NOT be recorded skipped
    -- 'skipped' is reserved for genuinely optional stages, so it can't be used
    to fake past a stage that actually has to run."""
    _passed_stage(make_args, run_id, "document_processing")

    rc_ok = dao.cmd_update_run_state(make_args(run_id=run_id, stage="indexing", status="skipped"))
    assert rc_ok == 0
    state = dao.load_run_state("CASE_009")
    assert next(s for s in state["stages"] if s["stage_name"] == "indexing")["status"] == "skipped"

    rc_bad = dao.cmd_update_run_state(make_args(run_id=run_id, stage="policy_clause_processing", status="skipped"))
    assert rc_bad == 1, "a non-skippable stage must not be recordable as skipped"


def test_missing_optional_stage_does_not_auto_satisfy_a_dependency(isolated_dao, make_args, run_id):
    """An optional stage that was never recorded is NOT silently treated as
    satisfied: claim_analysis requires policy_clause_processing=passed, and a
    missing policy_clause_processing entry blocks it just like an in_progress
    one would. (Guards against 'optional therefore fine'.)"""
    _passed_stage(make_args, run_id, "document_processing")
    # policy_clause_processing never recorded at all.

    rc = dao.cmd_update_run_state(make_args(run_id=run_id, stage="claim_analysis", status="in_progress"))

    assert rc == 1, "claim_analysis must block while policy_clause_processing is absent"


def test_update_run_state_cannot_set_passed_directly(isolated_dao, run_id):
    """A stage may never be marked passed via update-run-state -- passing is
    atomic with the P10 snapshot (finalize-stage). Called at the module level
    to bypass the CLI's argparse choices restriction, the way a future bug
    could."""
    result = dao._update_run_state("CASE_009", run_id, "intake", "passed", "tester")

    assert result is None, "direct passed must be refused"
    state = dao.load_run_state("CASE_009")
    assert state["stages"] == [], "nothing should have been written"


def test_passed_with_null_backup_path_is_schema_rejected(isolated_dao, run_id):
    """Belt-and-braces at the schema layer: even if a caller reached
    _update_run_state with finalize=True and no backup_path, the v0.3 schema
    (passed => backup_path required, non-null) rejects it and nothing persists."""
    result = dao._update_run_state("CASE_009", run_id, "intake", "passed", "tester",
                                   finalize=True, backup_path=None)

    assert result is None
    state = dao.load_run_state("CASE_009")
    assert state["stages"] == [], "a passed stage with no backup_path must not persist"


def test_finalize_records_passed_and_backup_path_together(isolated_dao, make_args, run_id):
    """finalize-stage passes a stage AND records its snapshot path in one shot;
    a passed stage always carries a real backup_path."""
    rc = dao.cmd_snapshot_backup(make_args(run_id=run_id, stage="intake"))

    assert rc == 0
    state = dao.load_run_state("CASE_009")
    entry = next(s for s in state["stages"] if s["stage_name"] == "intake")
    assert entry["status"] == "passed"
    assert entry["backup_path"], "a finalized stage must have a non-null backup_path"
    assert entry["completed_at"] is not None
    assert (isolated_dao / "outputs" / "CASE_009" / "_backups").exists()


def test_finalize_refused_when_dependency_unmet_leaves_stage_unpassed(isolated_dao, make_args, run_id):
    """Finalizing a stage whose upstream is not passed is refused; the stage is
    not marked passed and (since the dependency pre-check runs first) no
    snapshot is published for it."""
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="document_processing", status="in_progress"))

    rc = dao.cmd_snapshot_backup(make_args(run_id=run_id, stage="policy_clause_processing"))

    assert rc == 1
    state = dao.load_run_state("CASE_009")
    names = [s["stage_name"] for s in state["stages"] if s["status"] == "passed"]
    assert "policy_clause_processing" not in names


def test_finalize_refused_when_snapshot_fails_does_not_pass_stage(isolated_dao, make_args, run_id, monkeypatch):
    """If snapshot construction raises, the stage must NOT be recorded passed
    (P10: never half-finalize). Simulated by making the snapshot builder throw."""
    def boom(case_id, stage, prospective_state):
        raise OSError("disk full")
    monkeypatch.setattr(dao, "_build_snapshot_atomic", boom)

    rc = dao.cmd_snapshot_backup(make_args(run_id=run_id, stage="intake"))

    assert rc == 1
    state = dao.load_run_state("CASE_009")
    passed = [s for s in state["stages"] if s["status"] == "passed"]
    assert passed == [], "a snapshot failure must leave the stage un-passed"


def test_legacy_v02_invalid_pass_is_migrated_with_audit_history(
        isolated_dao, make_args, run_id):
    """CASE_030's legacy shape has a DAO-owned repair path. It never invents
    a backup: the unverified pass is downgraded and an audit entry is kept."""
    state = dao.load_run_state("CASE_009")
    state["run_id"] = run_id
    state["stages"] = [
        {
            "stage_name": "document_processing", "status": "in_progress",
            "started_at": dao.now_iso(), "completed_at": None,
            "attempt_count": 3, "backup_path": None,
        },
        {
            "stage_name": "policy_clause_processing", "status": "passed",
            "started_at": None, "completed_at": dao.now_iso(),
            "attempt_count": 0, "backup_path": None,
        },
    ]
    dao.atomic_write_json(dao.run_state_path("CASE_009"), state)

    rc = dao.cmd_migrate_run_state_v03(
        make_args(run_id=run_id, held_by="migration-test"))

    assert rc == 0
    migrated = dao.load_run_state("CASE_009")
    policy = next(
        s for s in migrated["stages"]
        if s["stage_name"] == "policy_clause_processing")
    assert policy["status"] == "failed"
    assert policy["backup_path"] is None
    history = migrated["migration_history"][-1]
    assert history["migration_id"] == "run_state_v02_to_v03"
    assert "missing P10 backup_path" in history["changes"][0]
    assert dao._schema_check(migrated, "run_state.schema.json") == []


def test_finalize_snapshot_contains_finalized_run_state(
        isolated_dao, make_args, run_id):
    rc = dao.cmd_snapshot_backup(
        make_args(run_id=run_id, stage="intake"))
    assert rc == 0
    live = dao.load_run_state("CASE_009")
    entry = next(s for s in live["stages"] if s["stage_name"] == "intake")
    snap_state = json.loads(
        (Path(entry["backup_path"]) / "_run_state.json").read_text(
            encoding="utf-8"))
    snap_entry = next(
        s for s in snap_state["stages"] if s["stage_name"] == "intake")
    assert snap_entry["status"] == "passed"
    assert snap_entry["backup_path"] == entry["backup_path"]


def test_finalize_never_overwrites_existing_backup(
        isolated_dao, make_args, run_id):
    backups = isolated_dao / "outputs" / "CASE_009" / "_backups"
    existing = backups / "step_01_intake"
    existing.mkdir(parents=True)
    sentinel = existing / "sentinel.txt"
    sentinel.write_text("immutable", encoding="utf-8")

    rc = dao.cmd_snapshot_backup(
        make_args(run_id=run_id, stage="intake"))

    assert rc == 0
    assert sentinel.read_text(encoding="utf-8") == "immutable"
    live = dao.load_run_state("CASE_009")
    entry = next(s for s in live["stages"] if s["stage_name"] == "intake")
    assert Path(entry["backup_path"]).name == "step_02_intake"


def test_snapshot_failure_leaves_no_partial_backup(isolated_dao, make_args, run_id, monkeypatch):
    """A crash partway through copying must not leave a partial real backup dir
    behind -- the copy happens in a temp dir, published atomically only on full
    success. Simulate a failure after the temp dir is made but before publish."""
    real_copytree = dao.shutil.copytree

    def fail_mid_copy(*a, **k):
        raise OSError("copy interrupted")

    # Force a failure during the snapshot copy. Seed an output file so the copy
    # loop actually runs.
    (isolated_dao / "outputs" / "CASE_009").mkdir(parents=True, exist_ok=True)
    (isolated_dao / "outputs" / "CASE_009" / "some_output.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dao.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("copy interrupted")))

    rc = dao.cmd_snapshot_backup(make_args(run_id=run_id, stage="intake"))

    assert rc == 1
    backups = isolated_dao / "outputs" / "CASE_009" / "_backups"
    # No published (real) backup dir; only possibly an orphan .tmp_snapshot_*.
    if backups.exists():
        published = [p for p in backups.iterdir() if not p.name.startswith(".tmp_snapshot_")]
        assert published == [], "a failed snapshot must not publish a partial backup"
