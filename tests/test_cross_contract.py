"""Cross-contract enforcement for normalized_policy_clause files.

Two layers are tested:
  1. The pure checks in _cross_contract.py (clause_id sequentiality, page/quote
     source verification, foreign-document rejection, SourceUnavailable).
  2. The DAO write-contract path end to end -- that a failing contract is
     REFUSED and, critically, that no file is written / no existing file is
     overwritten (fail-before-persist).

Every filesystem test runs against the isolated_dao tmp_path fixture, never the
real outputs/ or data/. schemas/ is the project's real schema set (conftest).
"""
import json
import copy
"""Cross-contract invariants (tools/_cross_contract.py) and their DAO gate.

Every attack below validated CLEANLY against the real committed outputs
before this layer existed -- they are reproductions of an audit's findings,
not hypotheticals:

  * denial_validation_result.json naming `DR_999`, a reason id that exists
    nowhere, passed schema validation.
  * So did dropping validations for real reasons, and duplicating one.
  * So did two denial reasons both calling themselves `DR_1`.
  * So did `candidate_codes` whose top entry disagreed with the assigned
    `taxonomy_code` -- the Top-1 evaluation input describing a classification
    nobody made.

None of these are expressible in JSON Schema: they compare a document to a
sibling file, or one array member to another. The fixtures are built to the
real contracts' shapes so a schema revision that breaks them shows up here.
"""
import copy
import json
from pathlib import Path

import pytest

import dao
import _cross_contract
from _cross_contract import (
    SourceUnavailable,
    check_normalized_policy_clause,
    check_clause_ids,
    check_stable_ids_and_semantics,
    doc_id_from_filename,
    split_pages,
    unresolved_reference_table_reviews,
)


# --------------------------------------------------------------------------
# Fixtures: a valid two-page redacted policy text and a matching clause file.
# --------------------------------------------------------------------------

REDACTED_TEXT = (
    "<<<PAGE page=1>>>\n"
    "제3조(보험금의 지급사유) 회사는 피보험자가 보험기간 중 상해로 사망한 경우 사망보험금을 지급합니다.\n"
    "다만 피보험자가 고의로 자신을 해친 경우에는 보험금을 지급하지 아니합니다.\n"
    "<<<PAGE page=2>>>\n"
    "제5조(보험금의 감액) 계약일부터 2년 이내에 보험금 지급사유가 발생한 경우 보험금의 50%를 감액하여 지급합니다.\n"
)


def _valid_clause():
    return {
        "clause_uid": "PC-1111111111111111",
        "clause_id": "C-1",
        "source_boundary_uids": ["PB-1111111111111111"],
        "clause_kind": "coverage",
        "coverage_type": "상해사망",
        "payout_conditions": [
            {
                "condition_uid": "CI-1111111111111111",
                "text": "보험기간 중 상해로 사망",
                "evidence_references": [
                    {
                        "document_id": "DOC_001",
                        "page": 1,
                        "quote": "피보험자가 보험기간 중 상해로 사망한 경우 사망보험금을 지급합니다",
                    }
                ],
                "support_level": "direct",
                "support_rationale": "The cited sentence states the insured event and payment.",
                "review_required": False,
            }
        ],
        "exclusions": [
            {
                "condition_uid": "CI-2222222222222222",
                "text": "고의로 자신을 해친 경우",
                "evidence_references": [
                    {
                        "document_id": "DOC_001",
                        "page": 1,
                        "quote": "피보험자가 고의로 자신을 해친 경우에는 보험금을 지급하지 아니합니다",
                    }
                ],
                "support_level": "direct",
                "support_rationale": "The cited sentence states the exclusion.",
                "review_required": False,
            }
        ],
        "reduction_conditions": [
            {
                "condition_uid": "CI-3333333333333333",
                "text": "계약일부터 2년 이내 지급사유 발생 시 50% 감액",
                "evidence_references": [
                    {
                        "document_id": "DOC_001",
                        "page": 2,
                        "quote": "계약일부터 2년 이내에 보험금 지급사유가 발생한 경우 보험금의 50%를 감액하여 지급합니다",
                    }
                ],
                "support_level": "direct",
                "support_rationale": "The cited sentence states the reduction period and rate.",
                "review_required": False,
            }
        ],
        "definitions": [],
        "obligations": [],
        "claim_requirements": [],
        "termination_conditions": [],
        "dispute_resolution_conditions": [],
        "coverage_start_conditions": [],
        "reference_table_refs": [],
        "confidence": 0.9,
        "evidence_references": [
            {"document_id": "DOC_001", "page": 1, "quote": "제3조(보험금의 지급사유)"}
        ],
        "review_required": False,
    }


def _contract(clauses, case_id="CASE_030", run_id="RUN_20260723_001"):
    return {
        "case_id": case_id,
        "run_id": run_id,
        "component": "policy-pipeline",
        "status": "success",
        "clauses": clauses,
    }


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_doc_id_from_filename():
    assert doc_id_from_filename("normalized_policy_clause_DOC_001.json") == "DOC_001"
    assert doc_id_from_filename("normalized_policy_clause_DOC_042.json") == "DOC_042"
    assert doc_id_from_filename("normalized_policy_clause.json") is None


def test_split_pages_basic():
    pages = split_pages(REDACTED_TEXT)
    assert set(pages) == {1, 2}
    assert "상해로 사망" in pages[1]
    assert "감액하여 지급합니다" in pages[2]


def test_split_pages_no_markers_is_source_unavailable():
    with pytest.raises(SourceUnavailable):
        split_pages("제3조 회사는 보험금을 지급합니다. (no page markers at all)")


def test_split_pages_non_monotonic_is_source_unavailable():
    with pytest.raises(SourceUnavailable):
        split_pages("<<<PAGE page=2>>>\nfoo\n<<<PAGE page=1>>>\nbar\n")


def test_split_pages_content_before_first_marker_is_source_unavailable():
    with pytest.raises(SourceUnavailable):
        split_pages("stray text\n<<<PAGE page=1>>>\nfoo\n")


def test_check_clause_ids_accepts_sequential():
    assert check_clause_ids([{"clause_id": "C-1"}, {"clause_id": "C-2"}, {"clause_id": "C-3"}]) == []


def test_check_clause_ids_rejects_duplicate():
    errors = check_clause_ids([{"clause_id": "C-1"}, {"clause_id": "C-1"}])
    assert any("duplicate" in e for e in errors)


def test_check_clause_ids_rejects_gap():
    errors = check_clause_ids([{"clause_id": "C-1"}, {"clause_id": "C-3"}])
    assert any("sequentiality" in e for e in errors)


# --------------------------------------------------------------------------
# check_normalized_policy_clause -- positive
# --------------------------------------------------------------------------

