"""Section 8 must emit one `{{E}}` per reference it publishes.

`document_assembly.render` refuses a section whose placeholder count differs
from its `evidence_references` count. Section 8 appended a matched coverage's
`clause_ref` -- and every requirement's evidence -- to the reference list
without ever emitting a marker, so any case reaching that branch died at
assembly with:

    Section '8. 관련 약관과 요건 자료상태': 0 {{E}} placeholders but
    2 evidence_references -- these must match 1:1.

It stayed invisible because two conditions have to hold at once: a
`screening_report_judgement.json` must be supplied, AND the linker must return
`matched` for a coverage. Before 2026-08-20 no coverage-level 약관 ever
matched (`find_clause` required exactly one clause hit, while a Korean coverage
is a whole 약관 of several articles), so the branch never ran with a real
`clause_ref`. CASE_700 was the first run where both held, and it failed.

The naive fix -- one marker per appended reference -- breaks the other way,
because the section publishes `_dedupe_references(...)`: two requirements
citing one sentence collapse to a single published reference and leave an
orphan placeholder. Both directions are silent at write time and fatal at
render time, so the tests below pin both.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import document_assembly as assembly  # noqa: E402
import run_screening_report as screening  # noqa: E402


def _section8(links):
    sections = [
        section for section in screening.markdown_sections({"policy_links": links})
        if section["heading"].startswith("8.")
    ]
    assert len(sections) == 1, "section 8 must be built exactly once"
    return sections[0]


def _balanced(section) -> bool:
    return (section["content"].count("{{E}}")
            == len(section.get("evidence_references") or []))


def _renders(section) -> None:
    """The real invariant: the assembly tool accepts the section."""
    assembly.render({"output_path": "unused.md", "sections": [section]})


MATCHED = {
    "coverage_name": "시설소유",
    "clause_link_status": "matched",
    "clause_ref": {"document_id": "DOC_009", "page": 2,
                   "quote": "보상하지 않는 손해"},
    "uncertainty_reason": "같은 담보의 나머지 조항 5건: ...",
    "requirements": [],
}
MATCHED_SECOND = {
    "coverage_name": "구내치료비",
    "clause_link_status": "matched",
    "clause_ref": {"document_id": "DOC_009", "page": 5,
                   "quote": "보상하는 손해"},
    "requirements": [],
}
NOT_FOUND = {
    "coverage_name": "수술",
    "clause_link_status": "not_found",
    "uncertainty_reason": "이 담보를 명시한 조항을 찾지 못했습니다",
    "requirements": [],
}


def test_the_case_700_shape_renders():
    """Two matched coverages plus a not_found -- exactly what failed."""
    section = _section8([MATCHED, MATCHED_SECOND, NOT_FOUND])
    assert section["content"].count("{{E}}") == 2
    assert len(section["evidence_references"]) == 2
    _renders(section)


def test_a_matched_clause_ref_is_cited_not_merely_printed():
    """The quote appears in the prose AND is published as a citation.

    Printing `DOC_009 p.2 보상하지 않는 손해` inline without a tag would leave
    a reader a reference they cannot look up in the sidecar.
    """
    section = _section8([MATCHED])
    assert "DOC_009" in section["content"]
    assert section["content"].count("{{E}}") == 1
    assert section["evidence_references"][0]["quote"] == "보상하지 않는 손해"
    _renders(section)


def test_requirement_evidence_is_cited_too():
    section = _section8([{
        "coverage_name": "입원",
        "clause_link_status": "matched",
        "clause_ref": {"document_id": "DOC_010", "page": 35,
                       "quote": "입원의 정의"},
        "requirements": [{
            "requirement_text": "입원 사실",
            "evidence_status": "supported",
            "evidence_references": [{"document_id": "DOC_017", "page": 1,
                                     "quote": "입원함"}],
        }],
    }])
    assert section["content"].count("{{E}}") == 2
    assert _balanced(section)
    _renders(section)


def test_a_repeated_citation_is_marked_once():
    """The dedupe direction. A clause_ref and a requirement citing the SAME
    sentence publish one reference, so exactly one marker may be emitted --
    marking each append would leave an orphan placeholder.
    """
    shared = {"document_id": "DOC_009", "page": 2, "quote": "보상하지 않는 손해"}
    section = _section8([{
        "coverage_name": "중복",
        "clause_link_status": "matched",
        "clause_ref": dict(shared),
        "requirements": [{
            "requirement_text": "같은 문장 재인용",
            "evidence_status": "supported",
            "evidence_references": [dict(shared)],
        }],
    }])
    assert section["content"].count("{{E}}") == 1
    assert len(section["evidence_references"]) == 1
    _renders(section)


def test_an_unusable_reference_gets_no_marker():
    """`_dedupe_references` drops a reference missing document_id or quote, so
    marking it would leave a placeholder with nothing to point at."""
    section = _section8([{
        "coverage_name": "빈 인용",
        "clause_link_status": "matched",
        "clause_ref": {"document_id": "DOC_009", "page": 2, "quote": "q"},
        "requirements": [{
            "requirement_text": "불완전",
            "evidence_status": "unknown",
            "evidence_references": [{"document_id": "", "page": 1, "quote": ""}],
        }],
    }])
    assert section["content"].count("{{E}}") == 1
    assert len(section["evidence_references"]) == 1
    _renders(section)


def test_an_unmatched_link_publishes_no_citation():
    """A not_found link prints its reason; a reason is not evidence."""
    section = _section8([NOT_FOUND])
    assert section["content"].count("{{E}}") == 0
    assert section.get("evidence_references") == []
    assert "찾지 못했습니다" in section["content"]
    _renders(section)


def test_a_case_with_no_policy_links_renders():
    section = _section8([])
    assert section["content"].count("{{E}}") == 0
    _renders(section)


def test_a_matched_link_keeps_its_note():
    """Guards the 2026-08-20 elif->if fix, which is what first routed a real
    clause_ref into this branch: a coverage spanning several articles elects
    one into `clause_ref`, and the sentence naming the rest must survive."""
    section = _section8([MATCHED])
    assert "나머지 조항 5건" in section["content"]


# --- every section, not one at a time ---------------------------------------
#
# Section 8 was fixed on 2026-08-20; section 6 carried the identical defect and
# was found only when CASE_701 crashed on it -- and because assembly stops at
# the first malformed section, dying in §6 meant §8's fix went unverified for a
# second run. Each round costs a full re-dispatch, so the invariant is checked
# for ALL sections at once from here on.

def _all_sections(report):
    return screening.markdown_sections(report)


def _report_with_a_conflict():
    """A conflict carrying THREE source documents -- CONFLICT_1's real shape."""
    return {
        "inconsistencies": [{
            "field": "liability_opinion_conclusion",
            "description": "두 법률의견서의 결론이 다릅니다",
            "conflict_ref": "CONFLICT_1",
            "severity": "high",
            "source_values": [
                {"document_id": "DOC_006", "page": 10, "quote": "성립"},
                {"document_id": "DOC_007", "page": 1, "quote": "불성립"},
                {"document_id": "DOC_008", "page": 4, "quote": "부담하지 않는다"},
            ],
        }],
    }


