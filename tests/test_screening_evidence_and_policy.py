"""What the screening report carries forward, and what it must not invent.

Two gaps, one shape. `markdown_sections` read `report["policy_links"]` and
`build_report` never wrote that key, so section 8 printed "연결된 약관 조항 없음"
on every case -- including cases whose claim analysis had matched a clause.
And every section that printed a medical fact handed the assembly tool an empty
`evidence_references`, so the tags and the sidecar that make a narrative claim
checkable simply did not exist for the facts a reader cares about most.

Both are P1 failures rather than cosmetic ones: a screening report is the first
document a human reads, and an uncited 진단명 is indistinguishable from one
nobody could source.

The rule these tests defend runs in both directions, and the second half is the
one easy to lose while fixing the first: a value WITH upstream evidence must
carry it, and a value WITHOUT must carry none. Filling a gap with the citation
from the fact printed on the line above would satisfy every "is there a
citation?" assertion and be exactly the fabrication the guardrail forbids.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance
import document_assembly
import run_screening_report as reporter


ROOT = Path(__file__).resolve().parent.parent

DIAGNOSIS_QUOTE = "우측 요골 골절"
ACCIDENT_QUOTE = "2026-03-02 작업 중 추락"
CODE_QUOTE = "S52.5"
PERIOD_QUOTE = "치료기간 2026-03-02 ~ 2026-05-01"
CLAUSE_QUOTE = "진단 담보"


def _fact(field_id, value, quote, *, document_id="DOC_001", page=1,
          status="asserted"):
    return {
        "field_id": field_id,
        "resolution_status": status,
        "selected_observation_ids": ["OBS_" + field_id] if status == "asserted" else [],
        "observations": [{
            "observation_id": "OBS_" + field_id,
            "value": value,
            "evidence_references": [{
                "document_id": document_id, "page": page, "quote": quote,
                "start_char": 0, "end_char": len(quote)}],
        }],
    }


def _policy_link(status="matched", *, candidates=()):
    link = {
        "coverage_id": "primary_diagnosis",
        "coverage_name": "진단",
        "clause_link_status": status,
        "requirements": [{
            "requirement_id": "REQ-1",
            "requirement_text": "진단 관련 담보 요건",
            "evidence_status": "supported",
            "conflict_candidate_ids": [],
            "reason": "An asserted claim fact states this condition.",
            "evidence_references": [],
        }],
    }
    if status == "matched":
        link["clause_ref"] = {"document_id": "DOC_900", "page": 3,
                              "quote": CLAUSE_QUOTE, "start_char": 0,
                              "end_char": len(CLAUSE_QUOTE)}
    else:
        link["uncertainty_reason"] = (
            f"{len(candidates)} clauses match this coverage term; choosing "
            "between them is a reviewer's judgement, not this stage's."
            if candidates else
            "No clause in the processed policy layer names this coverage.")
    return link


def _claim_analysis(*, facts=(), links=()):
    return {
        "claim_facts": list(facts),
        "case_type_assessment": [],
        "required_document_checklist": [],
        "policy_links": list(links),
    }


def _report(**over):
    claim_analysis = over.pop("claim_analysis", _claim_analysis())
    kwargs = {
        "case_id": "CASE_9401", "run_id": "RUN_20260820_1",
        "claim_analysis": claim_analysis,
        "consistency": {"checks": []},
        "config": {"fields": []},
    }
    kwargs.update(over)
    return reporter.build_report(**kwargs)


def _errors(instance, schema_name):
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


# ------------------------------------------------------------ policy links --

def test_matched_links_reach_the_report_json() -> None:
    """The dropped key: claim analysis had it, the report did not."""
    report = _report(claim_analysis=_claim_analysis(links=[_policy_link()]))

    assert "policy_links" in report
    link = report["policy_links"][0]
    assert link["clause_link_status"] == "matched"
    assert link["clause_ref"]["document_id"] == "DOC_900"
    assert link["clause_ref"]["quote"] == CLAUSE_QUOTE


def test_a_matched_link_names_the_clause_in_section_eight() -> None:
    report = _report(claim_analysis=_claim_analysis(links=[_policy_link()]))
    section = reporter.markdown_sections(report)[7]

    assert "연결된 약관 조항 없음" not in section["content"]
    assert "진단" in section["content"]
    assert "DOC_900" in section["content"]
    assert CLAUSE_QUOTE in section["content"]


def test_the_clause_reaches_the_sidecar_as_evidence() -> None:
    """Section 8's citation is what makes the clause checkable."""
    report = _report(claim_analysis=_claim_analysis(links=[_policy_link()]))
    section = reporter.markdown_sections(report)[7]

    assert {reference["quote"] for reference in section["evidence_references"]} \
        == {CLAUSE_QUOTE}
    assert section["evidence_references"][0]["document_id"] == "DOC_900"