def test_valid_contract_passes():
    errors = check_normalized_policy_clause(
        _contract([_valid_clause()]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert errors == []


def test_empty_condition_lists_are_fine():
    clause = _valid_clause()
    clause["exclusions"] = []
    clause["reduction_conditions"] = []
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert errors == []


def test_two_clauses_sequential_pass():
    c1 = _valid_clause()
    c2 = _valid_clause()
    c2["clause_id"] = "C-2"
    c2["clause_uid"] = "PC-2222222222222222"
    c2["source_boundary_uids"] = ["PB-2222222222222222"]
    for index, bucket in enumerate(("payout_conditions", "exclusions", "reduction_conditions"), 4):
        c2[bucket][0]["condition_uid"] = f"CI-{str(index) * 16}"
    errors = check_normalized_policy_clause(
        _contract([c1, c2]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert errors == []


def test_duplicate_stable_clause_uid_is_rejected():
    c1 = _valid_clause()
    c2 = _valid_clause()
    c2["clause_id"] = "C-2"
    errors = check_stable_ids_and_semantics([c1, c2])
    assert any("duplicate clause_uid" in error for error in errors)


def test_duplicate_condition_uid_is_rejected():
    clause = _valid_clause()
    clause["exclusions"][0]["condition_uid"] = \
        clause["payout_conditions"][0]["condition_uid"]
    errors = check_stable_ids_and_semantics([clause])
    assert any("duplicate condition_uid" in error for error in errors)


def test_obligation_cannot_be_stored_as_payout_condition():
    clause = _valid_clause()
    clause["clause_kind"] = "obligation"
    errors = check_stable_ids_and_semantics([clause])
    assert any("cannot populate" in error for error in errors)


def test_display_reordering_does_not_change_stable_uid():
    original = _valid_clause()
    stable_uid = original["clause_uid"]
    original["clause_id"] = "C-2"
    assert original["clause_uid"] == stable_uid


# --------------------------------------------------------------------------
# check_normalized_policy_clause -- negative (each task rule)
# --------------------------------------------------------------------------

def test_condition_missing_document_id():
    clause = _valid_clause()
    del clause["payout_conditions"][0]["evidence_references"][0]["document_id"]
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("missing document_id" in e for e in errors)


def test_condition_missing_page():
    clause = _valid_clause()
    del clause["payout_conditions"][0]["evidence_references"][0]["page"]
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("missing page" in e for e in errors)


def test_condition_empty_quote():
    clause = _valid_clause()
    clause["payout_conditions"][0]["evidence_references"][0]["quote"] = "   "
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("empty/whitespace quote" in e for e in errors)


def test_filename_docid_mismatch_with_reference():
    # File says DOC_002 but every reference cites DOC_001.
    errors = check_normalized_policy_clause(
        _contract([_valid_clause()]), "normalized_policy_clause_DOC_002.json", REDACTED_TEXT
    )
    # DOC_002 has no processed text here -> SourceUnavailable would fire first
    # only if references matched. They don't, so we get foreign-doc errors.
    assert any("another document" in e or "SOURCE" in e for e in errors) or errors


def test_foreign_document_reference_rejected():
    clause = _valid_clause()
    clause["reduction_conditions"][0]["evidence_references"][0]["document_id"] = "DOC_999"
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("another document" in e and "DOC_999" in e for e in errors)


def test_nonexistent_page_rejected():
    clause = _valid_clause()
    clause["reduction_conditions"][0]["evidence_references"][0]["page"] = 9
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("does not exist" in e and "page 9" in e for e in errors)


def test_quote_not_in_source_rejected():
    clause = _valid_clause()
    clause["payout_conditions"][0]["evidence_references"][0]["quote"] = "이 문장은 원문에 존재하지 않습니다"
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("not found on page" in e for e in errors)


def test_correct_quote_but_wrong_page_rejected():
    clause = _valid_clause()
    # This quote is real, but it lives on page 1, not page 2.
    clause["payout_conditions"][0]["evidence_references"][0]["page"] = 2
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("not found on page 2" in e for e in errors)


def test_clause_title_quote_reused_for_condition_is_rejected():
    # A condition whose only "quote" is the clause heading -- which does exist
    # on the page, but does not support the condition. The clause heading quote
    # ("제3조(보험금의 지급사유)") appears on the page, so a naive check passes;
    # the point of this test is that using a heading as a condition's grounding
    # is caught when the heading text does not actually contain the condition.
    # We model "title-only" as: the condition cites the heading quote, which is
    # too short to be the condition's real support. The verbatim check still
    # matches (heading is on-page), so this documents the residual semantic gap
    # the human sample sheet is meant to catch. Here we assert the structural
    # layer at least still requires the quote to be on-page.
    clause = _valid_clause()
    clause["payout_conditions"][0]["evidence_references"][0]["quote"] = "제3조(보험금의 지급사유)"
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("only clause heading" in error for error in errors)


def test_unrelated_on_page_quote_is_rejected_as_semantically_weak():
    clause = _valid_clause()
    clause["payout_conditions"][0]["evidence_references"][0]["quote"] = \
        "제5조(보험금의 감액)"
    clause["payout_conditions"][0]["evidence_references"][0]["page"] = 2
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        REDACTED_TEXT)
    assert any(
        "only clause heading" in error or "insufficient lexical support" in error
        for error in errors)


def test_insufficient_support_level_is_rejected():
    clause = _valid_clause()
    clause["payout_conditions"][0]["support_level"] = "insufficient"
    clause["payout_conditions"][0]["review_required"] = True
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        REDACTED_TEXT)
    assert any("cannot be emitted" in error for error in errors)


def test_composite_support_requires_review_flag():
    clause = _valid_clause()
    item = clause["payout_conditions"][0]
    item["support_level"] = "composite"
    item["evidence_references"].append({
        "document_id": "DOC_001", "page": 1,
        "quote": "사망보험금을 지급합니다",
    })
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        REDACTED_TEXT)
    assert any("requires review_required=true" in error for error in errors)


def test_direct_evidence_must_include_complete_operative_predicate():
    clause = _valid_clause()
    item = clause["exclusions"][0]
    item["text"] = "사망보험금을 지급하지 않는다"
    item["evidence_references"][0]["quote"] = \
        "피보험자가 고의로 자신을 해친 경우에는 사망보험금을"
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        REDACTED_TEXT)
    # The truncation itself is still the blocker. The polarity of a quote cut
    # off before its verb is deliberately no longer an error -- an unsettled
    # reading means the fragment does not state the predicate, which is what
    # "complete policy proposition" already says.
    assert any("complete policy proposition" in error for error in errors)


def test_exclusion_bucket_requires_exclusion_marker_in_evidence():
    clause = _valid_clause()
    item = clause["exclusions"][0]
    item["evidence_references"][0]["quote"] = \
        "피보험자가 고의로 자신을 해친 경우"
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        REDACTED_TEXT)
    assert any("operative marker" in error and "exclusions" in error
               for error in errors), errors


def test_numeric_and_temporal_terms_must_be_present_in_evidence():
    clause = _valid_clause()
    item = clause["reduction_conditions"][0]
    item["text"] = "계약일부터 2년 이내 지급사유 발생 시 50% 감액, 3회 한도"
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        REDACTED_TEXT)
    assert any("3회" in error and "absent from its evidence" in error
               for error in errors), errors


def test_truncated_payment_sentence_is_rejected():
    clause = _valid_clause()
    item = clause["payout_conditions"][0]
    item["text"] = "보험수익자에게 사망보험금을 지급하여"
    item["evidence_references"][0]["quote"] = \
        "사망보험금을 지급하여"
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        REDACTED_TEXT)
    assert any("complete policy proposition" in error for error in errors), errors


