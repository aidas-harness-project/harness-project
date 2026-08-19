"""Selective Claim Analysis driver tests.

Synthetic only: version-controlled config/schemas plus in-memory documents.
Never reads source-cases, outputs, data, or ground truth.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance
import claim_analysis_selection as selection
import run_claim_analysis_selective as driver


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = (
    ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
)


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _errors(instance: dict, schema_name: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


def _revision() -> dict:
    return {
        "sha256": "a" * 64,
        "run_id": "RUN_20260819_1",
        "schema_version": "medical_variables.v0.1",
        "config_version": "medical_structuring.v0.1",
    }


# ------------------------------------------------------ document intake --

def test_missing_medical_block_is_untyped_not_non_medical() -> None:
    manifest = {"documents": [
        {"document_id": "DOC_001"},
        {"document_id": "DOC_002", "medical_classification": {
            "status": "not_medical", "kind": None}},
        {"document_id": "DOC_003", "medical_classification": {
            "status": "ambiguous", "kind": None}},
        {"document_id": "DOC_004", "medical_classification": {
            "status": "deterministic_title", "kind": "diagnosis_certificate"}},
    ]}
    refs = {ref.document_id: ref for ref in driver.classified_documents(manifest)}
    assert refs["DOC_001"].kind is None and not refs["DOC_001"].ambiguous
    assert refs["DOC_002"].kind is None
    assert refs["DOC_003"].ambiguous is True
    assert refs["DOC_004"].kind == "diagnosis_certificate"


# ------------------------------------------------- evidence verification --

def test_exact_range_is_located_only_when_the_quote_is_unique() -> None:
    text = "우측 요골 골절 진단. 우측 요골 골절 재확인."
    located = driver.locate_exact(text, "재확인")
    assert located == (text.index("재확인"), text.index("재확인") + 3)
    assert text[located[0]:located[1]] == "재확인"
    # Occurs twice -- refused rather than bound to the first occurrence.
    assert driver.locate_exact(text, "우측 요골 골절") is None
    assert driver.locate_exact(text, "없는문구") is None


def test_reference_offsets_round_trip_against_the_page_text() -> None:
    text = "환자는 2026-03-02 계단에서 넘어져 우측 요골 골절."
    reference = driver.build_reference("DOC_001", 1, "계단에서 넘어져", text)
    assert reference is not None
    assert text[reference["start_char"]:reference["end_char"]] == reference["quote"]


# ------------------------------------------------------------ stop rule --

def _extractor(answers):
    """Build an extract() that answers per (field_id, document_id)."""
    calls: list[tuple[str, str]] = []

    def extract(field_row, document_id, kind, rank):
        calls.append((field_row["field_id"], document_id))
        return answers.get((field_row["field_id"], document_id))

    return extract, calls


def test_search_stops_at_the_first_trusted_value_in_priority_order() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
        selection.DocumentRef("DOC_003", "outpatient_record"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    page_text = {
        ("DOC_001", 1): "진단명: 우측 요골 골절",
        ("DOC_002", 1): "진단명: 우측 요골 골절",
        ("DOC_003", 1): "진단명: 우측 요골 골절",
    }
    extract, calls = _extractor({
        ("primary_diagnosis", "DOC_001"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        ("primary_diagnosis", "DOC_002"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        ("primary_diagnosis", "DOC_003"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())

    assert outcome.status == "resolved"
    assert outcome.stop_reason == "trusted_value_found"
    # primary_diagnosis is critical, so exactly ONE comparison is bought --
    # the third-priority document is never opened.
    assert [doc for _, doc in calls] == ["DOC_001", "DOC_002"]
    assert outcome.comparisons == 1


def test_a_non_critical_field_never_opens_a_second_source() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "diagnosis_department"
    )
    assert not field_row["critical_conflict_field"]
    documents = [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    page_text = {("DOC_001", 1): "진료과: 정형외과", ("DOC_002", 1): "진료과: 정형외과"}
    extract, calls = _extractor({
        ("diagnosis_department", "DOC_001"): {
            "value": "정형외과", "page": 1, "quote": "정형외과"},
        ("diagnosis_department", "DOC_002"): {
            "value": "정형외과", "page": 1, "quote": "정형외과"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())
    assert outcome.status == "resolved"
    assert [doc for _, doc in calls] == ["DOC_001"]


def test_two_disagreeing_sources_preserve_both_and_elect_no_canonical() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    page_text = {
        ("DOC_001", 1): "진단명: 우측 요골 골절",
        ("DOC_002", 1): "진단명: 좌측 요골 골절",
    }
    extract, _ = _extractor({
        ("primary_diagnosis", "DOC_001"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        ("primary_diagnosis", "DOC_002"): {
            "value": "좌측 요골 골절", "page": 1, "quote": "좌측 요골 골절"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())

    assert outcome.status == "conflict"
    assert outcome.stop_reason == "conflict_found"
    assert outcome.canonical_ids == []
    assert len(outcome.observations) == 2
    assert {o["value"] for o in outcome.observations} == {
        "우측 요골 골절", "좌측 요골 골절"
    }


def test_silence_in_a_source_is_not_a_conflict() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    extract, _ = _extractor({
        ("primary_diagnosis", "DOC_001"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        # DOC_002 simply does not mention it.
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract,
        {("DOC_001", 1): "진단명: 우측 요골 골절"},
        driver._observation_id_sequence())
    assert outcome.status == "resolved"


def test_the_comparison_budget_holds_across_priority_groups() -> None:
    """One extra source total, not one per rung."""
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("D1", "diagnosis_certificate"),
        selection.DocumentRef("D2", "admission_discharge_summary"),
        selection.DocumentRef("D3", "initial_visit_record"),
        selection.DocumentRef("D4", "outpatient_record"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    assert len(plan.steps) == 4, "the fixture must span four priority rungs"

    page_text = {(d, 1): "우측 요골 골절" for d in ("D1", "D2", "D3", "D4")}
    extract, calls = _extractor({
        ("primary_diagnosis", d): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"}
        for d in ("D1", "D2", "D3", "D4")
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())
    assert [doc for _, doc in calls] == ["D1", "D2"]
    assert outcome.comparisons == 1


def test_a_silent_source_does_not_consume_the_comparison_budget() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("D1", "diagnosis_certificate"),
        selection.DocumentRef("D2", "admission_discharge_summary"),
        selection.DocumentRef("D3", "initial_visit_record"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    page_text = {(d, 1): "우측 요골 골절" for d in ("D1", "D2", "D3")}
    extract, calls = _extractor({
        # D1 says nothing; the trusted value and its one comparison come from
        # D2 and D3.
        ("primary_diagnosis", "D2"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        ("primary_diagnosis", "D3"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())
    assert [doc for _, doc in calls] == ["D1", "D2", "D3"]
    assert outcome.status == "resolved"
    assert outcome.comparisons == 1


def test_a_quote_absent_from_the_page_is_dropped_never_repaired() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    plan = selection.plan_field(
        field_row, config, [selection.DocumentRef("DOC_001", "diagnosis_certificate")])
    extract, _ = _extractor({
        ("primary_diagnosis", "DOC_001"): {
            "value": "우측 요골 골절", "page": 1, "quote": "이 문구는 페이지에 없다"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract,
        {("DOC_001", 1): "진단명: 우측 요골 골절"},
        driver._observation_id_sequence())
    assert outcome.status == "unknown"
    assert outcome.observations == []


# -------------------------------------------------------- result shape --

_OBSERVATION_IDS = driver._observation_id_sequence()


def _resolved_outcome(field_id: str, domain: str, value, grade="A") -> driver.FieldExtractionOutcome:
    # Observation IDs are unique case-wide, not per field: the semantic
    # validator rejects the same id appearing under two claim facts.
    observation_id = next(_OBSERVATION_IDS)
    outcome = driver.FieldExtractionOutcome(
        field_id=field_id, domain_code=domain, grade=grade)
    outcome.status = "resolved"
    outcome.stop_reason = "trusted_value_found"
    outcome.canonical_ids = [observation_id]
    outcome.observations = [{
        "observation_id": observation_id,
        "value_state": "asserted",
        "value": value,
        "source_document_kind": "diagnosis_certificate",
        "source_priority_rank": 1,
        "extraction_wave": "A",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": "q",
            "start_char": 0, "end_char": 1,
        }],
    }]
    outcome.documents_read = 1
    return outcome


def test_built_result_validates_against_the_canonical_schema() -> None:
    config = _config()
    outcomes = [
        _resolved_outcome("primary_diagnosis", "diagnosis", "우측 요골 골절"),
        _resolved_outcome("injury_event_present", "event_timeline", True, grade="C"),
    ]
    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=outcomes,
        config=config,
        documents=[selection.DocumentRef("DOC_001", "diagnosis_certificate")],
        medical_revision=_revision(),
    )
    assert _errors(result, "claim_analysis_result.schema.json") == []
    assert len(result["case_type_assessment"]) == 4


def test_medical_domain_facts_are_published_as_projections_not_native() -> None:
    config = _config()
    outcomes = [
        _resolved_outcome("primary_diagnosis", "diagnosis", "골절"),
        _resolved_outcome("injury_event_present", "event_timeline", True),
    ]
    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=outcomes,
        config=config, documents=[], medical_revision=_revision())
    by_field = {row["field_id"]: row for row in result["claim_facts"]}
    assert by_field["primary_diagnosis"]["authority"] == "medical_variables_projection"
    assert by_field["injury_event_present"]["authority"] == "claim_analysis_native"


def test_conflict_becomes_a_candidate_and_never_a_ledger_entry() -> None:
    config = _config()
    outcome = driver.FieldExtractionOutcome(
        field_id="primary_diagnosis", domain_code="diagnosis", grade="A")
    outcome.status = "conflict"
    outcome.stop_reason = "conflict_found"
    outcome.reason = "two sources disagree"
    outcome.observations = [{
        "observation_id": f"CAO_000{n}",
        "value_state": "asserted",
        "value": value,
        "source_document_kind": "diagnosis_certificate",
        "source_priority_rank": n,
        "extraction_wave": "A",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": "q",
            "start_char": 0, "end_char": 1,
        }],
    } for n, value in ((1, "우측"), (2, "좌측"))]

    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=[outcome],
        config=config, documents=[], medical_revision=_revision())

    assert _errors(result, "claim_analysis_result.schema.json") == []
    assert len(result["conflict_candidates"]) == 1
    candidate = result["conflict_candidates"][0]
    assert candidate["consistency_status"] == "pending_consistency_check"
    assert candidate["field_id"] == "primary_diagnosis"
    assert len(candidate["observation_ids"]) == 2
    fact = result["claim_facts"][0]
    assert fact["canonical_observation_ids"] == []
    assert fact["conflict_candidate_ids"] == [candidate["conflict_candidate_id"]]


def test_unknown_field_retains_no_asserted_observation() -> None:
    config = _config()
    outcome = driver.FieldExtractionOutcome(
        field_id="primary_diagnosis", domain_code="diagnosis", grade="A")
    outcome.observations = [{
        "observation_id": "CAO_0001",
        "value_state": "asserted",
        "value": "골절",
        "source_document_kind": "diagnosis_certificate",
        "source_priority_rank": 1,
        "extraction_wave": "A",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": "q",
            "start_char": 0, "end_char": 1,
        }],
    }]
    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=[outcome],
        config=config, documents=[], medical_revision=_revision())
    assert _errors(result, "claim_analysis_result.schema.json") == []
    assert result["claim_facts"][0]["observations"][0]["value_state"] == "unknown"


def test_c_grade_override_field_publishes_as_b_not_c() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "injury_event_present"
    )
    assert field_row["medical_advisory_grade"] == "C"
    assert driver.priority_grade_for(field_row) == "B"


# --------------------------------------------------------------- trace --

def test_trace_is_separate_from_the_result_and_validates() -> None:
    config = _config()
    outcomes = [_resolved_outcome("primary_diagnosis", "diagnosis", "골절")]
    trace = driver.build_trace(
        case_id="CASE_9001", run_id="RUN_20260819_1", config=config,
        outcomes=outcomes,
        document_dispositions=[
            {"document_id": "DOC_001",
             "medical_document_kind": "diagnosis_certificate",
             "disposition": "read", "wave": "A",
             "reason": "priority source for primary_diagnosis",
             "field_ids": ["primary_diagnosis"]},
            {"document_id": "DOC_002",
             "medical_document_kind": "medical_expense_receipt",
             "disposition": "presence_only", "wave": "not_read",
             "reason": "cost document -- presence only",
             "field_ids": []},
        ],
        provider_calls=1)
    assert _errors(trace, "claim_analysis_trace.schema.json") == []
    assert trace["metrics"]["documents_presence_only"] == 1
    assert trace["metrics"]["documents_read"] == 1
    # The authority contract must not carry reading telemetry.
    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=outcomes,
        config=config, documents=[], medical_revision=_revision())
    assert "documents" not in result and "field_stops" not in result


def test_unmeasured_token_counts_record_as_null_not_zero() -> None:
    config = _config()
    trace = driver.build_trace(
        case_id="CASE_9001", run_id="RUN_20260819_1", config=config,
        outcomes=[], document_dispositions=[], provider_calls=0)
    assert trace["metrics"]["input_tokens"] is None
    assert trace["metrics"]["wall_time_seconds"] is None


# ----------------------------------------------------------- activation --

def test_driver_refuses_to_run_while_the_feature_is_disabled() -> None:
    config = _config()
    assert config["behavior_enabled"] is False
    with pytest.raises(RuntimeError, match="behavior_enabled=false"):
        driver.require_enabled(config)


@pytest.mark.parametrize("missing", [
    "approved_by", "authority_role", "approved_at", "scope",
])
def test_activation_metadata_requires_every_field(missing: str) -> None:
    config = _config()
    config["behavior_enabled"] = True
    config["activation"] = {
        "approved_by": "홍길동",
        "authority_role": "손해사정사",
        "approved_at": "2026-08-19T09:00:00Z",
        "scope": "traumatic injury PoC",
    }
    assert _errors(config, "claim_analysis_routing_config.schema.json") == []
    del config["activation"][missing]
    assert _errors(config, "claim_analysis_routing_config.schema.json"), (
        f"activation without {missing} must not validate"
    )


def test_a_recorded_activation_survives_rollback_to_disabled() -> None:
    """Turning the feature off must not erase who approved it."""
    config = _config()
    config["activation"] = {
        "approved_by": "홍길동",
        "authority_role": "손해사정사",
        "approved_at": "2026-08-19T09:00:00Z",
        "scope": "traumatic injury PoC",
    }
    config["behavior_enabled"] = False
    assert _errors(config, "claim_analysis_routing_config.schema.json") == []


def test_enabled_config_requires_recorded_activation_metadata() -> None:
    config = _config()
    config["behavior_enabled"] = True
    assert _errors(config, "claim_analysis_routing_config.schema.json"), (
        "enabling without an activation block must not validate"
    )
    config["activation"] = {
        "approved_by": "reviewer",
        "authority_role": "손해사정사",
        "approved_at": "2026-08-19T00:00:00Z",
        "scope": "traumatic injury PoC",
    }
    assert _errors(config, "claim_analysis_routing_config.schema.json") == []
    driver.require_enabled(config)
