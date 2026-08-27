"""tools/score_screening_fidelity.py -- the arithmetic and the envelope, in the repo.

The first fidelity scores were produced by scratch scripts that lived outside the
repository, over 15 rows each chosen after reading the answer key, out of the 56
fields claim_analysis had emitted. Two numbers taken that way are not taken with
the same instrument, and nothing recorded which 15 either had used.

What this tool moves out of the scorer's hands: the field universe and each
field's grade, whether it is decision-bearing, whether the pipeline is even asked
to extract it, the upstream account of an absence, the weights, the arithmetic
and the verdict rule. What stays the scorer's is the judgement -- does this value
match, did the report raise this issue, did the answer key rely on this document.

The coverage rule is the load-bearing one: a field the pipeline resolved must
have a row. Deciding the answer key says nothing about it is still a judgement,
but it becomes one that is written down instead of one that leaves no trace.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "score_screening_fidelity.py"
CONFIG = json.loads(
    (ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json")
    .read_text(encoding="utf-8")
)
FIELDS = {f["field_id"]: f for f in CONFIG["fields"]}


def _grade(fid):
    row = FIELDS[fid]
    return row.get("medical_advisory_grade") or row["priority_grade"]


def _row(fid, match_kind, **over):
    row = {
        "field_id": fid,
        "screening_value": "x",
        "ground_truth_value": "x",
        "match_kind": match_kind,
        "screening_absence_kind": "not_applicable",
        "screening_quote": "- 주요 진단명: x",
        "gt_locator": "III.2 손해조사",
    }
    row.update(over)
    return row


def _rows(case_id, fields):
    return {
        "case_id": case_id,
        "case_types": ["liability"],
        "created_at": "2026-08-27T19:00:00+09:00",
        "target_report_sha256": "a" * 64,
        "ground_truth_files": ["GT_001.pdf"],
        "fields": fields,
        "issues": [
            {"gt_issue_id": "GT-1", "gt_issue_label": "배상책임 성립 여부", "predicted": True,
             "screening_quote": "- **핵심 쟁점** …", "screening_section": "10",
             "gt_locator": "IV"},
            {"gt_issue_id": "GT-2", "gt_issue_label": "과실 유무", "predicted": False,
             "gt_locator": "V"},
        ],
        "documents": [
            {"document_kind": "diagnosis_certificate", "relied_on_by_ground_truth": True,
             "screening_classification": "보유", "verdict": "correct",
             "screening_quote": "- 진단서: 보유", "gt_locator": "별첨 제3호"},
            {"document_kind": "medical_expense_receipt", "relied_on_by_ground_truth": True,
             "screening_classification": "미확인", "verdict": "under",
             "screening_quote": "- 진료비 영수증: 미확인", "gt_locator": "별첨 제4호"},
        ],
        "conclusion_agreement": "screening_declined",
        "conclusion_note": "리포트가 방향을 단정하지 않았다.",
        "conclusion_quote": "판단하지 않으며",
        "out_of_universe_items": [],
        "findings": [],
    }


def _run(tmp_path, rows, run_id="RUN_20260827_001"):
    src = tmp_path / "rows.json"
    src.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "result.json"
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--rows", str(src), "--run-id", run_id,
         "--out", str(out), "--quiet"],
        capture_output=True, text=True, cwd=ROOT,
    )
    result = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return proc, result


def test_grade_and_core_are_read_from_the_config_not_the_input(tmp_path):
    """The input names a field; it does not get to say what grade it is."""
    proc, result = _run(tmp_path, _rows("CASE_099", [
        _row("primary_diagnosis", "exact"),
        _row("comparative_negligence_rate", "mismatch", ground_truth_value="0%"),
    ]))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rows = {c["field_id"]: c for c in result["dimensions"][0]["field_comparisons"]}
    assert rows["primary_diagnosis"]["field_grade"] == _grade("primary_diagnosis")
    assert rows["comparative_negligence_rate"]["field_grade"] == "A", (
        "the legal axis grades 과실비율 A; ranking it on what a physician needs is "
        "what once left the sole basis of a liability determination at grade C"
    )
    assert rows["comparative_negligence_rate"]["core_field"] is True
    assert result["verdict"] == "divergent", "a core-field mismatch pins the verdict"
    assert result["core_field_mismatch"] is True


def test_a_deferred_field_cannot_be_scored_as_a_miss(tmp_path):
    deferred = next(f["field_id"] for f in CONFIG["fields"]
                    if f.get("extraction_wave") == "deferred")
    proc, _ = _run(tmp_path, _rows("CASE_099", [
        _row("primary_diagnosis", "exact"),
        _row(deferred, "missing_in_screening"),
    ]))
    assert proc.returncode == 1
    assert "not asked to extract" in proc.stdout


def test_an_unknown_field_id_is_refused(tmp_path):
    proc, _ = _run(tmp_path, _rows("CASE_099", [
        _row("primary_diagnosis", "exact"),
        _row("invented_field", "exact"),
    ]))
    assert proc.returncode == 1
    assert "not a field_id" in proc.stdout


def test_excluded_kinds_leave_the_denominator(tmp_path):
    """A field the answer key does not speak to still needs a row -- it just
    does not count. That is the whole point of requiring the row."""
    proc, result = _run(tmp_path, _rows("CASE_099", [
        _row("primary_diagnosis", "exact"),
        _row("diagnosis_code", "missing_in_ground_truth",
             screening_value=None, ground_truth_value=None),
    ]))
    assert proc.returncode == 0
    f1 = result["dimensions"][0]
    assert f1["score"] == 100.0, "one agreeing row, one excluded row"
    assert len(f1["field_comparisons"]) == 2, "the excluded row is still recorded"


def test_weights_and_verdict_come_from_the_rubric(tmp_path):
    proc, result = _run(tmp_path, _rows("CASE_099", [_row("primary_diagnosis", "exact")]))
    assert proc.returncode == 0
    weights = {d["dimension_id"]: d["weight"] for d in result["dimensions"]}
    assert weights == {"F1": 50, "F2": 30, "F3": 20, "F4": 0}
    # F1 100 (1 agreeing row), F2 50 (1 of 2 issues), F3 50 (1 correct of 2 counted):
    # (100*50 + 50*30 + 50*20) / 100 = 75.0
    assert result["fidelity_score"] == 75.0
    assert result["verdict"] == "partial"
    f4 = next(d for d in result["dimensions"] if d["dimension_id"] == "F4")
    assert f4["score"] is None, "F4 must not reach the headline"


def test_upstream_absence_reason_is_copied_when_the_case_has_a_contract(tmp_path):
    """Read from claim_analysis_result, never invented. CASE_705 is imported and
    has one; a fabricated case id has none and the fields stay null."""
    proc, result = _run(tmp_path, _rows("CASE_099", [_row("primary_diagnosis", "exact")]))
    row = result["dimensions"][0]["field_comparisons"][0]
    assert row["pipeline_resolution_status"] is None
    assert row["pipeline_unavailable_reason"] is None


@pytest.mark.skipif(not (ROOT / "outputs" / "CASE_705" / "claim_analysis_result.json").exists(),
                    reason="the imported corpus is not present in this checkout")
def test_a_field_the_pipeline_resolved_must_have_a_row(tmp_path):
    proc, _ = _run(tmp_path, _rows("CASE_705", [_row("primary_diagnosis", "exact")]))
    assert proc.returncode == 1
    assert "have no row" in proc.stdout