# --------------------------------------------------------------------------
# Part 11B: bidirectional polarity and complete operative predicates.
# --------------------------------------------------------------------------

# A redacted page carrying both a positive payout sentence and a negative
# exclusion sentence, plus a truncated-predicate sentence, so each polarity
# case can cite a verbatim quote.
POLARITY_TEXT = (
    "<<<PAGE page=1>>>\n"
    "제3조(보험금의 지급) 회사는 피보험자가 사망한 경우 사망보험금을 지급합니다.\n"
    "회사는 피보험자가 고의로 자신을 해친 경우 보험금을 지급하지 않습니다.\n"
    "회사가 보험금을 지급하는 경우\n"
)


def _polarity_clause():
    """A single positive payout condition citing the positive sentence."""
    clause = _valid_clause()
    clause["exclusions"] = []
    clause["reduction_conditions"] = []
    clause["payout_conditions"] = [{
        "condition_uid": "CI-1111111111111111",
        "text": "피보험자가 사망한 경우 사망보험금을 지급",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1,
            "quote": "회사는 피보험자가 사망한 경우 사망보험금을 지급합니다",
        }],
        "support_level": "direct",
        "support_rationale": "positive payout sentence",
        "review_required": False,
    }]
    return clause


def test_positive_condition_with_negative_evidence_is_rejected():
    clause = _polarity_clause()
    # Positive payout condition, but grounded ONLY in the negative sentence.
    clause["payout_conditions"][0]["evidence_references"][0]["quote"] = \
        "회사는 피보험자가 고의로 자신을 해친 경우 보험금을 지급하지 않습니다"
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        POLARITY_TEXT)
    assert any("polarity contradiction" in e and "positive" in e for e in errors), errors


def test_negative_condition_with_positive_evidence_is_rejected():
    clause = _polarity_clause()
    clause["payout_conditions"] = []
    clause["exclusions"] = [{
        "condition_uid": "CI-2222222222222222",
        "text": "보험금을 지급하지 않는 경우",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1,
            # A positive sentence cited for a negative exclusion condition.
            "quote": "회사는 피보험자가 사망한 경우 사망보험금을 지급합니다",
        }],
        "support_level": "direct",
        "support_rationale": "mismatched polarity",
        "review_required": False,
    }]
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        POLARITY_TEXT)
    assert any("polarity contradiction" in e for e in errors), errors


def test_unresolved_operative_predicate_is_rejected():
    clause = _polarity_clause()
    clause["payout_conditions"][0]["text"] = "회사가 보험금을 지급하는 경우"
    clause["payout_conditions"][0]["evidence_references"][0]["quote"] = \
        "회사가 보험금을 지급하는 경우"
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        POLARITY_TEXT)
    assert any("unresolved operative predicate" in e for e in errors), errors


def test_normal_positive_payout_passes_polarity():
    clause = _polarity_clause()
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        POLARITY_TEXT)
    assert not any("polarity" in e or "unresolved operative" in e for e in errors), errors


def test_normal_exclusion_passes_polarity():
    clause = _polarity_clause()
    clause["payout_conditions"] = []
    clause["exclusions"] = [{
        "condition_uid": "CI-2222222222222222",
        "text": "고의로 자신을 해친 경우 지급하지 않음",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1,
            "quote": "회사는 피보험자가 고의로 자신을 해친 경우 보험금을 지급하지 않습니다",
        }],
        "support_level": "direct",
        "support_rationale": "negative exclusion sentence",
        "review_required": False,
    }]
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        POLARITY_TEXT)
    assert not any("polarity" in e for e in errors), errors


def test_composite_mixed_polarity_requires_review():
    clause = _polarity_clause()
    clause["payout_conditions"][0].update({
        "support_level": "composite",
        "review_required": False,  # the offending state -- must be flagged
        "text": "사망 시 지급하되 고의는 지급하지 않음",
        "evidence_references": [
            {"document_id": "DOC_001", "page": 1,
             "quote": "회사는 피보험자가 사망한 경우 사망보험금을 지급합니다"},
            {"document_id": "DOC_001", "page": 1,
             "quote": "회사는 피보험자가 고의로 자신을 해친 경우 보험금을 지급하지 않습니다"},
        ],
    })
    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        POLARITY_TEXT)
    assert any("mixes positive and negative polarity" in e for e in errors), errors


# --------------------------------------------------------------------------
# P1-2: predicate/scope-aware Korean policy polarity.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("보험금을 지급합니다", "affirmative"),
    ("제한 없이 보험금을 지급합니다", "affirmative"),
    ("보험금 지급을 제한하지 않습니다", "affirmative"),
    ("회사는 이 사유로 면책하지 않습니다", "affirmative"),
    ("보험금 지급 제한이 없습니다", "affirmative"),
    ("보험금을 지급하지 않습니다", "restrictive_or_negative"),
    ("보험금을 지급할 수 없습니다", "restrictive_or_negative"),
    ("보장 대상에서 제외합니다", "restrictive_or_negative"),
    ("회사는 책임을 지지 않습니다", "restrictive_or_negative"),
    ("보험금 지급을 제한합니다", "restrictive_or_negative"),
    ("제한이 없는 경우에도 보험금을 지급하지 않습니다",
     "restrictive_or_negative"),
    ("지급하지 않는 경우를 제한합니다", "ambiguous"),
    ("보험금을 지급하지 않는 것은 아닙니다", "ambiguous"),
    ("보장하지 않는 사항이 없습니다", "ambiguous"),
    ("보험금을 지급하지 않습니다. 다만 특정 수술은 지급합니다",
     "mixed"),
    ("보험금을 지급하되 고의 사고는 제외합니다", "mixed"),
])
def test_korean_policy_polarity_uses_predicate_and_negation_scope(
        text, expected):
    analysis = _cross_contract._analyze_policy_polarity(text)

    assert analysis.classification == expected
    assert analysis.reason
    assert analysis.matches
    for match in analysis.matches:
        assert match.target_predicate
        assert match.negation_scope
        assert match.start_char >= 0
        assert match.end_char > match.start_char
        assert text[match.start_char:match.end_char] == match.matched_text


def test_scope_analysis_distinguishes_restriction_from_unrestricted_payment():
    restricted = _cross_contract._analyze_policy_polarity(
        "보험금 지급을 제한합니다")
    unrestricted = _cross_contract._analyze_policy_polarity(
        "제한 없이 보험금을 지급합니다")

    assert restricted.classification == "restrictive_or_negative"
    assert unrestricted.classification == "affirmative"
    assert any(match.target_predicate == "제한"
               and match.negation_scope == "restriction_negated"
               for match in unrestricted.matches)


def test_polarity_analyzer_is_deterministic_and_does_not_rewrite_input():
    text = "  제한 없이 보험금을 지급합니다  "
    before = text

    first = _cross_contract._analyze_policy_polarity(text)
    second = _cross_contract._analyze_policy_polarity(text)

    assert first == second
    assert text == before


P1_2_POLARITY_TEXT = (
    "<<<PAGE page=1>>>\n"
    "제3조(보험금의 지급사유)\n"
    "회사는 제한 없이 보험금을 지급합니다.\n"
    "회사는 보험금을 지급하지 않습니다. 다만 특정 수술은 지급합니다.\n"
)