def test_a_conflict_with_several_sources_renders():
    """The exact CASE_701 crash: 0 placeholders against 3 references."""
    section = next(s for s in _all_sections(_report_with_a_conflict())
                   if s["heading"].startswith("6."))
    assert section["content"].count("{{E}}") == 3
    assert len(section["evidence_references"]) == 3
    _renders(section)


def test_a_conflicts_sources_are_cited_on_its_own_line():
    """Two conflicts must not pool their citations -- a reader following a tag
    has to land on the source for THAT disagreement."""
    report = _report_with_a_conflict()
    report["inconsistencies"].append({
        "field": "primary_diagnosis",
        "description": "진단명이 다릅니다",
        "source_values": [{"document_id": "DOC_011", "page": 1, "quote": "골절"}],
    })
    section = next(s for s in _all_sections(report)
                   if s["heading"].startswith("6."))
    first, second = section["content"].split("\n")[:2]
    assert first.count("{{E}}") == 3
    assert second.count("{{E}}") == 1
    _renders(section)


def test_no_conflicts_renders():
    section = next(s for s in _all_sections({"inconsistencies": []})
                   if s["heading"].startswith("6."))
    assert section["content"] == "- 검증된 충돌 없음"
    assert section["evidence_references"] == []
    _renders(section)


def test_every_section_balances_its_citations():
    """The whole-document guard. Assembly stops at the FIRST bad section, so a
    per-section test that happens to run in order hides the ones behind it.
    """
    report = _report_with_a_conflict()
    report["policy_links"] = [MATCHED, NOT_FOUND]
    unbalanced = [
        s["heading"] for s in _all_sections(report)
        if s["content"].count("{{E}}") != len(s.get("evidence_references") or [])
    ]
    assert not unbalanced, f"sections violating the 1:1 rule: {unbalanced}"
    # And the assembled document, which is what actually ships.
    assembly.render({"output_path": "unused.md",
                     "sections": _all_sections(report)})
