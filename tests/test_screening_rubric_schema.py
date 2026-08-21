"""screening_rubric_result.schema.json -- the auditable-score contract.

A rubric score is only worth something if a reader can check it. Two rules
carry that: every applicable axis cites at least one verbatim quote from the
report it scored, and the result names the exact bytes it scored
(target_report_sha256). Without the first, a score is a vibe; without the
second, a score silently outlives the report it describes -- the same failure
screening_report.schema.json's source_denial_contract_hash exists to prevent.

The N/A case is the other half. This scorer sees the report and nothing else,
so an axis can be genuinely inapplicable (disability anchors on a case with no
disability claim). Recording that as a zero would mean a report is punished for
a claim it never made, so applicable:false is a first-class state -- and it
must carry a reason, or it becomes a way to make a low axis disappear.
"""
import copy
from pathlib import Path

from _validation import load_registry, schema_name_for, validate_instance

SCHEMA = "screening_rubric_result.schema.json"

VALID = {
    "case_id": "CASE_705",
    "run_id": "RUN_20260821_001",
    "component": "screening-rubric",
    "status": "success",
    "created_at": "2026-08-21T14:00:00+09:00",
    "schema_version": "screening_rubric_result.v0.1",
    "rubric_version": "screening_rubric.v0.1",
    "target_report_path": "rubric_test/screening_report_705.md",
    "target_report_sha256": "a" * 64,
    "axis_scores": [
        {
            "axis_id": axis_id,
            "raw": 4,
            "weight": weight,
            "applicable": True,
            "na_reason": None,
            "rationale": "기준 충족",
            "evidence_quotes": ["## 1. 사고와 공통 의료정보"],
        }
        for axis_id, weight in
        [("A1", 10), ("A2", 25), ("A3", 15), ("A4", 20), ("A5", 15), ("A6", 15)]
    ],
    "weighted_total": 100.0,
    "gates": [
        {
            "gate_id": gate_id,
            "triggered": False,
            "quote": None,
            "section": None,
            "description": "해당 없음",
        }
        for gate_id in ["G1", "G2", "G3", "G4", "G5"]
    ],
    "verdict": "pass",
    "findings": [],
}


def _errors(instance):
    schemas, registry = load_registry()
    return validate_instance(instance, SCHEMA, schemas, registry)


def test_valid_result_passes():
    assert _errors(VALID) == []


def test_applicable_axis_without_quote_is_rejected():
    bad = copy.deepcopy(VALID)
    bad["axis_scores"][0]["evidence_quotes"] = []
    assert _errors(bad), "an applicable axis with no quote must not validate"


def test_raw_outside_zero_to_four_is_rejected():
    bad = copy.deepcopy(VALID)
    bad["axis_scores"][0]["raw"] = 5
    assert _errors(bad)


def test_na_axis_may_omit_quotes_but_needs_a_reason():
    ok = copy.deepcopy(VALID)
    ok["axis_scores"][0].update(
        {
            "applicable": False,
            "raw": None,
            "evidence_quotes": [],
            "na_reason": "후유장해 항목이 없는 사건",
        }
    )
    assert _errors(ok) == []

    bad = copy.deepcopy(ok)
    bad["axis_scores"][0]["na_reason"] = None
    assert _errors(bad), "applicable:false without na_reason must not validate"


def test_triggered_gate_requires_a_quote():
    bad = copy.deepcopy(VALID)
    bad["gates"][0].update({"triggered": True, "quote": None})
    assert _errors(bad), "a triggered gate with no quote must not validate"


def test_findings_use_stable_ids():
    bad = copy.deepcopy(VALID)
    bad["findings"] = [
        {
            "finding_id": "1",
            "severity": "high",
            "section": "4. 의료영역 확보 현황",
            "description": "주진단명 미기재",
            "fix_suggestion": "진단서 기재값을 옮기거나 부재를 명시",
        }
    ]
    assert _errors(bad), "finding_id must follow SR-<n>"

    ok = copy.deepcopy(bad)
    ok["findings"][0]["finding_id"] = "SR-1"
    assert _errors(ok) == []


def test_dropping_an_axis_is_rejected():
    """All six axes are always present. An axis that does not apply is recorded
    applicable:false with a reason -- omitting it entirely would let a weak axis
    vanish from the denominator without leaving a trace."""
    bad = copy.deepcopy(VALID)
    bad["axis_scores"] = bad["axis_scores"][:5]
    assert _errors(bad)


def test_duplicate_axis_id_is_rejected():
    bad = copy.deepcopy(VALID)
    bad["axis_scores"][1]["axis_id"] = "A1"
    assert _errors(bad)


def test_bad_sha_is_rejected():
    bad = copy.deepcopy(VALID)
    bad["target_report_sha256"] = "not-a-hash"
    assert _errors(bad)


def test_schema_is_registered_by_filename():
    assert schema_name_for(Path("screening_rubric_result_v1.json")) == SCHEMA
