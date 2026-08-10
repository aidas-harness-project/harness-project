"""Part 8 code gate: parent-level whole-page coverage.

The per-document boundary inventory only accounts for pages a segment already
owns; it cannot see a parent-PDF page that was never carved into any segment.
These tests exercise policy_completeness.check_policy_parent_coverage /
unresolved_parent_pages, which close that gap (CASE_030's 133 unowned pages).
"""
import dao
import policy_completeness as pc


FILENAME = "policy_parent_coverage_DOC_001.json"


def _manifest():
    return {
        "case_id": "CASE_030",
        "documents": [
            {"document_id": "DOC_001", "document_role": "physical",
             "source_total_pages": 3,
             "document_type": "insurance_policy",
             "downstream_disposition": "automated_text_pipeline"},
            {"document_id": "DOC_005", "document_role": "segment",
             "source_document_id": "DOC_001",
             "page_map": [{
                 "logical_page": 2, "source_physical_page": 2,
             }],
             "document_type": "insurance_policy",
             "downstream_disposition": "automated_text_pipeline"},
            {"document_id": "DOC_007", "document_role": "segment",
             "source_document_id": "DOC_001",
             "page_map": [{
                 "logical_page": 3, "source_physical_page": 3,
             }],
             "document_type": "insurance_policy",
             "downstream_disposition": "automated_text_pipeline"},
        ],
    }


def _reference_table_for(doc_id):
    # DOC_007 has a reference table RT-aaaa; anything else has none.
    if doc_id == "DOC_007":
        return {"tables": [{
            "table_uid": "RT-aaaaaaaaaaaaaaaa",
            "review_required": False,
            "evidence_references": [
                {"document_id": "DOC_007", "page": 3, "quote": "표 제목"},
            ],
            "rows": [{
                "row_uid": "RR-aaaaaaaaaaaaaaaa",
                "cells": [{
                    "cell_uid": "RC-aaaaaaaaaaaaaaaa",
                    "review_required": False,
                    "evidence_references": [
                        {"document_id": "DOC_007", "page": 3,
                         "quote": "표 값"},
                    ],
                }],
            }],
        }]}
    return None


def _page(lp, disp, **kw):
    base = {"logical_page": lp, "physical_page": lp, "disposition": disp,
            "owner_document_id": None, "table_uid": None,
            "reference_table_document_id": None, "reason": None,
            "evidence_references": []}
    base.update(kw)
    return base


def _clean_data(total=3):
    return {
        "case_id": "CASE_030",
        "component": "policy-pipeline",
        "status": "success",
        "parent_document_id": "DOC_001",
        "total_logical_pages": total,
        "total_physical_pages": total,
        "unpaged_physical_pages": [],
        "pages": [
            _page(
                1, "administrative_excluded", reason="표지",
                evidence_references=[{
                    "document_id": "DOC_001", "page": 1, "quote": "표지",
                }]),
            _page(2, "owned_by_segment", owner_document_id="DOC_005"),
            _page(3, "reference_table", table_uid="RT-aaaaaaaaaaaaaaaa",
                  reference_table_document_id="DOC_007"),
        ],
    }


# The parent's own processed text (Part 11D). Page 1 is a genuine cover: it
# carries the cited quote and no normative content. The DAO always supplies this
# to check_policy_parent_coverage, so the clean fixture does too.
PARENT_TEXT = (
    "<<<PAGE page=1>>>\n"
    "삼성화재 미니생활보험 약관 표지\n"
    "<<<PAGE page=2>>>\n"
    "제3조(보험금의 지급) 회사는 보험금을 지급합니다.\n"
    "<<<PAGE page=3>>>\n"
    "【별표 1】 장해분류표\n"
)


def test_clean_coverage_passes():
    errors = pc.check_policy_parent_coverage(
        _clean_data(), FILENAME, _manifest(), _reference_table_for,
        PARENT_TEXT)
    assert errors == [], errors


def test_clean_coverage_schema_v02_passes():
    assert dao._schema_check(
        _clean_data(), "policy_parent_coverage.schema.json") == []


def test_coverage_gap_is_blocker():
    data = _clean_data(total=4)  # declares 4 pages but only provides 3
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("coverage gap" in e for e in errors), errors


