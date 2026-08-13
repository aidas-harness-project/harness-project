"""Closed medical referral, request, response, and ledger shape tests."""
from _validation import load_registry, validate_instance

SHA = "a" * 64
REVISION = {
    "sha256": SHA,
    "run_id": "RUN_20260723_001",
    "schema_version": "medical_variables.v0.1",
    "config_version": "medical_structuring.v0.1",
}
ACTOR = {
    "actor_id": "coordinator-1",
    "display_name": "Synthetic Coordinator",
    "declared_role": "medical_coordinator",
    "specialty_code": "unspecified",
    "attested_at": "2026-07-23T00:00:00+09:00",
    "assertion_source": "authenticated_operator_policy",
    "operator_actor_id": "OP_MEDICAL_TEST",
    "operator_policy_version": "operator_auth_policy.v0.1",
    "authentication_method": "bearer_sha256_policy",
}


def errors(data: dict, schema: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(data, schema, schemas, registry)


def reinspection() -> dict:
    return {
        "source_reinspection_id": "MRI_0001-S01",
        "medical_variables_revision": REVISION,
        "performed": True,
        "actor_or_process": "synthetic-source-reinspection",
        "performed_at": "2026-07-23T00:00:00+09:00",
        "observation_ids": ["MO_0001"],
        "evidence_locator_ids": ["MEV_0001"],
        "result": "unresolved",
        "unresolved_reason": "The source supports a focused interpretation question but does not answer it.",
    }


def referral_inputs() -> dict:
    return {
        "referral_input_id": "MRI_0001-I01",
        "issue_id": "MCI_0001",
        "medical_variables_revision": REVISION,
        "anomalies": [{
            "anomaly_id": "MA_0001",
            "anomaly_kind": "synthetic_signal",
            "severity_state": "unclassified_pending_policy",
            "variable_ids": ["MV_0001"],
            "observation_ids": ["MO_0001"],
            "evidence_locator_ids": ["MEV_0001"],
            "rationale": "Synthetic test signal only.",
        }],
        "convergence": {
            "status": "single_signal",
            "contributing_anomaly_ids": ["MA_0001"],
            "common_issue_id": "MCI_0001",
            "rationale": "One synthetic signal concerns the issue.",
        },
        "importance_assignment_ids": ["MIA_0001"],
        "source_reinspection_id": "MRI_0001-S01",
        "source_coverage_statuses": ["consumed"],
        "schema_version": "medical_referral_inputs.v0.1",
        "config_version": "synthetic_anomaly.v0.1",
    }


def decision() -> dict:
    return {
        "decision_id": "MRI_0001-D01",
        "decision_version": 1,
        "decision": "do_not_refer",
        "decision_origin": "authorized_human_override",
        "decision_scope": "screening_route_only",
        "issue_id": "MCI_0001",
        "referral_inputs": referral_inputs(),
        "rationale": "The synthetic item does not require referral.",
        "evidence_locator_ids": ["MEV_0001"],
        "policy_version": None,
        "actor": ACTOR,
        "additional_information_required": None,
        "created_at": "2026-07-23T00:00:01+09:00",
    }


def request() -> dict:
    return {
        "request_id": "MRR_0001",
        "request_version": 1,
        "issue_id": "MCI_0001",
        "issue_category": "diagnosis",
        "decision_id": "MRI_0001-D01",
        "referral_input_id": "MRI_0001-I01",
        "medical_variables_revision": REVISION,
        "source_reinspection_ids": ["MRI_0001-S01"],
        "question": "How should the cited synthetic finding be interpreted for this issue?",
        "question_scope": {
            "scope_kind": "issue_only",
            "requested_interpretation": "synthetic_interpretation",
            "subject_variable_ids": ["MV_0001"],
            "subject_observation_ids": ["MO_0001"],
            "time_window": None,
        },
        "suggested_specialty_code": "unspecified",
        "case_summary": "Synthetic case summary.",
        "accident_summary": "Synthetic onset summary.",
        "timeline_observation_ids": ["MO_0001"],
        "prior_condition_observation_ids": [],
        "prognostic_observation_ids": [],
        "included_issue_evidence_link_ids": ["MIEL_0001"],
        "included_evidence_locator_ids": ["MEV_0001"],
        "omitted_supporting_links": [],
        "uncertainties": ["The source does not provide clinical interpretation."],
        "source_coverage": ["All synthetic eligible sources were consumed."],
        "schema_version": "medical_review_request.v0.1",
        "request_config_version": "medical_review_request.v0.1-test",
        "referral_policy_version": None,
        "created_at": "2026-07-23T00:00:02+09:00",
    }


def response() -> dict:
    return {
        "response_id": "MRR_0001-R01",
        "response_version": 1,
        "assignment_id": "MRR_0001-A01",
        "request_id": "MRR_0001",
        "request_version_reviewed": 1,
        "issue_id": "MCI_0001",
        "issue_category": "diagnosis",
        "decision_id": "MRI_0001-D01",
        "request_config_version_reviewed": "medical_review_request.v0.1-test",
        "referral_policy_version_reviewed": None,
        "reviewer": {**ACTOR, "actor_id": "reviewer-1", "display_name": "Synthetic Reviewer", "declared_role": "medical_reviewer"},
        "reviewed_at": "2026-07-23T00:10:00+09:00",
        "submitted_at": "2026-07-23T00:11:00+09:00",
        "evidence_locator_ids_reviewed": ["MEV_0001"],
        "interpretation": "Synthetic interpretation for contract testing.",
        "basis": "The cited synthetic evidence.",
        "uncertainty": "No real clinical assertion is made.",
        "alternative_interpretations": ["Another synthetic interpretation could apply."],
        "additional_evidence_needed": [],
        "downstream_adjustment_advice": None,
        "supersedes_response_id": None,
        "response_status": "completed",
        "attestation": "I attest that this is my own synthetic test response.",
    }


def test_persisted_referral_shapes_validate():
    assert not errors(reinspection(), "medical_source_reinspection.schema.json")
    assert not errors(referral_inputs(), "medical_referral_inputs.schema.json")
    assert not errors(decision(), "medical_referral_decision.schema.json")


def test_request_and_response_shapes_validate():
    assert not errors(request(), "medical_review_request.schema.json")
    assert not errors(response(), "medical_review_response.schema.json")


def test_referral_submission_forbids_caller_owned_actor_and_ids():
    submission = {
        "referral_inputs": {
            key: value for key, value in referral_inputs().items()
            if key not in {"referral_input_id", "medical_variables_revision", "source_reinspection_id"}
        },
        "source_reinspection": {
            key: value for key, value in reinspection().items()
            if key not in {
                "source_reinspection_id", "medical_variables_revision",
                "actor_or_process", "performed_at",
            }
        },
        "decision": {
            "decision": "do_not_refer",
            "decision_origin": "authorized_human_override",
            "decision_scope": "screening_route_only",
            "issue_id": "MCI_0001",
            "rationale": "Synthetic routing decision.",
            "evidence_locator_ids": ["MEV_0001"],
            "policy_version": None,
            "additional_information_required": None,
        },
    }
    assert not errors(submission, "medical_referral_submission.schema.json")
    submission["decision"]["actor"] = ACTOR
    assert errors(submission, "medical_referral_submission.schema.json")
    submission["decision"].pop("actor")
    submission["source_reinspection"]["actor_or_process"] = "caller-controlled"
    assert errors(submission, "medical_referral_submission.schema.json")


def test_response_submission_forbids_caller_owned_reviewer_and_assignment():
    submission = {
        key: value for key, value in response().items()
        if key not in {"response_id", "response_version", "assignment_id", "reviewer", "submitted_at"}
    }
    assert not errors(submission, "medical_review_response_submission.schema.json")
    submission["reviewer"] = ACTOR
    assert errors(submission, "medical_review_response_submission.schema.json")