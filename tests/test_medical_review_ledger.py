"""Medical-review ledger shape and transition policy tests."""
from _validation import load_registry, validate_instance
from medical_contracts import transition_allowed


def errors(data: dict, schema: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(data, schema, schemas, registry)


def role_policy(enabled: bool = True) -> dict:
    return {
        "schema_version": "medical_review_role_config.v0.1",
        "config_version": "medical_review_roles.v0.1",
        "operations_enabled": enabled,
        "approval": ({
            "approved_by": "synthetic-test-owner",
            "authority_role": "test-fixture",
            "decision_record": "tests/test_medical_review_ledger.py",
            "approved_at": "2026-07-23T00:00:00+09:00",
            "scope": "Synthetic tests only",
        } if enabled else None),
        "specialty_codes": ["unspecified"],
        "named_actors": [{
            "actor_id": "coordinator-1",
            "display_name": "Synthetic Coordinator",
            "declared_role": "medical_coordinator",
            "specialty_code": "unspecified",
        }],
        "action_permissions": [{"action": "close", "allowed_roles": ["medical_coordinator"]}],
    }


def actor() -> dict:
    return {
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


def test_empty_medical_review_ledger_validates():
    ledger = {
        "case_id": "CASE_9001",
        "schema_version": "medical_review_ledger.v0.1",
        "next_review_item_number": 1,
        "next_request_number": 1,
        "next_human_input_number": 1,
        "next_adjudication_number": 1,
        "next_event_number": 1,
        "updated_at": "2026-07-23T00:00:00+09:00",
        "review_items": [],
        "wait_episodes": [],
        "adjudications": [],
        "events": [],
    }
    assert not errors(ledger, "medical_review_ledger.schema.json")


def test_transition_policy_requires_enabled_named_actor_permission():
    assert transition_allowed(
        "answered", "close", actor=actor(), role_policy=role_policy(),
        has_current_response=True, has_pending_adjudication=False,
    )
    assert not transition_allowed(
        "answered", "close", actor=actor(), role_policy=role_policy(False),
        has_current_response=True, has_pending_adjudication=False,
    )
    assert not transition_allowed(
        "awaiting_expert", "close", actor=actor(), role_policy=role_policy(),
        has_current_response=True, has_pending_adjudication=False,
    )