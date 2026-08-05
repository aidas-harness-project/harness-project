"""Fresh authenticated medical-review lifecycle scenarios."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import dao
import medical_review_ledger
import operator_auth
from test_dao_medical_variables import (
    FIXTURE,
    _enabled_config,
    _enabled_projection_config,
    _write_dependencies,
)
from test_medical_operator_auth import _enabled_policy


def _enabled_role_policy(tmp_path: Path, actions: list[str]) -> Path:
    path = tmp_path / "medical-review-roles.json"
    path.write_text(json.dumps({
        "schema_version": "medical_review_role_config.v0.1",
        "config_version": "medical_review_roles.v0.1",
        "operations_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "authority_role": "test-fixture",
            "decision_record": "tests/test_medical_review_lifecycle_scenarios.py",
            "approved_at": "2026-08-04T00:00:00+09:00",
            "scope": "Synthetic authenticated lifecycle tests only",
        },
        "specialty_codes": ["unspecified"],
        "named_actors": [{
            "actor_id": "coordinator-1",
            "display_name": "Synthetic Coordinator",
            "declared_role": "medical_coordinator",
            "specialty_code": "unspecified",
        }],
        "action_permissions": [
            {"action": action, "allowed_roles": ["medical_coordinator"]}
            for action in actions
        ],
    }), encoding="utf-8")
    return path


def _enabled_request_config(tmp_path: Path) -> Path:
    path = tmp_path / "medical-review-request-config.json"
    path.write_text(json.dumps({
        "schema_version": "medical_review_request_config.v0.1",
        "config_version": "medical_review_request.v0.1",
        "requests_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "authority_role": "test-fixture",
            "decision_record": "tests/test_medical_review_lifecycle_scenarios.py",
            "approved_at": "2026-08-04T00:00:00+09:00",
            "scope": "Synthetic balanced-package tests only",
        },
        "interpretations": [{
            "code": "diagnosis_interpretation",
            "label": "Synthetic diagnosis interpretation",
            "allowed_issue_categories": ["diagnosis"],
            "allowed_specialty_codes": ["unspecified"],
        }],
    }), encoding="utf-8")
    return path


def _enabled_role_policy_with_reviewer(tmp_path: Path) -> Path:
    path = tmp_path / "medical-review-roles-with-reviewer.json"
    path.write_text(json.dumps({
        "schema_version": "medical_review_role_config.v0.1",
        "config_version": "medical_review_roles.v0.1",
        "operations_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "authority_role": "test-fixture",
            "decision_record": "tests/test_medical_review_lifecycle_scenarios.py",
            "approved_at": "2026-08-04T00:00:00+09:00",
            "scope": "Synthetic coordinator/reviewer lifecycle tests only",
        },
        "specialty_codes": ["unspecified"],
        "named_actors": [
            {
                "actor_id": "coordinator-1",
                "display_name": "Synthetic Coordinator",
                "declared_role": "medical_coordinator",
                "specialty_code": "unspecified",
            },
            {
                "actor_id": "reviewer-1",
                "display_name": "Synthetic Reviewer",
                "declared_role": "medical_reviewer",
                "specialty_code": "unspecified",
            },
        ],
        "action_permissions": [
            {"action": action, "allowed_roles": [role]}
            for action, role in (
                ("open", "medical_coordinator"),
                ("record_decision", "medical_coordinator"),
                ("assign", "medical_coordinator"),
                ("supplement_package", "medical_coordinator"),
                ("submit_response", "medical_reviewer"),
                ("amend_response", "medical_reviewer"),
                ("close", "medical_coordinator"),
            )
        ],
    }), encoding="utf-8")
    return path


def _enabled_multi_operator_policy(
    tmp_path: Path,
    coordinator_token: str,
    reviewer_token: str,
) -> Path:
    path = tmp_path / "multi-operator-policy.json"
    path.write_text(json.dumps({
        "schema_version": "operator_auth_policy.v0.1",
        "policy_version": "operator_auth_policy.v0.1",
        "operations_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "approved_at": "2026-08-04T00:00:00+09:00",
            "scope": "Synthetic coordinator/reviewer tests only",
        },
        "actors": [
            {
                "actor_id": "OP_MEDICAL_COORDINATOR",
                "display_name": "Synthetic Medical Coordinator",
                "role": "medical_coordinator",
                "medical_actor_id": "coordinator-1",
                "token_sha256": hashlib.sha256(
                    coordinator_token.encode("utf-8")
                ).hexdigest(),
                "allowed_actions": ["medical_review"],
            },
            {
                "actor_id": "OP_MEDICAL_REVIEWER",
                "display_name": "Synthetic Medical Reviewer",
                "role": "medical_reviewer",
                "medical_actor_id": "reviewer-1",
                "token_sha256": hashlib.sha256(
                    reviewer_token.encode("utf-8")
                ).hexdigest(),
                "allowed_actions": ["medical_review"],
            },
        ],
    }), encoding="utf-8")
    return path


def _enabled_three_operator_policy(
    tmp_path: Path,
    coordinator_token: str,
    reviewer_token: str,
    second_reviewer_token: str,
) -> Path:
    path = _enabled_multi_operator_policy(
        tmp_path,
        coordinator_token,
        reviewer_token,
    )
    policy = json.loads(path.read_text(encoding="utf-8"))
    policy["actors"].append({
        "actor_id": "OP_MEDICAL_REVIEWER_2",
        "display_name": "Synthetic Medical Reviewer Two",
        "role": "medical_reviewer",
        "medical_actor_id": "reviewer-2",
        "token_sha256": hashlib.sha256(
            second_reviewer_token.encode("utf-8")
        ).hexdigest(),
        "allowed_actions": ["medical_review"],
    })
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def _publish_issue(
    tmp_path: Path,
    monkeypatch,
    make_args,
    capsys,
    *,
    importance_tier: str = "A",
) -> str:
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
    candidate["importance_assignments"][0]["tier"] = importance_tier
    candidate_path = tmp_path / "lifecycle-medical-variables.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    assert dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(candidate_path),
        held_by="claim-analysis",
        run_id=candidate["run_id"],
    )) == 0, capsys.readouterr().out
    return case_id


def _authorize_open(tmp_path: Path, monkeypatch) -> str:
    token = "synthetic-medical-token"
    monkeypatch.setattr(
        operator_auth,
        "POLICY_PATH",
        _enabled_policy(tmp_path, token),
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_REVIEW_ROLE_CONFIG",
        _enabled_role_policy(tmp_path, ["open"]),
        raising=False,
    )
    monkeypatch.setenv(operator_auth.TOKEN_ENV, token)
    return token


def _authorize_actions(
    tmp_path: Path,
    monkeypatch,
    actions: list[str],
) -> str:
    token = "synthetic-medical-token"
    monkeypatch.setattr(
        operator_auth,
        "POLICY_PATH",
        _enabled_policy(tmp_path, token),
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_REVIEW_ROLE_CONFIG",
        _enabled_role_policy(tmp_path, actions),
        raising=False,
    )
    monkeypatch.setenv(operator_auth.TOKEN_ENV, token)
    return token


def _do_not_refer_submission() -> dict:
    return {
        "referral_inputs": {
            "issue_id": "MCI_0001",
            "anomalies": [{
                "anomaly_id": "MA_0001",
                "anomaly_kind": "synthetic_signal",
                "severity_state": "unclassified_pending_policy",
                "variable_ids": ["MV_0001"],
                "observation_ids": ["MO_0001"],
                "evidence_locator_ids": ["MEV_0001"],
                "rationale": "Synthetic issue-bound signal only.",
            }],
            "convergence": {
                "status": "single_signal",
                "contributing_anomaly_ids": ["MA_0001"],
                "common_issue_id": "MCI_0001",
                "rationale": "One synthetic Tier-B signal; no clinical conclusion.",
            },
            "importance_assignment_ids": ["MIA_0001"],
            "source_coverage_statuses": ["consumed"],
            "schema_version": "medical_referral_inputs.v0.1",
            "config_version": "synthetic_anomaly.v0.1",
        },
        "source_reinspection": {
            "performed": True,
            "observation_ids": ["MO_0001"],
            "evidence_locator_ids": ["MEV_0001"],
            "result": "resolved",
            "unresolved_reason": None,
        },
        "decision": {
            "decision": "do_not_refer",
            "decision_origin": "authorized_human_override",
            "decision_scope": "screening_route_only",
            "issue_id": "MCI_0001",
            "rationale": "Synthetic routing only; no appropriateness conclusion.",
            "evidence_locator_ids": ["MEV_0001"],
            "policy_version": None,
            "additional_information_required": None,
        },
    }


def _insufficient_information_submission() -> dict:
    submission = _do_not_refer_submission()
    submission["source_reinspection"].update({
        "result": "unresolved",
        "unresolved_reason": "Synthetic evidence remains incomplete.",
    })
    submission["decision"].update({
        "decision": "insufficient_information",
        "rationale": "Additional synthetic evidence is required; no conclusion.",
        "additional_information_required": "Commit a revised synthetic observation.",
    })
    return submission


def _refer_submission() -> dict:
    submission = _do_not_refer_submission()
    submission["decision"].update({
        "decision": "refer",
        "rationale": "A focused synthetic interpretation is requested; no verdict.",
    })
    submission["request"] = {
        "issue_id": "MCI_0001",
        "issue_category": "diagnosis",
        "question": "How should the documented synthetic finding be interpreted?",
        "question_scope": {
            "scope_kind": "issue_only",
            "requested_interpretation": "diagnosis_interpretation",
            "subject_variable_ids": ["MV_0001"],
            "subject_observation_ids": ["MO_0001"],
            "time_window": None,
        },
        "suggested_specialty_code": "unspecified",
        "case_summary": "Synthetic closed-evidence case summary.",
        "accident_summary": "No synthetic accident facts beyond the closed fixture.",
        "timeline_observation_ids": ["MO_0001"],
        "prior_condition_observation_ids": [],
        "prognostic_observation_ids": [],
        "included_issue_evidence_link_ids": ["MIEL_0001"],
        "included_evidence_locator_ids": ["MEV_0001"],
        "omitted_supporting_links": [],
        "uncertainties": ["The fixture does not encode a clinical conclusion."],
        "source_coverage": ["DOC_001:consumed"],
        "schema_version": "medical_review_request.v0.1",
        "referral_policy_version": None,
    }
    return submission


def _response_submission(*, amended: bool = False) -> dict:
    return {
        "request_id": "MRR_0001",
        "request_version_reviewed": 1,
        "issue_id": "MCI_0001",
        "issue_category": "diagnosis",
        "decision_id": "MRI_0001-D01",
        "request_config_version_reviewed": "medical_review_request.v0.1",
        "referral_policy_version_reviewed": None,
        "reviewed_at": "2026-08-04T00:10:00+09:00",
        "evidence_locator_ids_reviewed": ["MEV_0001"],
        "interpretation": (
            "Amended synthetic interpretation without a clinical verdict."
            if amended
            else "Synthetic interpretation without a clinical verdict."
        ),
        "basis": "Only the assigned synthetic locator was reviewed.",
        "uncertainty": "The closed fixture does not support a clinical conclusion.",
        "alternative_interpretations": [],
        "additional_evidence_needed": [],
        "downstream_adjustment_advice": None,
        "supersedes_response_id": "MRR_0001-R01" if amended else None,
        "response_status": "amended" if amended else "completed",
        "attestation": "Synthetic test response; no real medical opinion.",
    }


def test_authenticated_coordinator_opens_revision_pinned_item_and_gate_blocks(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    token = _authorize_open(tmp_path, monkeypatch)

    args = make_args(
        case_id=case_id,
        issue_id="MCI_0001",
        decision_owner="human",
        held_by="medical-coordinator",
        run_id="RUN_20260723_001",
    )
    assert dao.cmd_open_medical_review_item(args) == 0
    ledger = dao.load_medical_review_ledger(case_id)
    assert len(ledger["review_items"]) == 1
    item = ledger["review_items"][0]
    assert item["review_item_id"] == "MRI_0001"
    assert item["issue_id"] == "MCI_0001"
    assert item["state"] == "decision_pending"
    assert ledger["events"][0]["medical_variables_revision"]["sha256"]
    assert ledger["events"][0]["actor"]["operator_actor_id"] == "OP_MEDICAL_TEST"
    assert ledger["events"][0]["actor"]["assertion_source"] == "authenticated_operator_policy"
    run_state = dao.load_run_state(case_id)
    projected_wait = next(
        entry
        for entry in run_state["human_input_status"]
        if entry.get("human_input_id") == "MRH_000001"
    )
    assert projected_wait["stage_name"] == "claim_analysis"
    assert projected_wait["input_kind"] == "referral_decision"
    assert projected_wait["review_item_id"] == "MRI_0001"
    assert projected_wait["status"] == "waiting"
    tampered_owner = json.loads(json.dumps(ledger))
    tampered_owner["review_items"][0]["decision_owner"] = "policy"
    assert medical_review_ledger.validate_ledger_semantics(tampered_owner, case_id)
    tampered_authorization = json.loads(json.dumps(ledger))
    tampered_authorization["events"][0]["role_policy_snapshot"][
        "action_permissions"
    ][0]["allowed_roles"] = ["medical_reviewer"]
    assert medical_review_ledger.validate_ledger_semantics(
        tampered_authorization,
        case_id,
    )
    missing_required_wait = json.loads(json.dumps(ledger))
    missing_required_wait["wait_episodes"] = []
    assert medical_review_ledger.validate_ledger_semantics(
        missing_required_wait,
        case_id,
    )
    rewritten_wait_identity = json.loads(json.dumps(ledger))
    rewritten_wait = rewritten_wait_identity["wait_episodes"][0]
    rewritten_wait["input_kind"] = "medical_evidence"
    rewritten_wait["request_id"] = "MRR_9999"
    rewritten_wait["wait_cycle"] = 99
    assert medical_review_ledger.validate_ledger_semantics(
        rewritten_wait_identity,
        case_id,
    )
    for counter, invalid_value in (
        ("next_review_item_number", 1),
        ("next_request_number", 99),
        ("next_human_input_number", 1),
    ):
        rewritten_counter = json.loads(json.dumps(ledger))
        rewritten_counter[counter] = invalid_value
        assert medical_review_ledger.validate_ledger_semantics(
            rewritten_counter,
            case_id,
        )
    assert dao.cmd_check_medical_reviews_clear(
        make_args(case_id=case_id)
    ) == 1

    assert dao.cmd_open_medical_review_item(args) == 1
    monkeypatch.delenv(operator_auth.TOKEN_ENV)
    assert dao.cmd_open_medical_review_item(args) == 1
    monkeypatch.setenv(operator_auth.TOKEN_ENV, token)


def test_policy_owned_open_is_rejected_without_a_policy_decision_route(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    _authorize_open(tmp_path, monkeypatch)
    before = dao.load_medical_review_ledger(case_id)

    assert dao.cmd_open_medical_review_item(make_args(
        case_id=case_id,
        issue_id="MCI_0001",
        decision_owner="policy",
        held_by="medical-coordinator",
        run_id="RUN_20260723_001",
    )) == 1
    assert dao.load_medical_review_ledger(case_id) == before


def test_wait_projection_reconciliation_is_repairable_and_idempotent(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    _authorize_open(tmp_path, monkeypatch)
    run_id = "RUN_20260723_001"
    state = dao.load_run_state(case_id)
    state["run_id"] = run_id
    state["human_input_status"] = [{
        "stage_name": "intake",
        "status": "received",
        "description": "Synthetic non-medical input history.",
        "requested_at": "2026-08-04T00:00:00+09:00",
        "received_at": "2026-08-04T00:01:00+09:00",
    }]
    dao.save_run_state(case_id, state)
    args = make_args(
        case_id=case_id,
        issue_id="MCI_0001",
        decision_owner="human",
        held_by="medical-coordinator",
        run_id=run_id,
    )
    assert dao.cmd_open_medical_review_item(args) == 0
    projected = dao.load_run_state(case_id)
    assert projected["human_input_status"][0]["stage_name"] == "intake"
    projected["human_input_status"] = projected["human_input_status"][:1]
    dao.save_run_state(case_id, projected)

    reconcile_args = make_args(
        case_id=case_id,
        held_by="medical-coordinator",
        run_id=run_id,
    )
    assert dao.cmd_reconcile_medical_review_waits(reconcile_args) == 0
    repaired = dao.load_run_state(case_id)
    assert [
        entry.get("human_input_id")
        for entry in repaired["human_input_status"]
    ] == [None, "MRH_000001"]
    assert dao.cmd_reconcile_medical_review_waits(reconcile_args) == 0
    assert dao.load_run_state(case_id) == repaired


def test_projection_lock_failure_preserves_ledger_success_for_reconciliation(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    _authorize_open(tmp_path, monkeypatch)
    run_id = "RUN_20260723_001"
    run_state_path = dao.run_state_path(case_id)
    assert dao.acquire_lock(
        run_state_path,
        "synthetic-projection-holder",
        run_id,
        "exercise partial projection failure",
    ) is None
    args = make_args(
        case_id=case_id,
        issue_id="MCI_0001",
        decision_owner="human",
        held_by="medical-coordinator",
        run_id=run_id,
    )
    try:
        assert dao.cmd_open_medical_review_item(args) == 0
        assert "requires reconciliation" in capsys.readouterr().out
        assert len(dao.load_medical_review_ledger(case_id)["review_items"]) == 1
        assert dao.load_run_state(case_id)["human_input_status"] == []
    finally:
        dao.release_lock(run_state_path)

    assert dao.cmd_reconcile_medical_review_waits(args) == 0
    assert dao.load_run_state(case_id)["human_input_status"][0][
        "human_input_id"
    ] == "MRH_000001"


def test_reconciliation_reads_canonical_ledger_after_projection_lock(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    _authorize_actions(tmp_path, monkeypatch, ["open", "cancel"])
    common = {
        "case_id": case_id,
        "held_by": "medical-coordinator",
        "run_id": "RUN_20260723_001",
    }
    assert dao.cmd_open_medical_review_item(make_args(
        **common,
        issue_id="MCI_0001",
        decision_owner="human",
    )) == 0
    open_ledger = json.loads(json.dumps(
        dao.load_medical_review_ledger(case_id)
    ))
    cancel_path = tmp_path / "cancel-before-reconciliation-race.json"
    cancel_path.write_text("{}", encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="cancel",
        data_file=str(cancel_path),
        reason="Synthetic cancellation before reconciliation race.",
    )) == 0
    cancelled_ledger = json.loads(json.dumps(
        dao.load_medical_review_ledger(case_id)
    ))
    assert cancelled_ledger["wait_episodes"][0]["status"] == "no_longer_required"

    original_acquire_lock = dao.acquire_lock
    projection_locked = False
    run_state_path = dao.run_state_path(case_id)

    def acquire_lock_after_new_generation(target, held_by, run_id, purpose):
        nonlocal projection_locked
        result = original_acquire_lock(target, held_by, run_id, purpose)
        if target == run_state_path and result is None:
            projection_locked = True
        return result

    def load_generation_at_lock(_dao, requested_case_id, *, allow_initialize=False):
        assert requested_case_id == case_id
        assert not allow_initialize
        return json.loads(json.dumps(
            cancelled_ledger if projection_locked else open_ledger
        ))

    monkeypatch.setattr(dao, "acquire_lock", acquire_lock_after_new_generation)
    monkeypatch.setattr(
        medical_review_ledger,
        "load_ledger",
        load_generation_at_lock,
    )
    projected, changed, error = medical_review_ledger.reconcile_wait_projection(
        dao,
        make_args(**common),
    )
    assert (projected, error) == (True, None)
    assert changed is False
    assert dao.load_run_state(case_id)["human_input_status"][0][
        "status"
    ] == "no_longer_required"


def test_duplicate_role_action_key_is_rejected_before_authorization(
    isolated_dao, tmp_path: Path, monkeypatch
):
    path = _enabled_role_policy_with_reviewer(tmp_path)
    policy = json.loads(path.read_text(encoding="utf-8"))
    close_permission = next(
        row for row in policy["action_permissions"]
        if row["action"] == "close"
    )
    policy["action_permissions"].append({
        **close_permission,
        "allowed_roles": ["medical_reviewer"],
    })
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(dao, "MEDICAL_REVIEW_ROLE_CONFIG", path, raising=False)

    with pytest.raises(ValueError, match="duplicate action permission"):
        medical_review_ledger._load_role_policy(dao)


def test_authenticated_tier_b_routing_decision_resolves_without_fabricated_review(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(
        tmp_path,
        monkeypatch,
        make_args,
        capsys,
        importance_tier="B",
    )
    _authorize_actions(tmp_path, monkeypatch, ["open", "record_decision"])
    assert dao.cmd_open_medical_review_item(make_args(
        case_id=case_id,
        issue_id="MCI_0001",
        decision_owner="human",
        held_by="medical-coordinator",
        run_id="RUN_20260723_001",
    )) == 0

    decision_path = tmp_path / "tier-b-do-not-refer.json"
    decision_path.write_text(
        json.dumps(_do_not_refer_submission()),
        encoding="utf-8",
    )
    assert dao.cmd_record_medical_referral_decision(make_args(
        case_id=case_id,
        review_item_id="MRI_0001",
        decision_file=str(decision_path),
        held_by="medical-coordinator",
        run_id="RUN_20260723_001",
    )) == 0

    ledger = dao.load_medical_review_ledger(case_id)
    item = ledger["review_items"][0]
    assert item["state"] == "do_not_refer"
    assert len(item["decisions"]) == 1
    assert item["requests"] == []
    assert all(wait["status"] != "waiting" for wait in ledger["wait_episodes"])
    assert dao.cmd_check_medical_reviews_clear(
        make_args(case_id=case_id)
    ) == 0


def test_authenticated_refer_builds_balanced_revision_pinned_request(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(
        tmp_path,
        monkeypatch,
        make_args,
        capsys,
        importance_tier="B",
    )
    _authorize_actions(tmp_path, monkeypatch, ["open", "record_decision"])
    monkeypatch.setattr(
        dao,
        "MEDICAL_REVIEW_REQUEST_CONFIG",
        _enabled_request_config(tmp_path),
        raising=False,
    )
    common = {
        "case_id": case_id,
        "held_by": "medical-coordinator",
        "run_id": "RUN_20260723_001",
    }
    assert dao.cmd_open_medical_review_item(make_args(
        **common,
        issue_id="MCI_0001",
        decision_owner="human",
    )) == 0
    decision_path = tmp_path / "tier-b-refer.json"
    decision_path.write_text(json.dumps(_refer_submission()), encoding="utf-8")
    assert dao.cmd_record_medical_referral_decision(make_args(
        **common,
        review_item_id="MRI_0001",
        decision_file=str(decision_path),
    )) == 0

    ledger = dao.load_medical_review_ledger(case_id)
    item = ledger["review_items"][0]
    assert item["state"] == "package_ready"
    assert item["current_request_id"] == "MRR_0001"
    assert len(item["requests"]) == 1
    record = item["requests"][0]
    assert record["current_assignment_id"] is None
    assert record["current_response_id"] is None
    request = record["versions"][0]
    assert request["request_version"] == 1
    assert request["medical_variables_revision"] == ledger["events"][-1]["medical_variables_revision"]
    assert request["included_issue_evidence_link_ids"] == ["MIEL_0001"]
    assert request["included_evidence_locator_ids"] == ["MEV_0001"]
    assert request["source_reinspection_ids"] == ["MRI_0001-S01"]
    active_waits = [
        wait for wait in ledger["wait_episodes"] if wait["status"] == "waiting"
    ]
    assert len(active_waits) == 1
    assert active_waits[0]["input_kind"] == "expert_assignment"
    assert active_waits[0]["request_id"] == "MRR_0001"
    rewritten_operator = json.loads(json.dumps(ledger))
    rewritten_operator["events"][0]["actor"]["operator_actor_id"] = (
        "OP_FORGED_OPERATOR"
    )
    assert medical_review_ledger.validate_ledger_semantics(
        rewritten_operator,
        case_id,
    )
    rewritten_policy_decision = json.loads(json.dumps(ledger))
    rewritten_item = rewritten_policy_decision["review_items"][0]
    rewritten_item["decisions"][0]["decision_origin"] = "policy"
    rewritten_item["decisions"][0]["actor"] = None
    rewritten_item["decisions"][0]["policy_version"] = (
        "fabricated_policy.v0.1"
    )
    rewritten_item["requests"][0]["versions"][0][
        "referral_policy_version"
    ] = "fabricated_policy.v0.1"
    assert medical_review_ledger.validate_ledger_semantics(
        rewritten_policy_decision,
        case_id,
    )
    rewritten_reinspection = json.loads(json.dumps(ledger))
    rewritten_reinspection["review_items"][0][
        "source_reinspection_records"
    ][0]["performed"] = False
    assert medical_review_ledger.validate_ledger_semantics(
        rewritten_reinspection,
        case_id,
    )
    rewritten_source_reference = json.loads(json.dumps(ledger))
    rewritten_source_reference["review_items"][0]["requests"][0][
        "versions"
    ][0]["source_reinspection_ids"] = ["MRI_0001-S99"]
    assert medical_review_ledger.validate_ledger_semantics(
        rewritten_source_reference,
        case_id,
    )
    orphaned_decision = json.loads(json.dumps(ledger))
    orphaned_item = orphaned_decision["review_items"][0]
    fabricated_decision = json.loads(json.dumps(orphaned_item["decisions"][0]))
    fabricated_decision["decision_id"] = "MRI_0001-D02"
    fabricated_decision["referral_inputs"]["source_reinspection_id"] = (
        "MRI_0001-S02"
    )
    fabricated_reinspection = json.loads(json.dumps(
        orphaned_item["source_reinspection_records"][0]
    ))
    fabricated_reinspection["source_reinspection_id"] = "MRI_0001-S02"
    orphaned_item["decisions"].append(fabricated_decision)
    orphaned_item["source_reinspection_records"].append(fabricated_reinspection)
    assert medical_review_ledger.validate_ledger_semantics(
        orphaned_decision,
        case_id,
    )
    orphaned_request_version = json.loads(json.dumps(ledger))
    fabricated_request_version = json.loads(json.dumps(
        orphaned_request_version["review_items"][0]["requests"][0]["versions"][0]
    ))
    fabricated_request_version["request_version"] = 2
    orphaned_request_version["review_items"][0]["requests"][0][
        "versions"
    ].append(fabricated_request_version)
    assert medical_review_ledger.validate_ledger_semantics(
        orphaned_request_version,
        case_id,
    )
    orphaned_event = json.loads(json.dumps(ledger))
    fabricated_event = json.loads(json.dumps(orphaned_event["events"][0]))
    fabricated_event["event_id"] = (
        f"MRE_{orphaned_event['next_event_number']:06d}"
    )
    fabricated_event["review_item_id"] = "MRI_9999"
    orphaned_event["events"].append(fabricated_event)
    orphaned_event["next_event_number"] += 1
    assert medical_review_ledger.validate_ledger_semantics(
        orphaned_event,
        case_id,
    )
    retargeted_decision_graph = json.loads(json.dumps(ledger))
    retargeted_item = retargeted_decision_graph["review_items"][0]
    retargeted_decision = retargeted_item["decisions"][0]
    retargeted_decision["decision_id"] = "MRI_9999-D01"
    retargeted_decision["decision_version"] = 99
    retargeted_decision["referral_inputs"]["referral_input_id"] = (
        "MRI_9999-I01"
    )
    retargeted_decision["referral_inputs"]["source_reinspection_id"] = (
        "MRI_9999-S01"
    )
    retargeted_item["source_reinspection_records"][0][
        "source_reinspection_id"
    ] = "MRI_9999-S01"
    retargeted_version = retargeted_item["requests"][0]["versions"][0]
    retargeted_version["decision_id"] = "MRI_9999-D01"
    retargeted_version["referral_input_id"] = "MRI_9999-I01"
    retargeted_version["source_reinspection_ids"] = ["MRI_9999-S01"]
    next(
        event
        for event in retargeted_decision_graph["events"]
        if event["action"] == "record_decision"
    )["decision_id"] = "MRI_9999-D01"
    assert medical_review_ledger.validate_ledger_semantics(
        retargeted_decision_graph,
        case_id,
    )
    rewritten_referral_revision = json.loads(json.dumps(ledger))
    rewritten_referral_revision["review_items"][0]["decisions"][0][
        "referral_inputs"
    ]["medical_variables_revision"]["sha256"] = "0" * 64
    assert medical_review_ledger.validate_ledger_semantics(
        rewritten_referral_revision,
        case_id,
    )
    unavailable_revision = json.loads(json.dumps(ledger))

    def rewrite_revision_references(value):
        if isinstance(value, dict):
            revision = value.get("medical_variables_revision")
            if isinstance(revision, dict):
                revision["sha256"] = "0" * 64
            for nested in value.values():
                rewrite_revision_references(nested)
        elif isinstance(value, list):
            for nested in value:
                rewrite_revision_references(nested)

    rewrite_revision_references(unavailable_revision)
    assert medical_review_ledger.validate_ledger_semantics(
        unavailable_revision,
        case_id,
    ) == []
    ledger_path = dao.medical_review_ledger_path(case_id)
    dao.atomic_write_json(ledger_path, unavailable_revision)
    try:
        with pytest.raises(ValueError, match="unavailable immutable revision"):
            dao.load_medical_review_ledger(case_id)
    finally:
        dao.atomic_write_json(ledger_path, ledger)
    assert dao.cmd_check_medical_reviews_clear(make_args(case_id=case_id)) == 1


def test_authenticated_assignment_response_and_append_only_amendment(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    coordinator_token = "synthetic-coordinator-token"
    reviewer_token = "synthetic-reviewer-token"
    monkeypatch.setattr(
        operator_auth,
        "POLICY_PATH",
        _enabled_multi_operator_policy(
            tmp_path,
            coordinator_token,
            reviewer_token,
        ),
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_REVIEW_ROLE_CONFIG",
        _enabled_role_policy_with_reviewer(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_REVIEW_REQUEST_CONFIG",
        _enabled_request_config(tmp_path),
        raising=False,
    )
    monkeypatch.setenv(operator_auth.TOKEN_ENV, coordinator_token)
    common = {
        "case_id": case_id,
        "held_by": "medical-coordinator",
        "run_id": "RUN_20260723_001",
    }
    assert dao.cmd_open_medical_review_item(make_args(
        **common,
        issue_id="MCI_0001",
        decision_owner="human",
    )) == 0
    referral_path = tmp_path / "refer-for-expert.json"
    referral_path.write_text(json.dumps(_refer_submission()), encoding="utf-8")
    assert dao.cmd_record_medical_referral_decision(make_args(
        **common,
        review_item_id="MRI_0001",
        decision_file=str(referral_path),
    )) == 0

    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(json.dumps({
        "request_id": "MRR_0001",
        "request_version": 1,
        "reviewer_actor_id": "reviewer-1",
        "package_review_attestation": {
            "reviewed_by_actor_id": "coordinator-1",
            "reviewed_at": "2026-08-04T00:05:00+09:00",
            "source_reinspection_confirmed": True,
            "focused_question_confirmed": True,
            "no_verdict_confirmed": True,
        },
    }), encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="assign",
        data_file=str(assignment_path),
        reason=None,
    )) == 0

    monkeypatch.setenv(operator_auth.TOKEN_ENV, reviewer_token)
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps({
        "response": _response_submission(),
    }), encoding="utf-8")
    reviewer_common = {**common, "held_by": "medical-reviewer"}
    assert dao.cmd_transition_medical_review(make_args(
        **reviewer_common,
        review_item_id="MRI_0001",
        action="submit_response",
        data_file=str(response_path),
        reason=None,
    )) == 0
    first_ledger = dao.load_medical_review_ledger(case_id)
    first_record = first_ledger["review_items"][0]["requests"][0]
    assert first_ledger["review_items"][0]["state"] == "answered"
    assert first_record["current_response_id"] == "MRR_0001-R01"

    amendment_path = tmp_path / "amendment.json"
    amendment_submission = _response_submission(amended=True)
    amendment_submission["additional_evidence_needed"] = [
        "A non-routing synthetic follow-up note."
    ]
    amendment_path.write_text(json.dumps({
        "response": amendment_submission,
        "reason": "Correct the synthetic interpretation wording.",
    }), encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **reviewer_common,
        review_item_id="MRI_0001",
        action="amend_response",
        data_file=str(amendment_path),
        reason=None,
    )) == 0
    ledger = dao.load_medical_review_ledger(case_id)
    record = ledger["review_items"][0]["requests"][0]
    assert [row["response_id"] for row in record["responses"]] == [
        "MRR_0001-R01",
        "MRR_0001-R02",
    ]
    assert record["responses"][0] == first_record["responses"][0]
    assert record["responses"][1]["supersedes_response_id"] == "MRR_0001-R01"
    assert record["current_response_id"] == "MRR_0001-R02"

    monkeypatch.setenv(operator_auth.TOKEN_ENV, coordinator_token)
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="close",
        data_file=None,
        reason="Synthetic coordinator disposition completed.",
    )) == 0
    closed = dao.load_medical_review_ledger(case_id)
    assert closed["review_items"][0]["state"] == "closed"
    closed_record = closed["review_items"][0]["requests"][0]
    assert closed_record["current_assignment_id"] is None
    assert closed_record["current_response_id"] == "MRR_0001-R02"
    tampered_assignment_head = json.loads(json.dumps(closed))
    tampered_assignment_head["review_items"][0]["requests"][0][
        "current_assignment_id"
    ] = "MRR_0001-A01"
    assert any(
        "closed has an active assignment" in error
        for error in medical_review_ledger.validate_ledger_semantics(
            tampered_assignment_head,
            case_id,
        )
    )
    assert all(wait["status"] != "waiting" for wait in closed["wait_episodes"])
    assert dao.cmd_check_medical_reviews_clear(make_args(case_id=case_id)) == 0


def test_coordinator_supplements_package_after_expert_requests_evidence(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    coordinator_token = "synthetic-coordinator-token"
    reviewer_token = "synthetic-reviewer-token"
    monkeypatch.setattr(
        operator_auth,
        "POLICY_PATH",
        _enabled_multi_operator_policy(
            tmp_path,
            coordinator_token,
            reviewer_token,
        ),
    )
    role_path = _enabled_role_policy_with_reviewer(tmp_path)
    role_policy = json.loads(role_path.read_text(encoding="utf-8"))
    role_policy["action_permissions"].append({
        "action": "request_information",
        "allowed_roles": ["medical_reviewer"],
    })
    role_path.write_text(json.dumps(role_policy), encoding="utf-8")
    monkeypatch.setattr(
        dao,
        "MEDICAL_REVIEW_ROLE_CONFIG",
        role_path,
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_REVIEW_REQUEST_CONFIG",
        _enabled_request_config(tmp_path),
        raising=False,
    )
    common = {
        "case_id": case_id,
        "held_by": "medical-coordinator",
        "run_id": "RUN_20260723_001",
    }
    monkeypatch.setenv(operator_auth.TOKEN_ENV, coordinator_token)
    assert dao.cmd_open_medical_review_item(make_args(
        **common,
        issue_id="MCI_0001",
        decision_owner="human",
    )) == 0
    referral = _refer_submission()
    referral_path = tmp_path / "refer-before-supplement.json"
    referral_path.write_text(json.dumps(referral), encoding="utf-8")
    assert dao.cmd_record_medical_referral_decision(make_args(
        **common,
        review_item_id="MRI_0001",
        decision_file=str(referral_path),
    )) == 0

    assignment_path = tmp_path / "assignment-before-supplement.json"
    assignment_path.write_text(json.dumps({
        "request_id": "MRR_0001",
        "request_version": 1,
        "reviewer_actor_id": "reviewer-1",
        "package_review_attestation": {
            "reviewed_by_actor_id": "coordinator-1",
            "reviewed_at": "2026-08-04T00:05:00+09:00",
            "source_reinspection_confirmed": True,
            "focused_question_confirmed": True,
            "no_verdict_confirmed": True,
        },
    }), encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="assign",
        data_file=str(assignment_path),
        reason=None,
    )) == 0

    information_path = tmp_path / "request-information.json"
    information_path.write_text(json.dumps({
        "request_id": "MRR_0001",
        "request_version": 1,
        "information_required": "Provide one additional synthetic source clarification.",
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, reviewer_token)
    assert dao.cmd_transition_medical_review(make_args(
        **{**common, "held_by": "medical-reviewer"},
        review_item_id="MRI_0001",
        action="request_information",
        data_file=str(information_path),
        reason=None,
    )) == 0
    before = dao.load_medical_review_ledger(case_id)
    assert before["review_items"][0]["state"] == "expert_needs_information"

    false_reinspection = dict(referral["source_reinspection"])
    false_reinspection["performed"] = False
    false_supplement_path = tmp_path / "false-supplement-package.json"
    false_supplement_path.write_text(json.dumps({
        "request_id": "MRR_0001",
        "request": referral["request"],
        "source_reinspection": false_reinspection,
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, coordinator_token)
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="supplement_package",
        data_file=str(false_supplement_path),
        reason=None,
    )) == 1
    assert dao.load_medical_review_ledger(case_id) == before

    supplement_path = tmp_path / "supplement-package.json"
    supplement_path.write_text(json.dumps({
        "request_id": "MRR_0001",
        "request": referral["request"],
        "source_reinspection": referral["source_reinspection"],
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, coordinator_token)
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="supplement_package",
        data_file=str(supplement_path),
        reason=None,
    )) == 0

    ledger = dao.load_medical_review_ledger(case_id)
    item = ledger["review_items"][0]
    record = item["requests"][0]
    assert item["state"] == "package_ready"
    assert [version["request_version"] for version in record["versions"]] == [1, 2]
    assert [
        row["source_reinspection_id"]
        for row in item["source_reinspection_records"]
    ] == ["MRI_0001-S01", "MRI_0001-S02"]
    assert record["versions"][1]["source_reinspection_ids"] == ["MRI_0001-S02"]
    assert record["current_assignment_id"] is None
    assert record["current_response_id"] is None
    active_waits = [
        wait for wait in ledger["wait_episodes"] if wait["status"] == "waiting"
    ]
    assert len(active_waits) == 1
    assert active_waits[0]["input_kind"] == "expert_assignment"
    assert active_waits[0]["request_id"] == "MRR_0001"
    assert ledger["events"][-1]["action"] == "supplement_package"
    tampered_supplement_actor = json.loads(json.dumps(ledger))
    reviewer_actor = next(
        event["actor"]
        for event in ledger["events"]
        if event["action"] == "request_information"
    )
    tampered_supplement_actor["review_items"][0][
        "source_reinspection_records"
    ][1]["actor_or_process"] = reviewer_actor["actor_id"]
    assert medical_review_ledger.validate_ledger_semantics(
        tampered_supplement_actor,
        case_id,
    )


def test_coordinator_cancels_nonterminal_item_and_resolves_active_wait(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    _authorize_actions(tmp_path, monkeypatch, ["open", "cancel"])
    common = {
        "case_id": case_id,
        "held_by": "medical-coordinator",
        "run_id": "RUN_20260723_001",
    }
    assert dao.cmd_open_medical_review_item(make_args(
        **common,
        issue_id="MCI_0001",
        decision_owner="human",
    )) == 0
    cancel_path = tmp_path / "cancel-review.json"
    cancel_path.write_text("{}", encoding="utf-8")

    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="cancel",
        data_file=str(cancel_path),
        reason="Synthetic coordinator cancellation.",
    )) == 0

    ledger = dao.load_medical_review_ledger(case_id)
    item = ledger["review_items"][0]
    assert item["state"] == "cancelled"
    assert item["current_request_id"] is None
    assert item["current_adjudication_id"] is None
    assert all(
        wait["status"] == "no_longer_required"
        and wait["resolution"] == "cancelled"
        for wait in ledger["wait_episodes"]
    )
    assert ledger["events"][-1]["action"] == "cancel"
    assert ledger["events"][-1]["to_state"] == "cancelled"
    tampered_wait = json.loads(json.dumps(ledger))
    tampered_wait["wait_episodes"][0].update({
        "status": "received",
        "resolution": "decision:refer",
    })
    assert medical_review_ledger.validate_ledger_semantics(tampered_wait, case_id)


def test_additional_information_rebinds_stale_item_to_new_revision(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    _authorize_actions(
        tmp_path,
        monkeypatch,
        ["open", "record_decision", "provide_information"],
    )
    common = {
        "case_id": case_id,
        "held_by": "medical-coordinator",
        "run_id": "RUN_20260723_001",
    }
    assert dao.cmd_open_medical_review_item(make_args(
        **common,
        issue_id="MCI_0001",
        decision_owner="human",
    )) == 0
    first_decision = tmp_path / "needs-information.json"
    first_decision.write_text(
        json.dumps(_insufficient_information_submission()),
        encoding="utf-8",
    )
    assert dao.cmd_record_medical_referral_decision(make_args(
        **common,
        review_item_id="MRI_0001",
        decision_file=str(first_decision),
    )) == 0
    assert (
        dao.load_medical_review_ledger(case_id)["review_items"][0]["state"]
        == "needs_information"
    )

    revised = json.loads(FIXTURE.read_text(encoding="utf-8"))
    revised_observation = revised["variables"][0]["observations"][0]
    revised_observation["coded_value"]["display"] = "Synthetic revised finding"
    revised_observation["evidence"][0]["quote"] = (
        "Synthetic revised finding documented."
    )
    page_chunks = dao.load_json(dao.case_dir(case_id) / "page_chunks.json")
    page_chunks["chunks"][0]["text"] = (
        "Synthetic revised finding documented."
    )
    dao.atomic_write_json(
        dao.case_dir(case_id) / "page_chunks.json",
        page_chunks,
    )
    revised_path = tmp_path / "revised-medical-variables.json"
    revised_path.write_text(json.dumps(revised), encoding="utf-8")
    assert dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(revised_path),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )) == 0
    assert dao.cmd_check_medical_reviews_clear(make_args(case_id=case_id)) == 1

    information_path = tmp_path / "provided-information.json"
    information_path.write_text(json.dumps({
        "information": "A revised synthetic observation was committed.",
    }), encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="provide_information",
        data_file=str(information_path),
        reason=None,
    )) == 0
    ledger = dao.load_medical_review_ledger(case_id)
    assert ledger["review_items"][0]["state"] == "decision_pending"
    assert (
        ledger["events"][-1]["medical_variables_revision"]["sha256"]
        != ledger["events"][-2]["medical_variables_revision"]["sha256"]
    )

    final_path = tmp_path / "final-routing.json"
    final_path.write_text(
        json.dumps(_do_not_refer_submission()),
        encoding="utf-8",
    )
    assert dao.cmd_record_medical_referral_decision(make_args(
        **common,
        review_item_id="MRI_0001",
        decision_file=str(final_path),
    )) == 0
    assert dao.cmd_check_medical_reviews_clear(make_args(case_id=case_id)) == 0


def test_request_reassign_withdraw_and_reopen_preserve_heads_and_waits(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    coordinator_token = "synthetic-coordinator-token"
    reviewer_token = "synthetic-reviewer-token"
    reviewer_two_token = "synthetic-reviewer-two-token"
    monkeypatch.setattr(
        operator_auth,
        "POLICY_PATH",
        _enabled_three_operator_policy(
            tmp_path,
            coordinator_token,
            reviewer_token,
            reviewer_two_token,
        ),
    )
    role_path = _enabled_role_policy_with_reviewer(tmp_path)
    role_policy = json.loads(role_path.read_text(encoding="utf-8"))
    role_policy["named_actors"].append({
        "actor_id": "reviewer-2",
        "display_name": "Synthetic Medical Reviewer Two",
        "declared_role": "medical_reviewer",
        "specialty_code": "unspecified",
    })
    role_policy["action_permissions"].extend([
        {"action": "request_information", "allowed_roles": ["medical_reviewer"]},
        {"action": "reassign", "allowed_roles": ["medical_coordinator"]},
        {"action": "withdraw_response", "allowed_roles": ["medical_reviewer"]},
        {"action": "reopen", "allowed_roles": ["medical_coordinator"]},
    ])
    role_path.write_text(json.dumps(role_policy), encoding="utf-8")
    monkeypatch.setattr(
        dao,
        "MEDICAL_REVIEW_ROLE_CONFIG",
        role_path,
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_REVIEW_REQUEST_CONFIG",
        _enabled_request_config(tmp_path),
        raising=False,
    )
    common = {
        "case_id": case_id,
        "held_by": "medical-coordinator",
        "run_id": "RUN_20260723_001",
    }
    monkeypatch.setenv(operator_auth.TOKEN_ENV, coordinator_token)
    assert dao.cmd_open_medical_review_item(make_args(
        **common,
        issue_id="MCI_0001",
        decision_owner="human",
    )) == 0
    referral = _refer_submission()
    referral_path = tmp_path / "full-transition-referral.json"
    referral_path.write_text(json.dumps(referral), encoding="utf-8")
    assert dao.cmd_record_medical_referral_decision(make_args(
        **common,
        review_item_id="MRI_0001",
        decision_file=str(referral_path),
    )) == 0

    def assignment_payload(reviewer_actor_id: str) -> dict:
        return {
            "request_id": "MRR_0001",
            "request_version": 1,
            "reviewer_actor_id": reviewer_actor_id,
            "package_review_attestation": {
                "reviewed_by_actor_id": "coordinator-1",
                "reviewed_at": "2026-08-04T00:05:00+09:00",
                "source_reinspection_confirmed": True,
                "focused_question_confirmed": True,
                "no_verdict_confirmed": True,
            },
        }

    assignment_path = tmp_path / "full-transition-assignment.json"
    assignment_path.write_text(
        json.dumps(assignment_payload("reviewer-1")),
        encoding="utf-8",
    )
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="assign",
        data_file=str(assignment_path),
        reason=None,
    )) == 0

    information_path = tmp_path / "request-information.json"
    information_path.write_text(json.dumps({
        "request_id": "MRR_0001",
        "request_version": 1,
        "information_required": "Provide a synthetic clarification.",
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, reviewer_token)
    assert dao.cmd_transition_medical_review(make_args(
        **{**common, "held_by": "medical-reviewer"},
        review_item_id="MRI_0001",
        action="request_information",
        data_file=str(information_path),
        reason=None,
    )) == 0
    ledger = dao.load_medical_review_ledger(case_id)
    assert ledger["review_items"][0]["state"] == "expert_needs_information"
    assert ledger["wait_episodes"][-1]["input_kind"] == "medical_evidence"

    supplement_path = tmp_path / "full-transition-supplement.json"
    supplement_path.write_text(json.dumps({
        "request_id": "MRR_0001",
        "request": referral["request"],
        "source_reinspection": referral["source_reinspection"],
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, coordinator_token)
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="supplement_package",
        data_file=str(supplement_path),
        reason=None,
    )) == 0

    second_assignment = assignment_payload("reviewer-1")
    second_assignment["request_version"] = 2
    assignment_path.write_text(json.dumps(second_assignment), encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="assign",
        data_file=str(assignment_path),
        reason=None,
    )) == 0
    reassign = assignment_payload("reviewer-2")
    reassign["request_version"] = 2
    reassign_path = tmp_path / "reassign.json"
    reassign_path.write_text(json.dumps(reassign), encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="reassign",
        data_file=str(reassign_path),
        reason="Synthetic reassignment.",
    )) == 0
    reassigned_ledger = dao.load_medical_review_ledger(case_id)
    tampered_assignment_head = json.loads(json.dumps(reassigned_ledger))
    tampered_assignment_head["review_items"][0]["requests"][0][
        "current_assignment_id"
    ] = "MRR_0001-A02"
    assert medical_review_ledger.validate_ledger_semantics(
        tampered_assignment_head,
        case_id,
    )

    response_submission = _response_submission()
    response_submission["request_version_reviewed"] = 2
    response_submission["additional_evidence_needed"] = [
        "A non-routing synthetic follow-up note."
    ]
    response_path = tmp_path / "full-transition-response.json"
    response_path.write_text(
        json.dumps({"response": response_submission}),
        encoding="utf-8",
    )
    monkeypatch.setenv(operator_auth.TOKEN_ENV, reviewer_two_token)
    reviewer_args = {**common, "held_by": "medical-reviewer"}
    assert dao.cmd_transition_medical_review(make_args(
        **reviewer_args,
        review_item_id="MRI_0001",
        action="submit_response",
        data_file=str(response_path),
        reason=None,
    )) == 0
    ledger = dao.load_medical_review_ledger(case_id)
    record = ledger["review_items"][0]["requests"][0]
    current_response_id = record["current_response_id"]
    assert ledger["review_items"][0]["state"] == "answered"
    assert ledger["wait_episodes"][-1]["input_kind"] == "coordinator_disposition"

    tampered_assignment_policy = json.loads(json.dumps(ledger))
    tampered_assignment_policy["review_items"][0]["requests"][0][
        "assignments"
    ][-1]["role_policy_version"] = "fabricated-policy.v9"
    assert medical_review_ledger.validate_ledger_semantics(
        tampered_assignment_policy,
        case_id,
    )

    response_tampers = []
    superseded_assignment = json.loads(json.dumps(ledger))
    superseded_assignment["review_items"][0]["requests"][0]["responses"][0][
        "assignment_id"
    ] = "MRR_0001-A02"
    response_tampers.append(superseded_assignment)
    nonexistent_version = json.loads(json.dumps(ledger))
    nonexistent_version["review_items"][0]["requests"][0]["responses"][0][
        "request_version_reviewed"
    ] = 999
    response_tampers.append(nonexistent_version)
    foreign_evidence = json.loads(json.dumps(ledger))
    foreign_evidence["review_items"][0]["requests"][0]["responses"][0][
        "evidence_locator_ids_reviewed"
    ] = ["MEV_9999"]
    response_tampers.append(foreign_evidence)
    foreign_decision = json.loads(json.dumps(ledger))
    foreign_decision["review_items"][0]["requests"][0]["responses"][0][
        "decision_id"
    ] = "MRI_9999-D99"
    response_tampers.append(foreign_decision)
    future_assignment = json.loads(json.dumps(ledger))
    future_assignment["review_items"][0]["requests"][0]["assignments"][0][
        "request_version"
    ] = 2
    response_tampers.append(future_assignment)
    retargeted_assignment = json.loads(json.dumps(ledger))
    retargeted_assignment_record = retargeted_assignment["review_items"][0][
        "requests"
    ][0]
    retargeted_assignment_record["assignments"][0]["assignment_id"] = (
        "MRR_9999-A01"
    )
    retargeted_assignment_event_id = retargeted_assignment_record["assignments"][0][
        "assignment_event_id"
    ]
    next(
        event
        for event in retargeted_assignment["events"]
        if event["event_id"] == retargeted_assignment_event_id
    )["assignment_id"] = "MRR_9999-A01"
    response_tampers.append(retargeted_assignment)
    retargeted_response = json.loads(json.dumps(ledger))
    retargeted_response_record = retargeted_response["review_items"][0]["requests"][
        0
    ]
    original_response_id = retargeted_response_record["responses"][0]["response_id"]
    retargeted_response_record["responses"][0]["response_id"] = "MRR_9999-R01"
    retargeted_response_record["current_response_id"] = "MRR_9999-R01"
    next(
        event
        for event in retargeted_response["events"]
        if event.get("response_id") == original_response_id
    )["response_id"] = "MRR_9999-R01"
    response_tampers.append(retargeted_response)
    cross_item_response_event = json.loads(json.dumps(ledger))
    next(
        event
        for event in cross_item_response_event["events"]
        if event.get("action") == "submit_response"
    )["review_item_id"] = "MRI_9999"
    response_tampers.append(cross_item_response_event)
    assert all(
        medical_review_ledger.validate_ledger_semantics(tampered, case_id)
        for tampered in response_tampers
    )

    withdraw_path = tmp_path / "withdraw-response.json"
    withdraw_path.write_text(
        json.dumps({"response_id": current_response_id}),
        encoding="utf-8",
    )
    assert dao.cmd_transition_medical_review(make_args(
        **reviewer_args,
        review_item_id="MRI_0001",
        action="withdraw_response",
        data_file=str(withdraw_path),
        reason="Synthetic expert withdrawal.",
    )) == 0
    ledger = dao.load_medical_review_ledger(case_id)
    item = ledger["review_items"][0]
    record = item["requests"][0]
    assert item["state"] == "awaiting_expert"
    assert record["current_response_id"] is None
    assert record["responses"][-1]["response_status"] == "withdrawn"
    assert ledger["wait_episodes"][-1]["input_kind"] == "expert_response"

    assert dao.cmd_transition_medical_review(make_args(
        **reviewer_args,
        review_item_id="MRI_0001",
        action="submit_response",
        data_file=str(response_path),
        reason=None,
    )) == 0
    monkeypatch.setenv(operator_auth.TOKEN_ENV, coordinator_token)
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="close",
        data_file=None,
        reason="Synthetic disposition complete.",
    )) == 0
    closed_ledger = dao.load_medical_review_ledger(case_id)
    tampered_request_head = json.loads(json.dumps(closed_ledger))
    tampered_request_head["review_items"][0]["current_request_id"] = None
    assert medical_review_ledger.validate_ledger_semantics(
        tampered_request_head,
        case_id,
    )
    revised = json.loads(FIXTURE.read_text(encoding="utf-8"))
    revised_observation = revised["variables"][0]["observations"][0]
    revised_observation["coded_value"]["display"] = "Synthetic terminal revision"
    revised_observation["evidence"][0]["quote"] = (
        "Synthetic terminal revision documented."
    )
    page_chunks = dao.load_json(dao.case_dir(case_id) / "page_chunks.json")
    page_chunks["chunks"][0]["text"] = "Synthetic terminal revision documented."
    dao.atomic_write_json(dao.case_dir(case_id) / "page_chunks.json", page_chunks)
    revised_path = tmp_path / "terminal-revised-medical-variables.json"
    revised_path.write_text(json.dumps(revised), encoding="utf-8")
    assert dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(revised_path),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )) == 0
    reopen_path = tmp_path / "reopen.json"
    reopen_path.write_text(json.dumps({
        "decision_owner": "human",
        "reason": "Synthetic reopened review cycle.",
    }), encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="reopen",
        data_file=str(reopen_path),
        reason=None,
    )) == 0
    ledger = dao.load_medical_review_ledger(case_id)
    item = ledger["review_items"][0]
    assert item["state"] == "decision_pending"
    assert item["current_request_id"] is None
    assert ledger["wait_episodes"][-1]["input_kind"] == "referral_decision"
    assert (
        ledger["events"][-1]["medical_variables_revision"]["sha256"]
        == hashlib.sha256(dao.medical_variables_path(case_id).read_bytes()).hexdigest()
    )
    second_decision_path = tmp_path / "reopened-second-decision.json"
    second_decision_path.write_text(
        json.dumps(_do_not_refer_submission()),
        encoding="utf-8",
    )
    assert dao.cmd_record_medical_referral_decision(make_args(
        **common,
        review_item_id="MRI_0001",
        decision_file=str(second_decision_path),
    )) == 0
    reopened_ledger = dao.load_medical_review_ledger(case_id)
    reopened_item = reopened_ledger["review_items"][0]
    assert reopened_item["decisions"][-1]["decision_id"] == "MRI_0001-D02"
    assert reopened_item["decisions"][-1]["referral_inputs"][
        "referral_input_id"
    ] == "MRI_0001-I02"
    assert reopened_item["source_reinspection_records"][-1][
        "source_reinspection_id"
    ] == "MRI_0001-S03"
