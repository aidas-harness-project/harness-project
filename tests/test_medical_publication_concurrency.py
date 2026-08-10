"""Cross-owner serialization between medical publication and review mutations."""
from __future__ import annotations

import hashlib
import json
import threading

import dao
import medical_review_ledger
import operator_auth
from test_dao_medical_variables import (
    FIXTURE,
    _enabled_config,
    _enabled_projection_config,
    _write_dependencies,
)
from test_medical_review_lifecycle_scenarios import (
    _enabled_multi_operator_policy,
    _enabled_request_config,
    _enabled_role_policy_with_reviewer,
    _publish_issue,
    _refer_submission,
)


def test_revision_publication_serializes_with_package_supplement(
    isolated_dao, tmp_path, monkeypatch, make_args, capsys
):
    medical_repository = dao.sys.modules["medical_repository"]
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
    monkeypatch.setattr(dao, "MEDICAL_REVIEW_ROLE_CONFIG", role_path, raising=False)
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
    referral = _refer_submission()
    referral_path = tmp_path / "race-referral.json"
    referral_path.write_text(json.dumps(referral), encoding="utf-8")
    assert dao.cmd_record_medical_referral_decision(make_args(
        **common,
        review_item_id="MRI_0001",
        decision_file=str(referral_path),
    )) == 0
    assignment_path = tmp_path / "race-assignment.json"
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
    information_path = tmp_path / "race-request-information.json"
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

    canonical_before_removal = dao.medical_variables_path(case_id).read_bytes()
    removed_issue = json.loads(FIXTURE.read_text(encoding="utf-8"))
    removed_issue["medical_issues"] = []
    removed_issue["importance_assignments"] = []
    rejected_digest = hashlib.sha256(
        medical_repository.canonical_json_bytes(removed_issue)
    ).hexdigest()
    rejected_revision = (
        medical_repository.revisions_dir(dao, case_id)
        / f"{rejected_digest}.json"
    )
    assert not rejected_revision.exists()
    removed_issue_path = tmp_path / "race-removed-owned-issue.json"
    removed_issue_path.write_text(json.dumps(removed_issue), encoding="utf-8")
    assert dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(removed_issue_path),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )) == 1
    assert dao.medical_variables_path(case_id).read_bytes() == canonical_before_removal
    assert not rejected_revision.exists()

    canonical_target = dao.medical_variables_path(case_id)
    canonical_target.unlink()
    missing_canonical_result = dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(removed_issue_path),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    ))
    canonical_remained_absent = not canonical_target.exists()
    rejected_revision_remained_absent = not rejected_revision.exists()
    dao.atomic_write_bytes(canonical_target, canonical_before_removal)
    assert missing_canonical_result == 1
    assert canonical_remained_absent
    assert rejected_revision_remained_absent

    revised = json.loads(FIXTURE.read_text(encoding="utf-8"))
    observation = revised["variables"][0]["observations"][0]
    observation["coded_value"]["display"] = "Synthetic raced revision"
    observation["evidence"][0]["quote"] = "Synthetic raced revision documented."
    page_chunks = dao.load_json(dao.case_dir(case_id) / "page_chunks.json")
    page_chunks["chunks"][0]["text"] = "Synthetic raced revision documented."
    dao.atomic_write_json(dao.case_dir(case_id) / "page_chunks.json", page_chunks)
    revised_path = tmp_path / "race-revised-medical-variables.json"
    revised_path.write_text(json.dumps(revised), encoding="utf-8")
    publisher_args = make_args(
        case_id=case_id,
        data_file=str(revised_path),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )
    supplement_path = tmp_path / "race-supplement.json"
    supplement_path.write_text(json.dumps({
        "request_id": "MRR_0001",
        "request": referral["request"],
        "source_reinspection": referral["source_reinspection"],
    }), encoding="utf-8")
    supplement_args = make_args(
        **common,
        review_item_id="MRI_0001",
        action="supplement_package",
        data_file=str(supplement_path),
        reason=None,
    )

    publication_read = threading.Event()
    release_publication = threading.Event()
    supplement_acquired_ledger = threading.Event()
    original_load_ledger = medical_review_ledger.load_ledger
    original_acquire = dao.acquire_lock_blocking
    ledger_target = dao.medical_review_ledger_path(case_id)

    def paused_load_ledger(dao_module, loaded_case_id):
        ledger = original_load_ledger(dao_module, loaded_case_id)
        if threading.current_thread().name == "medical-publisher":
            publication_read.set()
            assert release_publication.wait(timeout=5)
        return ledger

    def observed_acquire(target, held_by, run_id, purpose):
        result = original_acquire(target, held_by, run_id, purpose)
        if (
            threading.current_thread().name == "medical-supplement"
            and target == ledger_target
            and result is None
        ):
            supplement_acquired_ledger.set()
        return result

    monkeypatch.setattr(medical_review_ledger, "load_ledger", paused_load_ledger)
    monkeypatch.setattr(dao, "acquire_lock_blocking", observed_acquire)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 5)
    monkeypatch.setenv(operator_auth.TOKEN_ENV, coordinator_token)
    publication_results = []
    supplement_results = []
    publisher = threading.Thread(
        target=lambda: publication_results.append(
            dao.cmd_write_medical_variables(publisher_args)
        ),
        name="medical-publisher",
    )
    publisher.start()
    assert publication_read.wait(timeout=5)
    supplementer = threading.Thread(
        target=lambda: supplement_results.append(
            dao.cmd_transition_medical_review(supplement_args)
        ),
        name="medical-supplement",
    )
    supplementer.start()
    acquired_before_publication = supplement_acquired_ledger.wait(timeout=0.5)
    if acquired_before_publication:
        supplementer.join(timeout=5)
        assert not supplementer.is_alive()
    assert acquired_before_publication is False
    release_publication.set()
    publisher.join(timeout=5)
    supplementer.join(timeout=5)
    assert not publisher.is_alive()
    assert not supplementer.is_alive()
    assert publication_results == [0]
    assert supplement_results == [0]
    recovered_ledger = dao.load_medical_review_ledger(case_id)
    assert recovered_ledger["review_items"][0]["state"] == "package_ready"
    assert (
        recovered_ledger["events"][-1]["medical_variables_revision"]["sha256"]
        == hashlib.sha256(dao.medical_variables_path(case_id).read_bytes()).hexdigest()
    )


def test_publication_rejects_foreign_canonical_run_owner(
    isolated_dao, tmp_path, monkeypatch, make_args, capsys
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
    foreign_run_id = "RUN_20260723_002"
    state = dao.load_run_state(case_id)
    state["run_id"] = foreign_run_id
    dao.save_run_state(case_id, state)

    result = dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    ))

    assert result == 1
    assert "canonical run owner" in capsys.readouterr().out
    assert dao.load_run_state(case_id)["run_id"] == foreign_run_id
    assert not dao.medical_variables_path(case_id).exists()
    assert not dao.medical_review_ledger_path(case_id).exists()
    assert dao.read_lock(dao.run_state_path(case_id)) is None
