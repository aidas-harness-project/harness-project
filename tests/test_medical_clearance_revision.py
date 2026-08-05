"""Revision-pinning behavior for the medical-review clearance gate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import dao
import medical_review_ledger
from test_dao_medical_variables import (
    FIXTURE,
    _enabled_config,
    _enabled_projection_config,
    _write_dependencies,
)


def _revision(sha: str, candidate: dict) -> dict:
    return {
        "sha256": sha,
        "run_id": candidate["run_id"],
        "schema_version": candidate["schema_version"],
        "config_version": candidate["config_version"],
    }


def _terminal_ledger(case_id: str, candidate: dict, sha: str) -> dict:
    ledger = medical_review_ledger.empty_ledger(dao, case_id)
    timestamp = dao.now_iso()
    actor = {
        "actor_id": "coordinator-1",
        "display_name": "Synthetic Medical Coordinator",
        "declared_role": "medical_coordinator",
        "specialty_code": "unspecified",
        "attested_at": timestamp,
        "assertion_source": "authenticated_operator_policy",
        "operator_actor_id": "OP_SYNTHETIC_COORDINATOR",
        "operator_policy_version": "operator_auth_policy.v0.1",
        "authentication_method": "bearer_sha256_policy",
    }
    role_policy = {
        "schema_version": "medical_review_role_config.v0.1",
        "config_version": "medical_review_roles.v0.1",
        "operations_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "authority_role": "test-fixture",
            "decision_record": "tests/test_medical_clearance_revision.py",
            "approved_at": "2026-07-23T00:00:00+09:00",
            "scope": "Synthetic revision-pinning fixture only",
        },
        "specialty_codes": ["unspecified"],
        "named_actors": [{
            "actor_id": "coordinator-1",
            "display_name": "Synthetic Medical Coordinator",
            "declared_role": "medical_coordinator",
            "specialty_code": "unspecified",
            "operator_actor_id": "OP_SYNTHETIC_COORDINATOR",
        }],
        "action_permissions": [
            {"action": "open", "allowed_roles": ["medical_coordinator"]},
            {"action": "cancel", "allowed_roles": ["medical_coordinator"]},
        ],
    }
    ledger.update({
        "next_review_item_number": 2,
        "next_human_input_number": 2,
        "next_event_number": 3,
    })
    ledger["review_items"] = [{
        "review_item_id": "MRI_0001",
        "issue_id": "MCI_0001",
        "decision_owner": "human",
        "state": "cancelled",
        "source_reinspection_records": [],
        "decisions": [],
        "requests": [],
        "current_request_id": None,
        "current_adjudication_id": None,
        "opened_at": timestamp,
        "updated_at": timestamp,
    }]
    ledger["events"] = [
        {
            "event_id": "MRE_000001",
            "review_item_id": "MRI_0001",
            "action": "open",
            "from_state": None,
            "to_state": "decision_pending",
            "actor": actor,
            "role_policy_snapshot": role_policy,
            "reason": None,
            "medical_variables_revision": _revision(sha, candidate),
            "created_at": timestamp,
        },
        {
            "event_id": "MRE_000002",
            "review_item_id": "MRI_0001",
            "action": "cancel",
            "from_state": "decision_pending",
            "to_state": "cancelled",
            "actor": actor,
            "role_policy_snapshot": role_policy,
            "reason": "Synthetic terminal routing state; no clinical judgment.",
            "medical_variables_revision": _revision(sha, candidate),
            "created_at": timestamp,
        },
    ]
    ledger["wait_episodes"] = [{
        "human_input_id": "MRH_000001",
        "input_kind": "referral_decision",
        "review_item_id": "MRI_0001",
        "related_review_item_ids": [],
        "request_id": None,
        "adjudication_id": None,
        "wait_cycle": 1,
        "status": "no_longer_required",
        "created_at": timestamp,
        "resolved_at": timestamp,
        "resolution": "cancelled",
    }]
    return ledger


def test_terminal_review_must_pin_current_medical_revision(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
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
    candidate_path = tmp_path / "revision-pinned-medical-variables.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    assert dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(candidate_path),
        held_by="claim-analysis",
        run_id=candidate["run_id"],
    )) == 0, capsys.readouterr().out

    current_sha = hashlib.sha256(
        dao.medical_variables_path(case_id).read_bytes()
    ).hexdigest()
    ledger = _terminal_ledger(case_id, candidate, current_sha)
    dao.atomic_write_json(dao.medical_review_ledger_path(case_id), ledger)
    assert dao.cmd_check_medical_reviews_clear(make_args(case_id=case_id)) == 0

    ledger["events"][-1]["medical_variables_revision"]["sha256"] = "0" * 64
    dao.atomic_write_json(dao.medical_review_ledger_path(case_id), ledger)
    assert dao.cmd_check_medical_reviews_clear(make_args(case_id=case_id)) == 1
