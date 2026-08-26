"""The screening report's reading half, produced in code rather than by an agent.

`run_screening_report.py` derives everything mechanical; what it cannot derive
is which questions matter, who can answer them, and how serious a deferred
conflict is. That half arrived only from the `screening-report` subagent --
and, per `screening_report_judgement.schema.json`'s own history, usually did not
arrive at all: no run in this repository had ever supplied the contract, so
every selective report fell back to the helper's floor values while reporting
success.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import run_screening_report as rsr  # noqa: E402
import screening_judgement as sj  # noqa: E402
from _validation import load_registry, validate_instance  # noqa: E402

CLAIM_ANALYSIS = {
    "case_type_assessment": {"primary_type": "상해", "confidence": 0.8},
    "claim_facts": [
        {"field_id": "accident_date", "field_state": "asserted", "value": "2025-03-04"},
        {"field_id": "disability_rate", "field_state": "unavailable",
         "unavailable_reason": "records_gap"},
    ],
}
CONSISTENCY = {"checks": [
    {"check_id": "CHK-1", "result": "inconsistent", "description": "부위 불일치"}]}
DENIAL = {"denial_reasons": [
    {"reason_id": "DR_1", "category": "면책", "stated_reason": "고지의무 위반"}]}
DEFERRED = [{"conflict_id": "CONFLICT_1",
             "professional_summary": "진단서와 수술기록지의 부위가 다릅니다."}]

GOOD = {
    "key_issues": [{"title": "부위 불일치", "description": "어느 기록이 맞는지 확인 필요",
                    "review_required": True, "reviewer_role": "의사"}],
    "review_points": [{"point": "우측/좌측 중 어느 쪽이 맞습니까?",
                       "reviewer_role": "의사", "priority": "high",
                       "source_refs": [{"contract": "evidence_validation_result.json",
                                        "element_id": "CHK-1"}]}],
    "conflict_severity": {"CONFLICT_1": "high"},
}


# ------------------------------------------------------------ the schema --

def test_the_severity_map_is_narrowed_to_this_case_s_conflicts():
    # A severity for a conflict nobody deferred is a judgement about nothing,
    # so the transport shape gives it nowhere to go.
    schema = sj.output_schema(["CONFLICT_1", "CONFLICT_2"])
    severity = schema["properties"]["conflict_severity"]
    assert sorted(severity["properties"]) == ["CONFLICT_1", "CONFLICT_2"]
    assert severity["additionalProperties"] is False


def test_nothing_deferred_means_no_severity_property_at_all():
    schema = sj.output_schema([])
    assert "conflict_severity" not in schema["properties"]
    assert "conflict_severity" not in schema["required"]


def test_the_model_is_never_offered_an_id_field():
    # ISSUE_N / RP-N are bookkeeping an expert review binds its answers to, not
    # judgement. The driver assigns them.
    schema = sj.output_schema([])
    issue = schema["properties"]["key_issues"]["items"]
    point = schema["properties"]["review_points"]["items"]
    assert "issue_id" not in issue["properties"]
    assert "point_id" not in point["properties"]
    assert issue["additionalProperties"] is False
    assert point["additionalProperties"] is False


# ------------------------------------------------------------ the prompt --

def test_the_prompt_carries_the_contracts_and_the_deferred_summaries():
    prompt = sj.build_prompt(claim_analysis=CLAIM_ANALYSIS, consistency=CONSISTENCY,
                             denial=DENIAL, deferred_conflicts=DEFERRED)
    for token in ("accident_date", "records_gap", "CHK-1", "DR_1", "고지의무 위반",
                  "CONFLICT_1", "진단서와 수술기록지의 부위가 다릅니다."):
        assert token in prompt, f"missing from the prompt: {token!r}"


def test_the_prompt_opens_no_case_document():
    # The stage's inputs are the upstream contracts. Re-opening pages here
    # would let the screening report introduce a fact no contract states.
    prompt = sj.build_prompt(claim_analysis=CLAIM_ANALYSIS, consistency=CONSISTENCY,
                             denial=DENIAL, deferred_conflicts=DEFERRED)
    assert "page_chunks" not in prompt and "read-redacted-text" not in prompt


def test_a_case_with_no_denial_says_so_rather_than_omitting_it():
    prompt = sj.build_prompt(claim_analysis=CLAIM_ANALYSIS, consistency=CONSISTENCY,
                             denial=None, deferred_conflicts=[])
    assert "no denial contract" in prompt
    assert "none deferred" in prompt


# ----------------------------------------------------------- the binding --

def test_ids_are_assigned_in_order_by_the_driver():
    bound = sj.bind_judgement(GOOD, conflict_ids=["CONFLICT_1"])
    assert bound["key_issues"][0]["issue_id"] == "ISSUE_1"
    assert bound["review_points"][0]["point_id"] == "RP-1"


def test_an_id_in_the_response_cannot_survive():
    """Caught a real defect: the id was assigned BEFORE the response was
    spread in, so a response supplying its own `issue_id` overrode the
    driver's -- the opposite of the property the code claimed to hold."""
    forged = {
        **GOOD,
        "key_issues": [{**GOOD["key_issues"][0], "issue_id": "ISSUE_99"}],
    }
    forged_point = {**GOOD["review_points"][0], "point_id": "RP-99"}
    bound = sj.bind_judgement({**forged, "review_points": [forged_point]},
                              conflict_ids=["CONFLICT_1"])
    assert bound["key_issues"][0]["issue_id"] == "ISSUE_1"
    assert bound["review_points"][0]["point_id"] == "RP-1"


