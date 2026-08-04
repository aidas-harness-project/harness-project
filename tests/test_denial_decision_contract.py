"""Contract regressions for denial/reduction separation and grounded policy links.

All examples are synthetic test fixtures. They exercise contracts only and are
never evaluation inputs or substitutes for human review.
"""
import copy

import pytest

import _cross_contract
from _validation import load_registry, validate_instance


REDUCTION_ONLY = [
    "R01", "R02", "R03", "R06", "R07", "R10", "R11", "R13",
    "R16", "R17", "R18", "R19", "R20", "R21",
]
DENIAL_ONLY = ["R04", "R05", "R08", "R09"]


def evidence(document_id="DOC_002", page=1, quote="보험사 합성 원문"):
    return {"document_id": document_id, "page": page, "quote": quote}


def reason(code="R01", decision_type="reduction", *, review_required=False):
    item = {
        "reason_id": "DR_1",
        "decision_type": decision_type,
        "payment_status": "unknown",
        "taxonomy_code": code,
        # Required since the audit fix: candidate_codes is the Top-1/Top-3
        # evaluation input, and an omitted list silently drops a reason from
        # that measurement instead of scoring it. The top entry must be the
        # assigned code (enforced by tools/_cross_contract.py).
        "candidate_codes": [{"taxonomy_code": code, "confidence": 0.9}],
        "raw_reason_text": "보험사 합성 원문",
        "insurer_claim_summary": "보험사가 해당 사유를 주장함",
        "grounds": {
            "contractual_basis": [],
            "medical_or_factual_basis": [],
            "calculation_basis": [],
        },
        "amounts": {
            "claimed_amount": None,
            "payable_amount": None,
            "denied_amount": None,
            "reduction_amount": None,
            "reduction_rate": None,
        },
        "requested_documents": [],
        "policy_matches": [],
        "confidence": 0.9,
        "evidence_references": [evidence()],
        "review_required": review_required,
    }
    if review_required:
        item["reviewer_role"] = "손해사정사"
    return item


def denial_result(*reasons):
    return {
        "case_id": "CASE_900",
        "component": "denial-response",
        "status": "success",
        "denial_reasons": list(reasons),
    }


@pytest.fixture(scope="module")
def validator():
    return load_registry()


def errors(instance, schema_name, validator):
    schemas, registry = validator
    return validate_instance(instance, schema_name, schemas, registry)


@pytest.mark.parametrize("code", REDUCTION_ONLY)
def test_reduction_only_code_rejects_denial(code, validator):
    assert errors(denial_result(reason(code, "denial")), "denial_reason_result.schema.json", validator)


@pytest.mark.parametrize("code", DENIAL_ONLY)
def test_denial_only_code_rejects_reduction(code, validator):
    assert errors(denial_result(reason(code, "reduction")), "denial_reason_result.schema.json", validator)


@pytest.mark.parametrize("code", ["R15", "R99"])
@pytest.mark.parametrize("decision_type", ["denial", "reduction"])
def test_dual_type_codes_accept_both_decisions(code, decision_type, validator):
    instance = denial_result(reason(code, decision_type))
    assert errors(instance, "denial_reason_result.schema.json", validator) == []


@pytest.mark.parametrize("code", ["R12", "R14"])
@pytest.mark.parametrize("decision_type", ["denial", "reduction"])
def test_unclassified_codes_require_adjuster_review(code, decision_type, validator):
    without_review = denial_result(reason(code, decision_type))
    assert errors(without_review, "denial_reason_result.schema.json", validator)

    with_review = denial_result(reason(code, decision_type, review_required=True))
    assert errors(with_review, "denial_reason_result.schema.json", validator) == []


def test_partial_payment_is_not_a_decision_type(validator):
    item = reason()
    item["decision_type"] = "partial_payment"
    assert errors(denial_result(item), "denial_reason_result.schema.json", validator)


@pytest.mark.parametrize("decision_type", ["denial", "reduction"])
@pytest.mark.parametrize("payment_status", ["unpaid", "partially_paid", "paid", "unknown"])
def test_payment_status_is_independent_until_real_data_supports_rules(
    decision_type, payment_status, validator
):
    code = "R04" if decision_type == "denial" else "R01"
    item = reason(code, decision_type)
    item["payment_status"] = payment_status
    assert errors(denial_result(item), "denial_reason_result.schema.json", validator) == []


