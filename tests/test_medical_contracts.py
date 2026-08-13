"""Medical structuring configuration and contract tests.

All fixtures are synthetic. No test reads source-cases, outputs, data, or ground truth.
"""
import json
from pathlib import Path

from _validation import load_registry, validate_instance

ROOT = Path(__file__).resolve().parent.parent


def _errors(instance: dict, schema_name: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


def test_disabled_medical_structuring_bootstrap_validates():
    config_path = ROOT / "config" / "medical" / "medical_structuring_v0.1.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["behavior_enabled"] is False
    assert config["enabled_case_types"] == []
    assert config["enabled_document_roles"] == []
    assert _errors(config, "medical_structuring_config.schema.json") == []


def test_medical_structuring_cannot_enable_without_approval():
    config = {
        "schema_version": "medical_structuring_config.v0.1",
        "config_version": "medical_structuring.v0.1",
        "behavior_enabled": True,
        "approval": None,
        "enabled_case_types": ["synthetic"],
        "enabled_document_roles": ["medical_record"],
        "domains": [],
        "variable_kinds": [],
        "importance_tiers": [],
        "units": [],
        "quantity_rules": [],
    }

    errors = _errors(config, "medical_structuring_config.schema.json")
    assert any("approval" in error for error in errors)


def _minimal_medical_variables() -> dict:
    return {
        "case_id": "CASE_9001",
        "case_type": "synthetic",
        "run_id": "RUN_20260723_001",
        "component": "claim-analysis",
        "status": "success",
        "schema_version": "medical_variables.v0.1",
        "config_version": "medical_structuring.v0.1",
        "source_coverage": [{
            "document_id": "DOC_001",
            "document_role": "medical_record",
            "coverage_status": "consumed",
            "reason": "Synthetic validated medical-record fixture.",
            "processing_contract_versions": {
                "document_manifest": "document_manifest.v0.1",
                "page_chunks": "page_chunks.v0.1",
            },
        }],
        "domains": [{
            "domain_id": "MD_diagnosis",
            "domain_code": "diagnosis",
            "label": "Diagnosis",
            "config_version": "medical_structuring.v0.1",
        }],
        "medical_issues": [],
        "variables": [{
            "variable_id": "MV_0001",
            "domain_id": "MD_diagnosis",
            "variable_kind": "diagnosis_code",
            "label": "Documented diagnosis",
            "related_variable_ids": [],
            "observations": [{
                "observation_id": "MO_0001",
                "value_state": "asserted",
                "value_type": "coded",
                "coded_value": {
                    "system": "synthetic",
                    "code": "SYN-001",
                    "display": "Synthetic finding",
                },
                "observed_at": {
                    "kind": "unknown",
                    "reason": "The synthetic source states no occurrence date.",
                },
                "evidence": [{
                    "locator_id": "MEV_0001",
                    "document_id": "DOC_001",
                    "page": 1,
                    "quote": "Synthetic finding documented.",
                }],
                "extraction_method": "model_extracted",
            }],
        }],
        "contradiction_groups": [],
        "importance_assignments": [],
        "quantity_summaries": [],
        "timeline_observation_ids": [],
    }


def test_minimal_canonical_medical_variable_validates():
    assert _errors(_minimal_medical_variables(), "medical_variables.schema.json") == []


def test_non_asserted_observation_cannot_carry_typed_payload():
    instance = _minimal_medical_variables()
    observation = instance["variables"][0]["observations"][0]
    observation["value_state"] = "unknown"
    observation["state_reason"] = "Not documented."

    errors = _errors(instance, "medical_variables.schema.json")
    assert any("coded_value" in error or "valid under" in error for error in errors)