@pytest.mark.parametrize("raw,expected", [
    ({**GOOD, "key_issues": []}, "at least one issue"),
    ({**GOOD, "review_points": []}, "at least one point"),
    ({**GOOD, "conflict_severity": {}}, "missing: CONFLICT_1"),
    ({**GOOD, "conflict_severity": {"CONFLICT_1": "high", "CONFLICT_9": "low"}},
     "not deferred"),
])
def test_a_judgement_the_report_could_not_use_is_refused(raw, expected):
    with pytest.raises(ValueError) as excinfo:
        sj.bind_judgement(raw, conflict_ids=["CONFLICT_1"])
    assert expected in str(excinfo.value)


def test_an_empty_judgement_is_refused_not_accepted_as_a_floor():
    # The floor values are what every run silently used until now. A judgement
    # that decides nothing must fail rather than look like one that did.
    with pytest.raises(ValueError):
        sj.bind_judgement({"key_issues": [], "review_points": []}, conflict_ids=[])


def test_the_contract_validates():
    bound = sj.bind_judgement(GOOD, conflict_ids=["CONFLICT_1"])
    contract = sj.build_contract(
        case_id="CASE_999", run_id="RUN_20260826_001", judgement=bound,
        model_name="fixture:stub", created_at="2026-08-26T00:00:00+00:00")
    schemas, registry = load_registry()
    assert validate_instance(contract, "screening_report_judgement.schema.json",
                             schemas, registry) == []


# -------------------------------------------------------------- the step --

def test_an_existing_judgement_is_never_overwritten(monkeypatch):
    # A judgement in hand was made by whoever had the case in hand; replacing
    # it silently would discard a reading nobody asked to redo.
    monkeypatch.setattr(rsr, "require_open_attempt", lambda *a, **k: None)

    def fake_dao(args, allow_missing=False):
        if "check-conflicts-clear" in args:
            return {"pending": [], "deferred_to_report": []}
        if "screening_report_judgement.json" in args:
            return {"key_issues": [], "review_points": []}
        if "read-conflict-ledger" in args:
            return {"conflicts": []}
        return {}

    monkeypatch.setattr(rsr, "_dao_json", fake_dao)

    class Exploding:
        provider_name = "none"
        model_name = "none"

    with pytest.raises(RuntimeError) as excinfo:
        rsr.run(case_id="CASE_999", run_id="RUN_20260826_001", held_by="t",
                provider=Exploding())

    assert "already exists" in str(excinfo.value)


def test_without_a_provider_the_tool_behaves_exactly_as_before(monkeypatch):
    # Coexistence: the agent route must keep working untouched.
    seen = {}
    monkeypatch.setattr(rsr, "require_open_attempt", lambda *a, **k: None)
    monkeypatch.setattr(rsr, "produce_judgement",
                        lambda **kw: seen.setdefault("called", True))

    def fake_dao(args, allow_missing=False):
        if "check-conflicts-clear" in args:
            return {"pending": [], "deferred_to_report": []}
        return None if allow_missing else {"claim_facts": []}

    monkeypatch.setattr(rsr, "_dao_json", fake_dao)
    monkeypatch.setattr(rsr, "build_report", lambda **kw: (_ for _ in ()).throw(
        StopIteration("reached assembly")))

    with pytest.raises(StopIteration):
        rsr.run(case_id="CASE_999", run_id="RUN_20260826_001", held_by="t")

    assert "called" not in seen, "no provider call may happen without --judge"


def test_the_cli_gates_the_provider_call_behind_a_flag():
    import subprocess
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "run_screening_report.py"), "--help"],
        capture_output=True, text=True, timeout=60).stdout
    assert "--judge" in out and "--provider" in out and "--model" in out