def test_unrestricted_payment_condition_and_evidence_pass_end_to_end():
    clause = _polarity_clause()
    item = clause["payout_conditions"][0]
    item["text"] = "제한 없이 보험금을 지급합니다"
    item["evidence_references"][0]["quote"] = \
        "회사는 제한 없이 보험금을 지급합니다"

    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        P1_2_POLARITY_TEXT)

    assert not any("polarity" in error for error in errors), errors


def test_mixed_polarity_inside_one_evidence_quote_is_allowed():
    """An unsettled reading is reported by the analyzer but does not block.

    A quote carrying both directions ("지급하지 않습니다. 다만 ... 지급합니다")
    is real policy language, and the buckets it would be gating have no
    downstream consumer -- clauses are matched on document/UID/location and
    quoted text. Only an explicit contradiction still blocks.
    """
    clause = _polarity_clause()
    item = clause["payout_conditions"][0]
    item["text"] = "특정 수술은 보험금을 지급합니다"
    item["evidence_references"][0]["quote"] = (
        "회사는 보험금을 지급하지 않습니다. 다만 특정 수술은 지급합니다")

    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        P1_2_POLARITY_TEXT)

    assert not any("polarity" in error for error in errors), errors


def test_ambiguous_double_negation_evidence_is_allowed():
    """Double negation the analyzer cannot settle no longer blocks the write.

    It reads "지급하지 않는 것은 아닙니다" as ambiguous rather than guessing at
    the scope, which is the honest answer -- but an honest "I cannot tell" is
    not evidence of a defect, and routing it to a human bought review time to
    protect a bucket field nothing downstream reads.
    """
    text = (
        "<<<PAGE page=1>>>\n"
        "제3조(보험금의 지급사유)\n"
        "회사가 보험금을 지급하지 않는 것은 아닙니다.\n"
    )
    clause = _polarity_clause()
    item = clause["payout_conditions"][0]
    item["text"] = "보험금을 지급합니다"
    item["evidence_references"][0]["quote"] = \
        "회사가 보험금을 지급하지 않는 것은 아닙니다"

    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", text)

    assert not any("polarity" in error for error in errors), errors


def test_polarity_validation_never_mutates_normalized_condition():
    clause = _polarity_clause()
    item = clause["payout_conditions"][0]
    item["text"] = "제한 없이 보험금을 지급합니다"
    item["evidence_references"][0]["quote"] = \
        "회사는 제한 없이 보험금을 지급합니다"
    contract = _contract([clause])
    before = copy.deepcopy(contract)

    check_normalized_policy_clause(
        contract, "normalized_policy_clause_DOC_001.json",
        P1_2_POLARITY_TEXT)

    assert contract == before


# --------------------------------------------------------------------------
# P1-2 follow-up: bucket/condition binding and modal double negation.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "보험금을 지급하지 않으면 안 됩니다",
    "보험금을 지급하지 않을 수 없습니다",
    "보험금을 지급하지 않아도 되는 것은 아닙니다",
    "보험금 지급을 제한하지 않을 수 없습니다",
    "보험금 지급을 제한하지 않는 것은 아닙니다",
    "회사가 면책하지 않는 것은 아닙니다",
])
def test_modal_or_repeated_negation_is_ambiguous(text):
    analysis = _cross_contract._analyze_policy_polarity(text)

    assert analysis.classification == "ambiguous"
    assert analysis.matches
    assert any(match.negation_scope == "modal_or_repeated_negation"
               for match in analysis.matches)


def _single_bucket_clause(bucket, condition, evidence):
    clause = _polarity_clause()
    for name in (
            "payout_conditions", "exclusions", "reduction_conditions",
            "coverage_start_conditions"):
        clause[name] = []
    clause[bucket] = [{
        "condition_uid": "CI-9999999999999999",
        "text": condition,
        "evidence_references": [{
            "document_id": "DOC_001",
            "page": 1,
            "quote": evidence,
        }],
        "support_level": "direct",
        "support_rationale": "P1-2 follow-up polarity fixture",
        "review_required": False,
    }]
    return clause


@pytest.mark.parametrize("bucket, condition, evidence, expected", [
    (
        "payout_conditions",
        "보험금을 지급하지 않습니다",
        "보험금을 지급하지 않습니다",
        "affirmative",
    ),
    (
        "exclusions",
        "회사는 이 사유로 면책하지 않습니다",
        "회사는 이 사유로 면책하지 않습니다",
        "restrictive_or_negative",
    ),
    (
        "reduction_conditions",
        "보험금을 전액 지급합니다",
        "보험금을 전액 지급합니다",
        "restrictive_or_negative",
    ),
    (
        "coverage_start_conditions",
        "회사는 보장하지 않습니다",
        "회사는 보장하지 않습니다",
        "affirmative",
    ),
])
def test_condition_and_evidence_cannot_agree_against_bucket(
        bucket, condition, evidence, expected):
    text = (
        "<<<PAGE page=1>>>\n"
        "제3조(보험금의 지급사유)\n"
        f"{evidence}\n"
    )
    clause = _single_bucket_clause(bucket, condition, evidence)

    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json", text)

    assert any(
        "bucket-condition polarity mismatch" in error
        and bucket in error
        and expected in error
        for error in errors
    ), errors


def test_trigger_only_condition_keeps_bucket_fallback():
    clause = _valid_clause()

    errors = check_normalized_policy_clause(
        _contract([clause]), "normalized_policy_clause_DOC_001.json",
        REDACTED_TEXT)

    assert not any("bucket-condition polarity mismatch" in error
                   for error in errors), errors


def test_followup_polarity_checks_do_not_mutate_contract():
    evidence = "보험금을 지급하지 않습니다"
    clause = _single_bucket_clause(
        "payout_conditions", evidence, evidence)
    contract = _contract([clause])
    before = copy.deepcopy(contract)
    text = (
        "<<<PAGE page=1>>>\n"
        "제3조(보험금의 지급사유)\n"
        f"{evidence}\n"
    )

    check_normalized_policy_clause(
        contract, "normalized_policy_clause_DOC_001.json", text)

    assert contract == before


def test_reference_table_review_flags_are_finalize_blockers():
    table = {
        "tables": [{
            "table_uid": "RT-1111111111111111",
            "review_required": True,
            "rows": [{
                "row_uid": "RR-1111111111111111",
                "cells": [{
                    "cell_uid": "RC-1111111111111111",
                    "review_required": True,
                }],
            }],
        }],
    }
    blockers = unresolved_reference_table_reviews(table)
    assert any("tables[0]" in blocker for blocker in blockers)
    assert any("cells[0]" in blocker for blocker in blockers)


