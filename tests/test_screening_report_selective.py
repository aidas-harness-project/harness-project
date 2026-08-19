"""Screening Report assembler tests for the selective lane.

Synthetic only: version-controlled config/schemas and in-memory contracts.
Never reads source-cases, outputs, data, or ground truth.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance
import run_screening_report as reporter


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = (
    ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
)


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _errors(instance: dict, schema_name: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


def _fact(field_id: str, domain: str, value, *, status="resolved") -> dict:
    row = {
        "field_id": field_id,
        "domain_code": domain,
        "priority_grade": "A",
        "authority": "source_document_extraction",
        "resolution_status": status,
        "selected_observation_ids": ["CAO_0001"] if status == "asserted" else [],
        "observations": [{
            "observation_id": "CAO_0001",
            "value_state": "asserted",
            "value": value,
            "source_document_kind": "diagnosis_certificate",
            "source_priority_rank": 1,
            "extraction_wave": "A",
            "evidence_references": [{
                "document_id": "DOC_001", "page": 1, "quote": "q",
                "start_char": 0, "end_char": 1,
            }],
        }] if status == "asserted" else [],
        "conflict_candidate_ids": [],
        "stop_reason": (
            "trusted_value_found" if status == "asserted" else "sources_exhausted"
        ),
    }
    if status != "resolved":
        row["resolution_reason"] = "자료에서 확인되지 않음"
    return row


def _assessment(case_type, status, *, filing="unknown", evidence=True) -> dict:
    return {
        "case_type": case_type,
        "status": status,
        "triggered_field_ids": ["injury_event_present"] if status == "applicable" else [],
        "conflicting_field_ids": [],
        "filing_status": filing,
        "reason": f"{case_type} basis",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": "근거",
            "start_char": 0, "end_char": 2,
        }] if (status == "applicable" and evidence) else [],
    }


def _claim_analysis(*, facts=None, assessments=None, checklist=None) -> dict:
    return {
        "case_id": "CASE_9001",
        "run_id": "RUN_20260819_1",
        "medical_projection_status": "not_configured",
        "claim_facts": facts if facts is not None else [
            _fact("primary_diagnosis", "diagnosis", "우측 요골 골절"),
        ],
        "case_type_assessment": assessments if assessments is not None else [
            _assessment("personal_insurance", "applicable"),
            _assessment("traffic_accident", "uncertain"),
            _assessment("industrial_accident", "uncertain"),
            _assessment("liability", "not_applicable"),
        ],
        "required_document_checklist": checklist if checklist is not None else [],
        "conflict_candidates": [],
    }


def _consistency(checks=()) -> dict:
    return {"checks": list(checks)}


# ------------------------------------------------------------- structure --

def test_report_validates_against_the_screening_schema() -> None:
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency(),
        config=_config())
    assert _errors(report, "screening_report_selective.schema.json") == []


def test_all_four_case_types_appear_with_a_verdict_and_its_basis() -> None:
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency(),
        config=_config())
    rows = report["case_summary"]["case_type_assessment"]
    assert len(rows) == 4
    by_type = {row["case_type"]: row for row in rows}
    assert by_type["personal_insurance"]["status_label"] == "해당"
    assert by_type["traffic_accident"]["status_label"] == "불확실"
    assert by_type["liability"]["status_label"] == "비해당"
    for row in rows:
        assert row["basis"]
    # A supported verdict shows its direct evidence.
    assert by_type["personal_insurance"]["evidence_references"]


def test_filing_status_is_reported_per_type_and_defaults_to_unconfirmed() -> None:
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(assessments=[
            _assessment("personal_insurance", "supported", filing="filed"),
            _assessment("traffic_accident", "uncertain"),
            _assessment("industrial_accident", "uncertain"),
            _assessment("liability", "uncertain"),
        ]),
        consistency=_consistency(), config=_config())
    by_type = {
        row["case_type"]: row
        for row in report["case_summary"]["case_type_assessment"]
    }
    assert by_type["personal_insurance"]["filing_status_label"] == "접수"
    assert by_type["traffic_accident"]["filing_status_label"] == "확인 불가"


def test_checklist_shows_holding_status_per_required_document() -> None:
    checklist = [
        {"document_kind": "diagnosis_certificate", "status": "available",
         "required_for_case_types": ["personal_insurance"],
         "document_ids": ["DOC_001"], "reason": "r"},
        {"document_kind": "imaging_interpretation", "status": "missing",
         "required_for_case_types": ["personal_insurance"],
         "document_ids": [], "reason": "r"},
    ]
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(checklist=checklist),
        consistency=_consistency(), config=_config())
    rows = {row["document_kind"]: row for row in report["required_document_checklist"]}
    assert rows["diagnosis_certificate"]["status_label"] == "보유"
    assert rows["imaging_interpretation"]["status_label"] == "미확인"
    assert any(
        item["document_type"] == "imaging_interpretation"
        for item in report["missing_documents"]
    )


def test_existing_disability_assessment_presence_is_reported() -> None:
    present = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(checklist=[{
            "document_kind": "disability_assessment", "status": "available",
            "required_for_case_types": ["personal_insurance"],
            "document_ids": ["DOC_007"], "reason": "r"}]),
        consistency=_consistency(), config=_config())
    assert present["existing_disability_assessment"]["present"] is True
    assert present["existing_disability_assessment"]["document_ids"] == ["DOC_007"]

    absent = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(checklist=[]),
        consistency=_consistency(), config=_config())
    assert absent["existing_disability_assessment"]["present"] is False
    assert absent["existing_disability_assessment"]["status_label"] == "미확인"


def test_unconfirmed_items_list_searched_fields_that_came_back_empty() -> None:
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(facts=[
            _fact("primary_diagnosis", "diagnosis", None, status="unavailable"),
            _fact("accident_date", "event_timeline", "2026-03-02"),
        ]),
        consistency=_consistency(), config=_config())
    field_ids = {row["field_id"] for row in report["unconfirmed_items"]}
    assert "primary_diagnosis" in field_ids
    assert "accident_date" not in field_ids


def test_medical_facts_are_labelled_as_source_extraction_not_projection() -> None:
    """The report must not describe these facts as canonical projections.

    A screening reader decides how much weight to give a value partly from
    where it came from. Presenting a document reading as a projection of the
    canonical medical revision would overstate its provenance to the one
    audience acting on it.
    """
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency(),
        config=_config())
    authority = report["case_summary"]["medical_authority"]
    assert authority["source"] == "source_document_extraction"
    assert authority["medical_projection_status"] == "not_configured"
    assert "projection" not in authority["note"]


# ------------------------------------------------------------ conflicts --

def test_only_confirmed_conflicts_reach_the_report() -> None:
    consistency = _consistency([
        {"check_id": "CHK-1", "topic": "primary_diagnosis (CAC_0001): confirmed",
         "values_compared": [{"quote": "q"}], "result": "inconsistent",
         "conflict_id": "CONFLICT_1"},
        {"check_id": "CHK-2", "topic": "diagnosis_department (CAC_0002): withdrawn",
         "values_compared": [{"quote": "q"}], "result": "consistent",
         "conflict_id": None},
    ])
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=consistency,
        config=_config(),
        conflict_entries={"CONFLICT_1": {
            "field_or_topic": "primary_diagnosis",
            "sources": [
                {"document_id": "DOC_001", "value": "우측", "quote": "우측"},
                {"document_id": "DOC_002", "value": "좌측", "quote": "좌측"},
            ],
        }})
    assert len(report["inconsistencies"]) == 1
    assert report["inconsistencies"][0]["conflict_ref"] == "CONFLICT_1"
    assert _errors(report, "screening_report_selective.schema.json") == []


def test_every_deferred_conflict_is_carried_by_conflict_ref() -> None:
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency(),
        config=_config(),
        conflict_entries={"CONFLICT_7": {
            "field_or_topic": "accident_date",
            "sources": [
                {"document_id": "DOC_001", "value": "2026-03-02", "quote": "a"},
                {"document_id": "DOC_002", "value": "2026-03-05", "quote": "b"},
            ],
        }},
        deferred_conflict_ids=["CONFLICT_7"])
    refs = {row.get("conflict_ref") for row in report["inconsistencies"]}
    assert "CONFLICT_7" in refs
    assert _errors(report, "screening_report_selective.schema.json") == []


def test_a_deferred_conflict_already_carried_is_not_duplicated() -> None:
    consistency = _consistency([
        {"check_id": "CHK-1", "topic": "t", "values_compared": [{"quote": "q"}],
         "result": "inconsistent", "conflict_id": "CONFLICT_1"},
    ])
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=consistency,
        config=_config(),
        conflict_entries={"CONFLICT_1": {"field_or_topic": "f", "sources": []}},
        deferred_conflict_ids=["CONFLICT_1"])
    refs = [row.get("conflict_ref") for row in report["inconsistencies"]]
    assert refs.count("CONFLICT_1") == 1


# ------------------------------------------------- development exclusion --

def test_no_reading_trace_or_telemetry_leaks_into_the_report() -> None:
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency([
            {"check_id": "CHK-1", "topic": "x (CAC_0001): withdrawn",
             "values_compared": [{"quote": "q"}], "result": "consistent",
             "conflict_id": None},
        ]), config=_config())
    serialized = json.dumps(report, ensure_ascii=False)

    for forbidden in (
        "documents_read", "provider_calls", "input_tokens", "output_tokens",
        "wall_time_seconds", "field_stops", "stop_reason", "a_stop_fields",
        "b_fallback_fields", "source_priority_rank", "extraction_wave",
        "conflict_candidate", "pending_consistency_check",
    ):
        assert forbidden not in serialized, forbidden


def test_withdrawn_candidates_are_not_shown_as_findings() -> None:
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency([
            {"check_id": "CHK-1", "topic": "primary_diagnosis (CAC_0001): withdrawn",
             "values_compared": [{"quote": "q"}], "result": "consistent",
             "conflict_id": None},
        ]), config=_config())
    assert report["inconsistencies"] == []


def test_judgment_section_stays_hedged_and_routed_for_review() -> None:
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency(),
        config=_config())
    assert report["review_required"] is True
    assert report["preliminary_assessment"]["priority_review_points"]
    # P3: no definitive eligibility verdict is asserted anywhere.
    serialized = json.dumps(report, ensure_ascii=False)
    for forbidden in ("지급 확정", "부지급 확정", "보상 확정"):
        assert forbidden not in serialized
