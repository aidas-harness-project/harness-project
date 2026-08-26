"""Coverage search terms come from config, and a liability case gets its own.

The defect, measured on CASE_053 (2026-08-20): `COVERAGE_HINT_FIELDS` was
hardcoded to personal-insurance coverages (수술/입원/후유장해/상해/진단). On a
영업배상책임보험 all seven matched zero clause headings, so `policy_links`
returned `not_found` seven times and `candidate_pages` returned {} -- while the
case's own index held 시설소유(관리)자 특별약관 제2조 보상하지 않는 손해 and
구내치료비 추가특별약관 제1조, the two clauses the insurer's denial rests on.
Nobody had checked the denial against the exclusion clause it must rely on.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import claim_analysis_policy_links as pl

CONFIG = {
    "policy_linking": {
        "coverage_term_fields": [
            {"field_id": "primary_diagnosis", "term": "진단"},
        ],
        "coverage_terms_by_case_type": {
            "personal_insurance": [],
            "liability": [
                {"coverage_id": "liability_premises_owner", "term": "시설소유"},
                {"coverage_id": "liability_premises_medical_expense",
                 "term": "구내치료비"},
            ],
        },
    }
}

INDEX = {"documents": [{"document_id": "DOC_009", "clauses": [
    {"page": 2, "policy_name": "시설소유(관리)자 특별약관", "article": "제2조",
     "heading": "보상하지 않는 손해"},
    {"page": 5, "policy_name": "구내치료비 추가특별약관", "article": "제1조",
     "heading": "보상하는 손해"},
    {"page": 40, "policy_name": "임상시험 배상책임 특별약관", "article": "제1조",
     "heading": "보상하는 손해"},
]}]}

MANIFEST = {"documents": [
    {"document_id": "DOC_009", "document_type": "insurance_policy"}]}

FACTS = [{"field_id": "primary_diagnosis", "resolution_status": "asserted"}]
LIABILITY = [{"case_type": "liability", "verdict": None}]


def test_a_liability_case_searches_its_own_policy_vocabulary():
    terms = pl.coverage_terms(FACTS, config=CONFIG,
                              case_type_assessment=LIABILITY)
    assert ("liability_premises_owner", "시설소유") in terms
    assert ("liability_premises_medical_expense", "구내치료비") in terms
    # the fact-justified term is still there
    assert ("primary_diagnosis", "진단") in terms


def test_the_disputed_clauses_become_reachable_pages():
    """The whole point: the exclusion clause reaches a reviewer."""
    pages = pl.candidate_pages(claim_facts=FACTS, manifest=MANIFEST,
                               index=INDEX, config=CONFIG,
                               case_type_assessment=LIABILITY)
    assert 2 in pages["DOC_009"], "시설소유 exclusion clause page missing"
    assert 5 in pages["DOC_009"], "구내치료비 clause page missing"

    # Reintroducing the defect (no config, no case types) finds nothing at all.
    assert pl.candidate_pages(claim_facts=FACTS, manifest=MANIFEST,
                              index=INDEX) == {}


def test_a_case_type_not_in_play_contributes_no_terms():
    ruled_out = [{"case_type": "liability", "verdict": "not_applicable"}]
    terms = pl.coverage_terms(FACTS, config=CONFIG,
                              case_type_assessment=ruled_out)
    assert all(cid != "liability_premises_owner" for cid, _ in terms)


def test_an_uncertain_verdict_still_counts_as_in_play():
    """불확실 is liability's ordinary outcome on this lane; requiring a positive
    verdict would reproduce the gap this fix closes."""
    terms = pl.coverage_terms(
        FACTS, config=CONFIG,
        case_type_assessment=[{"case_type": "liability", "verdict": None}])
    assert ("liability_premises_owner", "시설소유") in terms


def test_without_config_the_builtin_personal_insurance_list_still_applies():
    """No config must not mean no search -- existing callers keep working."""
    facts = [{"field_id": "admission_status", "resolution_status": "asserted"}]
    assert pl.coverage_terms(facts) == [("admission_status", "입원")]


def test_terms_are_not_duplicated():
    dupes = {"policy_linking": {
        "coverage_term_fields": [{"field_id": "primary_diagnosis", "term": "진단"}],
        "coverage_terms_by_case_type": {
            "liability": [{"coverage_id": "primary_diagnosis", "term": "진단"}]},
    }}
    terms = pl.coverage_terms(FACTS, config=dupes,
                              case_type_assessment=LIABILITY)
    assert len(terms) == len(set(terms))