def test_duplicate_clause_id_rejected():
    c1 = _valid_clause()
    c2 = _valid_clause()  # also C-1
    errors = check_normalized_policy_clause(
        _contract([c1, c2]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("duplicate" in e for e in errors)


def test_gap_clause_id_rejected():
    c1 = _valid_clause()
    c2 = _valid_clause()
    c2["clause_id"] = "C-3"  # gap: C-1 then C-3
    errors = check_normalized_policy_clause(
        _contract([c1, c2]), "normalized_policy_clause_DOC_001.json", REDACTED_TEXT
    )
    assert any("sequentiality" in e for e in errors)


def test_no_processed_text_is_source_unavailable():
    with pytest.raises(SourceUnavailable):
        check_normalized_policy_clause(
            _contract([_valid_clause()]), "normalized_policy_clause_DOC_001.json", None
        )


def test_untrustworthy_page_boundaries_is_source_unavailable():
    with pytest.raises(SourceUnavailable):
        check_normalized_policy_clause(
            _contract([_valid_clause()]),
            "normalized_policy_clause_DOC_001.json",
            "no markers here, just text 상해로 사망",
        )


# --------------------------------------------------------------------------
# DAO write-contract path -- fail-before-persist
# --------------------------------------------------------------------------

def _seed_redacted(isolated_dao, case_id="CASE_030", doc_id="DOC_001", text=REDACTED_TEXT):
    d = isolated_dao / "data" / "processed" / case_id / doc_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "redacted_text.md").write_text(text, encoding="utf-8")


def _seed_manifest(isolated_dao, case_id="CASE_030", doc_ids=("DOC_001",)):
    """A normalized_policy_clause write now requires the cited document to be a
    registered automated-text source in the manifest (Part 2). Seed a minimal
    physical entry per doc so the cross-contract quote checks -- the actual
    subject of these tests -- can run."""
    docs = [{
        "document_id": did, "file_name": f"{did}.pdf",
        "file_path": f"data/raw/{case_id}/{did}.pdf", "file_format": "pdf",
        "file_size_bytes": 100, "ocr_status": "completed",
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
    } for did in doc_ids]
    manifest = {"case_id": case_id, "documents": docs}
    out = isolated_dao / "outputs" / case_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "document_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def _write_contract(isolated_dao, make_args, contract, filename, case_id="CASE_030"):
    # The cited doc must be a registered automated-text manifest source (Part 2).
    if not (isolated_dao / "outputs" / case_id / "document_manifest.json").exists():
        _seed_manifest(isolated_dao, case_id=case_id, doc_ids=("DOC_001", "DOC_002"))
    data_file = isolated_dao / "contract.json"
    data_file.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
    args = make_args(
        case_id=case_id, filename=filename, data_file=str(data_file),
        schema_name="normalized_policy_clause.schema.json",
        held_by="policy-pipeline", run_id="RUN_20260723_001",
        stage="policy_clause_processing",
    )
    return dao.cmd_write_contract(args)


def test_dao_write_contract_valid_but_non_canonical_is_refused(isolated_dao,
                                                               make_args):
    """This asserted `rc == 0` until P0-3.

    The contract really is valid in every sense this module tests: its quotes
    are on the cited page, its document is a registered automated-text source,
    its shape passes the schema. What it is not is derived from a document
    whose UIDs were ever verified -- `DOC_001` here has no revision entry at
    all, so its `PC-…`/`CI-…` identifiers are strings the fixture chose.

    That combination passing was the P0-3 defect, and it is the reason a test
    named "valid persists" has to become a refusal: under the old code, "valid"
    did not include "expressed against a document whose identity layer exists".
    The canonical persist-path is covered in test_canonical_uid_mandatory.py,
    against real recomputed UIDs.
    """
    _seed_redacted(isolated_dao)
    rc = _write_contract(isolated_dao, make_args, _contract([_valid_clause()]),
                         "normalized_policy_clause_DOC_001.json")
    assert rc == 1
    target = isolated_dao / "outputs" / "CASE_030" / "normalized_policy_clause_DOC_001.json"
    assert not target.exists()


def test_dao_write_contract_bad_quote_refused_and_not_persisted(isolated_dao, make_args):
    _seed_redacted(isolated_dao)
    clause = _valid_clause()
    clause["payout_conditions"][0]["evidence_references"][0]["quote"] = "존재하지 않는 인용"
    rc = _write_contract(isolated_dao, make_args, _contract([clause]),
                         "normalized_policy_clause_DOC_001.json")
    assert rc == 1
    target = isolated_dao / "outputs" / "CASE_030" / "normalized_policy_clause_DOC_001.json"
    assert not target.exists(), "a failing contract must not be written"


def test_dao_write_contract_source_unavailable_refused(isolated_dao, make_args):
    # No redacted text seeded -> SourceUnavailable -> refuse, not pass.
    rc = _write_contract(isolated_dao, make_args, _contract([_valid_clause()]),
                         "normalized_policy_clause_DOC_001.json")
    assert rc == 1
    target = isolated_dao / "outputs" / "CASE_030" / "normalized_policy_clause_DOC_001.json"
    assert not target.exists()


def test_dao_write_contract_failure_does_not_overwrite_existing(isolated_dao, make_args):
    _seed_redacted(isolated_dao)
    _seed_manifest(isolated_dao, doc_ids=("DOC_001", "DOC_002"))
    target = isolated_dao / "outputs" / "CASE_030" / "normalized_policy_clause_DOC_001.json"
    # The pre-existing contract is placed directly rather than written through
    # the DAO: since P0-3 a non-canonical document cannot be written to at all,
    # and what this test is about is the atomicity of a REFUSED write, not the
    # provenance of what was there before it. A legacy artifact already on disk
    # is exactly the situation a migration starts from.
    target.write_text(
        json.dumps(_contract([_valid_clause()]), ensure_ascii=False),
        encoding="utf-8")
    good_bytes = target.read_bytes()
    # Then: a failing write for the same file must not clobber the good one.
    bad = _valid_clause()
    bad["reduction_conditions"][0]["evidence_references"][0]["document_id"] = "DOC_777"
    rc = _write_contract(isolated_dao, make_args, _contract([bad]),
                         "normalized_policy_clause_DOC_001.json")
    assert rc == 1
    assert target.read_bytes() == good_bytes, "existing good contract must survive a failed rewrite"


def test_dao_write_contract_foreign_doc_refused(isolated_dao, make_args):
    _seed_redacted(isolated_dao)
    clause = _valid_clause()
    for ref_list in ("payout_conditions", "exclusions", "reduction_conditions"):
        for ref in clause[ref_list][0]["evidence_references"]:
            ref["document_id"] = "DOC_002"
    for ref in clause["evidence_references"]:
        ref["document_id"] = "DOC_002"
    rc = _write_contract(isolated_dao, make_args, _contract([clause]),
                         "normalized_policy_clause_DOC_001.json")
    assert rc == 1
import _cross_contract as cc
from _cross_contract import check

ROOT = Path(__file__).resolve().parent.parent


def _reason(reason_id, code="R04", label="약관상 지급요건 미충족", matches=None):
    return {
        "reason_id": reason_id,
        "decision_type": "denial",
        "payment_status": "unpaid",
        "taxonomy_code": code,
        "taxonomy_label": label,
        "candidate_codes": [{"taxonomy_code": code, "confidence": 0.9}],
        "policy_matches": matches if matches is not None else [],
    }


@pytest.fixture
def reasons_doc():
    return {"denial_reasons": [_reason("DR_1"), _reason("DR_2")]}


@pytest.fixture
def case_dir(tmp_path, reasons_doc):
    """A case directory holding a real denial_reason_result.json, so the
    validation contract has something to resolve its ids against."""
    d = tmp_path / "CASE_TEST"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(
        json.dumps(reasons_doc, ensure_ascii=False), encoding="utf-8")
    return d


def _validation(reason_id, match_ids=()):
    return {
        "reason_id": reason_id,
        "verdict": "partially_supported",
        "policy_match_validations": [{"policy_match_id": m} for m in match_ids],
    }


def _derived(case_dir, payload):
    """Stamp the payload with the hash of the denial_reason_result.json in
    this case dir. Downstream contracts must declare what they were built
    from (see tests/test_upstream_staleness.py); these tests are about ids and
    decision types, so the provenance field is filled in correctly rather than
    re-asserted here."""
    upstream = json.loads((case_dir / "denial_reason_result.json").read_text(encoding="utf-8"))
    return dict(payload, source_denial_contract_hash=cc.upstream_hash(upstream))


# ---- denial_reason_result: ids and ranked lists ----

def test_clean_denial_reasons_pass(reasons_doc, case_dir):
    assert check("denial_reason_result.json", reasons_doc, case_dir) == []


def test_duplicate_reason_id_is_rejected(reasons_doc, case_dir):
    """Ids are how every downstream stage addresses a reason; two rows sharing
    one means the later silently wins."""
    reasons_doc["denial_reasons"].append(_reason("DR_1"))
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("duplicate reason_id 'DR_1'" in e for e in errors)


def test_duplicate_policy_match_id_is_rejected_across_reasons(reasons_doc, case_dir):
    """Uniqueness is contract-wide, not per-reason: denial_validation
    addresses matches by bare id with no reason qualifier."""
    reasons_doc["denial_reasons"][0]["policy_matches"] = [{"policy_match_id": "PM_1"}]
    reasons_doc["denial_reasons"][1]["policy_matches"] = [{"policy_match_id": "PM_1"}]
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("duplicate policy_match_id 'PM_1'" in e for e in errors)


def test_top_candidate_must_equal_assigned_code(reasons_doc, case_dir):
    """candidate_codes feeds Top-1/Top-3 evaluation. A list whose winner is not
    the assigned code scores a classification that was never made."""
    reasons_doc["denial_reasons"][0]["candidate_codes"] = [
        {"taxonomy_code": "R21", "confidence": 0.9},
        {"taxonomy_code": "R04", "confidence": 0.1},
    ]
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("candidate_codes[0] is 'R21' but taxonomy_code is 'R04'" in e for e in errors)


def test_candidate_codes_must_not_repeat_a_code(reasons_doc, case_dir):
    reasons_doc["denial_reasons"][0]["candidate_codes"] = [
        {"taxonomy_code": "R04", "confidence": 0.9},
        {"taxonomy_code": "R04", "confidence": 0.1},
    ]
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("lists 'R04' more than once" in e for e in errors)


def test_candidate_confidences_must_not_increase(reasons_doc, case_dir):
    """A 'ranked' list that is not ranked makes Top-1 meaningless."""
    reasons_doc["denial_reasons"][0]["candidate_codes"] = [
        {"taxonomy_code": "R04", "confidence": 0.4},
        {"taxonomy_code": "R05", "confidence": 0.9},
    ]
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("not non-increasing" in e for e in errors)


def test_equal_confidences_are_allowed(reasons_doc, case_dir):
    """Non-increasing, not strictly decreasing -- a genuine tie is honest."""
    reasons_doc["denial_reasons"][0]["candidate_codes"] = [
        {"taxonomy_code": "R04", "confidence": 0.5},
        {"taxonomy_code": "R05", "confidence": 0.5},
    ]
    assert check("denial_reason_result.json", reasons_doc, case_dir) == []


def test_taxonomy_label_must_match_the_codebook(reasons_doc, case_dir):
    """The codebook in common_component_output is the one machine-readable
    source of R-code labels; a contract must not invent a second one."""
    reasons_doc["denial_reasons"][0]["taxonomy_label"] = "그럴듯한 오답"
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("does not match the codebook label" in e for e in errors)


def test_omitted_taxonomy_label_is_not_invented(reasons_doc, case_dir):
    """Absent is not wrong -- the schema decides whether the field is required;
    this layer only checks a present label for agreement."""
    reasons_doc["denial_reasons"][0].pop("taxonomy_label")
    assert check("denial_reason_result.json", reasons_doc, case_dir) == []


# ---- denial_validation_result: the orphan-id findings ----

def test_validation_of_a_nonexistent_reason_is_rejected(case_dir):
    """The headline finding: DR_999 resolves to nothing and used to pass."""
    doc = {"validations": [_validation("DR_1"), _validation("DR_999")]}
    errors = check("denial_validation_result.json", _derived(case_dir, doc), case_dir)
    assert any("DR_999" in e and "does not exist" in e for e in errors)


def test_every_denial_reason_must_be_validated(case_dir):
    """Skipping one silently leaves an insurer's denial unrebutted while the
    contract still reports success."""
    doc = {"validations": [_validation("DR_1")]}
    errors = check("denial_validation_result.json", _derived(case_dir, doc), case_dir)
    assert any("DR_2" in e and "no validation" in e for e in errors)


def test_a_reason_must_not_be_validated_twice(case_dir):
    doc = {"validations": [_validation("DR_1"), _validation("DR_1"), _validation("DR_2")]}
    errors = check("denial_validation_result.json", _derived(case_dir, doc), case_dir)
    assert any("appears more than once" in e for e in errors)


def test_exact_one_to_one_validation_passes(case_dir):
    doc = {"validations": [_validation("DR_1"), _validation("DR_2")]}
    assert check("denial_validation_result.json", _derived(case_dir, doc), case_dir) == []


def test_policy_match_verification_must_resolve(tmp_path):
    reasons = {"denial_reasons": [_reason("DR_1", matches=[{"policy_match_id": "PM_1"}])]}
    d = tmp_path / "CASE_PM"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(json.dumps(reasons, ensure_ascii=False),
                                                 encoding="utf-8")
    doc = {"validations": [_validation("DR_1", match_ids=["PM_999"])]}
    errors = check("denial_validation_result.json", _derived(d, doc), d)
    assert any("PM_999" in e and "does not exist" in e for e in errors)
    assert any("PM_1" in e and "no verification" in e for e in errors)


def test_legacy_policy_matches_without_ids_are_not_retro_failed(tmp_path):
    """CASE_021 was written before policy_match_id existed. Those entries have
    no id to address, so demanding a verification for them would report a
    legacy shape as corruption -- the schema governs new writes instead."""
    reasons = {"denial_reasons": [
        _reason("DR_1", matches=[{"document_id": "DOC_002", "clause_id": "제3조"}])]}
    d = tmp_path / "CASE_LEGACY"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(json.dumps(reasons, ensure_ascii=False),
                                                 encoding="utf-8")
    assert check("denial_validation_result.json",
                 _derived(d, {"validations": [_validation("DR_1")]}), d) == []


def test_validation_before_its_source_contract_exists_is_rejected(tmp_path):
    """Writing Phase 2's validation with no Phase 1 reasons to validate means
    every id in it is unresolvable by definition."""
    d = tmp_path / "CASE_EMPTY"
    d.mkdir()
    errors = check("denial_validation_result.json", {"validations": [_validation("DR_1")]}, d)
    assert any("cannot be written before" in e for e in errors)


# ---- policy match ownership: a match belongs to ONE reason ----

@pytest.fixture
def owned_matches_case(tmp_path):
    """DR_1 owns PM_1, DR_2 owns PM_2 -- the setup a flat existence check
    cannot tell apart from 'both ids exist somewhere'."""
    reasons = {"denial_reasons": [
        _reason("DR_1", matches=[{"policy_match_id": "PM_1"}]),
        _reason("DR_2", matches=[{"policy_match_id": "PM_2"}]),
    ]}
    d = tmp_path / "CASE_OWN"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(json.dumps(reasons, ensure_ascii=False),
                                                 encoding="utf-8")
    return d


def test_match_verified_under_the_wrong_reason_is_rejected(owned_matches_case):
    """The finding: PM_1 belongs to DR_1, but was verified under DR_2. Every
    id exists, so a global-set check passes -- while PM_1 was never actually
    checked under the reason that owns it, and PM_2 never checked at all."""
    doc = {"validations": [
        _validation("DR_1"),
        _validation("DR_2", match_ids=["PM_1"]),
    ]}
    errors = check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case)
    assert any("PM_1" in e and "belongs to 'DR_1', not 'DR_2'" in e for e in errors)


