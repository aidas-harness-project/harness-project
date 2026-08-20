"""Stage 7 applies consistency_check's withdrawals at read time.

The defect these cover, measured on CASE_049 (2026-08-20): claim_analysis put
`primary_diagnosis` in `conflict` because a 진단서 said "우측 손목 요골 원위부
골절" and a 경과기록지 said "Fx. distal radius, wrist, Rt.". The
consistency-check agent withdrew all three candidates -- same fact, two
languages -- but nothing writes that back into `claim_analysis_result.json`,
whose only writer is stage 5. The field stayed `conflict` forever and the
report printed `주요 진단명: 확인 불가` two lines above `진단코드: S6280`, read
from the same sentence of the same page.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import run_screening_report as rsr


def _field(status, candidates, observations):
    return {
        "field_id": "primary_diagnosis",
        "resolution_status": status,
        "conflict_candidate_ids": list(candidates),
        "selected_observation_ids": [],
        "observations": list(observations),
    }


OBSERVATIONS = [
    {"observation_id": "CAO_1", "value": "우측 손목 요골 원위부 골절",
     "evidence_references": [{"document_id": "DOC_011", "page": 1,
                              "quote": "우측 손목 요골 원위부 골절"}]},
    {"observation_id": "CAO_2", "value": "Fx. distal radius, wrist, Rt.",
     "evidence_references": [{"document_id": "DOC_013", "page": 1,
                              "quote": "Xray> Fx. distal radius, wrist, Rt."}]},
]


def _check(result, conflict_id, candidate="CAC_1"):
    return {"checks": [{
        "check_id": "CHK-1", "conflict_candidate_id": candidate,
        "field_id": "primary_diagnosis", "result": result,
        "conflict_id": conflict_id, "topic": "t", "values_compared": [],
    }]}


def test_withdrawn_conflict_publishes_the_value_and_its_citation():
    facts = {"primary_diagnosis": _field("conflict", ["CAC_1"], OBSERVATIONS)}
    out = rsr.resolve_withdrawn_conflicts(facts, _check("consistent", None))

    field = out["primary_diagnosis"]
    assert field["resolution_status"] == "asserted"
    assert field["selected_observation_ids"] == ["CAO_1"]
    # The value the report prints, and the citation behind it -- reintroducing
    # the defect makes _first_value return None and this assertion fail.
    assert rsr._first_value(out, "primary_diagnosis") == "우측 손목 요골 원위부 골절"
    assert rsr._selected_evidence(field) == [
        {"document_id": "DOC_011", "page": 1, "quote": "우측 손목 요골 원위부 골절"}]
    assert field["conflict_resolution"]["outcome"] == "withdrawn"


def test_a_confirmed_conflict_is_never_promoted():
    """The safety property: a real disagreement must not be silently erased."""
    facts = {"primary_diagnosis": _field("conflict", ["CAC_1"], OBSERVATIONS)}
    out = rsr.resolve_withdrawn_conflicts(
        facts, _check("inconsistent", "CONFLICT_1"))

    assert out["primary_diagnosis"]["resolution_status"] == "conflict"
    assert out["primary_diagnosis"]["selected_observation_ids"] == []
    assert rsr._first_value(out, "primary_diagnosis") is None


def test_a_field_is_promoted_only_when_every_candidate_was_withdrawn():
    facts = {"primary_diagnosis": _field(
        "conflict", ["CAC_1", "CAC_2"], OBSERVATIONS)}
    # only CAC_1 was withdrawn; CAC_2 is still open
    out = rsr.resolve_withdrawn_conflicts(facts, _check("consistent", None))
    assert out["primary_diagnosis"]["resolution_status"] == "conflict"


def test_upstream_facts_are_not_mutated():
    """Stage 5 stays the only writer of claim_analysis_result.json."""
    facts = {"primary_diagnosis": _field("conflict", ["CAC_1"], OBSERVATIONS)}
    rsr.resolve_withdrawn_conflicts(facts, _check("consistent", None))
    assert facts["primary_diagnosis"]["resolution_status"] == "conflict"
    assert facts["primary_diagnosis"]["selected_observation_ids"] == []


def test_no_withdrawals_changes_nothing():
    facts = {"primary_diagnosis": _field("conflict", ["CAC_1"], OBSERVATIONS)}
    assert rsr.resolve_withdrawn_conflicts(facts, {"checks": []}) == dict(facts)


def test_a_check_without_a_candidate_id_is_ignored():
    """Contracts written before the identifier existed must not promote."""
    facts = {"primary_diagnosis": _field("conflict", ["CAC_1"], OBSERVATIONS)}
    legacy = {"checks": [{"check_id": "CHK-1", "result": "consistent",
                          "conflict_id": None, "topic": "CAC_1: withdrawn",
                          "values_compared": []}]}
    out = rsr.resolve_withdrawn_conflicts(facts, legacy)
    assert out["primary_diagnosis"]["resolution_status"] == "conflict"
