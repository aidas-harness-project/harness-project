"""Regression coverage for the two-axis case-type classification.

`case_type_result.schema.json` v0.2 replaces the flat `case_type` enum (plus
its `secondary_case_types` hedge) with two orthogonal axes -- `coverage_basis`
(보상 근거: who pays, on what legal basis) and `loss_type` (손해 유형: what loss
is computed). See open-decisions.md #8/#8a for why: every multi-type case on
record straddled in the same direction because the enum was asking two
questions at once, and 자동차보험 -- the corpus's largest series -- had no
value at all.

v0.2 is deliberately ADDITIVE: the axes are optional and `case_type` stays
required, so an in-flight case (CASE_907) completes against v0.1 semantics
and the four pre-existing outputs stay readable. These tests pin both halves
of that contract -- the new rules bite, and the old shape still validates.
"""
import copy
import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_NAME = "case_type_result.schema.json"
SCHEMA_PATH = ROOT / "schemas" / SCHEMA_NAME
MANIFEST_SCHEMA_NAME = "document_manifest.schema.json"

EXPECTED_COVERAGE_BASIS = ["배상책임", "개인보험", "자동차보험", None]
EXPECTED_LOSS_TYPE = ["후유장해", "진단·수술비", "실손", None]


def _validate(instance, schema_name=SCHEMA_NAME):
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


def _defs():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["$defs"]


def _base_result(**overrides):
    """A minimal v0.1-shaped case_type_result -- the shape every pre-2026-08-04
    output has, which must keep validating unchanged."""
    instance = {
        "case_id": "CASE_009",
        "component": "claim-analysis",
        "status": "success",
        "case_type": "배상책임",
        "template_id": "배상책임_후유장해형",
        "confidence": 0.9,
        "evidence_references": [{"quote": "시설소유자배상책임"}],
        "review_required": False,
    }
    instance.update(overrides)
    return instance


# ------------------------------------------------------- axis vocabulary --

def test_coverage_basis_enum_matches_the_decided_taxonomy():
    assert _defs()["coverage_basis"]["enum"] == EXPECTED_COVERAGE_BASIS


def test_loss_type_enum_matches_the_decided_taxonomy():
    assert _defs()["loss_type"]["enum"] == EXPECTED_LOSS_TYPE


def test_자동차보험_is_representable():
    """The gap that motivated the split: 16 TA files (자동차보험 대인배상, the
    corpus's largest series) had no value in the v0.1 enum."""
    assert "자동차보험" in _defs()["coverage_basis"]["enum"]


