"""Phase 2's validation and rebuttal, as a driver.

The only converted stage with no deterministic half to build on. Most of what
is asserted here is what the driver REFUSES to let a model decide: the record of
what retrieval surfaced, whether a cited quote exists, and whether a verdict may
stand with no evidence behind it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import run_denial_validation as rdv  # noqa: E402
from _validation import load_registry, validate_instance  # noqa: E402

REF = {"document_id": "DOC_001", "page": 3, "quote": "2025-03-04 사고 발생"}
GOOD = {
    "verdict": "not_supported",
    "verdict_explanation": "기록상 고지 대상 병력이 확인되지 않아 면책 사유로 보기 어려운 측면이 있습니다.",
    "confidence": 0.7,
    "review_required": True,
    "reviewer_role": "손해사정사",
    "evidence_references": [REF],
    "policy_match_validations": [
        {"policy_match_id": "PM_1", "verification_status": "verified",
         "verification_explanation": "해당 약관 조항이 인용 위치에 존재합니다.",
         "review_required": False, "evidence_references": [REF]}],
}


MATCHES = [{"policy_match_id": "PM_1", "match_source": "insurer_cited"}]


def bind(raw=None, *, matches=None, chunks=("CH_1", "CH_2")):
    return rdv.bind_validation(raw or GOOD, reason_id="DR_1",
                               policy_matches=list(MATCHES if matches is None
                                                   else matches),
                               retrieved_chunk_ids=list(chunks))


# ------------------------------------------------- what the driver owns --

def test_retrieved_chunk_ids_come_from_the_driver_not_the_model():
    # The schema wants what retrieval SURFACED, not what got cited. Asking the
    # model would be asking it to describe its own prompt.
    bound = bind(chunks=("CH_1", "CH_2", "CH_3"))
    assert bound["retrieved_chunk_ids"] == ["CH_1", "CH_2", "CH_3"]
    assert "retrieved_chunk_ids" not in rdv.validation_schema(["PM_1"])["properties"]


def test_chunk_ids_are_computed_from_the_bundle_that_was_sent():
    chunks = {"chunks": [
        {"chunk_id": "CH_1", "document_id": "DOC_001"},
        {"chunk_id": "CH_2", "document_id": "DOC_002"},
        {"chunk_id": "CH_3", "document_id": "DOC_001"}]}
    assert rdv.chunk_ids_for(chunks, ["DOC_001"]) == ["CH_1", "CH_3"]


def test_the_rebuttal_id_is_assigned_in_order():
    point = rdv.bind_rebuttal(
        {"rebuttal_argument": "분쟁 대응 여지가 있습니다.", "confidence": 0.6,
         "review_required": True, "evidence_references": [REF]},
        index=2, reason_id="DR_1", verdict="not_supported")
    assert point["point_id"] == "RB-2"
    assert "point_id" not in rdv.REBUTTAL_SCHEMA_SHAPE["properties"]


# ------------------------------------------------------------- refusals --

def test_a_verdict_with_no_evidence_is_refused_except_insufficient():
    with pytest.raises(ValueError) as excinfo:
        bind({**GOOD, "evidence_references": []})
    assert "insufficient_evidence" in str(excinfo.value)

    # The one verdict that may stand on nothing, because it is the verdict that
    # says the records settle it neither way.
    allowed = bind({**GOOD, "verdict": "insufficient_evidence",
                    "evidence_references": []})
    assert allowed["verdict"] == "insufficient_evidence"


def test_every_cited_policy_match_must_be_verified():
    # An unverified clause left out would look like a trusted basis afterwards.
    with pytest.raises(ValueError) as excinfo:
        bind({**GOOD, "policy_match_validations": []},
             matches=[{"policy_match_id": "PM_1", "match_source": "insurer_cited"},
                      {"policy_match_id": "PM_2", "match_source": "agent_inferred"}])
    assert "missing: PM_1, PM_2" in str(excinfo.value)


def test_a_verification_for_a_match_the_reason_does_not_cite_is_refused():
    extra = {**GOOD, "policy_match_validations": [
        *GOOD["policy_match_validations"],
        {"policy_match_id": "PM_9", "verification_status": "invalid",
         "verification_explanation": "x", "review_required": True}]}
    with pytest.raises(ValueError) as excinfo:
        bind(extra)
    assert "does not cite" in str(excinfo.value)


def test_a_verified_match_needs_its_own_evidence():
    bare = {**GOOD, "policy_match_validations": [
        {"policy_match_id": "PM_1", "verification_status": "verified",
         "verification_explanation": "확인됨", "review_required": False}]}
    with pytest.raises(ValueError) as excinfo:
        bind(bare)
    assert "needs at least one evidence reference" in str(excinfo.value)


def test_an_unverifiable_match_needs_none():
    ok = bind({**GOOD, "policy_match_validations": [
        {"policy_match_id": "PM_1", "verification_status": "unverifiable",
         "verification_explanation": "약관 원문이 팩에 없습니다.",
         "review_required": True}]})
    assert ok["policy_match_validations"][0]["verification_status"] == "unverifiable"


def test_an_unevidenced_rebuttal_is_refused():
    with pytest.raises(ValueError) as excinfo:
        rdv.bind_rebuttal(
            {"rebuttal_argument": "다툴 여지가 있습니다.", "confidence": 0.5,
             "review_required": True, "evidence_references": []},
            index=1, reason_id="DR_1", verdict="not_supported")
    assert "at least one piece of evidence" in str(excinfo.value)


def test_the_transport_schema_pins_the_match_ids_of_this_reason():
    schema = rdv.validation_schema(["PM_1", "PM_2"])
    match = schema["properties"]["policy_match_validations"]["items"]
    assert match["properties"]["policy_match_id"]["enum"] == ["PM_1", "PM_2"]


# --------------------------------------------------------- publication --

def test_only_a_failed_verdict_produces_a_rebuttal():
    # A reason whose position HELD UP produces no rebuttal, which is why this
    # is a second pass over a filtered set rather than a field on the first.
    assert set(rdv.REBUTTABLE) == {"not_supported", "partially_supported"}
    assert "supported" not in rdv.REBUTTABLE
    assert "insufficient_evidence" not in rdv.REBUTTABLE


def test_the_validation_contract_validates():
    contract = rdv.build_contract(
        case_id="CASE_999", run_id="RUN_20260826_001", validations=[bind()],
        model_name="fixture:stub", prompt_version=rdv.VALIDATION_PROMPT_VERSION,
        source_denial_hash="a" * 64)
    schemas, registry = load_registry()
    assert validate_instance(contract, "denial_validation_result.schema.json",
                             schemas, registry) == []


def test_the_rebuttal_contract_validates():
    point = rdv.bind_rebuttal(
        {"rebuttal_argument": "분쟁 대응 여지가 있습니다.", "confidence": 0.6,
         "review_required": True, "reviewer_role": "손해사정사",
         "evidence_references": [REF]},
        index=1, reason_id="DR_1", verdict="not_supported")
    contract = rdv.build_rebuttal_contract(
        case_id="CASE_999", run_id="RUN_20260826_001", points=[point],
        model_name="fixture:stub")
    schemas, registry = load_registry()
    assert validate_instance(contract, "rebuttal_points.schema.json",
                             schemas, registry) == []


def test_an_unverifiable_quote_stops_the_write(monkeypatch, tmp_path):
    # Every quote is checked verbatim against the page it names. One the model
    # produced from memory must fail here rather than reach the contract.
    monkeypatch.setattr(rdv, "_dao_json",
                        lambda args, allow_missing=False: {"verified_references": []})
    monkeypatch.setattr(rdv, "_temp_json", lambda value: tmp_path / "refs.json")

    with pytest.raises(RuntimeError) as excinfo:
        rdv.verify_references("CASE_999", "RUN_20260826_001", [REF])

    assert "does not appear on the page it names" in str(excinfo.value)


def test_a_case_with_no_denial_reasons_refuses_rather_than_reporting_success(monkeypatch):
    monkeypatch.setattr(rdv, "_dao_json",
                        lambda args, allow_missing=False: {"denial_reasons": []})

    class Exploding:
        provider_name = "none"
        model_name = "none"

    with pytest.raises(RuntimeError) as excinfo:
        rdv.run(case_id="CASE_999", run_id="RUN_20260826_001", held_by="t",
                provider=Exploding())

    assert "nothing to validate" in str(excinfo.value)


# ------------------------------------------------------------- the prompt --

def test_the_validation_prompt_carries_the_reason_and_its_matches():
    prompt = rdv.build_validation_prompt(
        reason={"reason_id": "DR_1", "decision_type": "면책",
                "raw_reason_text": "고지의무 위반", "grounds": ["기왕증"]},
        evidence_text="### DOC_001\n2025-03-04 사고 발생",
        policy_matches=[{"policy_match_id": "PM_1", "clause_title": "면책조항"}])
    for token in ("DR_1", "고지의무 위반", "기왕증", "PM_1", "면책조항",
                  "2025-03-04 사고 발생"):
        assert token in prompt


def test_the_rebuttal_prompt_forbids_claiming_an_outcome():
    flattened = " ".join(rdv.REBUTTAL_INSTRUCTIONS.split())
    assert "분쟁 대응 여지가 있다" in flattened
    assert "never that the claim wins" in flattened


def test_match_source_is_copied_from_the_upstream_contract():
    """`verified` means "the clause is where the match says", not "the insurer's
    basis checks out". Which of the two a reader may conclude depends on
    match_source, and that is a fact about denial_reason_result.json rather
    than anything the model judges -- so it is copied, not asked for."""
    bound = bind()
    assert bound["policy_match_validations"][0]["match_source"] == "insurer_cited"
    match_schema = rdv.validation_schema(["PM_1"])[
        "properties"]["policy_match_validations"]["items"]
    assert "match_source" not in match_schema["properties"]
    assert match_schema["additionalProperties"] is False


def test_a_match_the_upstream_contract_never_sourced_is_refused():
    with pytest.raises(ValueError) as excinfo:
        bind(matches=[{"policy_match_id": "PM_1"}])
    assert "no match_source" in str(excinfo.value)
