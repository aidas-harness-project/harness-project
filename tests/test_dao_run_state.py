"""dao.py's _run_state.json operations: update-run-state,
get-last-passed-stage -- the resume-from-interruption mechanism
(harness-guardrails P7/P10) reads through get_last_passed_stage, not by
re-deriving progress from scattered output files.
"""
import json
import threading
from pathlib import Path

import dao
import medical_review_ledger


def test_read_run_state_validates_schema_and_case_identity(
    isolated_dao, make_args, capsys
):
    case_id = "CASE_009"
    state = dao.load_run_state(case_id)
    state["run_id"] = "RUN_20260712_001"
    dao.save_run_state(case_id, state)

    assert dao.cmd_read_run_state(make_args(case_id=case_id)) == 0
    assert json.loads(capsys.readouterr().out)["case_id"] == case_id

    state["case_id"] = "CASE_010"
    dao.save_run_state(case_id, state)
    assert dao.cmd_read_run_state(make_args(case_id=case_id)) == 1
    assert "different case" in capsys.readouterr().out


def test_run_state_rejects_duplicate_stage_and_tampered_reconciliation_receipt(
    isolated_dao, make_args
):
    case_id = "CASE_009"
    state = dao.load_run_state(case_id)
    state["run_id"] = "RUN_20260712_001"
    stage = {
        "stage_name": "intake", "status": "passed", "attempt_count": 1,
        "started_at": None, "completed_at": dao.now_iso(), "backup_path": None,
    }
    state["stages"] = [stage, dict(stage)]
    dao.save_run_state(case_id, state)
    assert dao.cmd_read_run_state(make_args(case_id=case_id)) == 1

    state["stages"] = [stage]
    state["medical_review_wait_reconciliation_operations"] = [{
        "operation_id": "medical-projection:" + "a" * 64,
        "request_sha256": "b" * 64,
        "medical_review_ledger_sha256": "c" * 64,
        "completed_at": dao.now_iso(),
    }]
    dao.save_run_state(case_id, state)
    assert dao.cmd_read_run_state(make_args(case_id=case_id)) == 1


def test_legacy_evaluation_state_migrates_only_with_unambiguous_version(
    isolated_dao, make_args
):
    case_id = "CASE_009"
    legacy = {
        "case_id": case_id,
        "run_id": "RUN_20260712_001",
        "stages": [{
            "stage_name": "evaluation",
            "status": "pending",
            "attempt_count": 0,
        }],
        "human_input_status": [{
            "stage_name": "evaluation",
            "status": "waiting",
            "description": "expert review of draft_report_v1_reviewed.md",
            "requested_at": dao.now_iso(),
            "received_at": None,
        }],
    }
    dao.atomic_write_json(dao.run_state_path(case_id), legacy)

    migrated = dao.validated_run_state(case_id)
    assert migrated["run_state_version"] == "run_state.v0.3"
    assert migrated["stages"][0]["stage_name"] == "human_review_v1"
    assert migrated["human_input_status"][0]["stage_name"] == "human_review_v1"

    legacy["human_input_status"] = []
    dao.atomic_write_json(dao.run_state_path(case_id), legacy)
    assert dao.cmd_read_run_state(make_args(case_id=case_id)) == 1

def test_run_state_writer_rejects_foreign_case_state(
    isolated_dao, make_args, run_id
):
    state = dao.load_run_state("CASE_009")
    state.update({"case_id": "CASE_010", "run_id": run_id})
    dao.save_run_state("CASE_009", state)

    assert dao.cmd_update_run_state(make_args(
        run_id=run_id, stage="document_processing", status="in_progress",
    )) == 1
    assert dao.load_run_state("CASE_009")["stages"] == []


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


def test_duplicate_in_progress_is_an_idempotent_replay(isolated_dao, make_args, run_id):
    # A retry must first close the prior attempt. Replaying the begin command
    # while it is still active cannot turn one invocation into three attempts.
    args = make_args(run_id=run_id, stage="document_processing", status="in_progress")
    dao.cmd_update_run_state(args)
    dao.cmd_update_run_state(args)
    dao.cmd_update_run_state(args)

    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["attempt_count"] == 1