def test_a_candidate_link_shows_its_status_and_cites_nothing() -> None:
    """An unconfirmed clause is named as unconfirmed, never cited as found."""
    link = _policy_link("candidate", candidates=[1, 2])
    report = _report(claim_analysis=_claim_analysis(links=[link]))
    section = reporter.markdown_sections(report)[7]

    assert report["policy_links"][0]["clause_link_status"] == "candidate"
    assert "clause_ref" not in report["policy_links"][0]
    assert "후보 조항" in section["content"]
    assert "reviewer's judgement" in section["content"]
    assert section["evidence_references"] == []


def test_a_not_found_link_states_why_rather_than_disappearing() -> None:
    link = _policy_link("not_found")
    report = _report(claim_analysis=_claim_analysis(links=[link]))
    section = reporter.markdown_sections(report)[7]

    assert report["policy_links"][0]["clause_link_status"] == "not_found"
    assert "조항 미확인" in section["content"]
    assert "No clause" in section["content"]
    assert section["evidence_references"] == []


def test_a_conflict_requirement_keeps_its_candidate_id() -> None:
    """The id survives the hop into the report, or the disagreement is lost."""
    link = _policy_link()
    link["requirements"][0].update({
        "evidence_status": "conflict",
        "conflict_candidate_ids": ["CAC_0001"],
        "reason": "two conflicting readings",
    })
    report = _report(claim_analysis=_claim_analysis(links=[link]))

    requirement = report["policy_links"][0]["requirements"][0]
    assert requirement["evidence_status"] == "conflict"
    assert requirement["conflict_candidate_ids"] == ["CAC_0001"]

    section = reporter.markdown_sections(report)[7]
    assert "CAC_0001" in section["content"]
    assert "충돌 검토 필요" in section["content"]


def test_the_section_states_no_coverage_or_payout_verdict() -> None:
    report = _report(claim_analysis=_claim_analysis(links=[_policy_link()]))
    body = reporter.markdown_sections(report)[7]["content"]
    for forbidden in ("보상 대상", "지급 가능", "면책", "보장됩니다", "예상 보험금"):
        assert forbidden not in body


# ------------------------------------------------------- fact evidence --

def _full_facts():
    return [
        _fact("primary_diagnosis", DIAGNOSIS_QUOTE, DIAGNOSIS_QUOTE),
        _fact("accident_date", "2026-03-02", ACCIDENT_QUOTE, document_id="DOC_002"),
        _fact("diagnosis_code", CODE_QUOTE, CODE_QUOTE),
        _fact("treatment_period", {"start": "2026-03-02", "end": "2026-05-01"},
              PERIOD_QUOTE, document_id="DOC_003", page=2),
    ]


def test_each_core_fact_carries_its_own_upstream_citation() -> None:
    report = _report(claim_analysis=_claim_analysis(facts=_full_facts()))
    evidence = report["case_summary"]["fact_evidence"]

    assert evidence["main_diagnosis"][0]["quote"] == DIAGNOSIS_QUOTE
    assert evidence["accident_date"][0]["quote"] == ACCIDENT_QUOTE
    assert evidence["accident_date"][0]["document_id"] == "DOC_002"
    assert evidence["kcd_code"][0]["quote"] == CODE_QUOTE
    assert evidence["treatment_period"][0]["page"] == 2


