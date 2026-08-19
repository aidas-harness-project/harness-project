"""The deterministic half of Consistency Check, on both sides of the agent.

The helper prepares work items and registers verdicts. What it must NOT do is
decide, and several tests below exist only to pin that: with no verdicts it
registers nothing, and it has no path to a verdict of its own.

The rest guard the binding between the two halves. A verdict is written about
specific readings; if those readings change, the verdict is about a different
disagreement and must be refused rather than quietly applied.
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

import run_consistency_check as helper
import claim_analysis_contracts as contracts
from _validation import load_registry, validate_instance


QUOTE_RIGHT = "우측 요골 골절"
QUOTE_LEFT = "좌측 요골 골절"


def _observation(number: int, value: str, quote: str, doc: str) -> dict:
    return {
        "observation_id": f"CAO_{number:04d}",
        "value_state": "asserted",
        "value": value,
        "source_document_kind": "diagnosis_certificate",
        "source_priority_rank": 1,
        "extraction_wave": "A",
        "evidence_references": [{
            "document_id": doc, "page": 1, "quote": quote,
            "start_char": 0, "end_char": len(quote),
        }],
    }


def _result(field_id: str = "diagnosis_laterality") -> dict:
    return {
        "case_id": "CASE_9001",
        "run_id": "RUN_20260819_1",
        "claim_facts": [{
            "field_id": field_id,
            "domain_code": "diagnosis",
            "priority_grade": "B",
            "authority": "source_document_extraction",
            "resolution_status": "conflict",
            "selected_observation_ids": [],
            "observations": [
                _observation(1, QUOTE_RIGHT, QUOTE_RIGHT, "DOC_001"),
                _observation(2, QUOTE_LEFT, QUOTE_LEFT, "DOC_002"),
            ],
            "conflict_candidate_ids": ["CAC_0001"],
            "stop_reason": "conflict_found",
            "resolution_reason": "two sources disagree",
        }],
        "conflict_candidates": [{
            "conflict_candidate_id": "CAC_0001",
            "field_id": field_id,
            "observation_ids": ["CAO_0001", "CAO_0002"],
            "reason": "two priority sources disagree",
            "consistency_status": "pending_consistency_check",
        }],
    }


def _config() -> dict:
    return contracts.load_default_routing_config()


def _items(result: dict | None = None) -> list[dict]:
    return helper.build_work_items(result or _result(), _config())


def _verdict(item: dict, outcome: str = "confirmed", **overrides) -> dict:
    verdict = {
        "conflict_candidate_id": item["conflict_candidate_id"],
        "candidate_digest": item["candidate_digest"],
        "outcome": outcome,
        "reason": "the two records state opposite sides",
    }
    if outcome == "confirmed":
        verdict["professional_summary"] = (
            f"진단서는 {QUOTE_RIGHT}, 입퇴원요약은 {QUOTE_LEFT}로 기재되어 "
            "좌우가 다릅니다. 어느 기록이 사고 부위를 반영하는지 확인이 필요합니다."
        )
    verdict.update(overrides)
    return verdict


# ------------------------------------------------------------- prepare --

def test_prepare_emits_no_verdict_field() -> None:
    """A prepared answer would make the agent's decision by suggestion."""
    items = _items()
    assert len(items) == 1
    assert set(items[0]) == {
        "conflict_candidate_id", "candidate_digest", "field_id",
        "field_label", "decision_bearing", "readings",
    }
    serialized = json.dumps(items, ensure_ascii=False)
    for word in ("confirmed", "not_material", "withdrawn", "outcome", "verdict"):
        assert word not in serialized


def test_prepare_reports_decision_bearing_as_config_fact() -> None:
    """Whether the FIELD is critical is config; whether THIS conflict matters is not."""
    assert _items()[0]["decision_bearing"] is True
    assert _items(_result("diagnosis_department"))[0]["decision_bearing"] is False


def test_prepare_carries_both_readings_with_their_evidence() -> None:
    readings = _items()[0]["readings"]
    assert [row["value"] for row in readings] == [QUOTE_RIGHT, QUOTE_LEFT]
    assert all(row["evidence_references"] for row in readings)


def test_workitems_contract_validates() -> None:
    contract = helper.build_workitems_contract(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        work_items=_items(), claim_analysis_sha256="a" * 64)
    schemas, registry = load_registry()
    assert validate_instance(
        contract, "consistency_check_workitems.schema.json", schemas, registry) == []


# --------------------------------------------------------------- binding --

def test_digest_changes_when_a_cited_reading_changes() -> None:
    """The binding has to cover the readings, not just the candidate id.

    A candidate whose id and field are unchanged but whose underlying value was
    re-read is a different disagreement.
    """
    before = _items()[0]["candidate_digest"]
    changed = _result()
    changed["claim_facts"][0]["observations"][1]["value"] = "양측 요골 골절"
    assert helper.build_work_items(changed, _config())[0]["candidate_digest"] != before


def test_stale_digest_is_refused() -> None:
    items = _items()
    stale = _verdict(items[0], candidate_digest="b" * 64)
    errors = helper.validate_verdicts([stale], items)
    assert any("digest mismatch" in e for e in errors)


