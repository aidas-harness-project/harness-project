"""The fidelity rubric's denominators must be the routing config's, not the scorer's.

The first two cases were scored over field sets the scorer assembled after
reading both documents -- 11 fact fields for one case, 13 for the other, with no
rule to appeal to and no reason the numbers were comparable. Meanwhile the
pipeline had already declared the universe, the grade axes and the
decision-bearing fields, and the rubric had transcribed a near-miss of all three
by hand.

These tests pin the binding in both directions: the contract must speak the
config's vocabulary, and the config's own invariants (one grading axis per
field, deferred fields excluded from extraction) must still hold, so a config
change that would silently move a score fails here instead.
"""
import json
import pathlib

import pytest

from _validation import load_registry, validate_instance

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
SCHEMA = "screening_fidelity_result.schema.json"


@pytest.fixture(scope="module")
def config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fidelity_schema():
    return json.loads(
        (ROOT / "schemas" / SCHEMA).read_text(encoding="utf-8")
    )


def _field_comparison(schema):
    return schema["$defs"]["field_comparison"]["properties"]


# ---- the config's own invariants, which the rubric now depends on ----

def test_every_field_declares_exactly_one_grading_axis(config):
    """`selection.field_grade()` reads one axis or the other with no default,
    so a field carrying both or neither would make the rubric's grade
    ambiguous -- which is the state the hand-written A list was in."""
    for row in config["fields"]:
        axes = [k for k in ("medical_advisory_grade", "priority_grade") if row.get(k)]
        assert len(axes) == 1, (row["field_id"], axes)


def test_deferred_fields_are_identifiable_without_reading_the_artifacts(config):
    """A field the pipeline does not extract must be excludable from the
    denominator from the config alone -- otherwise the scorer decides again.

    Deferral is carried by `extraction_wave`/`activation_basis`, and NOT by
    `source_route_id`: `first_visit_date` is deferred while still naming the
    route it would read from. Only a grade-D field is required to have no
    route (the config's own allOf), so the rubric keys on the wave.
    """
    deferred = [r for r in config["fields"]
                if r.get("extraction_wave") == "deferred"
                or r.get("activation_basis") == "deferred"]
    assert deferred, "the deferred wave is what match_kind 'not_applicable' exists for"
    for row in deferred:
        assert row.get("extraction_wave") == "deferred", row["field_id"]
        assert row.get("activation_basis") == "deferred", row["field_id"]

    for row in config["fields"]:
        if row.get("medical_advisory_grade") == "D":
            assert row.get("extraction_wave") == "deferred", row["field_id"]
            assert row.get("source_route_id") is None, row["field_id"]


def test_critical_conflict_field_is_the_core_set(config):
    """The rubric's core_field is not a list it keeps; it is this flag."""
    critical = [r["field_id"] for r in config["fields"] if r.get("critical_conflict_field")]
    assert critical, "core_field has nothing to mirror"
    # Sanity: the flag is a real subset, not everything or nothing.
    assert 0 < len(critical) < len(config["fields"])


def test_required_documents_are_declared_per_case_type(config):
    req = config["required_documents_by_case_type"]
    kinds = {k for kinds in req.values() for k in kinds}
    declared = {d if isinstance(d, str) else d.get("kind")
                for d in config.get("document_kinds", [])}
    assert kinds, "F3's denominator would be empty"
    unknown = kinds - declared if declared else set()
    assert not unknown, f"required kinds absent from document_kinds: {sorted(unknown)}"


# ---- the contract speaks that vocabulary ----

def test_field_comparison_is_keyed_by_field_id(fidelity_schema):
    props = _field_comparison(fidelity_schema)
    required = fidelity_schema["$defs"]["field_comparison"]["required"]
    assert "field_id" in props and "field_id" in required
    assert "field_name" not in required, (
        "a human label must not be the identity -- two scorers writing 주진단명 "
        "and 'primary diagnosis' would produce rows nothing can join"
    )


def test_grade_and_core_fields_exist_with_the_config_ranges(fidelity_schema, config):
    props = _field_comparison(fidelity_schema)
    assert props["field_grade"]["enum"] == ["A", "B", "C", "D"]
    assert props["core_field"]["type"] == "boolean"
    # Every grade the config can produce must be expressible.
    produced = set()
    for row in config["fields"]:
        produced.add(row.get("medical_advisory_grade") or row["priority_grade"])
    assert produced <= set(props["field_grade"]["enum"]), produced