def test_reduction_without_stated_amount_or_rate_is_valid(validator):
    instance = denial_result(reason("R01", "reduction"))
    assert errors(instance, "denial_reason_result.schema.json", validator) == []


def inferred_policy_match(*, review_required):
    match = {
        "policy_match_id": "PM_1",
        "document_id": "DOC_001",
        "clause_id": "C-1",
        "match_source": "agent_inferred",
        "relevance_note": "합성 약관 조항이 관련될 가능성이 있음",
        "insurer_citation_evidence_references": [],
        "policy_clause_evidence_references": [evidence("DOC_001", 2, "보험금 지급요건")],
        "confidence": 0.7,
        "review_required": review_required,
    }
    if review_required:
        match["reviewer_role"] = "손해사정사"
    return match


def test_agent_inferred_policy_match_requires_review(validator):
    item = reason()
    item["policy_matches"] = [inferred_policy_match(review_required=False)]
    assert errors(denial_result(item), "denial_reason_result.schema.json", validator)

    item["policy_matches"] = [inferred_policy_match(review_required=True)]
    assert errors(denial_result(item), "denial_reason_result.schema.json", validator) == []


def test_basis_and_policy_match_without_source_location_are_rejected(validator):
    item = reason()
    item["grounds"]["medical_or_factual_basis"] = [{
        "basis_id": "BASIS_1",
        "text": "합성 의학적 근거",
        "basis_source": "insurer_stated",
        "evidence_references": [],
        "confidence": 0.8,
        "review_required": False,
    }]
    assert errors(denial_result(item), "denial_reason_result.schema.json", validator)

    item = reason()
    match = inferred_policy_match(review_required=True)
    match["policy_clause_evidence_references"] = []
    item["policy_matches"] = [match]
    assert errors(denial_result(item), "denial_reason_result.schema.json", validator)


def test_one_case_can_contain_separate_denial_and_reduction_reasons(validator):
    denied = reason("R04", "denial")
    reduced = reason("R01", "reduction")
    reduced["reason_id"] = "DR_2"
    instance = denial_result(denied, reduced)
    assert errors(instance, "denial_reason_result.schema.json", validator) == []


def screening_position(has_denial, has_reduction):
    return {
        "case_id": "CASE_900",
        "component": "screening-report",
        "status": "success",
        "report_path": "outputs/CASE_900/screening_report.md",
        "case_summary": {
            "case_type": "other",
            "main_diagnosis": "합성 진단",
            "claim_coverages": ["합성 담보"],
        },
        "insurer_position": {
            "has_denial": has_denial,
            "has_reduction": has_reduction,
            "has_denial_or_reduction": has_denial or has_reduction,
            "denial": {"reason_ids": ["DR_1"] if has_denial else [], "total_amount": None},
            "reduction": {"reason_ids": ["DR_2"] if has_reduction else [], "total_amount": None},
        },
        "key_issues": [],
        "inconsistencies": [],
        "missing_documents": [],
        "review_points": [],
        "preliminary_assessment": {
            "feasibility": "medium",
            "difficulty": "medium",
            "priority_review_points": ["합성 검수 포인트"],
        },
    }


def test_screening_keeps_denial_and_reduction_separate_with_compatibility_or(validator):
    instance = screening_position(True, True)
    assert errors(instance, "screening_report.schema.json", validator) == []

    bad = copy.deepcopy(instance)
    bad["insurer_position"]["has_denial_or_reduction"] = False
    assert errors(bad, "screening_report.schema.json", validator)


@pytest.fixture
def synthetic_policy_only_fixture():
    clause_evidence = evidence("DOC_001", 2, "보험금 지급요건")
    return {
        "case_id": "CASE_900",
        "component": "policy-pipeline",
        "status": "success",
        "clauses": [{
            "clause_uid": "PC-1111111111111111",
            "clause_id": "C-1",
            "source_boundary_uids": ["PB-1111111111111111"],
            "clause_kind": "coverage",
            "coverage_type": "합성 진단비",
            "payout_conditions": [{
                "condition_uid": "CI-1111111111111111",
                "text": "보험금 지급요건",
                "evidence_references": [clause_evidence],
                "support_level": "direct",
                "support_rationale": "인용된 문구가 보험금 지급요건을 직접 명시함",
                "review_required": False,
            }],
            "exclusions": [],
            "reduction_conditions": [],
            "definitions": [],
            "obligations": [],
            "claim_requirements": [],
            "termination_conditions": [],
            "dispute_resolution_conditions": [],
            "coverage_start_conditions": [],
            "reference_table_refs": [],
            "confidence": 0.95,
            "evidence_references": [clause_evidence],
            "review_required": False,
        }],
    }


