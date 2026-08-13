"""Synthetic shared medical adjudication lifecycle scenario."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import dao
import medical_review_ledger  # pyright: ignore[reportMissingImports]
import operator_auth
import pytest
from test_medical_review_lifecycle_scenarios import (
    FIXTURE,
    _enabled_request_config,
    _publish_issue,
    _refer_submission,
    _response_submission,
)


def _role_policy(tmp_path: Path) -> Path:
    path = tmp_path / "adjudication-roles.json"
    path.write_text(json.dumps({
        "schema_version": "medical_review_role_config.v0.1",
        "config_version": "medical_review_roles.v0.1",
        "operations_enabled": True,
        "specialty_codes": ["unspecified"],
        "approval": {
            "approved_by": "synthetic-test-owner",
            "authority_role": "test-fixture",
            "decision_record": "tests/test_medical_shared_adjudication_scenario.py",
            "approved_at": "2026-08-04T00:00:00+09:00",
            "scope": "Synthetic shared-adjudication test only",
        },
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
            {
                "actor_id": "adjudicator-1",
                "display_name": "Synthetic Adjudicator",
                "declared_role": "medical_adjudicator",
                "specialty_code": "unspecified",
            },
        ],
        "action_permissions": [
            {"action": action, "allowed_roles": [role]}
            for action, role in (
                ("open", "medical_coordinator"),
                ("record_decision", "medical_coordinator"),
                ("assign", "medical_coordinator"),
                ("submit_response", "medical_reviewer"),
                ("amend_response", "medical_reviewer"),
                ("flag_conflict", "medical_coordinator"),
                ("adjudicate", "medical_adjudicator"),
                ("cancel", "medical_coordinator"),
                ("close", "medical_coordinator"),
                ("reopen", "medical_coordinator"),
            )
        ],
    }), encoding="utf-8")
    return path


def _operator_policy(tmp_path: Path, tokens: dict[str, str]) -> Path:
    path = tmp_path / "adjudication-operators.json"
    identities = (
        ("COORDINATOR", "medical_coordinator", "coordinator-1"),
        ("REVIEWER", "medical_reviewer", "reviewer-1"),
        ("ADJUDICATOR", "medical_adjudicator", "adjudicator-1"),
    )
    path.write_text(json.dumps({
        "schema_version": "operator_auth_policy.v0.1",
        "policy_version": "operator_auth_policy.v0.1",
        "operations_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "approved_at": "2026-08-04T00:00:00+09:00",
            "scope": "Synthetic shared-adjudication test only",
        },
        "actors": [
            {
                "actor_id": f"OP_MEDICAL_{name}",
                "display_name": f"Synthetic Medical {name.title()}",
                "role": role,
                "medical_actor_id": medical_actor_id,
                "token_sha256": hashlib.sha256(
                    tokens[role].encode("utf-8")
                ).hexdigest(),
                "allowed_actions": ["medical_review"],
            }
            for name, role, medical_actor_id in identities
        ],
    }), encoding="utf-8")
    return path


def _create_pending_shared_adjudication(
    tmp_path: Path, monkeypatch, make_args, capsys
) -> tuple[str, dict[str, str], dict[str, str]]:
    case_id = _publish_issue(tmp_path, monkeypatch, make_args, capsys)
    tokens = {
        "medical_coordinator": "synthetic-coordinator-token",
        "medical_reviewer": "synthetic-reviewer-token",
        "medical_adjudicator": "synthetic-adjudicator-token",
    }
    monkeypatch.setattr(operator_auth, "POLICY_PATH", _operator_policy(tmp_path, tokens))
    monkeypatch.setattr(dao, "MEDICAL_REVIEW_ROLE_CONFIG", _role_policy(tmp_path), raising=False)
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

    def answer(item_number: int, request_number: int) -> None:
        item_id = f"MRI_{item_number:04d}"
        request_id = f"MRR_{request_number:04d}"
        monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_coordinator"])
        assert dao.cmd_open_medical_review_item(make_args(
            **common,
            issue_id="MCI_0001",
            decision_owner="human",
        )) == 0
        referral = tmp_path / f"refer-{item_id}.json"
        referral.write_text(json.dumps(_refer_submission()), encoding="utf-8")
        assert dao.cmd_record_medical_referral_decision(make_args(
            **common,
            review_item_id=item_id,
            decision_file=str(referral),
        )) == 0
        assignment = tmp_path / f"assignment-{request_id}.json"
        assignment.write_text(json.dumps({
            "request_id": request_id,
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
            review_item_id=item_id,
            action="assign",
            data_file=str(assignment),
            reason=None,
        )) == 0
        response = _response_submission()
        response.update({
            "request_id": request_id,
            "decision_id": f"{item_id}-D01",
        })
        response_path = tmp_path / f"response-{request_id}.json"
        response_path.write_text(json.dumps({"response": response}), encoding="utf-8")
        monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_reviewer"])
        assert dao.cmd_transition_medical_review(make_args(
            **{**common, "held_by": "medical-reviewer"},
            review_item_id=item_id,
            action="submit_response",
            data_file=str(response_path),
            reason=None,
        )) == 0

    answer(1, 1)
    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_coordinator"])
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="close",
        data_file=None,
        reason="Close before the second independent review cycle.",
    )) == 0
    answer(2, 2)

    amendment = _response_submission(amended=True)
    amendment_path = tmp_path / "amend-first-response.json"
    amendment_path.write_text(json.dumps({
        "response": amendment,
        "reason": "Record a distinct synthetic interpretation for adjudication.",
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_reviewer"])
    assert dao.cmd_transition_medical_review(make_args(
        **{**common, "held_by": "medical-reviewer"},
        review_item_id="MRI_0001",
        action="amend_response",
        data_file=str(amendment_path),
        reason=None,
    )) == 0

    conflict_path = tmp_path / "shared-conflict.json"
    conflict_path.write_text(json.dumps({
        "items": [
            {"review_item_id": "MRI_0001", "response_id": "MRR_0001-R02"},
            {"review_item_id": "MRI_0002", "response_id": "MRR_0002-R01"},
        ],
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_coordinator"])
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="flag_conflict",
        data_file=str(conflict_path),
        reason="The two synthetic response heads require human adjudication.",
    )) == 0
    return case_id, tokens, common


def test_two_answered_items_share_one_atomic_adjudication(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id, tokens, common = _create_pending_shared_adjudication(
        tmp_path, monkeypatch, make_args, capsys
    )
    pending = dao.load_medical_review_ledger(case_id)
    assert {item["state"] for item in pending["review_items"]} == {"adjudication_required"}
    assert len(pending["adjudications"]) == 1
    assert pending["adjudications"][0]["status"] == "pending"
    shared_waits = [
        wait for wait in pending["wait_episodes"]
        if wait["status"] == "waiting" and wait["review_item_id"] is None
    ]
    assert len(shared_waits) == 1
    assert shared_waits[0]["related_review_item_ids"] == ["MRI_0001", "MRI_0002"]

    adjudication_path = tmp_path / "adjudication.json"
    adjudication_path.write_text(json.dumps({
        "adjudication_id": "MRA_000001",
        "record": "The synthetic opinions remain distinct; no model answer selected.",
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_adjudicator"])
    assert dao.cmd_transition_medical_review(make_args(
        **{**common, "held_by": "medical-adjudicator"},
        review_item_id="MRI_0001",
        action="adjudicate",
        data_file=str(adjudication_path),
        reason=None,
    )) == 0
    resolved = dao.load_medical_review_ledger(case_id)
    assert {item["state"] for item in resolved["review_items"]} == {"answered"}
    assert all(item["current_adjudication_id"] is None for item in resolved["review_items"])
    assert resolved["adjudications"][0]["status"] == "resolved"
    assert resolved["adjudications"][0]["record"].startswith("The synthetic opinions")
    disposition_waits = [
        wait for wait in resolved["wait_episodes"]
        if wait["status"] == "waiting"
        and wait["input_kind"] == "coordinator_disposition"
    ]
    assert {wait["review_item_id"] for wait in disposition_waits} == {
        "MRI_0001",
        "MRI_0002",
    }
    assert medical_review_ledger.validate_ledger_semantics(resolved, case_id) == []

    malformed = copy.deepcopy(resolved)
    malformed["adjudications"][0]["response_ids"][1] = "MRR_9999-R01"
    assert medical_review_ledger.validate_ledger_semantics(malformed, case_id)

    malformed = copy.deepcopy(resolved)
    conflict_event = next(
        event for event in malformed["events"]
        if event["action"] == "flag_conflict"
    )
    conflict_event.pop("adjudication_id")
    assert medical_review_ledger.validate_ledger_semantics(malformed, case_id)

    malformed = copy.deepcopy(resolved)
    shared_wait = next(
        wait for wait in malformed["wait_episodes"]
        if wait["adjudication_id"] == "MRA_000001"
    )
    shared_wait["related_review_item_ids"] = ["MRI_0001"]
    assert medical_review_ledger.validate_ledger_semantics(malformed, case_id)

    malformed = copy.deepcopy(resolved)
    malformed["review_items"][0]["current_adjudication_id"] = "MRA_000001"
    assert medical_review_ledger.validate_ledger_semantics(malformed, case_id)

    malformed = copy.deepcopy(resolved)
    opening = next(
        event for event in malformed["events"]
        if event["review_item_id"] == "MRI_0001" and event["action"] == "open"
    )
    opening.update({
        "action": "close",
        "from_state": "closed",
        "to_state": "decision_pending",
    })
    assert medical_review_ledger.validate_ledger_semantics(malformed, case_id)

    malformed = copy.deepcopy(resolved)
    second_event = next(
        event for event in malformed["events"]
        if event["review_item_id"] == "MRI_0001" and event["action"] != "open"
    )
    second_event["from_state"] = "package_ready"
    assert medical_review_ledger.validate_ledger_semantics(malformed, case_id)

    malformed = copy.deepcopy(resolved)
    response_event = next(
        event for event in malformed["events"]
        if event["review_item_id"] == "MRI_0001"
        and event["action"] == "submit_response"
    )
    coordinator_event = next(
        event for event in malformed["events"]
        if event["review_item_id"] == "MRI_0001" and event["action"] == "assign"
    )
    response_event["actor"] = coordinator_event["actor"]
    assert medical_review_ledger.validate_ledger_semantics(malformed, case_id)

    stale_cohort = copy.deepcopy(resolved)
    stale_cohort["review_items"][0]["requests"][0]["current_response_id"] = (
        "MRR_0001-R01"
    )
    with pytest.raises(ValueError, match="stale against a current response head"):
        medical_review_ledger._assert_fresh_adjudication_cohort(
            stale_cohort["review_items"][1],
            stale_cohort,
        )

    later_cycle = copy.deepcopy(resolved)
    later_record = later_cycle["review_items"][1]["requests"][0]
    later_response = copy.deepcopy(later_record["responses"][-1])
    later_response["response_id"] = "MRR_0002-R99"
    later_response["response_version"] = 99
    later_record["responses"].append(later_response)
    later_record["current_response_id"] = "MRR_0002-R99"
    medical_review_ledger._assert_fresh_adjudication_cohort(
        later_cycle["review_items"][1],
        later_cycle,
    )

    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_coordinator"])
    for item_id in ("MRI_0001", "MRI_0002"):
        assert dao.cmd_transition_medical_review(make_args(
            **common,
            review_item_id=item_id,
            action="close",
            data_file=None,
            reason="Close unchanged response head after shared adjudication.",
        )) == 0
    terminal = dao.load_medical_review_ledger(case_id)
    assert {item["state"] for item in terminal["review_items"]} == {"closed"}
    assert medical_review_ledger.validate_ledger_semantics(terminal, case_id) == []
    outcomes = medical_review_ledger.build_downstream_review_outcomes(dao, case_id)
    assert {
        row["adjudication"]["adjudication_id"]
        for row in outcomes["review_items"]
    } == {"MRA_000001"}
    assert all(
        row["response"]["response_id"]
        in row["adjudication"]["response_ids"]
        for row in outcomes["review_items"]
    )
    assert {
        row["adjudication"]["adjudicator"]["display_name"]
        for row in outcomes["review_items"]
    } == {"Synthetic Adjudicator"}

    assert dao.cmd_read_medical_review_ledger(make_args(case_id=case_id)) == 0
    ledger_output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert ledger_output["case_id"] == case_id
    assert len(ledger_output["review_items"]) == 2

    evidence_args = make_args(
        case_id=case_id,
        review_item_id="MRI_0001",
        request_id="MRR_0001",
        request_version=1,
        locator_id="MEV_0001",
    )
    assert dao.cmd_read_medical_review_evidence(evidence_args) == 0
    evidence_output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert evidence_output["request_version"] == 1
    assert evidence_output["locator"]["locator_id"] == "MEV_0001"
    assert evidence_output["revision_sha"] == (
        terminal["review_items"][0]["requests"][0]["versions"][0]
        ["medical_variables_revision"]["sha256"]
    )

    evidence_args.locator_id = "MEV_9999"
    assert dao.cmd_read_medical_review_evidence(evidence_args) == 1
    assert "not included in the request version" in capsys.readouterr().out


    post_adjudication_amendment = _response_submission(amended=True)
    post_adjudication_amendment["supersedes_response_id"] = "MRR_0001-R02"
    amendment_path = tmp_path / "post-adjudication-amendment.json"
    amendment_path.write_text(json.dumps({
        "response": post_adjudication_amendment,
        "reason": "Amend one response after the shared adjudication.",
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_reviewer"])
    assert dao.cmd_transition_medical_review(make_args(
        **{**common, "held_by": "medical-reviewer"},
        review_item_id="MRI_0001",
        action="amend_response",
        data_file=str(amendment_path),
        reason=None,
    )) == 0
    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_coordinator"])
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="close",
        data_file=None,
        reason="Close the amended response cycle.",
    )) == 0
    amended_outcomes = medical_review_ledger.build_downstream_review_outcomes(
        dao, case_id
    )
    assert all(
        row["adjudication"] is None
        for row in amended_outcomes["review_items"]
    )

    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_coordinator"])
    reopen_path = tmp_path / "reopen-second-item.json"
    reopen_path.write_text(json.dumps({
        "decision_owner": "human",
        "reason": "Start a fresh synthetic decision cycle.",
    }), encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0002",
        action="reopen",
        data_file=str(reopen_path),
        reason=None,
    )) == 0
    cancellation_path = tmp_path / "cancel-reopened-item.json"
    cancellation_path.write_text("{}", encoding="utf-8")
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0002",
        action="cancel",
        data_file=str(cancellation_path),
        reason="The fresh cycle is no longer required.",
    )) == 0
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0002",
        action="close",
        data_file=None,
        reason="Close the cancelled fresh cycle.",
    )) == 0
    assert dao.cmd_check_medical_reviews_clear(
        make_args(case_id=case_id)
    ) == 0
    reopened_outcomes = medical_review_ledger.build_downstream_review_outcomes(
        dao, case_id
    )
    assert all(
        row["adjudication"] is None
        for row in reopened_outcomes["review_items"]
    )


def test_shared_adjudication_cancellation_is_cohort_atomic(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id, tokens, common = _create_pending_shared_adjudication(
        tmp_path, monkeypatch, make_args, capsys
    )
    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_coordinator"])
    missing_id_path = tmp_path / "cancel-shared-without-id.json"
    missing_id_path.write_text("{}", encoding="utf-8")
    before = dao.load_medical_review_ledger(case_id)

    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="cancel",
        data_file=str(missing_id_path),
        reason="Synthetic shared cancellation.",
    )) == 1
    assert dao.load_medical_review_ledger(case_id) == before

    cancel_path = tmp_path / "cancel-shared.json"
    cancel_path.write_text(
        json.dumps({"adjudication_id": "MRA_000001"}),
        encoding="utf-8",
    )
    assert dao.cmd_transition_medical_review(make_args(
        **common,
        review_item_id="MRI_0001",
        action="cancel",
        data_file=str(cancel_path),
        reason="Synthetic shared cancellation.",
    )) == 0

    cancelled = dao.load_medical_review_ledger(case_id)
    assert {item["state"] for item in cancelled["review_items"]} == {"cancelled"}
    assert all(
        item["current_adjudication_id"] is None
        for item in cancelled["review_items"]
    )
    assert all(
        record[head] is None
        for item in cancelled["review_items"]
        for record in item["requests"]
        for head in ("current_assignment_id", "current_response_id")
    )
    adjudication = cancelled["adjudications"][0]
    assert adjudication["status"] == "cancelled"
    assert adjudication["resolved_at"] is not None
    shared_wait = next(
        wait for wait in cancelled["wait_episodes"]
        if wait["adjudication_id"] == "MRA_000001"
    )
    assert shared_wait["status"] == "no_longer_required"
    assert shared_wait["resolution"] == "cancelled"
    cancel_events = [
        event for event in cancelled["events"] if event["action"] == "cancel"
    ]
    assert {event["review_item_id"] for event in cancel_events} == {
        "MRI_0001",
        "MRI_0002",
    }
    assert {
        event["adjudication_id"] for event in cancel_events
    } == {"MRA_000001"}
    assert medical_review_ledger.validate_ledger_semantics(cancelled, case_id) == []
    for item_id in ("MRI_0001", "MRI_0002"):
        assert dao.cmd_transition_medical_review(make_args(
            **common,
            review_item_id=item_id,
            action="close",
            data_file=None,
            reason="Close after shared adjudication cancellation.",
        )) == 0
    closed = dao.load_medical_review_ledger(case_id)
    assert {item["state"] for item in closed["review_items"]} == {"closed"}
    assert all(
        record["current_response_id"] is None
        for item in closed["review_items"]
        for record in item["requests"]
    )
    assert medical_review_ledger.validate_ledger_semantics(closed, case_id) == []
    tampered_adjudication = copy.deepcopy(cancelled)
    tampered_adjudication["adjudications"][0]["status"] = "resolved"
    assert medical_review_ledger.validate_ledger_semantics(
        tampered_adjudication,
        case_id,
    )


def test_old_resolved_adjudication_does_not_block_later_cancelled_cycle_close(
    isolated_dao, tmp_path: Path, monkeypatch, make_args, capsys
):
    case_id, tokens, common = _create_pending_shared_adjudication(
        tmp_path, monkeypatch, make_args, capsys
    )
    adjudication_path = tmp_path / "resolved-adjudication.json"
    adjudication_path.write_text(json.dumps({
        "adjudication_id": "MRA_000001",
        "record": "Resolve the synthetic shared response conflict.",
    }), encoding="utf-8")
    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_adjudicator"])
    assert dao.cmd_transition_medical_review(make_args(
        **{**common, "held_by": "medical-adjudicator"},
        review_item_id="MRI_0001",
        action="adjudicate",
        data_file=str(adjudication_path),
        reason=None,
    )) == 0
    monkeypatch.setenv(operator_auth.TOKEN_ENV, tokens["medical_coordinator"])
    for item_id in ("MRI_0001", "MRI_0002"):
        assert dao.cmd_transition_medical_review(make_args(
            **common,
            review_item_id=item_id,
            action="close",
            data_file=None,
            reason="Close the resolved shared cycle.",
        )) == 0

    revised = json.loads(FIXTURE.read_text(encoding="utf-8"))
    observation = revised["variables"][0]["observations"][0]
    observation["coded_value"]["display"] = "Synthetic reopened revision"
    observation["evidence"][0]["quote"] = "Synthetic reopened revision documented."
    page_chunks = dao.load_json(dao.case_dir(case_id) / "page_chunks.json")
    page_chunks["chunks"][0]["text"] = "Synthetic reopened revision documented."
    dao.atomic_write_json(dao.case_dir(case_id) / "page_chunks.json", page_chunks)
    revised_path = tmp_path / "reopened-revision.json"
    revised_path.write_text(json.dumps(revised), encoding="utf-8")
    assert dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(revised_path),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )) == 0

    reopen_path = tmp_path / "reopen-after-adjudication.json"
    reopen_path.write_text(json.dumps({
        "decision_owner": "human",
        "reason": "Open an independent synthetic cycle.",
    }), encoding="utf-8")
    for review_item_id in ("MRI_0001", "MRI_0002"):
        assert dao.cmd_transition_medical_review(make_args(
            **common,
            review_item_id=review_item_id,
            action="reopen",
            data_file=str(reopen_path),
            reason=None,
        )) == 0
        cancel_path = tmp_path / f"cancel-reopened-cycle-{review_item_id}.json"
        cancel_path.write_text("{}", encoding="utf-8")
        assert dao.cmd_transition_medical_review(make_args(
            **common,
            review_item_id=review_item_id,
            action="cancel",
            data_file=str(cancel_path),
            reason="Cancel only the independent reopened cycle.",
        )) == 0
        assert dao.cmd_transition_medical_review(make_args(
            **common,
            review_item_id=review_item_id,
            action="close",
            data_file=None,
            reason="Close after cancelling the reopened cycle.",
        )) == 0

    ledger = dao.load_medical_review_ledger(case_id)
    item = next(
        row for row in ledger["review_items"]
        if row["review_item_id"] == "MRI_0001"
    )
    assert item["state"] == "closed"
    assert item["current_request_id"] is None
    assert medical_review_ledger.validate_ledger_semantics(ledger, case_id) == []
    outcomes = medical_review_ledger.build_downstream_review_outcomes(dao, case_id)
    projected = next(
        row for row in outcomes["review_items"]
        if row["review_item_id"] == "MRI_0001"
    )
    assert projected["state"] == "closed"
    assert projected["decision"] is None
    assert projected["response"] is None
    assert projected["adjudication"] is None
