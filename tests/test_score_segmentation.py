"""Tests for the segmentation scorer -- Stage 1 build step 7's measurement.

The scorer is what makes 'did the crop-ratio change help?' answerable, so its
boundary-set math and its baseline parser both need to be right.
"""
import json

import pytest

import score_segmentation as ss


# --------------------------------------------------- baseline parsing --

def test_parses_boundaries_from_the_marked_code_block():
    md = (
        "# baseline\n\nsome table here\n\n"
        "## 경계 페이지 목록 (채점기 입력용)\n\n"
        "```\n1, 14, 15, 29,\n74, 105\n```\n\n"
        "trailing prose\n"
    )
    assert ss.parse_baseline_boundaries(md) == [1, 14, 15, 29, 74, 105]


def test_prefers_the_block_after_the_marker_not_an_earlier_one():
    md = (
        "```\n999\n```\n"           # an unrelated earlier block
        "\n경계 페이지 목록\n\n"
        "```\n1, 2, 3\n```\n"
    )
    assert ss.parse_baseline_boundaries(md) == [1, 2, 3]


def test_falls_back_to_a_pure_number_block_without_a_marker():
    md = "no marker here\n\n```\n5, 6, 7\n```\n"
    assert ss.parse_baseline_boundaries(md) == [5, 6, 7]


def test_raises_when_no_boundary_block_exists():
    with pytest.raises(ValueError):
        ss.parse_baseline_boundaries("just prose, no code block")


def test_dedupes_and_sorts():
    md = "경계 페이지 목록\n```\n3, 1, 1, 2\n```"
    assert ss.parse_baseline_boundaries(md) == [1, 2, 3]


# --------------------------------------------------------- scoring --

def _prop(starts):
    return {"segments": [{"page_start": s, "page_end": s} for s in starts]}


def test_perfect_match_scores_one():
    truth = [1, 14, 29, 74]
    r = ss.score(ss.proposal_boundaries(_prop(truth)), truth)
    assert r["precision"] == 1.0 and r["recall"] == 1.0 and r["f1"] == 1.0
    assert r["false_positives_over_split"] == []
    assert r["false_negatives_over_merge"] == []


def test_over_split_shows_up_as_false_positives():
    truth = [1, 14]
    pred = [1, 14, 20]  # 20 is a boundary the baseline does not have
    r = ss.score(pred, truth)
    assert r["false_positives_over_split"] == [20]
    assert r["recall"] == 1.0  # nothing real was missed
    assert r["precision"] < 1.0


def test_over_merge_shows_up_as_false_negatives():
    truth = [1, 14, 29]
    pred = [1, 14]  # missed the p29 boundary -> two docs merged into one
    r = ss.score(pred, truth)
    assert r["false_negatives_over_merge"] == [29]
    assert r["precision"] == 1.0  # everything predicted was real
    assert r["recall"] < 1.0


def test_empty_prediction_scores_zero_without_dividing_by_zero():
    r = ss.score([], [1, 2, 3])
    assert r["precision"] == 0.0 and r["recall"] == 0.0 and r["f1"] == 0.0


def test_proposal_boundaries_reads_segment_starts():
    proposal = {"segments": [
        {"page_start": 1, "page_end": 13}, {"page_start": 14, "page_end": 14}]}
    assert ss.proposal_boundaries(proposal) == [1, 14]


# ------------------------------------------------------------ CLI --

def test_cli_scores_a_proposal_against_inline_boundaries(tmp_path, capsys):
    proposal = tmp_path / "prop.json"
    proposal.write_text(json.dumps(_prop([1, 14, 29])), encoding="utf-8")
    rc = ss.main(["--proposal", str(proposal), "--baseline-boundaries", "1,14,29"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["f1"] == 1.0


def test_cli_returns_nonzero_when_boundaries_are_wrong(tmp_path, capsys):
    proposal = tmp_path / "prop.json"
    proposal.write_text(json.dumps(_prop([1, 14])), encoding="utf-8")
    rc = ss.main(["--proposal", str(proposal), "--baseline-boundaries", "1,14,29"])
    assert rc == 1  # a scripted run can gate on this
    out = json.loads(capsys.readouterr().out)
    assert out["false_negatives_over_merge"] == [29]


def test_cli_reads_boundaries_from_a_baseline_file(tmp_path, capsys):
    baseline = tmp_path / "baseline.md"
    baseline.write_text("경계 페이지 목록\n```\n1, 14, 29\n```", encoding="utf-8")
    proposal = tmp_path / "prop.json"
    proposal.write_text(json.dumps(_prop([1, 14, 29])), encoding="utf-8")
    rc = ss.main(["--proposal", str(proposal), "--baseline", str(baseline)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["recall"] == 1.0


def test_the_real_baseline_file_parses_to_seventy_boundaries():
    """Guards the actual ground-truth artifact: if an edit breaks the code block
    or the marker, this fails loudly rather than the E2E scoring silently
    reading the wrong set."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    baseline = root / "_segmentation_scratch" / "CASE_BASELINE_DOC_001" / "BASELINE.md"
    if not baseline.exists():
        pytest.skip("baseline artifact not present (gitignored scratch)")
    boundaries = ss.parse_baseline_boundaries(baseline.read_text(encoding="utf-8"))
    assert len(boundaries) == 70
    assert boundaries[0] == 1 and boundaries[-1] == 110
