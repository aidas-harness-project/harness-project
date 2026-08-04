"""dao.py's check-untagged-claims -- the deterministic floor under the
untagged-claim shape (known-gaps item 38).

read-evidence-tags compares tags PRESENT against the sidecar, so a paragraph
with zero tags is in neither set and is structurally invisible to it: an
untagged fabrication is LESS detectable than a badly-tagged one. This floors
that layer the way check-forbidden-expressions floors the semantic P3 pass.

The two signals are measured against CASE_907's real drafts. Note that the
cheap keyword rule known-gaps item 38 originally proposed would have caught
only ONE of the two real instances -- IV-1-나 3) restated the insurer's
argument with no citation and contains no statutory word at all. That is why
the sibling-asymmetry signal exists.
"""
import json

import pytest

import dao


LIABILITY = ["^IV\\. 관계법규 및 보상책임"]


def _find(text, patterns=None):
    return dao.find_untagged_claims(text, patterns or LIABILITY)


# ---- scope ----

def test_only_analytical_sections_are_scanned():
    """A facts section legitimately carries uncited statements; scanning it
    would bury the real findings."""
    text = (
        "## III. 사고 및 손해 발생\n"
        "민법 제758조에 관한 서술이지만 사실관계 절이므로 대상이 아님\n"
        "## IV. 관계법규 및 보상책임\n"
        "민법 제758조 제1항이 적용되는 사안임\n"
    )
    lines = [f["line"] for f in _find(text)]
    assert lines == [4]


def test_tagged_line_is_never_flagged():
    text = "## IV. 관계법규 및 보상책임\n민법 제758조 제1항이 적용되는 사안임 [E12]\n"
    assert _find(text) == []


# ---- signal 1: statutory reference with no citation ----

def test_statutory_reference_without_citation_is_flagged():
    text = "## IV. 관계법규 및 보상책임\n상법 제724조 제2항에 따른 직접청구가 가능한 담보임\n"
    findings = _find(text)
    assert [f["signal"] for f in findings] == ["statutory_ref_no_citation"]


def test_cross_reference_does_not_excuse_a_statutory_claim():
    """Regression on a real miss: the exemption list is line-wide, so `위 1항`
    suppressed CASE_907's CF-1 line -- the single most important hit -- because
    the same sentence also pointed at another section. Pointing elsewhere does
    not discharge the duty to cite a provision you assert the content of."""
    text = ("## IV. 관계법규 및 보상책임\n"
            "상법 제724조 제2항 및 약관상 직접청구 규정에 따른 담보이나, "
            "그 행사 가부는 위 1항의 성립 여부에 따라 달라짐.\n")
    assert [f["signal"] for f in _find(text)] == ["statutory_ref_no_citation"]


def test_bare_subheading_is_not_a_claim():
    """`- 나. 특별약관의 적정성 여부` is a label with no predicate. Real false
    positive found on CASE_021 v2."""
    text = "## IV. 관계법규 및 보상책임\n- 나. 특별약관의 적정성 여부\n"
    assert _find(text) == []


def test_same_marker_with_a_full_sentence_is_still_a_claim():
    """The exclusion above must not swallow the sentence form of 가./나."""
    text = ("## IV. 관계법규 및 보상책임\n"
            "- 가. 보험금의 지급사유 : 특별약관은 진단확정된 경우 지급하도록 정한다\n")
    assert len(_find(text)) == 1


def test_bold_run_in_heading_is_not_a_claim():
    text = "## IV. 관계법규 및 보상책임\n**1. 손해배상책임**\n"
    assert _find(text) == []


# ---- signal 2: untagged among cited siblings ----

def test_untagged_item_among_cited_siblings_is_flagged():
    """CASE_907 IV-1-나 3): siblings 1/2/4 cite evidence, 3) cites nothing and
    contains no statutory word -- invisible to the keyword signal alone."""
    text = (
        "## IV. 관계법규 및 보상책임\n"
        "  1) 강설 및 결빙은 예측하기 어려움 [E48]\n"
        "  2) 사고 장소는 규모가 큰 시설임 [E49]\n"
        "  3) 시설 이용자에게는 스스로 위험을 방지할 것이 기대됨\n"
        "  4) 배상책임을 부담하지 않는다는 취지임 [E50]\n"
    )
    findings = _find(text)
    assert [(f["line"], f["signal"]) for f in findings] == [(4, "untagged_among_cited_siblings")]


def test_single_cited_sibling_is_too_thin_to_set_a_norm():
    text = ("## IV. 관계법규 및 보상책임\n"
            "  1) 첫 번째 항목 [E1]\n"
            "  2) 두 번째 항목은 인용이 없음\n")
    assert _find(text) == []


def test_no_citation_owed_lines_are_exempt_from_the_sibling_signal():
    """A check that flags the draft's own P3 hedging teaches the critic to
    ignore it."""
    text = (
        "## IV. 관계법규 및 보상책임\n"
        "  1) 첫 항목 [E1]\n"
        "  2) 둘째 항목 [E2]\n"
        "  3) 최종 판단은 법률전문가의 검토를 요함\n"
        "  4) 면책사유 비해당 : 아래 다항 참조\n"
    )
    assert _find(text) == []


def test_a_line_is_reported_once_not_under_both_signals():
    text = (
        "## IV. 관계법규 및 보상책임\n"
        "  1) 첫 항목 [E1]\n"
        "  2) 둘째 항목 [E2]\n"
        "  3) 민법 제758조에 따라 책임이 성립함\n"
    )
    findings = _find(text)
    assert len(findings) == 1
    assert findings[0]["signal"] == "statutory_ref_no_citation"


def test_findings_are_ordered_by_line():
    text = (
        "## IV. 관계법규 및 보상책임\n"
        "  1) 첫 항목 [E1]\n"
        "  2) 둘째 항목 [E2]\n"
        "  3) 인용 없는 항목\n"
        "민법 제758조를 원용하는 문장\n"
    )
    assert [f["line"] for f in _find(text)] == [4, 5]


# ---- CLI contract ----

def test_unknown_template_never_silently_scans_everything(tmp_path, capsys):
    """A whole-file fallback would flood the caller and read as 'the check
    ran'."""
    draft = tmp_path / "d.md"
    draft.write_text("## IV. 관계법규 및 보상책임\n민법 제758조\n", encoding="utf-8")

    class A:
        doc_path, template = str(draft), "no_such_template"

    assert dao.cmd_check_untagged_claims(A()) == 2
    assert "NO_ANALYTICAL_SECTIONS" in capsys.readouterr().out


def test_missing_draft_reports_not_found(tmp_path, capsys):
    class A:
        doc_path, template = str(tmp_path / "missing.md"), "배상책임_후유장해형"

    assert dao.cmd_check_untagged_claims(A()) == 1
    assert "NOT_FOUND" in capsys.readouterr().out


def test_clean_draft_exits_zero(tmp_path, capsys):
    draft = tmp_path / "d.md"
    draft.write_text("## IV. 관계법규 및 보상책임\n민법 제758조가 적용됨 [E1]\n", encoding="utf-8")

    class A:
        doc_path, template = str(draft), "배상책임_후유장해형"

    assert dao.cmd_check_untagged_claims(A()) == 0
    assert json.loads(capsys.readouterr().out)["clean"] is True


def test_registry_declares_analytical_sections_for_every_draft_template():
    """The scope lives in the registry (open-decisions.md #2), so a template
    that renames its analysis sections updates one file. A draft template
    silently missing the key would make the check a no-op for it."""
    for key in ("배상책임_후유장해형", "진단수술비형"):
        assert dao._analytical_patterns(key), key
