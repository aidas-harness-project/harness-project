"""Regression coverage for the loss-adjuster-reviewed R-code taxonomy.

The schema enum is the validation boundary, its x-codebook carries stable
label/frequency/decision-type metadata, and pipeline.md is the human-readable
reference. These tests keep the three views from drifting.
"""
import json
import re
from pathlib import Path

from _validation import load_registry, validate_instance


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "common_component_output.schema.json"
PIPELINE_PATH = ROOT / "pipeline.md"
EXPECTED_CODES = [*(f"R{i:02d}" for i in range(1, 22)), "R99"]
FREQUENCY_TO_KO = {"high": "상", "medium": "중", "low": "하"}
DECISION_TYPES_TO_KO = {
    (): "미분류",
    ("reduction",): "감액",
    ("denial",): "거절",
    ("reduction", "denial"): "감액 및 거절",
}
EXPECTED_DECISION_TYPES = {
    "R01": ["reduction"],
    "R02": ["reduction"],
    "R03": ["reduction"],
    "R04": ["denial"],
    "R05": ["denial"],
    "R06": ["reduction"],
    "R07": ["reduction"],
    "R08": ["denial"],
    "R09": ["denial"],
    "R10": ["reduction"],
    "R11": ["reduction"],
    "R12": [],
    "R13": ["reduction"],
    "R14": [],
    "R15": ["reduction", "denial"],
    "R16": ["reduction"],
    "R17": ["reduction"],
    "R18": ["reduction"],
    "R19": ["reduction"],
    "R20": ["reduction"],
    "R21": ["reduction"],
    "R99": ["reduction", "denial"],
}


def _taxonomy_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["$defs"]["taxonomy_code"]


def _pipeline_rows():
    rows = {}
    pattern = re.compile(r"^\| (R\d{2}) \| (.+) \| ([상중하]) \| (.+) \|$")
    for line in PIPELINE_PATH.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            code, reason, frequency, classification = match.groups()
            label_ko = reason.split(" (", 1)[0]
            rows[code] = {
                "label_ko": label_ko,
                "frequency_ko": frequency,
                "classification_ko": classification,
            }
    return rows


def test_taxonomy_enum_and_codebook_cover_r01_through_r21_plus_r99():
    taxonomy = _taxonomy_schema()

    assert taxonomy["enum"] == EXPECTED_CODES
    assert list(taxonomy["x-codebook"]) == EXPECTED_CODES


def test_codebook_matches_adjuster_reviewed_decision_type_mapping():
    taxonomy = _taxonomy_schema()

    assert {
        code: metadata["applicable_decision_types"]
        for code, metadata in taxonomy["x-codebook"].items()
    } == EXPECTED_DECISION_TYPES


def test_pipeline_table_matches_schema_codebook_metadata():
    taxonomy = _taxonomy_schema()
    pipeline_rows = _pipeline_rows()

    assert list(pipeline_rows) == EXPECTED_CODES
    for code, metadata in taxonomy["x-codebook"].items():
        assert pipeline_rows[code] == {
            "label_ko": metadata["label_ko"],
            "frequency_ko": FREQUENCY_TO_KO[metadata["frequency"]],
            "classification_ko": DECISION_TYPES_TO_KO[
                tuple(metadata["applicable_decision_types"])
            ],
        }


def _denial_result(code):
    applicable_types = EXPECTED_DECISION_TYPES.get(code, ["reduction"])
    decision_type = applicable_types[0] if applicable_types else "reduction"
    review_required = code in {"R12", "R14"}
    reason = {
        "reason_id": "DR_1",
        "decision_type": decision_type,
        "payment_status": "unknown",
        "taxonomy_code": code,
        "raw_reason_text": "보험사 감액 또는 거절 사유 원문",
        "insurer_claim_summary": "보험사가 감액 또는 거절을 주장함",
        "grounds": {
            "contractual_basis": [],
            "medical_or_factual_basis": [],
            "calculation_basis": [],
        },
        "amounts": {
            "claimed_amount": None,
            "payable_amount": None,
            "denied_amount": None,
            "reduction_amount": None,
            "reduction_rate": None,
        },
        "requested_documents": [],
        "policy_matches": [],
        "confidence": 0.9,
        "evidence_references": [{
            "document_id": "DOC_001",
            "page": 1,
            "quote": "보험사 감액 또는 거절 사유 원문",
        }],
        "review_required": review_required,
    }
    if review_required:
        reason["reviewer_role"] = "손해사정사"
    return {
        "case_id": "CASE_001",
        "component": "denial-response",
        "status": "success",
        "denial_reasons": [reason],
    }


def test_new_r10_through_r21_codes_validate():
    schemas, registry = load_registry()
    for number in range(10, 22):
        code = f"R{number:02d}"
        assert validate_instance(
            _denial_result(code), "denial_reason_result.schema.json", schemas, registry
        ) == [], code


def test_undefined_r22_is_rejected():
    schemas, registry = load_registry()
    errors = validate_instance(
        _denial_result("R22"), "denial_reason_result.schema.json", schemas, registry
    )
    assert errors
