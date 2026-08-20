"""EVERY section must emit one `{{E}}` per reference it publishes -- not just
the section that happened to fail last.

`document_assembly.render` refuses a section whose placeholder count differs
from its `evidence_references` count, and the refusal kills the whole report.
This project has now paid for that invariant four times, one section at a time:

    CASE_489  section 1  "0 {{E}} placeholders but 1 evidence_references"
    CASE_700  section 8  "0 {{E}} placeholders but 2 evidence_references"
    CASE_701  section 6  "0 {{E}} placeholders but 3 evidence_references"
    (unfired) section 3  filing evidence, gathered but never marked

Each was found by a real case being the first to take a branch, after a
multi-minute agent dispatch had already been paid for. Per-section tests were
written each time and none of them covered the next section, because the defect
is not in any one section -- it is the shape "gather references in one
comprehension, write the prose in another", which is easy to reach for and
silent until rendered.

So this file does not test a section. It builds ONE report that switches on
every citation-bearing branch at once and asserts the invariant across whatever
sections `markdown_sections` returns, including sections added after this file
was written. A new section that gathers references without marking them fails
here on the first run, not on the first case that happens to populate it.

`test_screening_section8_citations.py` stays as it is: it pins section 8's
deduplication edge (two requirements citing one sentence collapse to a single
published reference and must not leave an orphan placeholder), which is a
sharper claim than the balance asserted here.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import document_assembly as assembly  # noqa: E402
import run_screening_report as screening  # noqa: E402


def _ref(document_id: str, page: int, quote: str) -> dict:
    return {"document_id": document_id, "page": page, "quote": quote}


# A report with every citation-bearing branch live at once. The values are
# shaped like CASE_701's (a facility-liability case with a verified conflict
# across three documents), with the filing evidence CASE_701 lacked -- that
# absence is exactly why section 3's defect never fired there.
FULL_REPORT = {
    "case_summary": {
        "accident_date": "2026-03-11",
        "main_diagnosis": "우측 손목 요골 원위부 골절",
        "kcd_code": "S52.5",
        "case_type": "liability",
        "claim_coverages": ["시설소유"],
        "medical_authority": "source_document_extraction",
        "fact_evidence": {
            "accident_date": [_ref("DOC_011", 1, "사고일")],
            "main_diagnosis": [_ref("DOC_011", 1, "Fx. distal radius")],
        },
        "case_type_assessment": [
            {
                "case_type": "liability",
                "case_type_label": "배상책임",
                "status": "applicable",
                "status_label": "해당",
                "basis": "시설 하자 책임이 특정되어 있습니다",
                "filing_status": "filed",
                "filing_status_label": "접수",
                "evidence_references": [_ref("DOC_006", 8, "민법 제758조")],
                "filing_evidence_references": [_ref("DOC_007", 1, "접수번호")],
            },
            {
                "case_type": "industrial_accident",
                "case_type_label": "산재",
                "status": "uncertain",
                "status_label": "확인 불가",
                "basis": "기재 없음",
                "filing_status": "not_filed",
                "filing_status_label": "미접수",
                "evidence_references": [],
                "filing_evidence_references": [_ref("DOC_008", 2, "산재 미접수")],
            },
            {
                "case_type": "traffic_accident",
                "case_type_label": "자동차",
                "status": "uncertain",
                "status_label": "확인 불가",
                "basis": "기재 없음",
                "filing_status": "unknown",
                "filing_status_label": "확인 불가",
                "evidence_references": [],
                # unknown must contribute NO reference and NO placeholder
                "filing_evidence_references": [_ref("DOC_099", 9, "무시되어야 함")],
            },
        ],
    },
    "established_facts": [
        {
            "domain_label": "진단",
            "facts": [
                {"field_label": "주요 진단명",
                 "value_text": "우측 손목 요골 원위부 골절",
                 "evidence_references": [_ref("DOC_011", 1, "Fx. distal radius")]},
                {"field_label": "장해율",
                 "value_text": "13%",
                 "evidence_references": [_ref("DOC_012", 1, "맥브라이드")]},
            ],
        },
    ],
    "existing_disability_assessment": {
        "status_label": "보유",
        "evidence_references": [_ref("DOC_012", 1, "후유장해진단서")],
    },
    "unconfirmed_items": [
        {"label": "사고일", "reason": "출처가 기재하지 않았습니다",
         "unavailable_reason": "not_mentioned", "gap_kind": "records_gap"},
        {"label": "산재 승인 상태", "reason": "경로가 활성화되지 않았습니다",
         "unavailable_reason": "route_not_activated", "gap_kind": "not_searched"},
    ],
    "required_document_checklist": [
        {"document_label": "진단서", "document_kind": "diagnosis_certificate",
         "status": "present", "status_label": "보유",
         "required_for_case_types": ["배상책임"], "document_ids": ["DOC_011"]},
    ],
    "inconsistencies": [
        {
            "field": "liability_opinion_conclusion",
            "description": "성립/불성립이 갈립니다",
            "conflict_ref": "CONFLICT_1",
            "severity": "high",
            "source_values": [
                {"document_id": "DOC_006", "page": 10, "quote": "성립"},
                {"document_id": "DOC_007", "page": 1, "quote": "불성립"},
                {"document_id": "DOC_008", "page": 4, "quote": "불성립"},
            ],
        },
    ],
    "policy_links": [
        {
            "coverage_name": "시설소유",
            "clause_link_status": "matched",
            "clause_link_status_label": "조항 확인",
            "clause_ref": _ref("DOC_009", 2, "보상하지 않는 손해"),
            "uncertainty_reason": "나머지 조항 5건: ...",
            "requirements": [
                {"requirement_text": "시설소유 요건",
                 "evidence_status_label": "자료 있음",
                 "evidence_references": [_ref("DOC_009", 3, "요건 근거")]},
            ],
        },
        {
            "coverage_name": "수술",
            "clause_link_status": "not_found",
            "clause_link_status_label": "조항 미확인",
            "uncertainty_reason": "이 약관에 없는 담보입니다",
            "requirements": [],
        },
    ],
    "insurer_position": {
        "denial": {"reason_ids": ["R05"],
                   "evidence_references": [_ref("DOC_007", 1, "자연현상")]},
        "reduction": {},
        "acceptance": {},
    },
}


def _sections():
    sections = screening.markdown_sections(FULL_REPORT)
    assert sections, "markdown_sections returned nothing"
    return sections


def _balance(section) -> tuple[int, int]:
    return (section["content"].count("{{E}}"),
            len(section.get("evidence_references") or []))


@pytest.mark.parametrize("section", _sections(), ids=lambda s: s["heading"][:24])
def test_every_section_balances_placeholders_and_references(section):
    """The invariant, section by section, so a failure names the section."""
    placeholders, references = _balance(section)
    assert placeholders == references, (
        f"{section['heading']!r}: {placeholders} {{{{E}}}} placeholders but "
        f"{references} evidence_references -- these must match 1:1. "
        "Cite through `_mark`/`_bullet` rather than gathering references "
        "in a comprehension separate from the prose."
    )


@pytest.mark.parametrize("section", _sections(), ids=lambda s: s["heading"][:24])
def test_every_section_renders(section):
    """The real invariant: the assembly tool itself accepts each section."""
    assembly.render({"output_path": "unused.md", "sections": [section]})


def test_whole_report_renders_in_one_pass():
    """render() aborts at the FIRST bad section, so a whole-report pass is what
    proves no section downstream of a failure is still hiding one."""
    doc_text, sidecar = assembly.render(
        {"output_path": "unused.md", "sections": _sections()})
    assert doc_text
    tags = sum(section["content"].count("{{E}}") for section in _sections())
    assert len(sidecar["citations"]) == tags, (
        "sidecar citation count must equal the report's total placeholder count")


def test_section3_marks_its_filing_evidence():
    """Section 3 specifically: the trap that never fired.

    Every case so far left `filing_status` unknown, so the section published
    nothing and its missing markers stayed invisible. Here two types resolve to
    filed/not_filed WITH references, and the third stays unknown -- whose
    reference must be ignored entirely rather than published unmarked.
    """
    section = next(s for s in _sections() if s["heading"].startswith("3."))
    placeholders, references = _balance(section)
    assert references == 2, (
        "only filed/not_filed rows contribute evidence; the unknown row's "
        f"reference must be dropped, got {references}")
    assert placeholders == 2
    published = {ref["document_id"] for ref in section["evidence_references"]}
    assert "DOC_099" not in published, (
        "the unknown row's reference was published despite contributing no "
        "status a reader can act on")
