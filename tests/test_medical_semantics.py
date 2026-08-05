"""Semantic validation for the canonical medical-variable contract."""
import json
from pathlib import Path

from medical_contracts import validate_medical_variables

ROOT = Path(__file__).resolve().parent.parent


def minimal_data() -> dict:
    return {
        "case_id": "CASE_9001",
        "case_type": "기타",
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
                "coded_value": {"system": "synthetic", "code": "SYN-001", "display": "Synthetic finding"},
                "observed_at": {"kind": "unknown", "reason": "No occurrence date."},
                "evidence": [{
                    "locator_id": "MEV_0001",
                    "document_id": "DOC_001",
                    "page": 1,
                    "quote": "Synthetic finding documented.",
                    "chunk_id": "CHUNK_001",
                }],
                "extraction_method": "model_extracted",
            }],
        }],
        "contradiction_groups": [],
        "importance_assignments": [],
        "quantity_summaries": [],
        "timeline_observation_ids": ["MO_0001"],
    }


def semantic_inputs() -> tuple[dict, dict, dict]:
    manifest = {
        "case_id": "CASE_9001",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "synthetic-medical-record.pdf",
            "file_path": "data/raw/CASE_9001/DOC_001/synthetic.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "pages": 1,
            "ocr_status": "completed",
            "cross_validation_status": "agreed",
            "document_type": "medical_record",
            "downstream_disposition": "automated_text_pipeline",
        }],
    }
    page_chunks = {
        "case_id": "CASE_9001",
        "component": "document-pipeline",
        "status": "success",
        "chunks": [{
            "chunk_id": "CHUNK_001",
            "document_id": "DOC_001",
            "page_start": 1,
            "page_end": 1,
            "text": "Synthetic finding documented.",
        }],
    }
    return manifest, page_chunks, {"case_id": "CASE_9001", "conflicts": []}


def enabled_config() -> dict:
    config = json.loads((ROOT / "config" / "medical" / "medical_structuring_v0.1.json").read_text(encoding="utf-8"))
    config["behavior_enabled"] = True
    config["approval"] = {
        "approved_by": "synthetic-test-owner",
        "authority_role": "test-fixture",
        "decision_record": "tests/test_medical_semantics.py",
        "approved_at": "2026-07-23T00:00:00+09:00",
        "scope": "Synthetic fixtures only",
    }
    config["enabled_case_types"] = ["기타"]
    config["enabled_document_roles"] = ["medical_record"]
    config["variable_kinds"] = [{
        "code": "diagnosis_code",
        "domain_code": "diagnosis",
        "label": "Synthetic diagnosis code",
    }]
    return config


def test_semantic_validator_accepts_resolvable_synthetic_contract():
    manifest, page_chunks, conflict_ledger = semantic_inputs()
    errors = validate_medical_variables(
        minimal_data(),
        manifest=manifest,
        page_chunks=page_chunks,
        conflict_ledger=conflict_ledger,
        config=enabled_config(),
        canonical_case_type="기타",
    )
    assert errors == []


def test_semantic_validator_accepts_resolvable_table_only_locator():
    data = minimal_data()
    locator = data["variables"][0]["observations"][0]["evidence"][0]
    locator.pop("quote")
    locator["table_location"] = {
        "table": "Diagnosis table",
        "row": "2",
        "column": "diagnosis",
        "value": "Synthetic finding documented.",
    }
    manifest, page_chunks, conflict_ledger = semantic_inputs()

    errors = validate_medical_variables(
        data,
        manifest=manifest,
        page_chunks=page_chunks,
        conflict_ledger=conflict_ledger,
        config=enabled_config(),
        canonical_case_type="기타",
    )

    assert errors == []


def test_semantic_validator_rejects_dangling_domain_and_evidence():
    data = minimal_data()
    data["variables"][0]["domain_id"] = "MD_missing"
    data["variables"][0]["observations"][0]["evidence"][0]["page"] = 99
    manifest, page_chunks, conflict_ledger = semantic_inputs()

    errors = validate_medical_variables(
        data,
        manifest=manifest,
        page_chunks=page_chunks,
        conflict_ledger=conflict_ledger,
        config=enabled_config(),
        canonical_case_type="기타",
    )
    assert any("MD_missing" in error for error in errors)
    assert any("page 99" in error for error in errors)