def test_correct_ownership_passes(owned_matches_case):
    doc = {"validations": [
        _validation("DR_1", match_ids=["PM_1"]),
        _validation("DR_2", match_ids=["PM_2"]),
    ]}
    assert check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case) == []


def test_owned_match_left_unverified_is_reported_against_its_reason(owned_matches_case):
    doc = {"validations": [_validation("DR_1"), _validation("DR_2", match_ids=["PM_2"])]}
    errors = check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case)
    assert any("DR_1" in e and "PM_1" in e and "no verification" in e for e in errors)


def test_same_match_verified_under_two_reasons_is_rejected(owned_matches_case):
    """Wrong-parent and duplicate at once -- both must surface."""
    doc = {"validations": [
        _validation("DR_1", match_ids=["PM_1"]),
        _validation("DR_2", match_ids=["PM_1", "PM_2"]),
    ]}
    errors = check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case)
    assert any("belongs to 'DR_1', not 'DR_2'" in e for e in errors)
    assert any("verified more than once" in e for e in errors)


def test_match_repeated_within_one_validation_is_rejected(owned_matches_case):
    doc = {"validations": [
        _validation("DR_1", match_ids=["PM_1", "PM_1"]),
        _validation("DR_2", match_ids=["PM_2"]),
    ]}
    errors = check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case)
    assert any("verified more than once" in e for e in errors)


