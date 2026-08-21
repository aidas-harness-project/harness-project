"""Selective-reading planner and four-type assessment tests.

These read only version-controlled config and schemas. They never touch
source-cases, outputs, data, or ground truth.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

import claim_analysis_case_types as case_types
import claim_analysis_selection as selection


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = (
    ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
)


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _docs(*pairs: tuple[str, str | None]) -> list[selection.DocumentRef]:
    return [selection.DocumentRef(document_id=d, kind=k) for d, k in pairs]


# --------------------------------------------------------------- planning --

def test_priority_ladder_follows_config_order_not_document_order() -> None:
    config = _config()
    # Deliberately supplied out of priority order: the outpatient record comes
    # first by document_id, the certificate last.
    documents = _docs(
        ("DOC_001", "outpatient_record"),
        ("DOC_002", "admission_discharge_summary"),
        ("DOC_003", "diagnosis_certificate"),
    )
    field = next(
        row for row in config["fields"] if row["field_id"] == "primary_diagnosis"
    )
    plan = selection.plan_field(field, config, documents)

    # `diagnosis` route ladder: certificate -> discharge summary -> initial
    # visit -> outpatient/progress/final.
    assert [step.priority_rank for step in plan.steps] == [1, 2, 3]
    assert plan.steps[0].document_ids == ("DOC_003",)
    assert plan.steps[1].document_ids == ("DOC_002",)
    assert plan.steps[2].document_ids == ("DOC_001",)


def test_a_wave_holds_only_a_wave_fields_and_b_wave_only_b() -> None:
    config = _config()
    a_ids = {row["field_id"] for row in selection.active_fields(config, "A")}
    b_ids = {row["field_id"] for row in selection.active_fields(config, "B")}

    assert a_ids and b_ids
    assert not (a_ids & b_ids)
    # Every A-wave field is an A-grade advisory field: the wave is the first
    # pass, so nothing lower-priority may pre-empt it.
    assert {
        row["medical_advisory_grade"]
        for row in selection.active_fields(config, "A")
    } == {"A"}


def test_deferred_and_opportunistic_fields_are_never_actively_searched() -> None:
    config = _config()
    searched = {
        row["field_id"]
        for wave in selection.SEARCHING_WAVES
        for row in selection.active_fields(config, wave)
    }
    for row in config["fields"]:
        if row["extraction_wave"] in {"deferred", "opportunistic"}:
            assert row["field_id"] not in searched


def test_d_grade_fields_are_absent_from_every_search_wave() -> None:
    config = _config()
    d_fields = [
        row["field_id"] for row in config["fields"]
        if row.get("medical_advisory_grade") == "D"
    ]
    assert d_fields, "the fixture must actually contain D-grade fields"
    searched = {
        row["field_id"]
        for wave in selection.SEARCHING_WAVES
        for row in selection.active_fields(config, wave)
    }
    for field_id in d_fields:
        assert field_id not in searched


def test_cost_documents_enter_a_read_plan_only_for_date_fields() -> None:
    """Narrowed 2026-08-22 from "never enter a plan".

    A 진료비 세부산정내역 is `date + 수가코드 + 항목명 + 금액` per line, and on
    CASE_705 it was the ONLY source of the surgery date, the imaging date and
    the treatment-period end date -- the clinical note recorded
    `Plan> admission, 내일 Op.` with no date at all. Blocking it outright
    published three fields as unavailable on a case that stated them.
    """
    config = _config()
    cost_ids = {"DOC_001", "DOC_002", "DOC_003"}
    documents = _docs(
        ("DOC_001", "medical_expense_receipt"),
        ("DOC_002", "medical_expense_itemization"),
        ("DOC_003", "pharmacy_payment_confirmation"),
    )
    for wave in selection.SEARCHING_WAVES:
        for plan in selection.plan_wave(config, documents, wave):
            planned = {doc for step in plan.steps for doc in step.document_ids}
            if not planned & cost_ids:
                continue
            assert selection.cost_documents_readable_for(plan.field_id), (
                f"{plan.field_id} planned a cost document read but is not a "
                "date field")


def test_a_cost_kind_forced_into_a_route_is_still_refused_at_runtime() -> None:
    """The config validator forbids this; the planner must not rely on it."""
    config = _config()
    route = next(r for r in config["source_routes"] if r["route_id"] == "treatment")
    route["priority_groups"] = [["medical_expense_receipt"]]
    field = next(
        row for row in config["fields"]
        if row["field_id"] == "major_treatment_category"
    )
    plan = selection.plan_field(field, config, _docs(("DOC_001", "medical_expense_receipt")))
    assert plan.steps == []
    assert plan.skip_reason is not None


def test_unclassified_and_ambiguous_documents_are_not_routed() -> None:
    config = _config()
    documents = [
        selection.DocumentRef("DOC_001", None),
        selection.DocumentRef("DOC_002", "diagnosis_certificate", ambiguous=True),
    ]
    field = next(
        row for row in config["fields"] if row["field_id"] == "primary_diagnosis"
    )
    plan = selection.plan_field(field, config, documents)
    assert plan.steps == []


def test_field_with_no_matching_document_records_why_it_was_skipped() -> None:
    config = _config()
    field = next(
        row for row in config["fields"] if row["field_id"] == "primary_diagnosis"
    )
    plan = selection.plan_field(field, config, _docs(("DOC_001", "nursing_routine_record")))
    assert plan.steps == []
    assert "no document" in (plan.skip_reason or "")


# ------------------------------------------------------------- stop rule --

@pytest.mark.parametrize("missing", [
    "from_priority_source", "has_exact_quote", "complete", "unambiguous",
])
def test_every_trusted_value_condition_is_individually_required(missing: str) -> None:
    kwargs = {
        "from_priority_source": True, "has_exact_quote": True,
        "complete": True, "unambiguous": True,
    }
    assert selection.is_trusted_value(**kwargs)
    kwargs[missing] = False
    assert not selection.is_trusted_value(**kwargs)


def test_only_critical_fields_buy_a_second_source_and_only_one() -> None:
    config = _config()
    critical = next(
        row for row in config["fields"] if row.get("critical_conflict_field")
    )
    ordinary = next(
        row for row in config["fields"]
        if not row.get("critical_conflict_field")
    )
    assert selection.comparison_budget(critical, config) == 1
    assert selection.comparison_budget(ordinary, config) == 0


# ------------------------------------------------------------- checklist --

def test_checklist_reports_presence_without_reading_cost_documents() -> None:
    config = _config()
    documents = _docs(
        ("DOC_001", "diagnosis_certificate"),
        ("DOC_002", "medical_expense_receipt"),
    )
    items = selection.presence_checklist(config, documents, ["traffic_accident"])
    by_kind = {row["document_kind"]: row for row in items}

    assert by_kind["medical_expense_receipt"]["status"] == "available"
    assert by_kind["medical_expense_receipt"]["document_ids"] == ["DOC_002"]
    # Required for traffic accident but absent from the case.
    assert by_kind["pharmacy_payment_confirmation"]["status"] == "missing"


def test_checklist_distinguishes_ambiguous_from_missing() -> None:
    config = _config()
    documents = [selection.DocumentRef("DOC_009", None, ambiguous=True)]
    items = selection.presence_checklist(config, documents, ["personal_insurance"])
    assert {row["status"] for row in items} == {"ambiguous"}


# ------------------------------------------------------- case-type assess --

def _asserted(field_id: str, value, *, status: str = "asserted") -> dict:
    return {
        "field_id": field_id,
        "domain_code": "event_timeline",
        "priority_grade": "B",
        "authority": "claim_analysis_native",
        "resolution_status": status,
        "selected_observation_ids": ["CAO_0001"],
        "observations": [{
            "observation_id": "CAO_0001",
            "value_state": "asserted",
            "value": value,
            "source_document_kind": "initial_visit_record",
            "source_priority_rank": 1,
            "extraction_wave": "B",
            "evidence_references": [{
                "document_id": "DOC_001", "page": 1,
                "quote": "q", "start_char": 0, "end_char": 1,
            }],
        }],
        "conflict_candidate_ids": [],
        "stop_reason": "trusted_value_found",
    }


def test_four_types_are_judged_independently_and_may_all_apply() -> None:
    facts = [
        _asserted("injury_event_present", True),
        _asserted("vehicle_involvement", True),
        _asserted("work_activity_context", "work_activity"),
        _asserted("facility_defect_or_third_party_responsibility", "난간 파손"),
    ]
    result = case_types.assess_case_types(facts)
    assert [row["case_type"] for row in result] == list(case_types.CASE_TYPES)
    assert {row["status"] for row in result} == {"applicable"}


def test_silence_never_becomes_not_applicable() -> None:
    result = case_types.assess_case_types([_asserted("injury_event_present", True)])
    by_type = {row["case_type"]: row for row in result}
    assert by_type["personal_insurance"]["status"] == "applicable"
    for case_type in ("traffic_accident", "industrial_accident", "liability"):
        assert by_type[case_type]["status"] == "uncertain"
        assert by_type[case_type]["evidence_references"] == []


def _unresolved(field_id: str, status: str = "unavailable") -> dict:
    """A field that WAS searched and came back without a value.

    Distinct from the field being absent from `claim_facts` altogether: this is
    the shape the driver emits after exhausting a route, and it is the one that
    most invites being read as a negative finding.
    """
    return {
        "field_id": field_id,
        "domain_code": "event_timeline",
        "priority_grade": "B",
        "authority": "claim_analysis_native",
        "resolution_status": status,
        "selected_observation_ids": [],
        "observations": [],
        "conflict_candidate_ids": [],
        "stop_reason": "sources_exhausted",
        "resolution_reason": "no source states the fact",
        "unavailable_reason": "not_mentioned",
    }


def _field_not_applicable(field_id: str) -> dict:
    """A field the case's own facts exclude, carrying the fact that excludes it.

    Under the five-state contract `not_applicable` is a grounded verdict, not a
    synonym for "nothing found" -- so this shape must cite evidence, unlike
    `_unresolved`.
    """
    return {
        "field_id": field_id,
        "domain_code": "event_timeline",
        "priority_grade": "B",
        "authority": "claim_analysis_native",
        "resolution_status": "not_applicable",
        "selected_observation_ids": [],
        "observations": [{
            "observation_id": "CAO_0001",
            "value_state": "not_applicable",
            "reason": "the case facts exclude this field",
            "extraction_wave": "B",
            "evidence_references": [{
                "document_id": "DOC_001", "page": 1,
                "quote": "q", "start_char": 0, "end_char": 1,
            }],
        }],
        "conflict_candidate_ids": [],
        "stop_reason": "not_applicable",
        "resolution_reason": "excluded by the case facts",
    }


@pytest.mark.parametrize("status", ["unavailable", "not_applicable"])
@pytest.mark.parametrize("field_id,case_type", [
    ("vehicle_involvement", "traffic_accident"),
    ("facility_defect_or_third_party_responsibility", "liability"),
    ("injury_event_present", "personal_insurance"),
    ("work_activity_context", "industrial_accident"),
])
def test_an_exhausted_search_is_uncertain_not_a_negative_finding(
    status: str, field_id: str, case_type: str
) -> None:
    field = (_unresolved(field_id) if status == "unavailable"
             else _field_not_applicable(field_id))
    result = case_types.assess_case_types([field])
    by_type = {row["case_type"]: row for row in result}
    assert by_type[case_type]["status"] == "uncertain"
    assert by_type[case_type]["evidence_references"] == []


def test_explicit_negative_is_the_only_route_to_not_applicable() -> None:
    result = case_types.assess_case_types([
        _asserted("injury_event_present", True),
        _asserted("vehicle_involvement", False),
    ])
    by_type = {row["case_type"]: row for row in result}
    assert by_type["traffic_accident"]["status"] == "not_applicable"


def test_commute_and_business_trip_stay_uncertain_for_industrial_accident() -> None:
    for value in sorted(case_types.WORK_UNCERTAIN):
        result = case_types.assess_case_types(
            [_asserted("work_activity_context", value)]
        )
        by_type = {row["case_type"]: row for row in result}
        assert by_type["industrial_accident"]["status"] == "uncertain", value


def test_a_conflicted_trigger_field_downgrades_the_type_to_uncertain() -> None:
    conflicted = _asserted("vehicle_involvement", True, status="conflict")
    result = case_types.assess_case_types([conflicted])
    by_type = {row["case_type"]: row for row in result}
    assert by_type["traffic_accident"]["status"] == "uncertain"
    assert by_type["traffic_accident"]["conflicting_field_ids"] == [
        "vehicle_involvement"
    ]


def test_filing_status_defaults_to_unknown_and_is_never_inferred() -> None:
    result = case_types.assess_case_types([_asserted("injury_event_present", True)])
    assert {row["filing_status"] for row in result} == {"unknown"}

    result = case_types.assess_case_types(
        [_asserted("injury_event_present", True)],
        filing_status_by_type={"personal_insurance": "filed"},
    )
    by_type = {row["case_type"]: row for row in result}
    assert by_type["personal_insurance"]["filing_status"] == "filed"
    assert by_type["traffic_accident"]["filing_status"] == "unknown"


def test_supported_always_carries_a_triggered_field_and_exact_evidence() -> None:
    result = case_types.assess_case_types([
        _asserted("injury_event_present", True),
        _asserted("vehicle_involvement", True),
    ])
    for row in result:
        if row["status"] == "supported":
            assert row["triggered_field_ids"]
            assert row["evidence_references"]
            for reference in row["evidence_references"]:
                assert {"document_id", "page", "quote", "start_char", "end_char"} <= (
                    reference.keys()
                )


def test_checklist_scope_includes_uncertain_types_but_drops_negatives() -> None:
    result = case_types.assess_case_types([
        _asserted("injury_event_present", True),
        _asserted("vehicle_involvement", False),
    ])
    scoped = case_types.selected_case_types(result)
    assert "personal_insurance" in scoped
    assert "industrial_accident" in scoped
    assert "traffic_accident" not in scoped
