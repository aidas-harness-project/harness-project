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


def test_leading_underscore_shared_state_files_resolve():
    """Regression: _source_ledger.json / _run_state.json / _conflict_ledger.json
    have always returned None from schema_name_for() -- their on-disk names
    carry a leading underscore but their schema files don't. Found by
    actually running validate_output.py against a real forked case's
    ledger and getting SKIP instead of PASS. These three files have never
    been validated by anything, ever, as a result (write-contract isn't
    used for them -- they have their own dedicated DAO write paths that
    don't call validate_instance at all)."""
    assert schema_name_for(Path("_source_ledger.json")) == "source_ledger.schema.json"
    assert schema_name_for(Path("_run_state.json")) == "run_state.schema.json"
    assert schema_name_for(Path("_conflict_ledger.json")) == "conflict_ledger.schema.json"


def test_evidence_sidecar_resolves_regardless_of_base_document_name():
    for name in ["draft_report_v1.evidence.json", "screening_report.evidence.json", "rebuttal_points.evidence.json"]:
        assert schema_name_for(Path(name)) == "evidence_sidecar.schema.json", name


def test_unknown_filename_returns_none():
    assert schema_name_for(Path("totally_made_up_thing.json")) is None


def test_ocr_result_page_text_path_nullable_for_disagreed_pages():
    """Regression: found by actually running a real 4-page document through
    checkpoint 1 (CASE_012/DOC_001) -- 3 pages agreed, 1 disagreed. A
    disagreed page is never written (P8, no tolerance threshold), so it has
    no text_path -- the schema used to require text_path as a non-null,
    pattern-matched string unconditionally, which made a real mixed-result
    document's own ocr_result.json unwritable."""
    schemas, registry = load_registry()
    page_disagreed = {
        "page": 4, "text_path": None, "uncertain_regions": [],
        "cross_validation": {"vision_model_reading": "x", "agreement": "disagreed",
                              "disagreement_details": ["DISAGREE: diagnosis code differ"]},
    }
    instance = {
        "case_id": "CASE_012", "run_id": "RUN_20260713_001", "component": "document-pipeline", "status": "success",
        "document_id": "DOC_001", "ocr_engine": "x", "vision_model_name": "x", "uncertain_confidence_threshold": 1.0,
        "extraction_method": "ocr", "ocr_status": "completed", "pages": [page_disagreed],
        "ocr_quality": "medium", "cross_validation_status": "disagreed_pending_review",
        "cross_validation_mode": "single_technology_weak_p8_poc",
        "cross_validation_note": "Both readers used claude-cli during the dev-phase weak-P8 fallback.",
        "review_required": True, "reviewer_role": "손해사정사",
    }
    assert validate_instance(instance, "ocr_result.schema.json", schemas, registry) == []

    without_note = dict(instance)
    without_note.pop("cross_validation_note")
    errors = validate_instance(without_note, "ocr_result.schema.json", schemas, registry)
    assert any("cross_validation_note" in error and "required" in error for error in errors)


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


def _claim_fields_instance(fields):
    return {
        "case_id": "CASE_021", "run_id": "RUN_20260714_001", "component": "claim-analysis",
        "status": "success", "fields": fields,
    }


def _date_field(value):
    return {"value": value, "confidence": 0.9, "review_required": False,
            "evidence_references": [{"document_id": "DOC_001", "page": 4, "quote": "q"}]}


def test_adhoc_date_field_validates_under_anyof():
    """Regression (CASE_021 end-to-end run, schema v0.2): additionalProperties
    used oneOf over the three shapes, but a YYYY-MM-DD string satisfies BOTH
    value_field and date_field -- exactly-one matching made every ad-hoc date
    field fail validation, which is why the run's imaging/receipt dates got
    smuggled through `warnings` instead of living as typed fields."""
    schemas, registry = load_registry()
    instance = _claim_fields_instance({"mri_read_date": _date_field("2025-10-14")})
    assert validate_instance(instance, "extracted_claim_fields.schema.json", schemas, registry) == []


def test_new_named_fields_from_case021_have_slots():
    schemas, registry = load_registry()
    value = {"value": "삼성화재, KB손해보험, 한화손해보험", "confidence": 0.95, "review_required": False,
             "evidence_references": [{"document_id": "DOC_001", "page": 1, "quote": "q"}]}
    instance = _claim_fields_instance({
        "imaging_date": _date_field("2025-10-10"),
        "claim_received_date": _date_field("2025-10-20"),
        "policy_contract_date": _date_field("2023-10-16"),
        "diagnosis_date": _date_field("2025-10-16"),
        "insurers": value,
    })
    assert validate_instance(instance, "extracted_claim_fields.schema.json", schemas, registry) == []


def test_malformed_date_is_actually_rejected():
    """Regression: format: date was decorative -- jsonschema skips every
    `format` keyword unless a FormatChecker is passed, so a named date_field
    holding garbage would have validated. validate_instance now passes
    Draft202012Validator.FORMAT_CHECKER."""
    schemas, registry = load_registry()
    instance = _claim_fields_instance({"accident_date": _date_field("2025-13-99")})
    errors = validate_instance(instance, "extracted_claim_fields.schema.json", schemas, registry)
    assert errors, "a malformed date must fail validation now that formats are checked"
