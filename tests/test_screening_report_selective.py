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


# --- markdown_sections must satisfy document_assembly's 1:1 tag rule -------
# Measured on CASE_489 (2026-08-20). `document_assembly.render` requires each
# section's `{{E}}` placeholder count to equal its `evidence_references` count,
# so it can substitute `[E#]` tags and emit the sidecar from one source -- the
# P1 mechanism that stops a tag and its citation drifting apart.
#
# `markdown_sections` collected the references but printed its bullets with no
# placeholders, so the very first section arrived with 0 placeholders and 1
# reference and assembly refused the whole report:
#
#   Section '1. 사고와 공통 의료정보': 0 {{E}} placeholders but
#   1 evidence_references -- these must match 1:1.
#
# The suite had 79 passing screening tests and none caught it: they assert on
# individual sections returned by `markdown_sections`, while the rule lives in
# `document_assembly`. The contract between the two tools sat in the gap
# between two test files. This test spans it.

def _sections_for_a_populated_report():
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency(),
        config=_config())
    return reporter.markdown_sections(report)


def test_every_section_pairs_one_placeholder_with_one_reference():
    for section in _sections_for_a_populated_report():
        placeholders = section["content"].count("{{E}}")
        references = len(section.get("evidence_references", []))
        assert placeholders == references, (
            f"section {section['heading']!r}: {placeholders} placeholders vs "
            f"{references} references -- document_assembly refuses this")


def test_the_sections_render_through_document_assembly(tmp_path):
    """The real gate, not a restatement of it: run the sections through
    document_assembly's own renderer."""
    import document_assembly
    spec = {"output_path": str(tmp_path / "screening_report.md"),
            "sections": _sections_for_a_populated_report()}
    doc_text, sidecar = document_assembly.render(spec)
    assert doc_text, "assembly produced no document"
    # every generated tag is backed by a sidecar citation
    import re
    tags = set(re.findall(r"\[E(\d+)\]", doc_text))
    assert len(tags) == len(sidecar.get("citations", sidecar.get("evidence", [])))


# --- the checklist must say WHICH document, not just that one exists -------
# Raised on CASE_489 (2026-08-20): section 5 rendered "진단서: 보유
# (산재/근재, 배상책임, ...)" and nothing else, so a 손해사정사 reading the
# report learns a diagnosis certificate exists but not which document to open.
# The ids were already in the contract -- `required_document_checklist` carries
# `document_ids: ["DOC_011"]` -- and only the rendering dropped them.

def test_the_checklist_names_the_documents_it_says_are_held():
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency(),
        config=_config())
    rows = report["required_document_checklist"]
    held = [row for row in rows if row.get("document_ids")]
    if not held:
        pytest.skip("fixture has no held document to name")
    section = next(s for s in reporter.markdown_sections(report)
                   if s["heading"].startswith("5."))
    for row in held:
        for document_id in row["document_ids"]:
            assert document_id in section["content"], (
                f"{row['document_label']} is reported as held but "
                f"{document_id} is not named")


# --- every asserted fact must reach the report ----------------------------
# Raised on CASE_489 (2026-08-20). claim_analysis resolved 19 fields with
# values -- surgery name (OR/IF c plate & screws), admission period
# (2023-12-04~12-07), disability type (부전강직), injury site and laterality
# (Rt. wrist), department (정형외과), joint range of motion -- and the screening
# report printed TWO of them. `SUMMARY_FACT_FIELDS` whitelisted four slots
# (accident_date, primary_diagnosis, diagnosis_code, treatment_period) and
# section 4 printed the diagnosis plus a COUNT of unconfirmed items, so
# everything outside those slots stayed in the contract and never reached the
# 손해사정사 reading the document.
#
# Widening the whitelist would repeat the defect the next time a field is
# added. The rule is the invariant instead: a fact claim analysis ASSERTED --
# with a value and a citation -- appears somewhere in the rendered report.
# Grouping is by `domain_code`, which the routing config already defines and
# every fact already carries.

def test_every_asserted_fact_value_appears_in_the_rendered_report():
    # Mirrors CASE_489's real shape: facts asserted across several domains,
    # only two of which the whitelist happened to cover.
    facts = [
        _fact("primary_diagnosis", "diagnosis", "우측 손목 요골 원위부 골절",
              status="asserted"),
        _fact("diagnosis_code", "diagnosis", "S6280", status="asserted"),
        _fact("diagnosis_department", "diagnosis", "정형외과", status="asserted"),
        _fact("surgery_or_procedure_name", "treatment",
              "OR/IF c plate & screws", status="asserted"),
        _fact("injury_site_and_laterality", "diagnosis_basis", "Rt. wrist",
              status="asserted"),
        _fact("disability_type", "disability", "부전강직", status="asserted"),
        _fact("accident_mechanism", "event_timeline", "전일 slip down",
              status="asserted"),
    ]
    analysis = _claim_analysis(facts=facts)
    report = reporter.build_report(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=analysis, consistency=_consistency(),
        config=_config())
    rendered = "\n".join(
        section["content"] for section in reporter.markdown_sections(report))

    missing = []
    for fact in analysis["claim_facts"]:
        if fact.get("resolution_status") != "asserted":
            continue
        selected = set(fact.get("selected_observation_ids") or [])
        for observation in fact.get("observations") or []:
            if observation.get("observation_id") not in selected:
                continue
            value = observation.get("value")
            text = (", ".join(str(item) for item in value)
                    if isinstance(value, list) else str(value))
            if text and text not in rendered:
                missing.append(f"{fact['field_id']}={text[:40]}")
    assert not missing, (
        "claim analysis asserted these values and the report does not show "
        f"them: {missing}")


# --- the judgement contract is not the report ------------------------------
# Measured on CASE_489 (2026-08-20). `_cross_contract` dispatched by
# `base.startswith("screening_report")`, so `screening_report_judgement.json`
# was routed into `check_screening_report` and refused for lacking
# `source_denial_contract_hash` -- a field only the derived REPORT carries,
# because only the report restates the insurer's decisions. The agent's
# judgement (key_issues, review_points, conflict_severity) derives from nothing
# upstream and cannot record such a hash, so the write was unsatisfiable: the
# lane's agent half could not be published at all.

def test_the_judgement_contract_is_not_validated_as_the_report(tmp_path):
    import _cross_contract
    # A denial contract must be present, or the staleness check never runs and
    # the test passes for the wrong reason.
    (tmp_path / "denial_reason_result.json").write_text(json.dumps({
        "case_id": "CASE_9001", "denial_reasons": [
            {"reason_id": "DR_1", "decision_type": "denial",
             "taxonomy_code": "R04", "policy_matches": []}],
    }), encoding="utf-8")
    judgement = {
        "case_id": "CASE_9001", "run_id": "RUN_20260819_1",
        "component": "screening-report", "status": "success",
        "created_at": "2026-08-20T00:00:00+00:00",
        "key_issues": [], "review_points": [],
    }
    errors = _cross_contract.check(
        "screening_report_judgement.json", judgement, tmp_path, None)
    assert not any("source_denial_contract_hash" in error for error in errors), (
        f"the judgement was validated as the report: {errors}")