def test_started_at_does_not_reset_across_retries(isolated_dao, make_args, run_id):
    args = make_args(run_id=run_id, stage="document_processing", status="in_progress")
    dao.cmd_update_run_state(args)
    first_started = dao.load_run_state("CASE_009")["stages"][0]["started_at"]
    dao.cmd_update_run_state(make_args(
        run_id=run_id, stage="document_processing", status="failed",
        attempt_outcome="failed"))
    dao.cmd_update_run_state(args)
    second_started = dao.load_run_state("CASE_009")["stages"][0]["started_at"]

    assert first_started == second_started, "a retry must not look like a fresh start"


def test_incidental_checkpoint_does_not_begin_or_increment_attempt(
        isolated_dao, run_id):
    state = dao._update_run_state(
        "CASE_009", run_id, "document_processing", "in_progress", "writer",
        dep_check="soft", explicit_attempt=False)
    entry = state["stages"][0]
    assert entry["status"] == "in_progress"
    assert entry["attempt_count"] == 0
    assert entry["started_at"] is None

    state = dao._update_run_state(
        "CASE_009", run_id, "document_processing", "in_progress", "writer",
        dep_check="soft", explicit_attempt=False)
    assert state["stages"][0]["attempt_count"] == 0


def _finalize(make_args, run_id, stage):
    """Pass a stage the only legitimate way now -- finalize-stage (snapshot +
    passed, atomic). update-run-state can no longer set 'passed' directly."""
    return dao.cmd_snapshot_backup(make_args(run_id=run_id, stage=stage))


def test_get_last_passed_stage_returns_the_latest_passed_only(isolated_dao, make_args, run_id):
    _finalize(make_args, run_id, "intake")
    _finalize(make_args, run_id, "document_processing")
    _finalize(make_args, run_id, "indexing")
    dao.cmd_update_run_state(make_args(run_id=run_id, stage="policy_clause_processing", status="in_progress"))

    state = dao.load_run_state("CASE_009")
    passed = [s["stage_name"] for s in state["stages"] if s["status"] == "passed"]
    assert passed == ["intake", "document_processing", "indexing"]


