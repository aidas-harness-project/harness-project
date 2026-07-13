"""_validation.py -- schema_name_for()'s filename-to-schema derivation.
Regression coverage for the *.evidence.json bug: Path.stem only strips one
suffix, so every sidecar was silently unresolvable (always SKIP, never
PASS/FAIL) until this was special-cased.
"""
from pathlib import Path

from _validation import schema_name_for, load_registry, validate_instance


def test_plain_filename():
    assert schema_name_for(Path("extracted_claim_fields.json")) == "extracted_claim_fields.schema.json"


def test_versioned_filename_strips_suffix():
    assert schema_name_for(Path("critic_result_v2.json")) == "critic_result.schema.json"


def test_case_suffixed_filename_strips_suffix():
    assert schema_name_for(Path("extracted_claim_fields_CASE_009.json")) == "extracted_claim_fields.schema.json"


def test_doc_suffixed_filename_strips_suffix():
    """Regression: normalized_policy_clause_{document_id}.json (policy-pipeline's
    one-file-per-policy-document convention, fixed in the end-to-end pipeline
    review) used to resolve to None -- validate_output.py would silently
    SKIP every one of these instead of validating them."""
    assert schema_name_for(Path("normalized_policy_clause_DOC_004.json")) == "normalized_policy_clause.schema.json"
    assert schema_name_for(Path("normalized_policy_clause_DOC_001.json")) == "normalized_policy_clause.schema.json"


def test_ocr_result_doc_suffixed_filename_resolves():
    """ocr_result.json was found to have the exact same silent-overwrite
    risk as normalized_policy_clause.json -- fixed the same way, one file
    per document."""
    assert schema_name_for(Path("ocr_result_DOC_001.json")) == "ocr_result.schema.json"
    assert schema_name_for(Path("ocr_result_DOC_004.json")) == "ocr_result.schema.json"


def test_classification_result_doc_suffixed_filename_resolves():
    """Same silent-overwrite risk found in classification_result.json,
    fixed the same way."""
    assert schema_name_for(Path("classification_result_DOC_001.json")) == "classification_result.schema.json"


def test_redaction_result_doc_suffixed_filename_resolves():
    """Same silent-overwrite risk found in redaction_result.json, fixed the
    same way -- the third and last of the three files sharing this bug."""
    assert schema_name_for(Path("redaction_result_DOC_001.json")) == "redaction_result.schema.json"


def test_evidence_sidecar_resolves_regardless_of_base_document_name():
    for name in ["draft_report_v1.evidence.json", "screening_report.evidence.json", "rebuttal_points.evidence.json"]:
        assert schema_name_for(Path(name)) == "evidence_sidecar.schema.json", name


def test_unknown_filename_returns_none():
    assert schema_name_for(Path("totally_made_up_thing.json")) is None


def test_registry_loads_every_schema_and_resolves_cross_file_refs():
    schemas, registry = load_registry()
    assert len(schemas) >= 25
    # a schema that $refs another file -- confirms the registry actually wires cross-file refs, not just parses JSON
    sample = {
        "case_id": "CASE_009", "component": "claim-analysis", "status": "success",
        "coverages": [{
            "coverage_name": "a", "standardized_coverage_name": "b", "applicable": True,
            "confidence": 0.9, "evidence_references": [{"document_id": "DOC_001", "quote": "q"}],
            "review_required": False,
        }],
    }
    assert validate_instance(sample, "coverage_result.schema.json", schemas, registry) == []
