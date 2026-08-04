"""core_field_accuracy split into fact-extraction vs discretionary agreement
(findings 2026-08-04 §9).

A single accuracy number silently measures different things depending on what
the answer key happens to be. CASE_907's ground truth was a 손해액 산정서, so
normative values (과실률, which figure counts as 손해액) landed in the
denominator and BOTH of its misses came from there -- its 8 fact fields matched
8/8. Reported as one number, 0.80 reads as worse than a 0.875 whose denominator
held no discretionary field at all.

Computing the split rather than hand-counting it also reconciles the headline
against its own comparison list, which caught a real arithmetic error in a
shipped result (CASE_024 recorded 0.7 for 6 matches of 10).
"""
import json

import pytest

import dao


def _c(name, match, kind=None):
    c = {"field_name": name, "predicted_value": "p", "actual_value": "a", "match": match}
    if kind:
        c["field_kind"] = kind
    return c


def test_splits_by_field_kind():
    result = dao.split_core_field_accuracy([
        _c("kcd_code", True, "fact"),
        _c("surgery_name", True, "fact"),
        _c("과실률", False, "discretionary"),
    ])
    assert result["fact_extraction_score"] == 1.0
    assert result["discretionary_agreement_score"] == 0.0
    assert result["overall_score"] == pytest.approx(2 / 3, abs=1e-4)


def test_perfect_extraction_can_sit_under_a_mediocre_headline():
    """The finding in one assertion: 12/12 facts correct, headline 0.75."""
    comparisons = [_c(f"f{i}", True, "fact") for i in range(12)]
    comparisons += [_c(f"d{i}", False, "discretionary") for i in range(4)]
    result = dao.split_core_field_accuracy(comparisons)
    assert result["fact_extraction_score"] == 1.0
    assert result["overall_score"] == 0.75


def test_unclassified_comparisons_are_counted_but_never_guessed():
    """A pre-2026-08-04 file has no field_kind. Inferring one would silently
    move the very numbers this split exists to keep honest, so unclassified
    entries are excluded from both sub-scores and reported."""
    result = dao.split_core_field_accuracy([
        _c("classified", True, "fact"),
        _c("legacy_a", False),
        _c("legacy_b", True),
    ])
    assert result["fact_extraction_score"] == 1.0
    assert result["discretionary_agreement_score"] is None
    assert result["counts"]["unclassified"] == 2
    # The overall score still covers everything -- it is the pre-existing metric.
    assert result["overall_score"] == pytest.approx(2 / 3, abs=1e-4)


def test_absent_bucket_scores_null_not_zero():
    """No discretionary field is 'not measured', not 'scored zero'."""
    result = dao.split_core_field_accuracy([_c("kcd", True, "fact")])
    assert result["discretionary_agreement_score"] is None
    assert result["fact_extraction_score"] == 1.0


def test_empty_comparisons_are_all_null():
    result = dao.split_core_field_accuracy([])
    assert result["fact_extraction_score"] is None
    assert result["discretionary_agreement_score"] is None
    assert result["overall_score"] is None


def test_counts_are_reported_for_every_bucket():
    result = dao.split_core_field_accuracy([
        _c("a", True, "fact"), _c("b", False, "fact"),
        _c("c", False, "discretionary"),
    ])
    assert result["counts"] == {
        "fact": 2, "fact_matched": 1,
        "discretionary": 1, "discretionary_matched": 0,
        "unclassified": 0, "total": 3,
    }


# ---- the real shipped files ----

def test_every_shipped_evaluation_result_reconciles(tmp_path):
    """A recorded score that disagrees with its own comparison list is a
    counting error. CASE_024 shipped with 0.7 for 6/10 and is corrected;
    this keeps any future one from shipping."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    mismatches = []
    for path in sorted(root.glob("outputs/CASE_*/evaluation_result*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        cfa = data.get("core_field_accuracy") or {}
        if not cfa.get("applicable") or cfa.get("score") is None:
            continue
        computed = dao.split_core_field_accuracy(cfa.get("field_comparisons") or [])
        if computed["overall_score"] is None:
            continue
        if abs(computed["overall_score"] - cfa["score"]) >= 0.005:
            mismatches.append((path.name, cfa["score"], computed["overall_score"]))
    assert mismatches == [], f"recorded score disagrees with field_comparisons: {mismatches}"


def test_schema_accepts_the_split_fields_and_rejects_a_bad_kind():
    from _validation import load_registry, validate_instance
    schemas, registry = load_registry()
    schema = "evaluation_result.schema.json"

    base = {"core_field_accuracy": {
        "applicable": True, "na_reason": None, "score": 1.0,
        "fact_extraction_score": 1.0, "discretionary_agreement_score": None,
        "field_comparisons": [_c("kcd", True, "fact")]}}
    errors = validate_instance(base, schema, schemas, registry)
    assert not any("core_field_accuracy" in str(e) for e in errors), errors

    bad = json.loads(json.dumps(base))
    bad["core_field_accuracy"]["field_comparisons"][0]["field_kind"] = "judgement"
    errors2 = validate_instance(bad, schema, schemas, registry)
    assert any("field_kind" in str(e) or "judgement" in str(e) for e in errors2), errors2