def test_section_one_carries_the_evidence_for_what_it_printed() -> None:
    report = _report(claim_analysis=_claim_analysis(facts=_full_facts()))
    section = reporter.markdown_sections(report)[0]

    quotes = {reference["quote"] for reference in section["evidence_references"]}
    assert quotes == {DIAGNOSIS_QUOTE, ACCIDENT_QUOTE, CODE_QUOTE, PERIOD_QUOTE}
    assert DIAGNOSIS_QUOTE in section["content"]


def test_an_ungrounded_value_gets_no_citation_at_all() -> None:
    """The direction that matters: no borrowing from the neighbouring fact."""
    facts = [_fact("primary_diagnosis", DIAGNOSIS_QUOTE, DIAGNOSIS_QUOTE)]
    report = _report(claim_analysis=_claim_analysis(facts=facts))
    evidence = report["case_summary"]["fact_evidence"]

    assert set(evidence) == {"main_diagnosis"}
    section = reporter.markdown_sections(report)[0]
    assert len(section["evidence_references"]) == 1
    assert "사고일: 확인 불가" in section["content"]
    assert "진단코드: 확인 불가" in section["content"]


def test_an_unresolved_field_contributes_no_evidence() -> None:
    """`unavailable` means nothing was established -- including its sources."""
    facts = [_fact("primary_diagnosis", None, DIAGNOSIS_QUOTE, status="unavailable")]
    report = _report(claim_analysis=_claim_analysis(facts=facts))

    assert report["case_summary"]["fact_evidence"] == {}
    assert reporter.markdown_sections(report)[0]["evidence_references"] == []


def test_only_the_elected_observation_is_cited() -> None:
    """A discarded reading is not evidence for the value that was published."""
    fact = _fact("primary_diagnosis", DIAGNOSIS_QUOTE, DIAGNOSIS_QUOTE)
    fact["observations"].append({
        "observation_id": "OBS_other", "value": "좌측 요골 골절",
        "evidence_references": [{"document_id": "DOC_002", "page": 1,
                                 "quote": "좌측 요골 골절", "start_char": 0,
                                 "end_char": 8}]})
    report = _report(claim_analysis=_claim_analysis(facts=[fact]))

    quotes = {reference["quote"]
              for reference in report["case_summary"]["fact_evidence"]["main_diagnosis"]}
    assert quotes == {DIAGNOSIS_QUOTE}


def test_the_medical_area_section_reuses_the_same_citation() -> None:
    """Section 4 prints the diagnosis too; one fact, one source."""
    report = _report(claim_analysis=_claim_analysis(facts=_full_facts()))
    section = reporter.markdown_sections(report)[3]

    assert {reference["quote"] for reference in section["evidence_references"]} \
        == {DIAGNOSIS_QUOTE}


def test_the_insurer_decision_carries_the_grounds_it_states() -> None:
    denial = {
        "denial_reasons": [
            {"reason_id": "DR_1", "decision_type": "denial", "amount": None,
             "evidence_references": [{"document_id": "DOC_700", "page": 1,
                                      "quote": "부지급 사유: 고지의무 위반"}]},
            {"reason_id": "DR_3", "decision_type": "reduction", "amount": None,
             "evidence_references": [{"document_id": "DOC_700", "page": 2,
                                      "quote": "감액 사유: 기왕증 기여도"}]},
        ],
        "accepted_coverages": [
            {"accepted_coverage_id": "AC_1",
             "evidence_references": [{"document_id": "DOC_700", "page": 2,
                                      "quote": "일부 담보 지급 결정"}]},
        ],
    }
    report = _report(denial_reasons=denial)
    section = reporter.markdown_sections(report)[8]

    quotes = {reference["quote"] for reference in section["evidence_references"]}
    assert quotes == {"부지급 사유: 고지의무 위반", "감액 사유: 기왕증 기여도",
                      "일부 담보 지급 결정"}
    assert "거절: DR_1" in section["content"]


