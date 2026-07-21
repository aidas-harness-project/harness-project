"""Regression coverage for the loss-adjuster-reviewed R-code taxonomy.

The schema enum is the validation boundary, its x-codebook carries stable
label/frequency metadata, and pipeline.md is the human-readable reference.
These tests keep the three views from drifting.
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


def _taxonomy_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["$defs"]["taxonomy_code"]


def _pipeline_rows():
    rows = {}
    pattern = re.compile(r"^\| (R\d{2}) \| (.+) \| ([상중하]) \|$")
    for line in PIPELINE_PATH.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            code, reason, frequency = match.groups()
            label_ko = reason.split(" (", 1)[0]
            rows[code] = {"label_ko": label_ko, "frequency_ko": frequency}
    return rows


def test_taxonomy_enum_and_codebook_cover_r01_through_r21_plus_r99():
    taxonomy = _taxonomy_schema()

    assert taxonomy["enum"] == EXPECTED_CODES
    assert list(taxonomy["x-codebook"]) == EXPECTED_CODES


def test_pipeline_table_matches_schema_codebook_labels_and_frequencies():
    taxonomy = _taxonomy_schema()
    pipeline_rows = _pipeline_rows()

    assert list(pipeline_rows) == EXPECTED_CODES
    for code, metadata in taxonomy["x-codebook"].items():
        assert pipeline_rows[code] == {
            "label_ko": metadata["label_ko"],
            "frequency_ko": FREQUENCY_TO_KO[metadata["frequency"]],
        }


def _denial_result(code):
    return {
        "case_id": "CASE_001",
        "component": "denial-response",
        "status": "success",
        "denial_reasons": [{
            "reason_id": "DR_1",
            "reason_type": "reduction",
            "taxonomy_code": code,
            "raw_reason_text": "보험사 감액 사유 원문",
            "policy_matches": [],
            "confidence": 0.9,
            "evidence_references": [{
                "document_id": "DOC_001",
                "page": 1,
                "quote": "보험사 감액 사유 원문",
            }],
            "review_required": False,
        }],
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
