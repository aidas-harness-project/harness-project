"""A real disagreement must never render as settled fact, and a field that was
read must not be reported as unroutable.

Two defects found on CASE_704 (2026-08-21), both the same shape: an upstream
stage made a three-way distinction and the downstream contract could only carry
two, so the finer information was lost silently.

**1. `not_material` was written as `consistent`.** `consistency-check.md`
defines three outcomes -- `confirmed` (a contradiction), `not_material` (a REAL
difference that changes no decision), `withdrawn` (not a contradiction at all).
`run_consistency_check.py` wrote `"inconsistent" if confirmed else
"consistent"`, so `not_material` and `withdrawn` became the same value. Stage 7
promotes a `consistent` field's first observation to a settled value, so
section 4 of CASE_704's report printed DOC_006's "the victim walked normally"
as established fact while DOC_008 p.4 stated the accident arose from the
victim's carelessness -- and the reader saw only the first.

**2. A field stage 3-a read was reported unroutable.** The common pass stamps a
route-less field `source_document_missing` / "the field is not routed to any
document source", which is true of the MEDICAL routes. Stage 3-a then opens the
`legal_opinion` and `insurer_response` documents that
`additional_fields_by_case_type` names, and nothing rewrote the stale reason.
On CASE_704 `comparative_negligence_rate` told a 손해사정사 the field was
unroutable while DOC_008 carried a whole 「피해자의 과실비율」 section that
declined to state a rate -- a records finding, not a routing defect.

The regression guard for the class, not just these two instances, is
`test_every_agent_outcome_reaches_a_distinct_result` below: it asserts the
three outcomes stay three values downstream.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import run_screening_report as screening  # noqa: E402


def _check(candidate_id, result, conflict_id=None):
    return {"check_id": "CHK-1", "conflict_candidate_id": candidate_id,
            "field_id": "negligence_reasoning", "topic": "t",
            "values_compared": [], "result": result,
            "conflict_id": conflict_id}


# --- 1. a contested field is not promoted ----------------------------------

def _fact(status, candidates):
    return {"negligence_reasoning": {
        "field_id": "negligence_reasoning",
        "resolution_status": status,
        "conflict_candidate_ids": candidates,
        "selected_observation_ids": [],
        "observations": [
            {"observation_id": "CAO_1", "value": "피해자에게 과실이 없음",
             "value_state": "asserted"},
            {"observation_id": "CAO_2", "value": "피해자의 부주의로 발생",
             "value_state": "asserted"},
        ],
    }}


def test_withdrawn_still_promotes():
    """The CASE_049 fix must keep working: a wording variance resolves."""
    resolved = screening.resolve_withdrawn_conflicts(
        _fact("conflict", ["CAC_1"]),
        {"checks": [_check("CAC_1", "consistent")]})
    field = resolved["negligence_reasoning"]
    assert field["resolution_status"] == "asserted"
    assert field["selected_observation_ids"] == ["CAO_1"]


def test_contested_not_decisive_does_not_promote():
    """`not_material` is a real disagreement. Electing one reading prints a
    contested claim as fact -- the CASE_704 section-4 defect."""
    resolved = screening.resolve_withdrawn_conflicts(
        _fact("conflict", ["CAC_1"]),
        {"checks": [_check("CAC_1", "contested_not_decisive")]})
    field = resolved["negligence_reasoning"]
    assert field["resolution_status"] == "conflict", (
        "a materially-contested field was promoted to a settled value")
    assert field["selected_observation_ids"] == []


def test_confirmed_does_not_promote():
    resolved = screening.resolve_withdrawn_conflicts(
        _fact("conflict", ["CAC_1"]),
        {"checks": [_check("CAC_1", "inconsistent", "CONFLICT_1")]})
    assert resolved["negligence_reasoning"]["resolution_status"] == "conflict"


def test_every_agent_outcome_reaches_a_distinct_result():
    """The guard for the CLASS: three outcomes, three downstream values.

    This project has lost an upstream distinction to a narrower downstream
    contract repeatedly. If a fourth outcome is added to the agent's
    vocabulary, this fails until the mapping carries it too.

    Asserted by CALLING the mapping, not by searching the source: the words
    `confirmed` and `not_material` appear in that module's docstring whatever
    the code does, so a source search passes even with the collapse restored.
    """
    import run_consistency_check as helper

    verdicts = [
        {"conflict_candidate_id": "CAC_1", "outcome": "confirmed",
         "professional_summary": "요약", "candidate_digest": "d1"},
        {"conflict_candidate_id": "CAC_2", "outcome": "not_material",
         "candidate_digest": "d2"},
        {"conflict_candidate_id": "CAC_3", "outcome": "withdrawn",
         "candidate_digest": "d3"},
    ]
    items = [
        {"conflict_candidate_id": f"CAC_{n}", "field_id": "negligence_reasoning",
         "field_label": "과실 판단 근거", "documents": ["DOC_006", "DOC_008"],
         "readings": [], "candidate_digest": f"d{n}"}
        for n in (1, 2, 3)
    ]
    contract = helper.build_contract(
        verdicts=verdicts, work_items=items,
        registered={"CAC_1": "CONFLICT_1"},
        case_id="CASE_X", run_id="RUN_X")
    results = {c["conflict_candidate_id"]: c["result"]
               for c in contract["checks"]}
    assert results["CAC_1"] == "inconsistent"
    assert results["CAC_3"] == "consistent"
    assert results["CAC_2"] == "contested_not_decisive", (
        "not_material collapsed into consistent, so a real disagreement "
        "becomes indistinguishable from a wording variance and stage 7 "
        "promotes one side of it to settled fact")

    schema = json.loads(
        (ROOT / "schemas" / "evidence_validation_result.schema.json").read_text(
            encoding="utf-8"))
    enum = schema["$defs"]["check"]["properties"]["result"]["enum"]
    assert set(enum) == set(results.values()), (
        "the agent judges three outcomes; the contract must carry three")


def test_the_schema_keeps_conflict_id_null_for_a_contested_check():
    """Only a confirmed check owns a ledger entry."""
    schema = json.loads(
        (ROOT / "schemas" / "evidence_validation_result.schema.json").read_text(
            encoding="utf-8"))
    rules = schema["$defs"]["check"]["allOf"]
    contested = [r for r in rules
                 if r["if"]["properties"]["result"].get("const")
                 == "contested_not_decisive"]
    assert contested, "no rule pins conflict_id for contested_not_decisive"
    assert contested[0]["then"]["properties"]["conflict_id"]["const"] is None


# --- 2. a field that was read is not called unroutable ----------------------

def test_a_field_the_extra_round_read_is_not_source_document_missing():
    """Stage 3-a opens legal_opinion / insurer_response for the liability
    fields, so `source_document_missing` on one of them is stale from the
    common pass, not what happened."""
    source = (ROOT / "tools" / "run_claim_analysis_selective.py").read_text(
        encoding="utf-8")
    assert "eligible_field_ids" in source, (
        "the merge must consult which fields the extra round asked for")
    assert 'outcome.unavailable_reason = "not_mentioned"' in source, (
        "a field that was read and found silent is not_mentioned")


def test_the_config_routes_the_negligence_fields_to_real_documents():
    """The premise of the fix: these fields DO have sources, just not medical
    ones. If this ever stops being true the reason string above is wrong."""
    config = json.loads(
        (ROOT / "config" / "claim_analysis"
         / "claim_analysis_routing_v0.1.json").read_text(encoding="utf-8"))
    liability = config["additional_fields_by_case_type"]["liability"]
    assert "comparative_negligence_rate" in liability["field_ids"]
    assert set(liability["sources"]) >= {"legal_opinion", "insurer_response"}
