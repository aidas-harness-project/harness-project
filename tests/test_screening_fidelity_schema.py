"""screening_fidelity_result.schema.json -- the ground-truth comparison contract.

This contract is written by the one stage D1 admits to the answer key, so it
carries two jobs at once: record a comparison, and not become a copy of the
thing it compared against. The tests below pin the parts where getting it wrong
is silent.

  * **No passage-shaped field on the ground-truth side.** The locator and the
    short field value are capped; there is no quote field at all. A result that
    reproduced the answer key sentence by sentence would be the answer key, in
    a file producing stages can reach.
  * **A core-field mismatch pins the verdict.** A report naming the wrong
    diagnosis is not "80% aligned", so the schema refuses to store that pair.
  * **F4 is structurally excluded from the headline.** Conclusion direction is
    discretionary; a report that correctly declines to assert one must not be
    able to drag the score down, so F4 carries weight 0 and no score.
"""
import copy

from _validation import load_registry, validate_instance

SCHEMA = "screening_fidelity_result.schema.json"

GT_REF = {"file": "GT_001.pdf", "locator": "p.3 사정 결과"}


def _dim(dimension_id, weight, score, **extra):
    base = {
        "dimension_id": dimension_id,
        "weight": weight,
        "applicable": True,
        "na_reason": None,
        "score": score,
        "rationale": "정답지 항목과 대조함",
        "screening_quotes": ["- 주요 진단명: 우측 손목 요골 원위부 골절 [E1]"],
    }
    base.update(extra)
    return base


VALID = {
    "case_id": "CASE_021",
    "run_id": "RUN_20260825_001",
    "component": "screening-fidelity",
    "status": "success",
    "created_at": "2026-08-25T10:00:00+09:00",
    "schema_version": "screening_fidelity_result.v0.1",
    "rubric_version": "screening_fidelity.v0.1",
    "target_report_path": "outputs/CASE_021/screening_report.md",
    "target_report_sha256": "b" * 64,
    "ground_truth_access": {
        "version": "screening",
        "caller_stage": "screening_fidelity",
        "screening_stage_passed": True,
        "ground_truth_files": ["GT_001.pdf"],
    },
    "dimensions": [
        _dim(
            "F1", 50, 92.0,
            fact_match_rate=0.92,
            discretionary_agreement_rate=0.0,
            field_comparisons=[
                {
                    "field_name": "주진단명",
                    "field_kind": "fact",
                    "core_field": True,
                    "screening_value": "기타 명시된 뇌혈관질환",
                    "ground_truth_value": "기타 명시된 뇌혈관질환",
                    "match_kind": "exact",
                    "screening_absence_kind": "not_applicable",
                    "screening_quote": "'기타 명시된 뇌혈관질환(I67.8)'으로 진단받았다고 청구한",
                    "ground_truth_ref": GT_REF,
                }
            ],
        ),
        _dim(
            "F2", 30, 75.0,
            issue_matches=[
                {
                    "gt_issue_id": "GT-1",
                    "gt_issue_label": "진단확정의 객관적 근거",
                    "predicted": True,
                    "screening_quote": "본 건의 핵심 쟁점은 2025-10-10 영상소견이",
                    "screening_section": "3. 핵심 쟁점",
                    "ground_truth_ref": GT_REF,
                }
            ],
            screening_only_issues=[],
        ),
        _dim(
            "F3", 20, 80.0,
            document_comparisons=[
                {
                    "document_kind": "진단서",
                    "relied_on_by_ground_truth": True,
                    "screening_classification": "부족",
                    "verdict": "under",
                    "screening_quote": "I67.8 진단서 및 의무기록(경과기록)",
                    "ground_truth_ref": GT_REF,
                }
            ],
        ),
        _dim("F4", 0, None, conclusion_agreement="screening_declined"),
    ],
    "fidelity_score": 86.1,
    "core_field_mismatch": False,
    "verdict": "aligned",
    "findings": [],
}


def _errors(instance):
    schemas, registry = load_registry()
    return validate_instance(instance, SCHEMA, schemas, registry)


def _dim_of(instance, dimension_id):
    return next(d for d in instance["dimensions"] if d["dimension_id"] == dimension_id)


def test_valid_result_passes():
    assert _errors(VALID) == []


# ---- leak boundary ----

def test_ground_truth_side_has_no_quote_field():
    """There is no sanctioned place to put a sentence from the answer key."""
    bad = copy.deepcopy(VALID)
    _dim_of(bad, "F1")["field_comparisons"][0]["ground_truth_quote"] = (
        "피보험자의 청구는 약관상 지급요건을 충족하지 아니하므로"
    )
    assert _errors(bad), "an added ground-truth quote field must not validate"


