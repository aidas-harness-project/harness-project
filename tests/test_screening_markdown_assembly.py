"""The screening report's narrative half, and the tool that must produce it.

The helper wrote `screening_report.json` and set `report_path` to a filename
that nothing created. A reviewer following that path found no file, and the
evidence sidecar -- the artifact that makes every `[E#]` tag checkable -- did
not exist at all.

These tests pin the seam rather than the prose: the ten sections satisfy the
selective template's contract, evidence references travel with the content so
`document_assembly.py` can generate tags and sidecar from one source, and the
call is delegated to that tool rather than reimplemented. A renderer written
here would be a second, unverified way to produce the deliverable -- and it is
the verification (every quote checked against the processed text, refuse the
whole document otherwise) that the tool exists for.
"""
from __future__ import annotations

import json

import pytest

import document_assembly
import run_screening_report as reporter


def _report(**over) -> dict:
    report = {
        "case_summary": {
            "accident_date": "2026-03-02",
            "main_diagnosis": "우측 요골 골절",
            "kcd_code": "S52.5",
            "treatment_period": {"start_date": "2026-03-02", "end_date": None},
            "case_type_assessment": [{
                "case_type": "industrial_accident",
                "case_type_label": "산재/근재",
                "status": "applicable", "status_label": "해당",
                "basis": "작업 중 추락으로 기재됨",
                "filing_status_label": "확인 불가",
                "evidence_references": [{
                    "document_id": "DOC_001", "page": 1,
                    "quote": "작업 중 사다리에서 추락"}],
            }],
        },
        "inconsistencies": [{
            "field": "primary_diagnosis",
            "description": "진단서는 우측, 입퇴원요약은 좌측으로 기재되어 있습니다.",
            "summary_source": "consistency_check_professional_summary",
            "source_values": [
                {"document_id": "DOC_001", "page": 1, "quote": "우측 요골 골절"},
                {"document_id": "DOC_002", "page": 1, "quote": "좌측 요골 골절"},
            ],
        }],
        "required_document_checklist": [{
            "document_label": "진단서", "status_label": "보유",
            "required_for_case_types": ["산재/근재"]}],
        "unconfirmed_items": [{"label": "영상소견", "reason": "자료 없음"}],
        "existing_disability_assessment": {"status_label": "미확인"},
        "policy_links": [
            {"coverage_name": "수술", "clause_link_status": "matched"}],
        "insurer_position": {
            "denial": {"reason_ids": ["DR_1"]},
            "reduction": {"reason_ids": ["DR_3"]},
            "acceptance": {"accepted_coverage_ids": ["AC_1"]},
        },
    }
    report.update(over)
    return report


# ------------------------------------------------- the template contract --

def test_the_selective_template_is_the_one_used() -> None:
    """Not the legacy template, whose section 7 is 진행 가능성/난이도."""
    assert reporter.TEMPLATE == "screening_report_selective"


def test_the_sections_satisfy_the_template_exactly() -> None:
    headings = [s["heading"] for s in reporter.markdown_sections(_report())]
    assert len(headings) == 10
    assert document_assembly.validate_template(headings, reporter.TEMPLATE) == []


def test_the_legacy_template_would_reject_these_sections() -> None:
    """The two templates are genuinely different documents, not aliases."""
    headings = [s["heading"] for s in reporter.markdown_sections(_report())]
    assert document_assembly.validate_template(headings, "screening_report") != []


def test_every_section_renders_even_when_it_has_nothing_to_say() -> None:
    """A silently dropped section reads the same as one nobody looked at."""
    bare = {"case_summary": {"case_type_assessment": []}, "inconsistencies": [],
            "required_document_checklist": [], "unconfirmed_items": [],
            "existing_disability_assessment": {}, "policy_links": [],
            "insurer_position": {}}
    sections = reporter.markdown_sections(bare)
    assert len(sections) == 10
    assert all(section["content"].strip() for section in sections)
    assert document_assembly.validate_template(
        [s["heading"] for s in sections], reporter.TEMPLATE) == []


# -------------------------------------------------- content and evidence --

def test_evidence_travels_with_the_content_not_as_hand_written_tags() -> None:
    """P1: the tool generates the tags and the sidecar from one source."""
    sections = reporter.markdown_sections(_report())
    total = sum(len(s["evidence_references"]) for s in sections)
    assert total >= 3
    body = "\n".join(s["content"] for s in sections)
    assert "[E1]" not in body and "[E#]" not in body


def test_the_conflict_section_carries_the_ledger_wording() -> None:
    sections = reporter.markdown_sections(_report())
    conflicts = sections[5]
    assert "진단서는 우측" in conflicts["content"]
    quotes = {ref["quote"] for ref in conflicts["evidence_references"]}
    assert quotes == {"우측 요골 골절", "좌측 요골 골절"}


def test_denial_and_reduction_stay_separate_lines() -> None:
    """Collapsing them misstates what the insurer actually decided."""
    insurer = reporter.markdown_sections(_report())[8]["content"]
    assert "거절: DR_1" in insurer
    assert "감액: DR_3" in insurer
    assert "승인: AC_1" in insurer


def test_the_four_verdicts_and_filing_status_are_both_present() -> None:
    sections = reporter.markdown_sections(_report())
    assert "산재/근재: 해당" in sections[1]["content"]
    assert "접수 확인 불가" in sections[2]["content"]


def test_the_narrative_states_no_feasibility_or_payout() -> None:
    body = json.dumps(reporter.markdown_sections(_report()), ensure_ascii=False)
    for forbidden in ("진행 가능성", "난이도", "지급 가능성", "예상 보험금", "청구 권고"):
        assert forbidden not in body


# ------------------------------------------------------ delegation, not reimplementation --

def test_rendering_is_delegated_to_document_assembly(monkeypatch, tmp_path) -> None:
    """The quote verification and the sidecar are that tool's job."""
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(reporter.subprocess, "run", fake_run)
    path = reporter.render_markdown(
        case_id="CASE_9401", run_id="RUN_20260820_1",
        held_by="screening-report", report=_report())

    assert path == "outputs/CASE_9401/screening_report.md"
    assert len(calls) == 1
    command = calls[0]
    assert str(reporter.ASSEMBLY) in command
    assert "--template" in command
    assert command[command.index("--template") + 1] == "screening_report_selective"
    # Locked and attributed like any other governed write.
    assert "--held-by" in command and "--run-id" in command


def test_a_refused_render_raises_rather_than_reporting_success(monkeypatch) -> None:
    """A citation that does not resolve must stop the stage, not be routed around."""
    class Refused:
        returncode = 1
        stdout = "FAIL: quote does not resolve in DOC_001"
        stderr = ""

    monkeypatch.setattr(reporter.subprocess, "run", lambda cmd, **kw: Refused())
    with pytest.raises(RuntimeError) as excinfo:
        reporter.render_markdown(
            case_id="CASE_9401", run_id="RUN_20260820_1",
            held_by="screening-report", report=_report())
    assert "quote does not resolve" in str(excinfo.value)
