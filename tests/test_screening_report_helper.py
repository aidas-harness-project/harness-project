"""What the screening report may say, and on whose authority.

Three separations this file pins:

* The insurer's decision is REPORTED, not assumed. Hard-coding "no denial" was
  wrong in the one direction that matters -- the report is the first document a
  human reads, and a case where the insurer denied would have looked like one
  where it had not.
* A verified conflict is described in the sentence the consistency-check agent
  wrote when it had the evidence in hand. Regenerating it here would be this
  stage's second reading of a disagreement it never examined.
* Feasibility, difficulty, and payout likelihood are not produced at all. Placed
  beside evidenced facts in a practitioner's briefing they read with the same
  weight, and nothing in the records supports them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import run_screening_report as reporter
import claim_analysis_contracts as contracts
from _validation import load_registry, validate_instance


ROOT = Path(__file__).resolve().parent.parent
SELECTIVE_SCHEMA = "screening_report_selective.schema.json"


def _errors(instance: dict, schema: str = SELECTIVE_SCHEMA) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, schema, schemas, registry)


def _config() -> dict:
    return contracts.load_default_routing_config()


def _fact(field_id: str, domain: str, value, status: str = "asserted") -> dict:
    field = {
        "field_id": field_id,
        "domain_code": domain,
        "priority_grade": "B",
        "authority": "source_document_extraction",
        "resolution_status": status,
        "selected_observation_ids": ["CAO_0001"] if status == "asserted" else [],
        "observations": [],
        "conflict_candidate_ids": [],
        "stop_reason": ("trusted_value_found" if status == "asserted"
                        else "sources_exhausted"),
    }
    if status == "asserted":
        field["observations"] = [{
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
        }]
    else:
        field["resolution_reason"] = "no source stated this field"
        field["unavailable_reason"] = "not_mentioned"
    return field


def _claim_analysis() -> dict:
    return {
        "case_id": "CASE_9001",
        "run_id": "RUN_20260819_1",
        "medical_projection_status": "not_configured",
        "claim_facts": [_fact("primary_diagnosis", "diagnosis", "우측 요골 골절")],
        "case_type_assessment": [{
            "case_type": case_type,
            "status": "applicable" if case_type == "personal_insurance" else "uncertain",
            "triggered_field_ids": [],
            "conflicting_field_ids": [],
            "filing_status": "unknown",
            "reason": "reason",
            "evidence_references": [],
        } for case_type in (
            "personal_insurance", "traffic_accident",
            "industrial_accident", "liability")],
        "required_document_checklist": [],
        "conflict_candidates": [],
    }


def _consistency(conflict_id: str | None = "CONFLICT_1") -> dict:
    return {
        "checks": [{
            "check_id": "CHK-1",
            "topic": "diagnosis_laterality (CAC_0001): confirmed",
            "values_compared": [{"quote": "two sources disagree"}],
            "result": "inconsistent",
            "conflict_id": conflict_id,
        }],
    }


LEDGER_SUMMARY = (
    "진단서는 우측, 입퇴원요약은 좌측으로 기재되어 좌우가 다릅니다. "
    "어느 기록이 사고 부위를 반영하는지 확인이 필요합니다."
)


def _entry(*, summary: str | None = LEDGER_SUMMARY) -> dict:
    entry = {
        "conflict_id": "CONFLICT_1",
        "field_or_topic": "diagnosis_laterality",
        "sources": [
            {"document_id": "DOC_001", "page": 1,
             "value": "우측 요골 골절", "quote": "우측 요골 골절"},
            {"document_id": "DOC_002", "page": 1,
             "value": "좌측 요골 골절", "quote": "좌측 요골 골절"},
        ],
        "verdict": "pending",
    }
    if summary is not None:
        entry["professional_summary"] = summary
    return entry


def _report(**overrides):
    kwargs = dict(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        claim_analysis=_claim_analysis(), consistency=_consistency(),
        config=_config(), conflict_entries={"CONFLICT_1": _entry()},
    )
    kwargs.update(overrides)
    return reporter.build_report(**kwargs)


# ------------------------------------------------------- insurer position --

def _denial_contract() -> dict:
    return {
        "denial_reasons": [
            {"reason_id": "DR_1", "decision_type": "denial", "amount": 1_000_000},
            {"reason_id": "DR_2", "decision_type": "denial", "amount": 500_000},
            {"reason_id": "DR_3", "decision_type": "reduction", "amount": 200_000},
        ],
        "accepted_coverages": [{"accepted_coverage_id": "AC_1"}],
    }


def test_denial_reduction_and_acceptance_are_all_preserved() -> None:
    position = reporter.insurer_position(_denial_contract())
    assert position["has_denial"] is True
    assert position["has_reduction"] is True
    assert position["has_acceptance"] is True
    assert position["denial"]["reason_ids"] == ["DR_1", "DR_2"]
    assert position["reduction"]["reason_ids"] == ["DR_3"]
    assert position["acceptance"]["accepted_coverage_ids"] == ["AC_1"]


def test_a_reason_never_lands_in_the_wrong_section() -> None:
    """A reduction filed under denial inverts what the insurer decided."""
    position = reporter.insurer_position(_denial_contract())
    assert "DR_3" not in position["denial"]["reason_ids"]
    assert "DR_1" not in position["reduction"]["reason_ids"]
    assert set(position["denial"]["reason_ids"]) & set(
        position["reduction"]["reason_ids"]) == set()


def test_a_total_is_stated_only_when_every_amount_is() -> None:
    """A partial sum shown as a total understates what was withheld."""
    assert reporter.insurer_position(_denial_contract())["denial"]["total_amount"] == 1_500_000

    partial = _denial_contract()
    partial["denial_reasons"][1]["amount"] = None
    assert reporter.insurer_position(partial)["denial"]["total_amount"] is None


def test_false_only_when_there_is_no_insurer_response() -> None:
    position = reporter.insurer_position(None)
    assert position["has_denial"] is False
    assert position["has_reduction"] is False
    assert position["has_denial_or_reduction"] is False
    assert "has_acceptance" not in position


def test_the_report_carries_the_insurer_position() -> None:
    report = _report(denial_reasons=_denial_contract())
    assert report["insurer_position"]["has_denial"] is True
    assert report["insurer_position"]["denial"]["reason_ids"] == ["DR_1", "DR_2"]


# ----------------------------------------------------- conflict reporting --

def test_the_ledger_summary_is_carried_verbatim() -> None:
    row = _report()["inconsistencies"][0]
    assert row["description"] == LEDGER_SUMMARY
    assert row["summary_source"] == "consistency_check_professional_summary"


def test_a_legacy_entry_without_a_summary_shows_its_sources_instead() -> None:
    """No summary to carry means none is invented here."""
    row = _report(conflict_entries={"CONFLICT_1": _entry(summary=None)})["inconsistencies"][0]
    assert row["summary_source"] == "legacy_entry_without_summary"
    assert "우측 요골 골절" in row["description"]
    assert "좌측 요골 골절" in row["description"]
    assert row["description"] != LEDGER_SUMMARY


def test_both_source_values_survive_into_the_report() -> None:
    row = _report()["inconsistencies"][0]
    values = {source["value"] for source in row["source_values"]}
    assert values == {"우측 요골 골절", "좌측 요골 골절"}


def test_severity_comes_from_the_agent() -> None:
    row = _report(agent_judgement={
        "conflict_severity": {"CONFLICT_1": "medium"}})["inconsistencies"][0]
    assert row["severity"] == "medium"


def test_a_deferred_conflict_is_carried_by_conflict_ref() -> None:
    """finalize-stage refuses the report unless every deferred id appears."""
    report = _report(consistency={"checks": []},
                     deferred_conflict_ids=["CONFLICT_1"])
    refs = {row.get("conflict_ref") for row in report["inconsistencies"]}
    assert "CONFLICT_1" in refs
    assert report["inconsistencies"][0]["description"] == LEDGER_SUMMARY


def test_a_withdrawn_candidate_never_reaches_the_report() -> None:
    consistency = _consistency(conflict_id=None)
    consistency["checks"][0]["result"] = "consistent"
    assert _report(consistency=consistency)["inconsistencies"] == []


# ------------------------------------------------------- removed judgement --

def test_preliminary_assessment_is_not_required_by_the_selective_schema() -> None:
    schema = json.loads(
        (ROOT / "schemas" / SELECTIVE_SCHEMA).read_text(encoding="utf-8"))
    assert "preliminary_assessment" not in schema["allOf"][1]["required"]


def test_feasibility_and_difficulty_are_helper_constants() -> None:
    """Not an agent judgement, and not derived from the case either."""
    assessment = _report()["preliminary_assessment"]
    assert assessment["feasibility"] == "not_assessed"
    assert assessment["difficulty"] == "not_assessed"


def test_an_agent_cannot_supply_a_feasibility_verdict() -> None:
    """Even offered one, the helper does not accept it."""
    assessment = _report(agent_judgement={
        "preliminary_assessment": {"feasibility": "high", "difficulty": "low"},
    })["preliminary_assessment"]
    assert assessment["feasibility"] == "not_assessed"
    assert assessment["difficulty"] == "not_assessed"


def test_the_report_states_no_payout_or_amount_forecast() -> None:
    serialized = json.dumps(_report(denial_reasons=_denial_contract()),
                            ensure_ascii=False)
    for forbidden in ("예상 보험금", "지급 가능성", "청구 권고", "예상 손해액"):
        assert forbidden not in serialized


def test_development_detail_is_excluded() -> None:
    serialized = json.dumps(_report(), ensure_ascii=False)
    for forbidden in ("stop_reason", "withdrawn", "not_material",
                      "provider_calls", "input_tokens", "wall_time_seconds"):
        assert forbidden not in serialized


# ------------------------------------------------------------ agent scope --

def test_key_issues_and_review_points_come_from_the_agent() -> None:
    report = _report(agent_judgement={
        "key_issues": [{
            "issue_id": "ISSUE_1",
            "title": "좌우 불일치",
            "description": "사고 부위 좌우가 기록 간 다릅니다.",
            "review_required": True,
            "reviewer_role": "의사",
        }],
        "review_points": [{
            "point": "영상 판독으로 좌우를 확인해야 합니다.",
            "reviewer_role": "의사",
            "priority": "high",
        }],
    })
    assert report["key_issues"][0]["reviewer_role"] == "의사"
    assert report["review_points"][0]["reviewer_role"] == "의사"


def test_the_fallback_states_facts_rather_than_judgement() -> None:
    """With no agent input the report still names what is unresolved."""
    report = _report()
    assert report["key_issues"]
    assert report["review_points"]
    assert all(point.get("priority") for point in report["review_points"])


def test_the_selective_report_validates() -> None:
    assert _errors(_report(denial_reasons=_denial_contract())) == []
