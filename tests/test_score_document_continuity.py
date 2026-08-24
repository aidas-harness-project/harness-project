"""The continuity detector must separate a real start from a continuation.

known-gaps #56. A document whose page 1 prints "- 7 -" after a document that
ends on "- 6 -" is the second half of that document. Measured on this repo's
corpus: 25 such pairs, 23 of them typed differently on each side, so the same
author's single opinion enters claim analysis as two independent sources.

These tests pin the DISCRIMINATIONS, not the corpus counts, which change as
cases are added. Every case runs against a tmp_path tree, never the real
outputs/ or data/ trees.

The two false-positive shapes are pinned deliberately, because #56's own
proposed rule ("first line is a page marker or a mid-outline heading") gets
both wrong: a legal-opinion letterhead opens `번    호 :` and a document can
open at `Ⅰ.`, and both are real starts.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import score_document_continuity as scorer  # noqa: E402


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A miniature outputs/+data/processed tree the scorer reads instead."""
    monkeypatch.setattr(scorer, "ROOT", str(tmp_path))

    def build(case_id: str, documents: list[dict]) -> None:
        """documents: [{id, type, pages:[first-line, ...]}]"""
        manifest = {"documents": []}
        for doc in documents:
            pages = doc["pages"]
            manifest["documents"].append({
                "document_id": doc["id"],
                "document_type": doc.get("type"),
                "pages": len(pages),
                **({"parent_document_id": doc["parent"]} if doc.get("parent")
                   else {}),
            })
            doc_dir = tmp_path / "data" / "processed" / case_id / doc["id"]
            doc_dir.mkdir(parents=True, exist_ok=True)
            for number, first_line in enumerate(pages, start=1):
                (doc_dir / f"page_{number:03d}.md").write_text(
                    f"{first_line}\n\nbody text\n", encoding="utf-8")
        case_dir = tmp_path / "outputs" / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "document_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    return build


def test_a_document_continuing_its_predecessor_is_reported(corpus):
    """The CASE_002 shape: a 6-page opinion followed by its own pages 7-10."""
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion",
         "pages": ["- 1 -", "- 2 -", "- 3 -", "- 4 -", "- 5 -", "- 6 -"]},
        {"id": "DOC_002", "type": "legal_reference",
         "pages": ["- 7 -", "- 8 -", "- 9 -", "- 10 -"]},
    ])
    pairs = scorer.scan()["continuation_pairs"]

    assert len(pairs) == 1
    assert pairs[0]["parent"] == "DOC_001"
    assert pairs[0]["child"] == "DOC_002"
    assert pairs[0]["parent_last_page"] == 6
    assert pairs[0]["child_first_page"] == 7
    assert pairs[0]["types_disagree"] is True


def test_a_document_starting_at_page_one_is_left_alone(corpus):
    """Two real documents that each number themselves from 1."""
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
    ])
    result = scorer.scan()

    assert result["continuation_pairs"] == []
    assert result["documents_starting_at_page_one"] == 2


def test_a_letterhead_first_page_is_not_a_continuation(corpus):
    """`번    호 :` opens a legal-opinion reply -- a real document start.

    #56's proposed rule would have merged these 11 documents into whatever
    preceded them. The printed number is absent here, so this rule declines
    rather than guessing.
    """
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion",
         "pages": ["- 1 -", "- 2 -", "- 3 -"]},
        {"id": "DOC_002", "type": "legal_opinion",
         "pages": ["번    호 :", "수    신 :"]},
    ])
    assert scorer.scan()["continuation_pairs"] == []


def test_a_roman_section_heading_is_not_a_continuation(corpus):
    """`Ⅰ. 사안의 요지 및 질의내용` is section one -- a start, not a middle."""
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_opinion",
         "pages": ["Ⅰ. 사안의 요지 및 질의내용", "1. 사안의 요지"]},
    ])
    assert scorer.scan()["continuation_pairs"] == []


def test_a_gap_in_the_numbering_is_not_a_continuation(corpus):
    """Numbering must run on EXACTLY. p6 then p9 is two documents that happen
    to be numbered, not one document split -- reporting it would invite a merge
    that loses pages 7-8."""
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion",
         "pages": ["- 1 -", "- 2 -", "- 3 -", "- 4 -", "- 5 -", "- 6 -"]},
        {"id": "DOC_002", "type": "legal_reference", "pages": ["- 9 -", "- 10 -"]},
    ])
    assert scorer.scan()["continuation_pairs"] == []