def test_a_conflict_carries_both_readings() -> None:
    entry = {
        "field_or_topic": "primary_diagnosis",
        "professional_summary": "진단서는 우측, 입퇴원요약은 좌측으로 기재되어 있습니다.",
        "sources": [
            {"document_id": "DOC_001", "page": 1, "value": "우측",
             "quote": DIAGNOSIS_QUOTE},
            {"document_id": "DOC_002", "page": 1, "value": "좌측",
             "quote": "좌측 요골 골절"},
        ],
    }
    report = _report(
        consistency={"checks": [{"result": "inconsistent",
                                 "conflict_id": "CONFLICT_1"}]},
        conflict_entries={"CONFLICT_1": entry})
    section = reporter.markdown_sections(report)[5]

    assert {reference["quote"] for reference in section["evidence_references"]} \
        == {DIAGNOSIS_QUOTE, "좌측 요골 골절"}


def test_no_section_writes_a_tag_by_hand() -> None:
    """P1: the assembly tool generates every `[E#]`, from the references above."""
    report = _report(claim_analysis=_claim_analysis(
        facts=_full_facts(), links=[_policy_link()]))
    body = "\n".join(section["content"]
                     for section in reporter.markdown_sections(report))
    assert "[E1]" not in body and "[E#]" not in body


def test_a_duplicate_citation_is_not_emitted_twice() -> None:
    """Two facts off one sentence must not read as two corroborations."""
    facts = [_fact("primary_diagnosis", DIAGNOSIS_QUOTE, DIAGNOSIS_QUOTE),
             _fact("diagnosis_code", CODE_QUOTE, DIAGNOSIS_QUOTE)]
    report = _report(claim_analysis=_claim_analysis(facts=facts))
    section = reporter.markdown_sections(report)[0]

    assert len(section["evidence_references"]) == 1


def test_asserted_disability_assessment_is_recorded_in_claim_analysis() -> None:
    checklist = [{
        "document_kind": reporter.DISABILITY_DOCUMENT_KIND,
        "document_ids": ["DOC_050"],
    }]
    facts = {"existing_disability_assessment": _fact(
        "existing_disability_assessment", ["AMA 10%"], "AMA 10%",
        document_id="DOC_050",
    )}

    result = reporter.existing_disability_documents(checklist, facts)

    assert result["present"] is True
    assert result["recorded_in_claim_analysis"] is True
    assert result["evidence_references"][0]["document_id"] == "DOC_050"


# ------------------------------------------------------- contract validity --

def test_the_enriched_report_validates_against_its_schema() -> None:
    report = _report(claim_analysis=_claim_analysis(
        facts=_full_facts(), links=[_policy_link()]))
    assert _errors(report, "screening_report_selective.schema.json") == []


def test_the_sections_still_satisfy_the_template() -> None:
    report = _report(claim_analysis=_claim_analysis(
        facts=_full_facts(), links=[_policy_link()]))
    headings = [section["heading"]
                for section in reporter.markdown_sections(report)]
    assert len(headings) == 9
    assert document_assembly.validate_template(headings, reporter.TEMPLATE) == []


def test_the_report_json_is_the_only_source_for_the_narrative() -> None:
    """The Markdown is a view of the stored contract, not a second reading.

    Every value section 1 prints must be findable in `case_summary`; nothing is
    recomputed from the claim analysis at render time.
    """
    report = _report(claim_analysis=_claim_analysis(facts=_full_facts()))
    summary = report["case_summary"]
    content = reporter.markdown_sections(report)[0]["content"]

    assert summary["main_diagnosis"] in content
    assert summary["accident_date"] in content
    assert summary["kcd_code"] in content
