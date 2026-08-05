"""Bounded downstream medical-review outcome access for QG-MED-1."""
from __future__ import annotations

import json
from pathlib import Path

import dao
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