@pytest.fixture
def synthetic_policy_and_denial_fixture(synthetic_policy_only_fixture):
    item = reason("R04", "denial")
    item["policy_matches"] = [{
        "policy_match_id": "PM_1",
        "document_id": "DOC_001",
        "clause_id": "C-1",
        "match_source": "insurer_cited",
        "relevance_note": "보험사가 합성 약관 지급요건을 인용함",
        "insurer_citation_evidence_references": [
            evidence("DOC_002", 1, "약관상 보험금 지급요건을 충족하지 않음")
        ],
        "policy_clause_evidence_references": [evidence("DOC_001", 2, "보험금 지급요건")],
        "confidence": 0.95,
        "review_required": False,
    }]
    item["raw_reason_text"] = "약관상 보험금 지급요건을 충족하지 않음"
    item["evidence_references"] = [
        evidence("DOC_002", 1, "약관상 보험금 지급요건을 충족하지 않음")
    ]
    return {
        "policy": synthetic_policy_only_fixture,
        "denial": denial_result(item),
    }


@pytest.fixture
def synthetic_full_phase2_fixture(synthetic_policy_and_denial_fixture):
    return {
        **synthetic_policy_and_denial_fixture,
        "page_chunks": {
            "case_id": "CASE_900",
            "component": "document-pipeline",
            "status": "success",
            "chunks": [{
                "chunk_id": "CHUNK_1",
                "document_id": "DOC_003",
                "page_start": 1,
                "page_end": 1,
                "text": "합성 사건 증거",
            }],
        },
        "claim_fields": {
            "case_id": "CASE_900",
            "component": "claim-analysis",
            "status": "success",
            "fields": {
                "diagnosis_name": {
                    "value": "합성 진단",
                    "confidence": 0.9,
                    "evidence_references": [evidence("DOC_003", 1, "합성 진단")],
                    "review_required": False,
                }
            },
        },
        "validation": {
            "case_id": "CASE_900",
            "component": "denial-validation",
            "status": "success",
            "validations": [{
                "reason_id": "DR_1",
                "verdict": "not_supported",
                "verdict_explanation": "합성 사건 증거와 보험사 주장이 일치하지 않을 가능성이 있음",
                "retrieved_chunk_ids": ["CHUNK_1"],
                "policy_match_validations": [{
                    "policy_match_id": "PM_1",
                    "verification_status": "verified",
                    "verification_explanation": "합성 약관 조항과 기록 위치가 일치함",
                    "evidence_references": [evidence("DOC_001", 2, "보험금 지급요건")],
                    "review_required": False,
                }],
                "evidence_references": [evidence("DOC_003", 1, "합성 사건 증거")],
                "confidence": 0.8,
                "review_required": True,
                "reviewer_role": "손해사정사",
            }],
        },
    }


def test_synthetic_policy_only_fixture_validates(synthetic_policy_only_fixture, validator):
    assert errors(
        synthetic_policy_only_fixture, "normalized_policy_clause.schema.json", validator
    ) == []


def test_synthetic_policy_plus_denial_fixture_has_resolvable_policy_link(
    synthetic_policy_and_denial_fixture, validator
):
    policy = synthetic_policy_and_denial_fixture["policy"]
    denial = synthetic_policy_and_denial_fixture["denial"]
    assert errors(policy, "normalized_policy_clause.schema.json", validator) == []
    assert errors(denial, "denial_reason_result.schema.json", validator) == []

    clause_ids = {clause["clause_id"] for clause in policy["clauses"]}
    match = denial["denial_reasons"][0]["policy_matches"][0]
    assert match["clause_id"] in clause_ids


def test_synthetic_full_phase2_fixture_validates_all_stage_contracts(
    synthetic_full_phase2_fixture, validator
):
    bundle = synthetic_full_phase2_fixture
    expected = [
        ("policy", "normalized_policy_clause.schema.json"),
        ("denial", "denial_reason_result.schema.json"),
        ("page_chunks", "page_chunks.schema.json"),
        ("claim_fields", "extracted_claim_fields.schema.json"),
        ("validation", "denial_validation_result.schema.json"),
    ]
    for key, schema_name in expected:
        assert errors(bundle[key], schema_name, validator) == [], key