def test_a_number_inside_prose_is_not_read_as_a_page_marker(corpus):
    """The marker is matched whole-line. A sentence containing a hyphenated
    number must not be mistaken for one, or ordinary body text would start
    dissolving document boundaries."""
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_opinion",
         "pages": ["- 3 - 항의 기재를 참조하라", "다음 장"]},
    ])
    # The line BEGINS "- 3 -" and then continues into prose. `.match()` alone
    # accepts that; only the trailing `$` rejects it. A cross-reference to a
    # numbered item must not be read as this page's own number.
    assert scorer.scan()["continuation_pairs"] == []


def test_only_the_first_line_is_consulted(corpus):
    """A marker further down the page is not this page's own number.

    The corpus prints the number at the top. A footer-style marker, or a
    cross-reference sitting in the body, would let a document that opens with a
    real title be read as a continuation -- so scanning past the first
    non-empty line has to stay refused rather than merely unused.
    """
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_opinion", "pages": ["법률의견서"]},
    ])
    doc_dir = (Path(scorer.ROOT) / "data" / "processed" / "CASE_001" / "DOC_002")
    (doc_dir / "page_001.md").write_text(
        chr(10).join(["법률의견서", "", "- 3 -"]), encoding="utf-8")

    assert scorer.scan()["continuation_pairs"] == []


def test_a_three_way_split_reports_each_adjacent_pair(corpus):
    """CASE_046's shape: one opinion arriving as DOC_006/007/008, pages
    1-2/3-4/5-6. Each adjacent join is its own finding, so a reader sees the
    whole chain rather than only its first link."""
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_opinion", "pages": ["- 3 -", "- 4 -"]},
        {"id": "DOC_003", "type": "legal_reference", "pages": ["- 5 -", "- 6 -"]},
    ])
    pairs = scorer.scan()["continuation_pairs"]

    assert [(p["parent"], p["child"]) for p in pairs] == [
        ("DOC_001", "DOC_002"), ("DOC_002", "DOC_003")]


def test_same_typed_pairs_are_reported_but_flagged_as_agreeing(corpus):
    """`types_disagree` separates the harmful case from the merely untidy one.

    A pair typed the same is still a wrongly-split document, but it does not
    produce the double-counting #56 is about, so a reader should be able to
    tell them apart without re-deriving it.
    """
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_opinion", "pages": ["- 3 -", "- 4 -"]},
    ])
    pairs = scorer.scan()["continuation_pairs"]

    assert len(pairs) == 1
    assert pairs[0]["types_disagree"] is False


def test_a_segmentation_child_is_labelled_as_one(corpus):
    """The origin field is what says whether segment_case.py could have
    prevented it. Every pair in the real corpus is a separate raw PDF, so a
    fix there would have changed nothing -- that claim has to stay checkable
    rather than resting on the commit message."""
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_opinion", "pages": ["- 3 -", "- 4 -"],
         "parent": "DOC_000"},
    ])
    pairs = scorer.scan()["continuation_pairs"]

    assert pairs[0]["from_segmentation"] is True


def test_an_unnumbered_document_pair_is_ignored(corpus):
    """Most of the corpus prints no page numbers at all. Those documents must
    not be compared -- absence of a marker is not evidence of anything."""
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "medical_record", "pages": ["진 단 서", "소견"]},
        {"id": "DOC_002", "type": "receipt", "pages": ["영수증", "합계"]},
    ])
    result = scorer.scan()

    assert result["continuation_pairs"] == []
    assert result["documents_starting_at_page_one"] == 0


def test_scan_can_be_restricted_to_one_case(corpus):
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_reference", "pages": ["- 3 -"]},
    ])
    corpus("CASE_002", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_reference", "pages": ["- 3 -"]},
    ])

    assert scorer.scan()["cases_scanned"] == 2
    assert len(scorer.scan()["continuation_pairs"]) == 2

    one = scorer.scan("CASE_002")
    assert one["cases_scanned"] == 1
    assert one["affected_cases"] == ["CASE_002"]


def test_the_tool_reports_rather_than_gates(corpus):
    """Exit 0 even with findings. Merging documents is a decision about source
    material; failing a pipeline stage over it would force the wrong hand."""
    corpus("CASE_001", [
        {"id": "DOC_001", "type": "legal_opinion", "pages": ["- 1 -", "- 2 -"]},
        {"id": "DOC_002", "type": "legal_reference", "pages": ["- 3 -"]},
    ])
    assert scorer.main([]) == 0