# ---- policy documents, clauses, and cited locations must be real ----

CLAUSE_QUOTE = "「뇌혈관질환」의 진단확정은 의료법 제3조에서 규정한 의료기관의 의사에 의하여"


def _policy_doc(document_id="DOC_002", clause_id="제3조", page=2, quote=CLAUSE_QUOTE):
    return {
        "document_id": document_id,
        "clauses": [{
            "clause_id": clause_id,
            "evidence_references": [
                {"document_id": document_id, "page": page, "quote": quote}],
        }],
    }


def _match(policy_match_id="PM_1", document_id="DOC_002", clause_id="제3조",
           ref_document_id=None, page=2, quote=CLAUSE_QUOTE, match_source="insurer_cited"):
    return {
        "policy_match_id": policy_match_id,
        "document_id": document_id,
        "clause_id": clause_id,
        "match_source": match_source,
        "policy_clause_evidence_references": [
            {"document_id": ref_document_id or document_id, "page": page, "quote": quote}],
    }


@pytest.fixture
def policy_case(tmp_path):
    d = tmp_path / "CASE_POLICY"
    d.mkdir()
    (d / "normalized_policy_clause_DOC_002.json").write_text(
        json.dumps(_policy_doc(), ensure_ascii=False), encoding="utf-8")
    return d


def _reasons_with(match):
    return {"denial_reasons": [_reason("DR_1", matches=[match])]}


def test_resolvable_policy_match_passes(policy_case):
    assert check("denial_reason_result.json", _reasons_with(_match()), policy_case) == []