def test_ground_truth_value_is_capped_to_a_field_value():
    bad = copy.deepcopy(VALID)
    _dim_of(bad, "F1")["field_comparisons"][0]["ground_truth_value"] = "가" * 201
    assert _errors(bad), "a 201-char ground-truth value is a passage, not a field value"


def test_ground_truth_locator_rejects_a_sentence_length_string():
    bad = copy.deepcopy(VALID)
    _dim_of(bad, "F1")["field_comparisons"][0]["ground_truth_ref"] = {
        "file": "GT_001.pdf",
        "locator": "나" * 121,
    }
    assert _errors(bad)


def test_access_record_pins_the_d1_conditions():
    for field, value in [("caller_stage", "evaluation"), ("screening_stage_passed", False)]:
        bad = copy.deepcopy(VALID)
        bad["ground_truth_access"][field] = value
        assert _errors(bad), field


# ---- scoring integrity ----

def test_core_field_mismatch_forces_divergent():
    bad = copy.deepcopy(VALID)
    bad["core_field_mismatch"] = True
    bad["verdict"] = "aligned"
    assert _errors(bad), "a core-field mismatch cannot be reported as aligned"

    ok = copy.deepcopy(bad)
    ok["verdict"] = "divergent"
    assert _errors(ok) == []


def test_f4_carries_no_score_and_no_weight():
    bad = copy.deepcopy(VALID)
    _dim_of(bad, "F4")["score"] = 100.0
    assert _errors(bad), "F4 must not carry a score -- it is excluded from the headline"

    bad2 = copy.deepcopy(VALID)
    _dim_of(bad2, "F4")["weight"] = 15
    assert _errors(bad2)


def test_f4_must_state_its_agreement():
    bad = copy.deepcopy(VALID)
    del _dim_of(bad, "F4")["conclusion_agreement"]
    assert _errors(bad)


def test_screening_declined_is_expressible():
    ok = copy.deepcopy(VALID)
    _dim_of(ok, "F4")["conclusion_agreement"] = "screening_declined"
    assert _errors(ok) == []


def test_applicable_dimension_needs_a_screening_quote():
    bad = copy.deepcopy(VALID)
    _dim_of(bad, "F1")["screening_quotes"] = []
    assert _errors(bad)


def test_na_dimension_needs_a_reason_and_may_drop_quotes():
    ok = copy.deepcopy(VALID)
    _dim_of(ok, "F3").update(
        {"applicable": False, "score": None, "screening_quotes": [],
         "na_reason": "정답지가 근거 문서를 열거하지 않음"}
    )
    assert _errors(ok) == []

    bad = copy.deepcopy(ok)
    _dim_of(bad, "F3")["na_reason"] = None
    assert _errors(bad)


def test_dropping_a_dimension_is_rejected():
    bad = copy.deepcopy(VALID)
    bad["dimensions"] = [d for d in bad["dimensions"] if d["dimension_id"] != "F2"]
    assert _errors(bad)


def test_predicted_issue_must_quote_the_report():
    bad = copy.deepcopy(VALID)
    _dim_of(bad, "F2")["issue_matches"][0]["screening_quote"] = None
    assert _errors(bad), "claiming the report predicted an issue requires the line that did"


def test_unpredicted_issue_needs_no_quote():
    ok = copy.deepcopy(VALID)
    ok_issue = _dim_of(ok, "F2")["issue_matches"][0]
    ok_issue.update({"predicted": False, "screening_quote": None, "screening_section": None})
    assert _errors(ok) == []


def test_screening_only_issue_is_classified():
    ok = copy.deepcopy(VALID)
    _dim_of(ok, "F2")["screening_only_issues"] = [
        {
            "label": "기지급 진료비 중복보상",
            "screening_quote": "구내치료비 ₩2,000,000의 실제 지급 여부가",
            "classification": "손사_미기재",
        }
    ]
    assert _errors(ok) == []

    bad = copy.deepcopy(ok)
    _dim_of(bad, "F2")["screening_only_issues"][0]["classification"] = "우수"
    assert _errors(bad)


def test_findings_use_stable_ids():
    bad = copy.deepcopy(VALID)
    bad["findings"] = [
        {
            "finding_id": "1",
            "severity": "high",
            "dimension_id": "F1",
            "description": "사고일이 정답지 기재와 다름",
            "fix_suggestion": "일자 출처를 재확인",
        }
    ]
    assert _errors(bad)

    ok = copy.deepcopy(bad)
    ok["findings"][0]["finding_id"] = "SF-1"
    assert _errors(ok) == []