def test_absence_carries_the_upstream_reason(fidelity_schema):
    props = _field_comparison(fidelity_schema)
    reasons = set(props["pipeline_unavailable_reason"]["enum"]) - {None}
    assert {"source_document_missing", "printed_but_blank", "not_mentioned"} <= reasons, (
        "these three were scored identically by the declared/silent split, and they "
        "call for different fixes"
    )


def test_deferred_rows_can_leave_the_denominator(fidelity_schema):
    props = _field_comparison(fidelity_schema)
    assert "not_applicable" in props["match_kind"]["enum"]


def test_result_names_the_config_version_it_scored_against(fidelity_schema):
    body = fidelity_schema["allOf"][1]
    assert "routing_config_version" in body["properties"]
    assert "routing_config_version" in body["required"]


def test_out_of_universe_channel_exists(fidelity_schema):
    """Binding the denominator to the pipeline makes the rubric blind to gaps in
    the pipeline. 사고경위서 -- the answer key's evidence for the accident
    narrative in both scored cases -- is in no document_kind, so without this
    channel that finding becomes unreportable rather than fixed."""
    body = fidelity_schema["allOf"][1]
    assert "out_of_universe_items" in body["required"]
    item = fidelity_schema["$defs"]["out_of_universe_item"]
    assert set(item["properties"]["kind"]["enum"]) == {"field", "document_kind"}


def test_the_gap_this_channel_was_added_for_is_real(config):
    """If a 사고경위서-like kind ever enters the config, this test failing is the
    signal to move that finding out of out_of_universe and into F3."""
    kinds = {d if isinstance(d, str) else d.get("kind")
             for d in config.get("document_kinds", [])}
    assert not any("accident" in (k or "") and "statement" in (k or "") for k in kinds)


# ---- a bound result validates ----

def test_a_bound_row_validates(fidelity_schema, config):
    schemas, registry = load_registry()
    row = config["fields"][0]
    grade = row.get("medical_advisory_grade") or row["priority_grade"]
    instance = {
        "case_id": "CASE_705",
        "run_id": "RUN_20260827_001",
        "component": "screening-fidelity",
        "status": "success",
        "created_at": "2026-08-27T10:00:00+09:00",
        "schema_version": "screening_fidelity_result.v0.1",
        "rubric_version": "screening_fidelity.v0.1",
        "routing_config_version": config["config_version"],
        "target_report_path": "outputs/CASE_705/screening_report.md",
        "target_report_sha256": "a" * 64,
        "ground_truth_access": {
            "version": "screening",
            "caller_stage": "screening_fidelity",
            "screening_stage_passed": True,
            "ground_truth_files": ["GT_001.pdf"],
        },
        "dimensions": [
            {
                "dimension_id": "F1", "weight": 50, "applicable": True, "na_reason": None,
                "score": 80.0, "rationale": "bound to the routing config",
                "screening_quotes": ["- 사고일: 확인 불가"],
                "field_comparisons": [
                    {
                        "field_id": row["field_id"],
                        "field_name": row.get("label_ko"),
                        "field_grade": grade,
                        "core_field": bool(row.get("critical_conflict_field")),
                        "screening_value": None,
                        "ground_truth_value": "2024-11-13",
                        "match_kind": "missing_in_screening",
                        "screening_absence_kind": "declared",
                        "pipeline_resolution_status": "unavailable",
                        "pipeline_unavailable_reason": "source_document_missing",
                        "ground_truth_ref": {"file": "GT_001.pdf", "locator": "III.1 사고조사"},
                    }
                ],
            },
            {"dimension_id": "F2", "weight": 30, "applicable": True, "na_reason": None,
             "score": 50.0, "rationale": "x", "screening_quotes": ["q"], "issue_matches": []},
            {"dimension_id": "F3", "weight": 20, "applicable": True, "na_reason": None,
             "score": 60.0, "rationale": "x", "screening_quotes": ["q"],
             "document_comparisons": []},
            {"dimension_id": "F4", "weight": 0, "applicable": True, "na_reason": None,
             "score": None, "rationale": "x", "screening_quotes": ["q"],
             "conclusion_agreement": "screening_declined"},
        ],
        "fidelity_score": 67.0,
        "core_field_mismatch": False,
        "verdict": "partial",
        "out_of_universe_items": [
            {
                "kind": "document_kind",
                "label": "사고경위서",
                "why_it_matters": "정답지가 사고 경위의 근거로 삼았으나 설정의 문서종에 없다.",
                "ground_truth_ref": {"file": "GT_001.pdf", "locator": "별첨 제5호"},
            }
        ],
        "findings": [],
    }
    assert validate_instance(instance, SCHEMA, schemas, registry) == []