def test_match_citing_a_different_document_is_rejected(policy_case):
    """A DOC_002 match whose clause evidence points at DOC_999: the citation
    does not come from the document the match claims."""
    doc = _reasons_with(_match(ref_document_id="DOC_999"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("DOC_999" in e and "DOC_002" in e for e in errors)


def test_match_on_a_nonexistent_clause_is_rejected(policy_case):
    doc = _reasons_with(_match(clause_id="제99조"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("제99조" in e and "does not exist" in e for e in errors)


def test_match_on_an_unnormalized_policy_document_is_rejected(policy_case):
    """Fail-safe: a link that cannot be checked is not allowed to stand as
    though it had been."""
    doc = _reasons_with(_match(document_id="DOC_777"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("normalized_policy_clause_DOC_777.json" in e for e in errors)


def test_clause_evidence_on_the_wrong_page_is_rejected(policy_case):
    doc = _reasons_with(_match(page=9))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("does not match any evidence reference" in e for e in errors)


def test_clause_evidence_with_an_unrecorded_quote_is_rejected(policy_case):
    doc = _reasons_with(_match(quote="약관에 그렇게 적혀 있다고 함"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("does not match any evidence reference" in e for e in errors)


def test_agent_inferred_matches_are_verified_identically(policy_case):
    """An inferred link is the one most in need of checking, not least."""
    doc = _reasons_with(_match(clause_id="제99조", match_source="agent_inferred"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("제99조" in e and "does not exist" in e for e in errors)


def test_match_may_cite_a_condition_level_evidence_reference(tmp_path):
    """A match may turn on a specific payout condition rather than the clause
    header, so condition-level references count as recorded locations."""
    policy = _policy_doc()
    policy["clauses"][0]["payout_conditions"] = [
        {"text": "진단확정 요건", "evidence_references": [
            {"document_id": "DOC_002", "page": 3, "quote": "조건 본문"}]}]
    d = tmp_path / "CASE_COND"
    d.mkdir()
    (d / "normalized_policy_clause_DOC_002.json").write_text(
        json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    doc = _reasons_with(_match(page=3, quote="조건 본문"))
    assert check("denial_reason_result.json", doc, d) == []


def test_legacy_matches_without_ids_are_still_skipped(policy_case):
    """CASE_021's shape must not be retro-failed by the new clause checks."""
    doc = {"denial_reasons": [_reason("DR_1", matches=[
        {"document_id": "DOC_999", "clause_id": "제3조", "relevance_note": "legacy"}])]}
    assert check("denial_reason_result.json", doc, policy_case) == []


# ---- screening_report ----

def test_screening_report_reason_ids_must_resolve(case_dir):
    doc = {"denial_summary": {"reason_ids": ["DR_1", "DR_7"]}}
    errors = check("screening_report.json", _derived(case_dir, doc), case_dir)
    assert any("DR_7" in e for e in errors)


def test_screening_report_with_valid_ids_passes(case_dir):
    doc = {"denial_summary": {"reason_ids": ["DR_1", "DR_2"]}}
    assert check("screening_report.json", _derived(case_dir, doc), case_dir) == []


@pytest.fixture
def mixed_decision_case(tmp_path):
    """DR_1 is a denial, DR_2 a reduction -- the distinction the screening
    report's two sections exist to preserve."""
    reasons = {"denial_reasons": [
        dict(_reason("DR_1", code="R04"), decision_type="denial"),
        dict(_reason("DR_2", code="R01"), decision_type="reduction"),
    ]}
    d = tmp_path / "CASE_MIXED"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(json.dumps(reasons, ensure_ascii=False),
                                                 encoding="utf-8")
    return d


def _position(denial_ids, reduction_ids):
    return {"insurer_position": {
        "denial": {"reason_ids": list(denial_ids)},
        "reduction": {"reason_ids": list(reduction_ids)},
    }}


def test_correctly_split_screening_sections_pass(mixed_decision_case):
    assert check("screening_report.json",
                 _derived(mixed_decision_case, _position(["DR_1"], ["DR_2"])),
                 mixed_decision_case) == []


def test_swapped_denial_and_reduction_sections_are_rejected(mixed_decision_case):
    """The regression the audit asked for: both ids resolve, so an
    existence-only check passes while the report inverts what the insurer
    actually decided."""
    errors = check("screening_report.json", _derived(mixed_decision_case, _position(["DR_2"], ["DR_1"])), mixed_decision_case)
    assert any("DR_2" in e and "'reduction'" in e and "denial" in e for e in errors)
    assert any("DR_1" in e and "'denial'" in e and "reduction" in e for e in errors)


def test_a_reduction_listed_under_denial_is_rejected(mixed_decision_case):
    errors = check("screening_report.json", _derived(mixed_decision_case, _position(["DR_1", "DR_2"], [])), mixed_decision_case)
    assert any("DR_2" in e and "must not be summarized as a denial" in e for e in errors)


def test_same_reason_in_both_sections_is_rejected(mixed_decision_case):
    errors = check("screening_report.json", _position(["DR_1"], ["DR_1", "DR_2"]),
                   mixed_decision_case)
    assert any("BOTH denial and reduction" in e for e in errors)


def test_reason_repeated_within_one_section_is_rejected(mixed_decision_case):
    errors = check("screening_report.json", _position(["DR_1", "DR_1"], ["DR_2"]),
                   mixed_decision_case)
    assert any("more than once" in e for e in errors)


# ---- dispatch and the DAO gate ----

def test_unknown_contracts_are_not_gated(case_dir):
    """Additive layer: a contract with no registered invariants must not need
    to opt out."""
    assert check("ocr_result_DOC_001.json", {"anything": True}, case_dir) == []


def test_dao_write_contract_refuses_and_does_not_persist(isolated_dao, make_args,
                                                        tmp_path, capsys):
    """The gate that matters: a corrupt contract must be refused at the DAO,
    and the previous good file must survive untouched -- the same
    fail/don't-persist rule schema validation already follows.

    Built from the real CASE_903 output rather than a minimal fixture, so the
    payload is genuinely schema-valid and the ONLY thing wrong with it is the
    duplicate id. Otherwise schema validation would reject it first and this
    would never reach the cross-contract layer it means to test.
    """
    real = ROOT / "outputs" / "CASE_903" / "denial_reason_result.json"
    if not real.exists():
        pytest.skip("CASE_903 output not present in this checkout")
    reasons_doc = json.loads(real.read_text(encoding="utf-8"))

    case = dao.case_dir("CASE_009")
    case.mkdir(parents=True, exist_ok=True)
    good = json.dumps(reasons_doc, ensure_ascii=False)
    (case / "denial_reason_result.json").write_text(good, encoding="utf-8")

    corrupt = copy.deepcopy(reasons_doc)
    corrupt["denial_reasons"].append(copy.deepcopy(corrupt["denial_reasons"][0]))
    payload = tmp_path / "corrupt.json"
    payload.write_text(json.dumps(corrupt, ensure_ascii=False), encoding="utf-8")

    rc = dao.cmd_write_contract(make_args(
        filename="denial_reason_result.json", data_file=str(payload),
        schema_name="denial_reason_result.schema.json"))
    out = capsys.readouterr().out

    assert rc == 1
    assert "cross-contract validation errors" in out
    assert (case / "denial_reason_result.json").read_text(encoding="utf-8") == good
    assert not list(case.glob("*.lock")), "the lock must be released even when the write is refused"


# ---- screening_report: the acceptance side ---------------------------------
#
# `denial_reason_result` gained `accepted_coverages` so a response that denies
# one coverage and accepts another stops reading as a total denial. The report
# that SUMMARIZES that contract has to carry the axis too -- CASE_907's first
# run described the split correctly in prose while `insurer_position` still
# showed only has_denial:true, which a structural consumer takes as a total
# denial: the exact misreading the upstream field exists to prevent.

@pytest.fixture
def split_outcome_case(tmp_path):
    """DR_1 denies one coverage; AC_1 records another that was accepted."""
    reasons = {
        "denial_reasons": [dict(_reason("DR_1", code="R04"),
                                decision_type="denial",
                                decided_coverage="배상책임")],
        "accepted_coverages": [{
            "accepted_coverage_id": "AC_1",
            "coverage_name": "구내치료비",
            "payment_status": "unknown",
            "accepted_amount": 2000000,
        }],
    }
    d = tmp_path / "CASE_SPLIT"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(
        json.dumps(reasons, ensure_ascii=False), encoding="utf-8")
    return d


def _split_position(accepted_ids, has_acceptance=True):
    position = {"insurer_position": {
        "denial": {"reason_ids": ["DR_1"]},
        "reduction": {"reason_ids": []},
        "has_acceptance": has_acceptance,
    }}
    if accepted_ids is not None:
        position["insurer_position"]["acceptance"] = {
            "accepted_coverage_ids": list(accepted_ids)}
    return position


def test_a_report_carrying_the_acceptance_passes(split_outcome_case):
    assert check("screening_report.json",
                 _derived(split_outcome_case, _split_position(["AC_1"])),
                 split_outcome_case) == []


def test_omitting_a_recorded_acceptance_is_rejected(split_outcome_case):
    """Silence about an acceptance is what makes a split outcome read total."""
    errors = check("screening_report.json",
                   _derived(split_outcome_case, _split_position([])),
                   split_outcome_case)
    assert any("omits" in e for e in errors), errors


def test_denying_an_acceptance_happened_is_rejected(split_outcome_case):
    errors = check("screening_report.json",
                   _derived(split_outcome_case,
                            _split_position(["AC_1"], has_acceptance=False)),
                   split_outcome_case)
    assert any("has_acceptance is false" in e for e in errors), errors


def test_an_unknown_accepted_coverage_id_is_rejected(split_outcome_case):
    errors = check("screening_report.json",
                   _derived(split_outcome_case, _split_position(["AC_9"])),
                   split_outcome_case)
    assert any("AC_9" in e for e in errors), errors


def test_a_report_predating_the_acceptance_field_is_not_retro_failed(
        split_outcome_case):
    """Additive: a report written before the field existed opts out by absence,
    and must not be failed for a field it never had."""
    legacy = {"insurer_position": {
        "denial": {"reason_ids": ["DR_1"]}, "reduction": {"reason_ids": []}}}
    assert check("screening_report.json",
                 _derived(split_outcome_case, legacy),
                 split_outcome_case) == []