def test_verdict_for_an_unprepared_candidate_is_refused() -> None:
    items = _items()
    orphan = _verdict(items[0])
    orphan["conflict_candidate_id"] = "CAC_9999"
    assert helper.validate_verdicts([orphan], items) != []


def test_the_same_candidate_may_not_be_judged_twice() -> None:
    items = _items()
    verdict = _verdict(items[0])
    assert helper.validate_verdicts([verdict, deepcopy(verdict)], items) != []


def test_operation_id_covers_both_the_candidate_and_its_content() -> None:
    """Idempotent on retry, distinct when the disagreement itself changed."""
    item = _items()[0]
    same = helper.operation_id_for(
        "CASE_9001", "RUN_20260819_1", item["conflict_candidate_id"], item["candidate_digest"])
    again = helper.operation_id_for(
        "CASE_9001", "RUN_20260819_1", item["conflict_candidate_id"], item["candidate_digest"])
    assert same == again
    assert item["conflict_candidate_id"] in same and item["candidate_digest"] in same

    changed = helper.operation_id_for(
        "CASE_9001", "RUN_20260819_1", item["conflict_candidate_id"], "c" * 64)
    assert changed != same


# ------------------------------------------------------ confirmed floors --

def test_confirmed_requires_a_professional_summary() -> None:
    items = _items()
    verdict = _verdict(items[0])
    del verdict["professional_summary"]
    assert helper.validate_verdicts([verdict], items) != []


def test_confirmed_requires_two_asserted_readings() -> None:
    """Silence on one side is not a contradiction, whatever the agent says."""
    result = _result()
    result["claim_facts"][0]["observations"][1] = {
        "observation_id": "CAO_0002",
        "value_state": "unavailable",
        "reason": "not stated",
        "unavailable_reason": "not_mentioned",
        "extraction_wave": "B",
        "evidence_references": [],
    }
    items = helper.build_work_items(result, _config())
    errors = helper.validate_verdicts([_verdict(items[0])], items)
    assert any("silence is not a contradiction" in e for e in errors)


def test_confirmed_requires_evidence_on_both_sides() -> None:
    result = _result()
    result["claim_facts"][0]["observations"][1]["evidence_references"] = []
    items = helper.build_work_items(result, _config())
    errors = helper.validate_verdicts([_verdict(items[0])], items)
    assert any("no exact evidence" in e for e in errors)


# ---------------------------------------------------- summary inspection --

def test_summary_must_state_both_values() -> None:
    items = _items()
    one_sided = _verdict(items[0], professional_summary=(
        f"진단서에 {QUOTE_RIGHT}로 기재되어 있습니다."))
    errors = helper.validate_verdicts([one_sided], items)
    assert any(QUOTE_LEFT in e for e in errors)


def test_summary_may_not_introduce_an_unsourced_date_or_amount() -> None:
    items = _items()
    invented = _verdict(items[0], professional_summary=(
        f"{QUOTE_RIGHT}와 {QUOTE_LEFT}가 다릅니다. 2026-03-02 수술 기록 확인 필요."))
    errors = helper.validate_verdicts([invented], items)
    assert any("2026-03-02" in e for e in errors)


@pytest.mark.parametrize("phrase", ["이 맞다", "로 확정", "채택합니다"])
def test_assertive_phrases_are_reported_with_the_matched_pattern(phrase: str) -> None:
    """The check is a limited denylist, and says so when it fires."""
    items = _items()
    picked = _verdict(items[0], professional_summary=(
        f"{QUOTE_RIGHT}와 {QUOTE_LEFT} 중 진단서{phrase}."))
    errors = helper.validate_verdicts([picked], items)
    assert any(repr(phrase) in e for e in errors)
    assert any("not a neutrality guarantee" in e for e in errors)


def test_a_clean_summary_passes_without_claiming_neutrality() -> None:
    items = _items()
    assert helper.validate_verdicts([_verdict(items[0])], items) == []


# ----------------------------------------------------- helper never judges --

def test_helper_registers_nothing_without_verdicts() -> None:
    assert helper.register_confirmed(
        "CASE_9001", "RUN_20260819_1", "consistency-check", [], _items()) == {}


def test_set_aside_verdicts_create_no_ledger_entry() -> None:
    items = _items()
    for outcome in ("not_material", "withdrawn"):
        assert helper.register_confirmed(
            "CASE_9001", "RUN_20260819_1", "consistency-check",
            [_verdict(items[0], outcome)], items) == {}


def test_audit_contract_records_the_set_aside_candidates_too() -> None:
    """The development trail keeps them; the screening report will not."""
    items = _items()
    contract = helper.build_contract(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        verdicts=[_verdict(items[0], "withdrawn")], registered={})
    assert contract["checks"][0]["result"] == "consistent"
    assert contract["checks"][0]["conflict_id"] is None
    schemas, registry = load_registry()
    assert validate_instance(
        contract, "evidence_validation_result.schema.json", schemas, registry) == []


def test_confirmed_check_points_at_its_ledger_entry() -> None:
    items = _items()
    contract = helper.build_contract(
        case_id="CASE_9001", run_id="RUN_20260819_1",
        verdicts=[_verdict(items[0])], registered={"CAC_0001": "CONFLICT_1"})
    assert contract["checks"][0]["result"] == "inconsistent"
    assert contract["checks"][0]["conflict_id"] == "CONFLICT_1"
    assert contract["review_required"] is True
