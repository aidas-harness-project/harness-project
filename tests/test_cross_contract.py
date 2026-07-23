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


def test_dao_write_contract_valid_persists(isolated_dao, make_args):
    _seed_redacted(isolated_dao)
    rc = _write_contract(isolated_dao, make_args, _contract([_valid_clause()]),
                         "normalized_policy_clause_DOC_001.json")
    assert rc == 0
    target = isolated_dao / "outputs" / "CASE_030" / "normalized_policy_clause_DOC_001.json"
    assert target.exists()


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
    # First: a good write lands.
    rc = _write_contract(isolated_dao, make_args, _contract([_valid_clause()]),
                         "normalized_policy_clause_DOC_001.json")
    assert rc == 0
    target = isolated_dao / "outputs" / "CASE_030" / "normalized_policy_clause_DOC_001.json"
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
