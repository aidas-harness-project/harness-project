"""Deterministic compatibility projection from canonical medical variables."""
import json
from pathlib import Path

import pytest

from medical_contracts import project_legacy_medical_fields

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "medical" / "minimal_variables.json"


def projection_config() -> dict:
    return {
        "schema_version": "medical_projection_config.v0.1",
        "config_version": "medical_projection.v0.1",
        "projection_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "authority_role": "test-fixture",
            "decision_record": "tests/test_medical_claim_projection.py",
            "approved_at": "2026-07-23T00:00:00+09:00",
            "scope": "Synthetic fixtures only",
        },
        "field_rules": [{
            "rule_id": "synthetic_diagnosis_display",
            "field_name": "diagnosis_name",
            "variable_kind": "diagnosis_code",
            "value_type": "coded",
            "value_path": "coded_value.display",
            "selection_strategy": "single_distinct_or_fail",
            "compatibility_confidence": 0.9,
            "review_required": False,
        }],
        "primary_diagnosis_rule": {
            "rule_id": "legacy_document_character_priority",
            "enabled": False,
            "reason_disabled": "Document-role taxonomy and clinical approval remain deferred."
        },
    }


def test_single_distinct_canonical_candidate_projects_with_provenance():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    projected = project_legacy_medical_fields(data, projection_config=projection_config())

    field = projected["diagnosis_name"]
    assert field["value"] == "Synthetic finding"
    assert field["source_variable_ids"] == ["MV_0001"]
    assert field["source_observation_ids"] == ["MO_0001"]
    assert field["projection_rule_id"] == "synthetic_diagnosis_display"
    assert field["projection_config_version"] == "medical_projection.v0.1"


def test_ambiguous_distinct_candidates_fail_closed():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    second = json.loads(json.dumps(data["variables"][0]["observations"][0]))
    second["observation_id"] = "MO_0002"
    second["coded_value"] = {"system": "synthetic", "code": "SYN-002", "display": "Different finding"}
    second["evidence"][0]["locator_id"] = "MEV_0002"
    data["variables"][0]["observations"].append(second)

    with pytest.raises(ValueError, match="requires_human_projection"):
        project_legacy_medical_fields(data, projection_config=projection_config())