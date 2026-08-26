"""The deliverable's structured contract, authored one block at a time.

The renderer was already deterministic; producing the contract it renders was
not. What is asserted here is mostly the citation-integrity story: the model
never writes a document id, a page or a quote, only an `E<N>` into a registry
the driver built -- so it cannot cite evidence that does not exist, correctly or
otherwise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import draft_authoring as da  # noqa: E402
import run_draft_report as rdr  # noqa: E402
from _validation import load_registry, validate_instance  # noqa: E402

CLAIM_ANALYSIS = {
    "case_type_assessment": {"primary_type": "상해"},
    "claim_facts": [
        {"field_id": "accident_date", "field_state": "asserted", "value": "2025-03-04",
         "observations": [{"observation_id": "OBS_1", "source_document_kind": "진단서",
                           "evidence_references": [
                               {"document_id": "DOC_001", "page": 2,
                                "quote": "2025-03-04 사고"}]}]},
        {"field_id": "diagnosis", "field_state": "asserted", "value": "골절",
         "observations": [{"observation_id": "OBS_2", "source_document_kind": "진단서",
                           "evidence_references": [
                               {"document_id": "DOC_001", "page": 2,
                                "quote": "2025-03-04 사고"},
                               {"document_id": "DOC_002", "page": 1,
                                "quote": "우측 요골 골절"}]}]},
    ],
}


# ------------------------------------------------------------- registry --

def test_the_registry_is_built_from_the_upstream_contract():
    registry = da.evidence_registry(CLAIM_ANALYSIS)
    assert [e["evidence_id"] for e in registry] == ["E1", "E2"]
    assert registry[0]["document_id"] == "DOC_001"
    assert registry[1]["quote"] == "우측 요골 골절"


def test_one_source_cited_twice_gets_one_id():
    # The renderer's tag numbering expects one id per source.
    registry = da.evidence_registry(CLAIM_ANALYSIS)
    quotes = [e["quote"] for e in registry]
    assert len(quotes) == len(set(quotes))


def test_the_model_cannot_write_a_document_page_or_quote():
    # The whole citation-integrity story: statements carry ids, not locations,
    # so there is no field in which to invent a source.
    statement = da.SECTION_SCHEMA["properties"]["statements"]["items"]
    assert set(statement["properties"]) == {
        "text", "evidence_refs", "support_type", "confidence",
        "uncertainty_note", "human_review_required"}
    assert statement["additionalProperties"] is False


def test_a_citation_outside_the_registry_is_refused():
    raw = {"heading": "요약", "status": "included", "rationale": "",
           "statements": [{"text": "사고일은 2025-03-04입니다.",
                           "evidence_refs": ["E9"], "support_type": "direct",
                           "confidence": "high", "human_review_required": False}]}
    with pytest.raises(ValueError) as excinfo:
        da.bind_section(raw, section_key="summary", evidence_ids=["E1", "E2"])
    assert "not in the registry: E9" in str(excinfo.value)


def test_an_uncited_statement_is_refused():
    raw = {"heading": "요약", "status": "included", "rationale": "",
           "statements": [{"text": "사고가 있었습니다.", "evidence_refs": [],
                           "support_type": "professional_judgment",
                           "confidence": "low", "human_review_required": True}]}
    with pytest.raises(ValueError) as excinfo:
        da.bind_section(raw, section_key="facts", evidence_ids=["E1"])
    assert "every statement cites evidence" in str(excinfo.value)


# --------------------------------------------------------- section rules --

def test_an_included_section_needs_a_statement():
    with pytest.raises(ValueError) as excinfo:
        da.bind_section({"heading": "판단", "status": "included", "rationale": "",
                         "statements": []}, section_key="analysis",
                        evidence_ids=["E1"])
    assert "at least one statement" in str(excinfo.value)


@pytest.mark.parametrize("status", ["not_applicable", "deferred"])
def test_a_section_that_is_not_written_must_say_why(status):
    with pytest.raises(ValueError) as excinfo:
        da.bind_section({"heading": "산정", "status": status, "rationale": "  ",
                         "statements": []}, section_key="calculation_summary",
                        evidence_ids=["E1"])
    assert "must say why in rationale" in str(excinfo.value)

    ok = da.bind_section({"heading": "산정", "status": status,
                          "rationale": "금액 산정은 본 PoC 범위 밖입니다.",
                          "statements": []},
                         section_key="calculation_summary", evidence_ids=["E1"])
    assert ok["status"] == status


def test_all_eight_sections_are_requested():
    assert len(da.SECTION_KEYS) == 8
    import _validation as v
    canonical = json.loads(v.LOSS_ADJUSTMENT_SCHEMA.read_text(encoding="utf-8"))
    required = canonical["$defs"]["sections"]["required"]
    assert sorted(da.SECTION_KEYS) == sorted(required)


def test_each_section_is_its_own_call_not_one_giant_one():
    # The whole contract is 33.7KB of schema and a full report against a
    # 16,000-token cap that refuses a truncated answer outright.
    encoded = json.dumps(da.SECTION_SCHEMA, ensure_ascii=False)
    assert len(encoded) < 2000, "the per-section schema must stay small"


def test_the_prompt_lists_only_registry_ids_as_citable():
    prompt = da.build_section_prompt(
        section_key="facts", registry=da.evidence_registry(CLAIM_ANALYSIS),
        case_summary="요약")
    assert "E1" in prompt and "2025-03-04 사고" in prompt
    flattened = " ".join(prompt.split())
    assert "You cannot cite anything that is not in that registry" in flattened


def test_a_later_section_sees_the_earlier_ones():
    prompt = da.build_section_prompt(
        section_key="conclusion", registry=da.evidence_registry(CLAIM_ANALYSIS),
        case_summary="요약",
        prior_sections={"summary": {"status": "included", "statements": [
            {"text": "이미 쓴 문장"}]}})
    assert "이미 쓴 문장" in prompt
    assert "do not repeat them" in prompt


# ------------------------------------------------------------- the stage --

def test_a_case_with_no_citable_evidence_refuses_to_author(monkeypatch):
    class Exploding:
        provider_name = "none"
        model_name = "none"

    with pytest.raises(RuntimeError) as excinfo:
        rdr.author(case_id="CASE_999", version="v1", provider=Exploding(),
                   claim_analysis={"claim_facts": []}, rebuttals=None,
                   document_profile={}, case_reference={})
    assert "could be cited" in str(excinfo.value)


def test_a_calculation_cannot_rest_on_nothing():
    """The placeholder this replaced was not expressible.

    A `literal` calculation input REQUIRES evidence_refs, so a stub entry saying
    "not computed" would have had to cite a basis for an amount nobody worked
    out. The contract does not allow a calculation that rests on nothing, so
    calculations are authored and the out-of-scope amount is expressed the way
    the contract provides for: a null result with human_review_required.
    """
    with pytest.raises(ValueError) as excinfo:
        da.bind_calculations({"calculations": [{
            "category": "other", "label": "미실시", "operation": "identity",
            "inputs": [], "result_not_determined": True,
            "rounding_mode": "none", "rounding_unit": 1,
            "status": "human_review_required"}]}, evidence_ids=["E1"])
    assert "rests on nothing" in str(excinfo.value)


def test_an_undetermined_result_becomes_null_not_zero():
    bound = da.bind_calculations({"calculations": [{
        "category": "treatment_cost", "label": "치료비", "operation": "identity",
        "inputs": [{"label": "청구 치료비", "value": "1200000", "unit": "KRW",
                    "evidence_refs": ["E1"]}],
        "result_not_determined": True, "rounding_mode": "none",
        "rounding_unit": 1, "status": "human_review_required"}]},
        evidence_ids=["E1"])
    assert bound[0]["result"] is None
    assert bound[0]["calculation_id"] == "C1"
    assert bound[0]["rounding_rule"] == {"mode": "none", "unit": 1}


def test_the_final_amount_is_null_when_none_was_reached():
    # A zero reads as "nothing is payable", which is a different statement from
    # "no figure was reached".
    statement = {"text": "판단", "evidence_refs": ["E1"], "support_type": "derived",
                 "confidence": "low", "human_review_required": True}
    bound = da.bind_final_assessment(
        {"outcome": "undetermined", "amount_not_determined": True,
         "reasoning_summary": statement, "evidence_refs": ["E1"],
         "reservations": ["금액 미산정"]}, evidence_ids=["E1"])
    assert bound["amount"] is None
    assert "amount_not_determined" not in bound


def test_reasoning_issues_are_authored_rather_than_stubbed():
    """They were nearly a placeholder, and the schema stopped it.

    `reasoningIssue` requires `facts`, `rules` and `application_steps` with at
    least one entry each, and every one of those cites evidence. A stub would
    have had to invent a rule reference and its citation -- fabricating the
    grounds of an assessment to satisfy minItems. So the issues are a real
    bounded call and only `calculations`, whose placeholder needs no evidence,
    stays a stated gap.
    """
    issue = da.ISSUE_SCHEMA["properties"]["issues"]["items"]
    assert "issue_id" not in issue["properties"]
    for required in ("facts", "rules", "application_steps", "finding",
                     "disposition", "outcome_effect"):
        assert required in issue["required"]


def test_an_issue_citing_evidence_outside_the_registry_is_refused():
    statement = {"text": "x", "evidence_refs": ["E9"], "support_type": "direct",
                 "confidence": "high", "human_review_required": False}
    raw = {"issues": [{
        "issue_kind": "coverage", "question": "보장 여부",
        "facts": [statement], "rules": [], "application_steps": [],
        "counterevidence": [], "alternative_interpretations": [],
        "unresolved_items": [], "finding": statement,
        "disposition": "undetermined", "outcome_effect": "none"}]}
    with pytest.raises(ValueError) as excinfo:
        da.bind_issues(raw, evidence_ids=["E1"])
    message = str(excinfo.value)
    assert "not in the registry" in message
    assert "rules: at least one entry is required" in message


def test_issue_ids_are_assigned_by_the_driver():
    statement = {"text": "x", "evidence_refs": ["E1"], "support_type": "direct",
                 "confidence": "high", "human_review_required": False}
    rule = {"rule_type": "policy", "citation": "제3조", "rule_text": "…",
            "evidence_refs": ["E1"]}
    issue = {"issue_kind": "coverage", "question": "보장 여부",
             "facts": [statement], "rules": [rule], "application_steps": [statement],
             "counterevidence": [], "alternative_interpretations": [],
             "unresolved_items": [], "finding": statement,
             "disposition": "undetermined", "outcome_effect": "none"}
    bound = da.bind_issues({"issues": [issue, dict(issue)]}, evidence_ids=["E1"])
    assert [i["issue_id"] for i in bound] == ["I1", "I2"]


def test_a_report_that_turns_on_nothing_is_refused():
    with pytest.raises(ValueError) as excinfo:
        da.bind_issues({"issues": []}, evidence_ids=["E1"])
    assert "at least one issue is required" in str(excinfo.value)


def test_v2_without_rebuttals_refuses(monkeypatch):
    def fake_dao(args, allow_missing=False):
        if "loss_adjustment_report_v2.json" in args:
            return None
        if "rebuttal_points.json" in args:
            return None
        return CLAIM_ANALYSIS

    monkeypatch.setattr(rdr, "_dao_json", fake_dao)

    class Exploding:
        provider_name = "none"
        model_name = "none"

    with pytest.raises(RuntimeError) as excinfo:
        rdr.run(case_id="CASE_999", version="v2", run_id="RUN_20260826_001",
                held_by="t", provider=Exploding(), document_profile={},
                case_reference={})
    assert "it would be v1 written twice" in str(excinfo.value)


def test_an_existing_version_is_never_overwritten(monkeypatch):
    monkeypatch.setattr(rdr, "_dao_json",
                        lambda args, allow_missing=False: {"already": "there"})

    class Exploding:
        provider_name = "none"
        model_name = "none"

    with pytest.raises(RuntimeError) as excinfo:
        rdr.run(case_id="CASE_999", version="v1", run_id="RUN_20260826_001",
                held_by="t", provider=Exploding(), document_profile={},
                case_reference={})
    assert "never overwritten" in str(excinfo.value)


def test_the_assembled_document_validates_against_the_canonical_contract():
    registry = da.evidence_registry(CLAIM_ANALYSIS)
    section = {"heading": "요약", "status": "included", "rationale": "",
               "statements": [{"text": "본 건은 2025-03-04 상해 사고입니다.",
                               "evidence_refs": ["E1"], "support_type": "direct",
                               "confidence": "high", "human_review_required": False}]}
    document = {
        "schema_version": "loss_adjustment_report.v1",
        "document_profile": {
            "family": "personal_accident_benefit",
            "claim_mechanism": "personal_accident_policy_benefit",
            "mode": "full", "title": "손해사정서", "language": "ko-KR",
            "ordered_components": [
                "cover", "submission_letter", "inner_cover", "summary",
                "assignment_contract", "facts", "governing_basis", "analysis",
                "calculations", "conclusion", "evidence_index", "signature"]},
        "case_reference": {"case_id": "CASE_999",
                           "pseudonymization_status": "pseudonymized",
                           "event_type": "accident",
                           "disability_benefit_claimed": False},
        "evidence_registry": registry,
        "sections": {key: dict(section) for key in da.SECTION_KEYS},
        "reasoning_issues": da.bind_issues({"issues": [{
            "issue_kind": "coverage", "question": "상해보험금 지급 대상인지 여부",
            "facts": [{"text": "2025-03-04 사고가 확인됩니다.",
                       "evidence_refs": ["E1"], "support_type": "direct",
                       "confidence": "high", "human_review_required": False}],
            "rules": [{"rule_type": "policy", "citation": "보통약관 제3조",
                       "rule_text": "상해로 인한 치료비를 보상합니다.",
                       "evidence_refs": ["E1"]}],
            "application_steps": [{"text": "사고일이 보험기간 내입니다.",
                                   "evidence_refs": ["E1"],
                                   "support_type": "derived", "confidence": "medium",
                                   "human_review_required": True}],
            "counterevidence": [], "alternative_interpretations": [],
            "unresolved_items": ["금액 산정 미실시"],
            "finding": {"text": "지급 대상으로 볼 여지가 있습니다.",
                        "evidence_refs": ["E1"],
                        "support_type": "professional_judgment",
                        "confidence": "low", "human_review_required": True},
            "disposition": "undetermined", "outcome_effect": "none"}]},
            evidence_ids=["E1", "E2"]),
        "calculations": da.bind_calculations({"calculations": [{
            "category": "benefit_amount", "label": "보험금", "operation": "identity",
            "inputs": [{"label": "청구 치료비", "value": "1200000", "unit": "KRW",
                        "evidence_refs": ["E1"]}],
            "result_not_determined": True, "rounding_mode": "none",
            "rounding_unit": 1, "status": "human_review_required"}]},
            evidence_ids=["E1"]),
        "final_assessment": da.bind_final_assessment(
            {"outcome": "undetermined", "amount_not_determined": True,
             "reasoning_summary": {"text": "금액 산정은 범위 밖입니다.",
                                   "evidence_refs": ["E1"],
                                   "support_type": "professional_judgment",
                                   "confidence": "low",
                                   "human_review_required": True},
             "evidence_refs": ["E1"],
             "reservations": ["금액 산정은 범위 밖입니다."]},
            evidence_ids=["E1"]),
        "review_gates": {"evidence": "review_required",
                         "calculation": "review_required",
                         "medical": "review_required", "legal": "review_required",
                         "finalization": "review_required"},
    }
    schemas, registry_obj = load_registry()
    errors = validate_instance(document, "loss_adjustment_report.schema.json",
                               schemas, registry_obj)
    assert errors == [], errors



def test_the_family_s_required_issue_kinds_come_from_the_schema():
    """Eleven document-level conditional rules decide what a family's report
    must contain. Read out of the schema rather than restated: a copy would
    drift from the thing write-contract actually enforces."""
    assert da.family_requirements("personal_accident_benefit") == {
        "issue_kinds": ["coverage"], "calculation_categories": ["benefit_amount"]}
    assert da.family_requirements("liability_damages")["issue_kinds"] == [
        "comparative_negligence", "liability"]


def test_the_requirements_reach_the_prompt():
    rendered = da.render_requirements("disease_benefit")
    assert "coverage" in rendered and "diagnosis_definition_match" in rendered
    assert "benefit_amount" in rendered


def test_a_family_with_no_extra_requirement_says_so():
    assert "no family-specific requirement" in da.render_requirements("other_review_required")


def test_a_gate_cannot_be_closed_while_judgment_is_open():
    # The combination claims a discipline does not arise on a report that is
    # still asking it questions.
    with pytest.raises(ValueError) as excinfo:
        da.check_gates({"medical": "not_applicable", "legal": "review_required"},
                       judgment_open=True)
    assert "cannot be not_applicable" in str(excinfo.value)

    # With nothing open it is a legitimate answer.
    da.check_gates({"medical": "not_applicable", "legal": "not_applicable"},
                   judgment_open=False)