def test_duplicate_logical_page_is_blocker():
    data = _clean_data()
    data["pages"].append(_page(2, "administrative_excluded", reason="dup"))
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("more than once" in e for e in errors), errors


def test_out_of_range_page_is_blocker():
    data = _clean_data()
    data["pages"].append(_page(9, "administrative_excluded", reason="oob"))
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("outside 1.." in e for e in errors), errors


def test_total_physical_pages_must_match_document_processing_metadata():
    data = _clean_data()
    data["total_physical_pages"] = 4
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("does not match manifest source_total_pages" in e
               for e in errors), errors


def test_every_physical_page_must_be_accounted_for():
    data = _clean_data()
    data["total_physical_pages"] = 4
    manifest = _manifest()
    manifest["documents"][0]["source_total_pages"] = 4
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, manifest, _reference_table_for)
    assert any("physical pages not accounted" in e for e in errors), errors


def test_unpaged_physical_page_requires_human_provenance_when_excluded():
    data = _clean_data(total=2)
    data["total_physical_pages"] = 3
    data["unpaged_physical_pages"] = [{
        "physical_page": 3,
        "disposition": "administrative_excluded",
        "reason": "back cover",
        "verified_by": None,
        "verified_at": None,
    }]
    errors = dao._schema_check(
        data, "policy_parent_coverage.schema.json")
    assert any("verified_by" in error or "verified_at" in error
               for error in errors), errors


def test_owned_page_must_match_segment_page_map():
    data = _clean_data()
    data["pages"][1]["physical_page"] = 1
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("disagrees with owner" in e for e in errors), errors


def test_administrative_exclusion_requires_page_evidence():
    data = _clean_data()
    data["pages"][0]["evidence_references"] = []
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("administrative exclusion has no processed page evidence" in e
               for e in errors), errors


def test_owned_by_unregistered_segment_is_blocker():
    data = _clean_data()
    data["pages"][1] = _page(2, "owned_by_segment", owner_document_id="DOC_099")
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("not a registered manifest document" in e for e in errors), errors


def test_reference_table_uid_absent_is_blocker():
    data = _clean_data()
    data["pages"][2] = _page(3, "reference_table",
                             table_uid="RT-bbbbbbbbbbbbbbbb",
                             reference_table_document_id="DOC_007")
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("not present in reference_table" in e for e in errors), errors


def test_reference_table_document_missing_is_blocker():
    data = _clean_data()
    data["pages"][2] = _page(3, "reference_table",
                             table_uid="RT-aaaaaaaaaaaaaaaa",
                             reference_table_document_id="DOC_099")
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("no reference_table contract" in e for e in errors), errors


def test_reference_table_must_have_evidence_on_every_claimed_page():
    data = _clean_data()
    data["pages"][2]["logical_page"] = 2
    data["pages"][1]["logical_page"] = 3
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert any("no table/cell evidence" in e for e in errors), errors


def test_reference_table_review_flag_is_not_resolved_coverage():
    def review_table(_doc_id):
        table = _reference_table_for("DOC_007")
        table["tables"][0]["review_required"] = True
        table["tables"][0]["rows"][0]["cells"][0]["review_required"] = True
        return table

    errors = pc.check_policy_parent_coverage(
        _clean_data(), FILENAME, _manifest(), review_table)
    assert any("review_required=true" in e for e in errors), errors


def test_parent_being_a_segment_is_blocker():
    manifest = _manifest()
    # make DOC_001 look like a segment
    manifest["documents"][0]["document_role"] = "segment"
    errors = pc.check_policy_parent_coverage(
        _clean_data(), FILENAME, manifest, _reference_table_for)
    assert any("is a segment" in e for e in errors), errors


def test_filename_doc_mismatch_is_blocker():
    errors = pc.check_policy_parent_coverage(
        _clean_data(), "policy_parent_coverage_DOC_002.json", _manifest(),
        _reference_table_for)
    assert any("does not match filename" in e for e in errors), errors


def test_unresolved_parent_pages_reported():
    data = _clean_data()
    data["pages"][0] = _page(1, "review_required", reason="병합셀 표 재검토 필요")
    # cross-contract check itself does not treat review_required as an error...
    errors = pc.check_policy_parent_coverage(
        data, FILENAME, _manifest(), _reference_table_for)
    assert errors == [], errors
    # ...but the finalize-time unresolved check does.
    blockers = pc.unresolved_parent_pages(data)
    assert any("review_required" in b for b in blockers), blockers