def test_get_last_passed_stage_prints_the_actual_stage_name(isolated_dao, make_args, run_id, capsys):
    _finalize(make_args, run_id, "intake")
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
    """run_state.schema.json v0.3: stage_name is an enum of canonical
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


def test_claim_analysis_cannot_pass_or_snapshot_without_medical_clearance(
    isolated_dao, make_args, run_id, monkeypatch
):
    calls: list[tuple[str, str]] = []

    def block_clearance(_dao, case_id, requested_run_id):
        calls.append((case_id, requested_run_id))
        raise ValueError("medical review clearance is blocked")

    monkeypatch.setattr(
        medical_review_ledger, "require_clearance", block_clearance,
        raising=False,
    )
    assert dao.cmd_update_run_state(make_args(
        run_id=run_id,
        stage="claim_analysis",
        status="passed",
    )) == 1
    assert dao.cmd_snapshot_backup(make_args(
        run_id=run_id,
        stage="claim_analysis",
    )) == 1
    state = dao.load_run_state("CASE_009")
    assert not any(
        entry["stage_name"] == "claim_analysis" and entry["status"] == "passed"
        for entry in state["stages"]
    )
    assert calls == [("CASE_009", run_id)]


def test_downstream_start_and_snapshot_recheck_medical_clearance(
    isolated_dao, make_args, run_id, monkeypatch
):
    dao.case_dir("CASE_009").mkdir(parents=True, exist_ok=True)
    dao.medical_variables_path("CASE_009").write_text("{}", encoding="utf-8")
    state = dao.load_run_state("CASE_009")
    state.update({"run_id": run_id, "medical_review_adopted": True})
    dao.save_run_state("CASE_009", state)
    calls = []

    def block_clearance(_dao, case_id, requested_run_id):
        calls.append((case_id, requested_run_id))
        raise ValueError("medical review reopened")

    monkeypatch.setattr(medical_review_ledger, "require_clearance", block_clearance)
    assert dao.cmd_update_run_state(make_args(
        run_id=run_id, stage="consistency_check", status="in_progress",
    )) == 1
    assert dao.cmd_snapshot_backup(make_args(
        run_id=run_id, stage="consistency_check",
    )) == 1
    assert calls == [("CASE_009", run_id), ("CASE_009", run_id)]


def test_post_publication_gate_survives_missing_pointer_and_covers_denial_response(
    isolated_dao, make_args, run_id, monkeypatch
):
    state = dao.load_run_state("CASE_009")
    state.update({"run_id": run_id, "medical_review_adopted": True})
    dao.save_run_state("CASE_009", state)
    calls = []

    def block_clearance(_dao, case_id, requested_run_id):
        calls.append((case_id, requested_run_id))
        raise ValueError("canonical medical pointer is missing")

    monkeypatch.setattr(medical_review_ledger, "require_clearance", block_clearance)
    for stage in ("consistency_check", "denial_response"):
        assert dao.cmd_update_run_state(make_args(
            run_id=run_id, stage=stage, status="in_progress",
        )) == 1
        assert dao.cmd_snapshot_backup(make_args(
            run_id=run_id, stage=stage,
        )) == 1
    assert calls == [("CASE_009", run_id)] * 4


def test_medical_artifacts_reassert_adoption_when_run_state_is_missing_or_false(
    isolated_dao, make_args, run_id, monkeypatch
):
    case_dir = dao.case_dir("CASE_009")
    (case_dir / "_medical_variable_revisions").mkdir(parents=True)
    calls = []

    def block_clearance(_dao, case_id, requested_run_id):
        calls.append((case_id, requested_run_id))
        raise ValueError("medical publication is incomplete")

    monkeypatch.setattr(medical_review_ledger, "require_clearance", block_clearance)
    assert dao.cmd_update_run_state(make_args(
        run_id=run_id, stage="consistency_check", status="in_progress",
    )) == 1

    state = dao.load_run_state("CASE_009")
    state.update({"run_id": run_id, "medical_review_adopted": False})
    dao.save_run_state("CASE_009", state)
    assert dao.cmd_update_run_state(make_args(
        run_id=run_id, stage="denial_response", status="in_progress",
    )) == 1
    assert calls == [("CASE_009", run_id)] * 2


def test_predecessor_reconciliation_receipt_derives_v03_binding(
    isolated_dao
):
    case_id = "CASE_009"
    run_id = "RUN_20260712_001"
    operation = {
        "operation_id": "medical:test-legacy-receipt",
        "medical_review_ledger_sha256": "a" * 64,
        "completed_at": dao.now_iso(),
    }
    operation["request_sha256"] = dao.hashlib.sha256(
        dao._canonical_json_bytes(dao._reconciliation_request(
            case_id, run_id, operation["operation_id"],
            operation["medical_review_ledger_sha256"],
        ))
    ).hexdigest()
    dao.atomic_write_json(dao.run_state_path(case_id), {
        "case_id": case_id,
        "run_id": run_id,
        "stages": [],
        "human_input_status": [],
        "medical_review_wait_reconciliation_operations": [operation],
    })

    migrated = dao.validated_run_state(case_id)
    assert migrated["medical_review_wait_reconciliation_operations"][0][
        "receipt_sha256"
    ] == dao._reconciliation_receipt_sha(operation)


def test_snapshot_contains_final_run_state_and_completion_manifest(
    isolated_dao, make_args, run_id
):
    case_dir = dao.case_dir("CASE_009")
    case_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(case_dir / "artifact.json", {"version": 1})

    assert dao.cmd_snapshot_backup(make_args(
        run_id=run_id,
        stage="document_processing",
        held_by="snapshot-test",
    )) == 0

    live_state = dao.load_run_state("CASE_009")
    backup_path = live_state["stages"][0]["backup_path"]
    backup = dao.Path(backup_path)
    snapshotted_state = json.loads(
        (backup / "_run_state.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (backup / "_snapshot_manifest.json").read_text(encoding="utf-8")
    )
    assert snapshotted_state == live_state
    assert manifest["complete"] is True
    assert manifest["run_id"] == run_id
    assert manifest["stage"] == "document_processing"


def test_snapshot_retries_instead_of_publishing_torn_artifacts(
    isolated_dao, make_args, run_id, monkeypatch
):
    case_dir = dao.case_dir("CASE_009")
    case_dir.mkdir(parents=True, exist_ok=True)
    left = case_dir / "left.json"
    right = case_dir / "right.json"
    left.write_text('{"generation": "A"}', encoding="utf-8")
    right.write_text('{"generation": "A"}', encoding="utf-8")
    original_copy = dao._copy_snapshot_file
    changed = False

    def mutate_between_copies(source, destination):
        nonlocal changed
        result = original_copy(source, destination)
        if dao.Path(source) == left and not changed:
            changed = True
            left.write_text('{"generation": "B"}', encoding="utf-8")
            right.write_text('{"generation": "B"}', encoding="utf-8")
        return result

    monkeypatch.setattr(dao, "_copy_snapshot_file", mutate_between_copies)
    assert dao.cmd_snapshot_backup(make_args(
        run_id=run_id,
        stage="document_processing",
        held_by="snapshot-test",
    )) == 0

    backup = dao.Path(
        dao.load_run_state("CASE_009")["stages"][0]["backup_path"]
    )
    assert json.loads((backup / "left.json").read_text())["generation"] == "B"
    assert json.loads((backup / "right.json").read_text())["generation"] == "B"


def test_repeated_snapshots_never_reuse_a_published_destination(
    isolated_dao, make_args, run_id
):
    case_dir = dao.case_dir("CASE_009")
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "artifact.txt").write_text("synthetic", encoding="utf-8")

    for _ in range(3):
        assert dao.cmd_snapshot_backup(make_args(
            run_id=run_id,
            stage="document_processing",
            held_by="snapshot-test",
        )) == 0

    published = sorted(
        path.name
        for path in (case_dir / "_backups").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    assert published == [
        "step_01_document_processing",
        "step_02_document_processing",
        "step_03_document_processing",
    ]


def test_run_state_writers_cannot_replace_an_established_owner(
    isolated_dao, make_args, run_id, capsys
):
    assert dao.cmd_update_run_state(make_args(
        run_id=run_id,
        stage="document_processing",
        status="in_progress",
        held_by="owner-one",
    )) == 0
    (dao.case_dir("CASE_009") / "medical_variables.json").write_text(
        "{}",
        encoding="utf-8",
    )

    assert dao.cmd_update_run_state(make_args(
        run_id="RUN_20260712_002",
        stage="claim_analysis",
        status="passed",
        held_by="owner-two",
    )) == 1
    assert dao.cmd_set_human_input_status(make_args(
        run_id="RUN_20260712_002",
        stage="claim_analysis",
        status="waiting",
        description="Synthetic foreign wait.",
        held_by="owner-two",
    )) == 1
    assert "canonical run owner" in capsys.readouterr().out
    state = dao.load_run_state("CASE_009")
    assert state["run_id"] == run_id
    assert state["stages"][0]["status"] == "in_progress"
    assert state["human_input_status"] == []


def test_snapshot_rejects_foreign_owner_and_removes_incomplete_staging(
    isolated_dao, make_args, run_id, monkeypatch
):
    assert dao.cmd_update_run_state(make_args(
        run_id=run_id,
        stage="intake",
        status="in_progress",
        held_by="owner-one",
    )) == 0
    foreign = make_args(
        run_id="RUN_20260712_002",
        stage="document_processing",
        held_by="owner-two",
    )
    assert dao.cmd_snapshot_backup(foreign) == 1

    monkeypatch.setattr(
        dao,
        "_copy_snapshot_file",
        lambda *_args: (_ for _ in ()).throw(OSError("synthetic copy failure")),
    )
    (dao.case_dir("CASE_009") / "artifact.txt").write_text(
        "synthetic",
        encoding="utf-8",
    )
    assert dao.cmd_snapshot_backup(make_args(
        run_id=run_id,
        stage="document_processing",
        held_by="owner-one",
    )) == 1
    backups = dao.case_dir("CASE_009") / "_backups"
    assert list(backups.iterdir()) == []
    assert dao.load_run_state("CASE_009")["stages"][0]["backup_path"] is None


def test_concurrent_snapshots_publish_distinct_complete_destinations(
    isolated_dao, make_args, run_id, monkeypatch
):
    case_dir = dao.case_dir("CASE_009")
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "artifact.txt").write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 2)
    start = threading.Barrier(2)
    results = []

    def snapshot(held_by):
        start.wait(timeout=2)
        results.append(dao.cmd_snapshot_backup(make_args(
            run_id=run_id,
            stage="document_processing",
            held_by=held_by,
        )))

    threads = [
        threading.Thread(target=snapshot, args=(f"snapshot-{index}",))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sorted(results) == [0, 0]
    destinations = sorted(
        path for path in (case_dir / "_backups").iterdir() if path.is_dir()
    )
    assert [path.name for path in destinations] == [
        "step_01_document_processing",
        "step_02_document_processing",
    ]
    assert all((path / "_snapshot_manifest.json").is_file() for path in destinations)


def test_snapshot_promotion_never_replaces_a_destination_that_appears(
    isolated_dao, make_args, run_id, monkeypatch
):
    case_dir = dao.case_dir("CASE_009")
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "artifact.txt").write_text("synthetic", encoding="utf-8")
    original_promote = dao._rename_snapshot_directory_noreplace
    foreign_inode = None

    def insert_foreign_destination(source, destination):
        nonlocal foreign_inode
        destination.mkdir()
        foreign_inode = destination.stat().st_ino
        original_promote(source, destination)

    monkeypatch.setattr(
        dao,
        "_rename_snapshot_directory_noreplace",
        insert_foreign_destination,
    )
    assert dao.cmd_snapshot_backup(make_args(
        run_id=run_id,
        stage="document_processing",
        held_by="snapshot-test",
    )) == 1
    destination = case_dir / "_backups" / "step_01_document_processing"
    assert foreign_inode is not None
    assert destination.stat().st_ino == foreign_inode
    assert not (destination / "_snapshot_manifest.json").exists()
    assert dao.load_run_state("CASE_009")["stages"] == []


def test_snapshot_failure_never_deletes_a_foreign_replacement(
    isolated_dao, make_args, run_id, monkeypatch
):
    case_dir = dao.case_dir("CASE_009")
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "artifact.txt").write_text("synthetic", encoding="utf-8")
    state_target = dao.run_state_path("CASE_009")
    original_atomic = dao.atomic_write_json
    survivor = case_dir / "owned-snapshot-survivor"
    foreign_marker = None

    def fail_after_replacement(path, data):
        nonlocal foreign_marker
        path = dao.Path(path)
        if path == state_target:
            destination = case_dir / "_backups" / "step_01_document_processing"
            destination.rename(survivor)
            destination.mkdir()
            foreign_marker = destination / "foreign.txt"
            foreign_marker.write_text("foreign replacement", encoding="utf-8")
            raise OSError("synthetic run-state failure after replacement")
        return original_atomic(path, data)

    monkeypatch.setattr(dao, "atomic_write_json", fail_after_replacement)
    assert dao.cmd_snapshot_backup(make_args(
        run_id=run_id,
        stage="document_processing",
        held_by="snapshot-test",
    )) == 1
    assert foreign_marker is not None
    assert foreign_marker.read_text(encoding="utf-8") == "foreign replacement"
    assert (survivor / "_snapshot_manifest.json").is_file()
    assert dao.load_run_state("CASE_009")["stages"] == []


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
    def boom(source, destination):
        raise OSError("disk full")
    monkeypatch.setattr(dao, "_copy_snapshot_file", boom)
    case_dir = isolated_dao / "outputs" / "CASE_009"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "artifact.json").write_text("{}", encoding="utf-8")

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
    # Force a failure during the snapshot copy. Seed an output file so the copy
    # loop actually runs.
    (isolated_dao / "outputs" / "CASE_009").mkdir(parents=True, exist_ok=True)
    (isolated_dao / "outputs" / "CASE_009" / "some_output.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        dao,
        "_copy_snapshot_file",
        lambda *a, **k: (_ for _ in ()).throw(OSError("copy interrupted")),
    )

    rc = dao.cmd_snapshot_backup(make_args(run_id=run_id, stage="intake"))

    assert rc == 1
    backups = isolated_dao / "outputs" / "CASE_009" / "_backups"
    # No published (real) backup dir; only possibly an orphan .tmp_snapshot_*.
    if backups.exists():
        published = [p for p in backups.iterdir() if not p.name.startswith(".tmp_snapshot_")]
        assert published == [], "a failed snapshot must not publish a partial backup"
