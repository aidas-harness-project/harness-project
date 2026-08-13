"""Bounded downstream medical-review outcome access for QG-MED-1."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import dao
import medical_review_ledger
from test_dao_medical_variables import (
    FIXTURE,
    _enabled_config,
    _enabled_projection_config,
    _write_dependencies,
)


def _publish_no_issue(
    tmp_path: Path, monkeypatch, make_args, capsys
) -> tuple[str, str]:
    case_id = "CASE_9001"
    case_dir = dao.case_dir(case_id)
    _write_dependencies(case_dir)
    monkeypatch.setattr(
        dao,
        "MEDICAL_STRUCTURING_CONFIG",
        _enabled_config(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )
    candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))
    candidate["medical_issues"] = []
    candidate["importance_assignments"] = []
    candidate_path = tmp_path / "outcome-no-issue-medical-variables.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    assert dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(candidate_path),
        held_by="claim-analysis",
        run_id=candidate["run_id"],
    )) == 0, capsys.readouterr().out
    return case_id, candidate["run_id"]


def _run_state(case_id: str, run_id: str, status: str) -> dict:
    timestamp = dao.now_iso()
    return {
        "case_id": case_id,
        "run_id": run_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "stages": [{
            "stage_name": "screening_report",
            "status": status,
            "started_at": timestamp,
            "completed_at": None,
            "attempt_count": 1,
            "backup_path": None,
        }],
        "human_input_status": [],
    }


def test_only_matching_in_progress_downstream_stage_reads_no_issue_outcome(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id, run_id = _publish_no_issue(
        tmp_path, monkeypatch, make_args, capsys
    )
    dao.save_run_state(case_id, _run_state(case_id, run_id, "in_progress"))

    assert dao.cmd_read_medical_review_outcomes(make_args(
        case_id=case_id,
        caller_stage="claim_analysis",
        run_id=run_id,
    )) == 1
    assert dao.cmd_read_medical_review_outcomes(make_args(
        case_id=case_id,
        caller_stage="screening_report",
        run_id="RUN_19990101_999",
    )) == 1

    dao.save_run_state(case_id, _run_state(case_id, run_id, "passed"))
    assert dao.cmd_read_medical_review_outcomes(make_args(
        case_id=case_id,
        caller_stage="screening_report",
        run_id=run_id,
    )) == 1

    dao.save_run_state(case_id, _run_state(case_id, run_id, "in_progress"))
    capsys.readouterr()
    assert dao.cmd_read_medical_review_outcomes(make_args(
        case_id=case_id,
        caller_stage="screening_report",
        run_id=run_id,
    )) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["schema_version"] == "medical_review_outcomes.v0.1"
    assert payload["case_id"] == case_id
    assert payload["medical_variables_revision"]["run_id"] == run_id
    assert payload["review_items"] == []


def test_outcome_read_rejects_run_state_for_a_different_case(
    isolated_dao, monkeypatch, make_args, run_id
):
    state = _run_state("CASE_9999", run_id, "in_progress")
    dao.save_run_state("CASE_9001", state)
    monkeypatch.setattr(
        medical_review_ledger, "cmd_read_outcomes", lambda _dao, _args: 0
    )

    assert dao.cmd_read_medical_review_outcomes(make_args(
        case_id="CASE_9001",
        caller_stage="screening_report",
        run_id=run_id,
    )) == 1


def test_outcome_projection_serializes_against_canonical_publication(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id, run_id = _publish_no_issue(tmp_path, monkeypatch, make_args, capsys)
    dao.save_run_state(case_id, _run_state(case_id, run_id, "in_progress"))
    old_sha = hashlib.sha256(dao.medical_variables_path(case_id).read_bytes()).hexdigest()
    candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))
    candidate["medical_issues"] = []
    candidate["importance_assignments"] = []
    candidate["variables"][0]["label"] += " revised"
    candidate_path = tmp_path / "concurrent-medical-variables.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 5)

    projection_entered = threading.Event()
    release_projection = threading.Event()
    original_load_ledger = medical_review_ledger.load_ledger

    def blocked_projection_load(*args, **kwargs):
        ledger = original_load_ledger(*args, **kwargs)
        if threading.current_thread().name == "outcome-projection":
            projection_entered.set()
            assert release_projection.wait(timeout=5)
        return ledger

    monkeypatch.setattr(medical_review_ledger, "load_ledger", blocked_projection_load)
    result: dict = {}

    def project():
        result["outcomes"] = medical_review_ledger.build_downstream_review_outcomes(
            dao, case_id,
        )

    def publish():
        result["publication_status"] = dao.cmd_write_medical_variables(make_args(
            case_id=case_id,
            data_file=str(candidate_path),
            held_by="claim-analysis",
            run_id=run_id,
        ))

    projection_thread = threading.Thread(target=project, name="outcome-projection")
    projection_thread.start()
    assert projection_entered.wait(timeout=5)
    publication_thread = threading.Thread(target=publish, name="medical-publication")
    publication_thread.start()
    time.sleep(0.05)
    assert publication_thread.is_alive(), "publication bypassed the outcome snapshot lock"
    release_projection.set()
    projection_thread.join(timeout=5)
    publication_thread.join(timeout=5)
    assert not projection_thread.is_alive()
    assert not publication_thread.is_alive()
    assert result["publication_status"] == 0
    assert result["outcomes"]["medical_variables_revision"]["sha256"] == old_sha
    assert hashlib.sha256(dao.medical_variables_path(case_id).read_bytes()).hexdigest() != old_sha