def test_extraction_failed_is_unresolved():
    data = _clean_data()
    data["pages"][0] = _page(1, "extraction_failed", reason="OCR 실패")
    assert any("extraction_failed" in b for b in pc.unresolved_parent_pages(data))


def test_all_resolved_has_no_unresolved():
    assert pc.unresolved_parent_pages(_clean_data()) == []


# --------------------------------------------------------------------------
# Part 11D: administrative exclusions verified against the parent's own source.
# --------------------------------------------------------------------------

def _admin_data(quote, reason="표지", page=1):
    data = _clean_data()
    data["pages"][0] = _page(
        1, "administrative_excluded", reason=reason,
        evidence_references=[{
            "document_id": "DOC_001", "page": page, "quote": quote,
        }])
    return data


def test_fabricated_admin_quote_is_rejected():
    """A quote that exists nowhere on the page must not pass."""
    errors = pc.check_policy_parent_coverage(
        _admin_data("THIS QUOTE DOES NOT EXIST"), FILENAME, _manifest(),
        _reference_table_for, PARENT_TEXT)
    assert any("does not appear on that page" in e for e in errors), errors


def test_real_quote_from_another_page_is_rejected():
    """A real quote lifted from a DIFFERENT page is not evidence for this one."""
    errors = pc.check_policy_parent_coverage(
        _admin_data("장해분류표"), FILENAME, _manifest(),
        _reference_table_for, PARENT_TEXT)
    assert any("does not appear on that page" in e for e in errors), errors


def test_real_quote_from_the_same_page_passes():
    errors = pc.check_policy_parent_coverage(
        _admin_data("미니생활보험 약관 표지"), FILENAME, _manifest(),
        _reference_table_for, PARENT_TEXT)
    assert errors == [], errors


def test_empty_admin_quote_is_rejected():
    errors = pc.check_policy_parent_coverage(
        _admin_data("   "), FILENAME, _manifest(),
        _reference_table_for, PARENT_TEXT)
    assert any("empty/whitespace quote" in e for e in errors), errors


def test_normative_page_cannot_be_administratively_excluded():
    """An operative policy predicate on the page defeats the exclusion."""
    text = (
        "<<<PAGE page=1>>>\n"
        "제7조(보험금의 지급) 회사는 피보험자가 사망한 경우 보험금을 지급합니다.\n"
        "<<<PAGE page=2>>>\n본문\n<<<PAGE page=3>>>\n표\n"
    )
    errors = pc.check_policy_parent_coverage(
        _admin_data("제7조(보험금의 지급)"), FILENAME, _manifest(),
        _reference_table_for, text)
    assert any("operative policy predicate" in e for e in errors), errors


def test_table_of_contents_page_is_still_administrative():
    """A 목차 lists 제N조 titles but states no rule -- it must stay allowed."""
    text = (
        "<<<PAGE page=1>>>\n"
        "목  차\n"
        "제1조(목적) ............ 5\n"
        "제2조(용어의 정의) ...... 6\n"
        "<<<PAGE page=2>>>\n본문\n<<<PAGE page=3>>>\n표\n"
    )
    errors = pc.check_policy_parent_coverage(
        _admin_data("목  차", reason="목차 페이지 -- 조문 본문 없음"),
        FILENAME, _manifest(), _reference_table_for, text)
    assert errors == [], errors


def test_blanket_admin_reason_is_rejected():
    errors = pc.check_policy_parent_coverage(
        _admin_data("미니생활보험 약관 표지", reason="appendix"),
        FILENAME, _manifest(), _reference_table_for, PARENT_TEXT)
    assert any("blanket category" in e for e in errors), errors


def test_admin_exclusion_without_parent_text_cannot_be_verified():
    """Fail closed: no processed text means the exclusion is unverifiable."""
    errors = pc.check_policy_parent_coverage(
        _admin_data("미니생활보험 약관 표지"), FILENAME, _manifest(),
        _reference_table_for, None)
    assert any("cannot be verified" in e for e in errors), errors
