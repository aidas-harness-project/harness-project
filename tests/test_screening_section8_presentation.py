"""Section 8 presents a coverage once, quotes the clause, and does not call a
missing clause a failed search.

Three reader-facing defects found on CASE_702 (2026-08-21), all in how section
8 PRESENTS what the linker already established -- none of them a data defect,
which is why they survived every schema and citation check:

1. **수술 and 후유장해 each printed twice.** `policy_links` is built per FIELD,
   so one coverage arrives under several `coverage_id`s (수술 as
   `surgery_or_major_procedure_status` and `surgery_or_procedure_name`,
   후유장해 as `disability_type` and `disability_related_diagnosis`). The rows
   differ only in an id the reader never sees.
2. **The clause was named but never quoted.** `clause_ref.quote` holds the
   ARTICLE HEADING, because that is the string the linker verifies -- so a
   reviewer saw `DOC_009 p.2 보상하지 않는 손해` and never what the article
   said, in the one section whose purpose is putting the clause in front of
   them.
3. **"조항을 찾지 못했습니다" read as a lookup failure.** On a pack whose only
   약관 is an 영업배상책임보험, 수술/입원/후유장해 are absent from the PRODUCT.
   Searching CASE_702's policy text for both spellings (후유장해 / 후유장애)
   plus 장해분류표 / 장해지급률 / 후유장해보험금 returned nothing outside the
   의무보험 지급한도 clauses of two 추가특별약관 -- but this stage cannot
   establish that in general, so the report states the possibility instead of
   asserting the fact.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import run_screening_report as screening  # noqa: E402


def _link(coverage_id, coverage_name, status, **extra):
    row = {
        "coverage_id": coverage_id,
        "coverage_name": coverage_name,
        "clause_link_status": status,
        "clause_link_status_label": "조항 확인" if status == "matched" else "조항 미확인",
        "requirements": [{"requirement_id": "REQ-1",
                          "requirement_text": f"{coverage_name} 관련 담보 요건",
                          "evidence_status_label": "자료 있음",
                          "evidence_references": []}],
    }
    row.update(extra)
    return row


# --- 1. one row per coverage ------------------------------------------------

def test_two_fields_of_one_coverage_render_as_one_row():
    merged = screening._merge_links_by_coverage([
        _link("surgery_or_major_procedure_status", "수술", "not_found",
              uncertainty_reason="찾지 못했습니다"),
        _link("surgery_or_procedure_name", "수술", "not_found",
              uncertainty_reason="찾지 못했습니다"),
    ])
    assert len(merged) == 1, "수술 printed twice on CASE_702"
    assert merged[0]["contributing_coverage_ids"] == [
        "surgery_or_major_procedure_status", "surgery_or_procedure_name"]


def test_distinct_coverages_are_never_merged():
    merged = screening._merge_links_by_coverage([
        _link("liability_premises_owner", "시설소유", "matched"),
        _link("liability_premises_medical_expense", "구내치료비", "matched"),
    ])
    assert [row["coverage_name"] for row in merged] == ["시설소유", "구내치료비"]


def test_a_matched_field_wins_over_a_not_found_one():
    """A coverage whose clause was found through ANY field is linked."""
    reference = {"document_id": "DOC_009", "page": 2, "quote": "보상하지 않는 손해"}
    merged = screening._merge_links_by_coverage([
        _link("disability_type", "후유장해", "not_found",
              uncertainty_reason="찾지 못했습니다"),
        _link("disability_related_diagnosis", "후유장해", "matched",
              clause_ref=reference),
    ])
    assert len(merged) == 1
    assert merged[0]["clause_link_status"] == "matched"
    assert merged[0]["clause_ref"] == reference


def test_merging_keeps_every_requirement():
    merged = screening._merge_links_by_coverage([
        _link("a", "수술", "not_found"),
        {**_link("b", "수술", "not_found"),
         "requirements": [{"requirement_id": "REQ-2",
                           "requirement_text": "다른 요건",
                           "evidence_status_label": "자료 없음",
                           "evidence_references": []}]},
    ])
    texts = {r["requirement_text"] for r in merged[0]["requirements"]}
    assert texts == {"수술 관련 담보 요건", "다른 요건"}


# --- 2. the clause's own words ----------------------------------------------

PAGE = (
    "구내치료비 추가특별약관\n(시설소유(관리)자 특별약관에 적용)\n\n"
    "제1조(보상하는 손해)\n회사는 영업배상책임보험 보통약관 제3조의 규정에도 "
    "불구하고 특별약관 제1조에 기재된 사고로 피해자가 입은 신체장해에 대한 "
    "치료비를 보상하여 드립니다.\n\n"
    "제2조(보상하지 않는 손해)\n회사는 아래에 기재된 치료비를 보상하지 않습니다.\n"
)


def test_the_body_under_a_heading_is_extracted():
    body = screening._excerpt_article(PAGE, "보상하는 손해")
    assert "치료비를 보상하여 드립니다" in body


def test_the_excerpt_stops_at_the_next_article():
    body = screening._excerpt_article(PAGE, "보상하는 손해")
    assert "보상하지 않습니다" not in body, (
        "the excerpt ran past 제2조 into the next article")


def test_a_heading_absent_from_the_page_yields_nothing():
    assert screening._excerpt_article(PAGE, "존재하지 않는 조항") == ""
    assert screening._excerpt_article("", "보상하는 손해") == ""


def test_a_long_article_is_capped():
    page = "제1조(긴 조항)\n" + ("가" * 900)
    body = screening._excerpt_article(page, "긴 조항")
    assert len(body) <= screening.CLAUSE_BODY_MAX_CHARS + 2
    assert body.endswith("…")


def test_the_rendered_section_carries_the_clause_body():
    reference = {"document_id": "DOC_009", "page": 5, "quote": "보상하는 손해"}
    report = {"policy_links": [
        _link("liability_premises_medical_expense", "구내치료비", "matched",
              clause_ref=reference)]}
    section = next(
        s for s in screening.markdown_sections(
            report,
            clause_body=lambda doc, page, heading: screening._excerpt_article(
                PAGE, heading))
        if s["heading"].startswith("8."))
    assert "조항 본문:" in section["content"]
    assert "치료비를 보상하여 드립니다" in section["content"]


def test_the_clause_body_takes_no_citation_marker():
    """The body is an excerpt of the clause the row ALREADY cites.

    Giving it a second `{{E}}` would unbalance the section, which the assembler
    refuses -- and would tag one clause twice rather than cite a new source.
    """
    reference = {"document_id": "DOC_009", "page": 5, "quote": "보상하는 손해"}
    report = {"policy_links": [
        _link("liability_premises_medical_expense", "구내치료비", "matched",
              clause_ref=reference)]}
    section = next(
        s for s in screening.markdown_sections(
            report,
            clause_body=lambda doc, page, heading: screening._excerpt_article(
                PAGE, heading))
        if s["heading"].startswith("8."))
    assert (section["content"].count("{{E}}")
            == len(section["evidence_references"]) == 1)


# --- 3. a missing coverage is not a failed search ---------------------------

def test_a_not_found_row_says_the_coverage_may_be_absent():
    report = {"policy_links": [
        _link("disability_type", "후유장해", "not_found",
              uncertainty_reason="처리된 약관 자료에서 이 담보를 명시한 조항을 "
                                 "찾지 못했습니다.")]}
    section = next(s for s in screening.markdown_sections(report)
                   if s["heading"].startswith("8."))
    assert "본건 약관에 처음부터 없을 가능성" in section["content"]


def test_a_matched_row_does_not_get_the_absence_note():
    report = {"policy_links": [
        _link("liability_premises_owner", "시설소유", "matched",
              clause_ref={"document_id": "DOC_009", "page": 2,
                          "quote": "보상하지 않는 손해"})]}
    section = next(s for s in screening.markdown_sections(report)
                   if s["heading"].startswith("8."))
    assert "처음부터 없을 가능성" not in section["content"]


# --- 4. the insurer's decision says what it decided ------------------------

DENIAL_CONTRACT = {
    "denial_reasons": [{
        "reason_id": "DR_1",
        "decision_type": "denial",
        "decided_coverage": "시설소유자배상책임",
        "insurer_claim_summary": "강설·결빙이라는 자연현상은 시설의 하자로 볼 수 없다는 판례에 따라 배상책임이 성립하지 않는다는 결정.",
        "amounts": {"claimed_amount": "5,000,000원", "payable_amount": None,
                    "denied_amount": "5,000,000원", "reduction_amount": None,
                    "reduction_rate": None},
        "evidence_references": [{"document_id": "DOC_007", "page": 1,
                                 "quote": "법률상배상책임이 발생하지 않는다"}],
    }],
    "accepted_coverages": [],
}


def _section9(contract):
    report = {"insurer_position": screening.insurer_position(contract)}
    return next(s for s in screening.markdown_sections(report)
                if s["heading"].startswith("9."))


def test_the_insurers_reason_is_printed_not_just_its_id():
    """CASE_703 rendered "거절: DR_1" and nothing else.

    The id is an internal handle. The denial contract already held
    `insurer_claim_summary`; the report layer dropped it, so the section named
    a decision without ever stating its grounds -- the section-8 defect again.
    """
    section = _section9(DENIAL_CONTRACT)
    assert "자연현상은 시설의 하자로 볼 수 없다" in section["content"]
    assert "시설소유자배상책임" in section["content"]


def test_stated_amounts_are_labelled_in_korean():
    section = _section9(DENIAL_CONTRACT)
    assert "청구금액: 5,000,000원" in section["content"]
    assert "부지급금액: 5,000,000원" in section["content"]


def test_null_amounts_are_omitted_not_printed_as_field_names():
    """`amounts` is a mapping of five figures, usually mostly null.

    Iterating it as a list printed five bare key names under the decision, and
    a null is not 0원 -- it is a figure the insurer did not state.
    """
    contract = json.loads(json.dumps(DENIAL_CONTRACT))
    contract["denial_reasons"][0]["amounts"] = {
        "claimed_amount": None, "payable_amount": None, "denied_amount": None,
        "reduction_amount": None, "reduction_rate": None}
    section = _section9(contract)
    for key in ("claimed_amount", "청구금액", "denied_amount", "부지급금액"):
        assert key not in section["content"]


def test_section9_stays_balanced_with_statements():
    """The statement restates the reason the line already cites, so it takes no
    marker of its own -- a second one would unbalance the section."""
    section = _section9(DENIAL_CONTRACT)
    assert (section["content"].count("{{E}}")
            == len(section["evidence_references"]) == 1)


def test_a_reason_without_a_summary_contributes_no_line():
    contract = json.loads(json.dumps(DENIAL_CONTRACT))
    del contract["denial_reasons"][0]["insurer_claim_summary"]
    contract["denial_reasons"][0]["raw_reason_text"] = ""
    section = _section9(contract)
    assert "거절: DR_1" in section["content"]
    assert section["content"].count("\n  - ") == 0


# --- 5. no insurer document is not a failed lookup -------------------------

def test_a_case_with_no_insurer_document_says_so():
    """CASE_710/711/712 all rendered 「거절/감액/승인: 확인 불가」 on cases whose
    manifest holds no `insurer_response` at all.

    확인 불가 means "we looked and could not tell". With no document filed in
    the pack, nothing was ever looked at -- a different finding, and the one a
    손해사정사 acts on differently (request the insurer's letter, rather than
    re-read one).
    """
    section = _section9({"denial_reasons": [], "accepted_coverages": []})
    assert "편철된 보험사 회신 문서가 없습니다" in section["content"]
    assert "확인 불가" not in section["content"]


def test_the_no_insurer_branch_still_renders_ten_sections():
    """The template pins ten headings with allow_extra_sections: false, so an
    early return that skips section 10 produces a document the assembler
    refuses outright."""
    sections = screening.markdown_sections(
        {"insurer_position": screening.insurer_position(None)})
    assert len(sections) == 10
    assert sections[-1]["heading"].startswith("10.")


def test_an_insurer_decision_still_renders_normally():
    """The new branch must not swallow the case section 9 exists for."""
    section = _section9(DENIAL_CONTRACT)
    assert "거절: DR_1" in section["content"]
    assert "편철된 보험사 회신 문서가 없습니다" not in section["content"]