def test_verified_policy_match_requires_verification_evidence(
    synthetic_full_phase2_fixture, validator
):
    bad = copy.deepcopy(synthetic_full_phase2_fixture["validation"])
    bad["validations"][0]["policy_match_validations"][0]["evidence_references"] = []
    assert errors(bad, "denial_validation_result.schema.json", validator)


# --- v0.5: a split outcome (some coverage denied, another accepted) ----------
#
# `decision_type`/`payment_status` were always scoped to "one claim coverage or
# claim item", but nothing recorded WHICH one -- so CASE_907's insurer denying
# 배상책임 while paying 2,000,000원 구내치료비 could only be recorded as the
# denial, and `payment_status: unpaid` read as "nothing was paid".

def _split_outcome():
    return {
        "case_id": "CASE_907", "component": "denial-response",
        "status": "success",
        "denial_reasons": [{
            "reason_id": "DR_1",
            "decided_coverage": "배상책임",
            "decision_type": "denial", "payment_status": "unpaid",
            "taxonomy_code": "R04", "candidate_codes": [
                {"taxonomy_code": "R04", "confidence": 0.8}],
            "raw_reason_text": "법률상 배상책임 불성립",
            "insurer_claim_summary": "시설 하자 없음",
            "grounds": {"contractual_basis": [], "medical_or_factual_basis": [],
                        "calculation_basis": []},
            "amounts": {"claimed_amount": None, "payable_amount": None,
                        "denied_amount": None, "reduction_amount": None,
                        "reduction_rate": None},
            "requested_documents": [], "policy_matches": [],
            "confidence": 0.8, "review_required": True,
            "reviewer_role": "손해사정사",
            "evidence_references": [
                {"document_id": "DOC_002", "page": 1, "quote": "불성립"}],
        }],
        "accepted_coverages": [{
            "accepted_coverage_id": "AC_1",
            "coverage_name": "구내치료비",
            "payment_status": "paid",
            "accepted_amount": 2000000,
            "confidence": 0.9, "review_required": False,
            "evidence_references": [
                {"document_id": "DOC_002", "page": 5, "quote": "구내치료비"}],
        }],
    }


def test_a_split_outcome_records_both_sides(validator):
    assert errors(
        _split_outcome(), "denial_reason_result.schema.json", validator) == []
    assert _cross_contract.check_denial_reason_result(_split_outcome()) == []


def test_a_contract_predating_the_split_fields_still_validates(validator):
    """Both fields are additive; every pre-2026-08-04 contract must still pass."""
    legacy = _split_outcome()
    del legacy["accepted_coverages"]
    del legacy["denial_reasons"][0]["decided_coverage"]
    assert errors(
        legacy, "denial_reason_result.schema.json", validator) == []


def test_one_coverage_cannot_be_both_denied_and_accepted():
    """A response cannot pay and refuse the same coverage.

    Far likelier than a real contradiction is one decision filed twice, which
    would show a reader a split outcome that never happened.
    """
    bad = _split_outcome()
    bad["accepted_coverages"][0]["coverage_name"] = "배상책임"
    found = _cross_contract.check_denial_reason_result(bad)
    assert any("cannot be both accepted and denied" in e for e in found), found


def test_policy_match_ids_are_unique_across_reasons_and_acceptances():
    """The two lists share one id namespace -- downstream resolves an id
    without knowing which side it came from."""
    bad = _split_outcome()
    match = {"policy_match_id": "PM_1", "document_id": "DOC_004",
             "clause_id": "C-1", "match_source": "agent_inferred",
             "relevance_note": "x",
             "insurer_citation_evidence_references": [],
             "policy_clause_evidence_references": [
                 {"document_id": "DOC_004", "page": 1, "quote": "q"}],
             "confidence": 0.7, "review_required": True}
    bad["denial_reasons"][0]["policy_matches"] = [match]
    bad["accepted_coverages"][0]["policy_matches"] = [dict(match)]
    found = _cross_contract.check_denial_reason_result(bad)
    assert any("duplicate policy_match_id" in e for e in found), found


def test_an_acceptance_does_not_disturb_an_existing_upstream_hash():
    """Adding the field must not mark real, unchanged downstream work stale."""
    without = _split_outcome()
    del without["accepted_coverages"]
    baseline = _cross_contract.upstream_hash(without)
    without["accepted_coverages"] = []
    assert _cross_contract.upstream_hash(without) == baseline
    assert _cross_contract.upstream_hash(_split_outcome()) != baseline