def test_case_type_is_marked_deprecated_but_still_required():
    """v0.2 keeps it required so in-flight and historical outputs stay valid;
    v0.3 flips this once CASE_907 completes."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert "DEPRECATED" in _defs()["case_type"]["description"]
    assert "case_type" in schema["allOf"][1]["required"]


# --------------------------------------------- backward compatibility --

def test_v0_1_shaped_output_still_validates():
    """CASE_907 compatibility: a result carrying neither axis is still valid."""
    assert _validate(_base_result()) == []


@pytest.mark.parametrize("case_dir,expected_case_type", [
    ("CASE_021", "진단·수술비"),
    ("CASE_024", "후유장해"),
    ("CASE_112", "배상책임"),
])
def test_existing_outputs_on_disk_still_validate(case_dir, expected_case_type):
    path = ROOT / "outputs" / case_dir / "case_type_result.json"
    if not path.exists():
        pytest.skip(f"{case_dir} has no case_type_result.json on disk")
    instance = json.loads(path.read_text(encoding="utf-8"))
    assert instance["case_type"] == expected_case_type
    assert _validate(instance) == []


# ----------------------------------------------------------- axis rules --

@pytest.mark.parametrize("basis,loss", [
    ("배상책임", "후유장해"),
    ("개인보험", "진단·수술비"),
    ("개인보험", "후유장해"),
    ("자동차보험", "후유장해"),
])
def test_valid_axis_pairs_are_accepted(basis, loss):
    assert _validate(_base_result(coverage_basis=basis, loss_type=loss)) == []


@pytest.mark.parametrize("partial", [
    {"coverage_basis": "배상책임"},
    {"loss_type": "후유장해"},
])
def test_half_classified_claim_is_rejected(partial):
    """Declaring one axis commits to declaring both -- otherwise a template
    would be selected from half a classification."""
    assert _validate(_base_result(**partial)) != []


def test_unknown_coverage_basis_is_rejected():
    assert _validate(_base_result(coverage_basis="산재보험", loss_type="후유장해")) != []


def test_unknown_loss_type_is_rejected():
    assert _validate(_base_result(coverage_basis="배상책임", loss_type="휴업손해")) != []


# ------------------------------------------------------ non-claim cases --

def test_non_claim_case_with_null_axes_is_accepted():
    """CASE_030's shape: 약관/증권/청약 with no accident and no insurer
    response. It has no case type, which v0.1 could only express by abusing
    기타 -- the same value it used for 'unusual claim'."""
    assert _validate(_base_result(
        is_claim_case=False, coverage_basis=None, loss_type=None)) == []


@pytest.mark.parametrize("axes", [
    {"coverage_basis": "배상책임", "loss_type": "후유장해"},
    {"coverage_basis": "배상책임", "loss_type": None},
    {"coverage_basis": None, "loss_type": "후유장해"},
])
def test_non_claim_case_cannot_carry_an_axis(axes):
    assert _validate(_base_result(is_claim_case=False, **axes)) != []


# ------------------------------------------------ adjuster provenance --

def test_adjuster_input_requires_the_cross_check():
    """An adjuster-supplied type must record checkpoint 3's independent view,
    so a contradiction can never be silently dropped (open-decisions.md #8)."""
    assert _validate(_base_result(
        coverage_basis="배상책임", loss_type="후유장해",
        case_type_source="adjuster_input")) != []


def test_adjuster_input_with_cross_check_is_accepted():
    assert _validate(_base_result(
        coverage_basis="배상책임", loss_type="후유장해",
        case_type_source="adjuster_input",
        axis_cross_check={"agrees": True})) == []


def test_cross_check_disagreement_records_the_inferred_view():
    instance = _base_result(
        coverage_basis="배상책임", loss_type="후유장해",
        case_type_source="adjuster_input", review_required=True,
        reviewer_role="손해사정사",
        axis_cross_check={
            "agrees": False,
            "inferred_coverage_basis": "개인보험",
            "inferred_loss_type": "후유장해",
            "note": "pack contains no 배상 claim documents",
        })
    assert _validate(instance) == []


def test_inferred_source_needs_no_cross_check():
    assert _validate(_base_result(
        coverage_basis="배상책임", loss_type="후유장해",
        case_type_source="inferred")) == []


def test_unknown_case_type_source_is_rejected():
    assert _validate(_base_result(
        coverage_basis="배상책임", loss_type="후유장해",
        case_type_source="guessed")) != []


# ------------------------------------- manifest-side adjuster input --

def _manifest(adjuster_case_type=None):
    manifest = {
        "case_id": "CASE_009",
        "documents": [{
            "document_id": "DOC_001", "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_009/DOC_001.pdf", "file_format": "pdf",
            "file_size_bytes": 100, "pre_flagged_type": None, "pages": None,
            "ocr_status": "pending", "ocr_text_path": None, "ocr_quality": None,
            "uncertain_region_count": None, "cross_validation_status": None,
            "redacted_text_path": None, "document_type": None,
            "classification_confidence": None,
        }],
    }
    if adjuster_case_type is not None:
        manifest["adjuster_case_type"] = adjuster_case_type
    return manifest


def test_manifest_without_the_field_validates():
    """Every pre-2026-08-04 manifest, including CASE_907's in-flight one."""
    assert _validate(_manifest(), MANIFEST_SCHEMA_NAME) == []


def test_manifest_with_a_supplied_case_type_validates():
    assert _validate(_manifest({
        "coverage_basis": "자동차보험", "loss_type": "후유장해",
        "supplied_by": "Kim TY", "supplied_at": "2026-08-04T10:00:00+09:00",
    }), MANIFEST_SCHEMA_NAME) == []


def test_manifest_case_type_requires_a_supplier():
    assert _validate(_manifest({
        "coverage_basis": "자동차보험", "loss_type": "후유장해",
        "supplied_at": "2026-08-04T10:00:00+09:00",
    }), MANIFEST_SCHEMA_NAME) != []


def test_manifest_rejects_a_half_classified_case_type():
    assert _validate(_manifest({
        "coverage_basis": "자동차보험", "loss_type": None,
        "supplied_by": "Kim TY", "supplied_at": "2026-08-04T10:00:00+09:00",
    }), MANIFEST_SCHEMA_NAME) != []


def test_manifest_non_claim_case_nulls_both_axes():
    assert _validate(_manifest({
        "coverage_basis": None, "loss_type": None, "is_claim_case": False,
        "supplied_by": "Kim TY", "supplied_at": "2026-08-04T10:00:00+09:00",
    }), MANIFEST_SCHEMA_NAME) == []


def test_manifest_non_claim_case_cannot_carry_an_axis():
    assert _validate(_manifest({
        "coverage_basis": "배상책임", "loss_type": "후유장해", "is_claim_case": False,
        "supplied_by": "Kim TY", "supplied_at": "2026-08-04T10:00:00+09:00",
    }), MANIFEST_SCHEMA_NAME) != []
