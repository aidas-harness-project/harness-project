import json
from pathlib import Path

import validate_loss_adjustment_report as validator


def _load_example() -> dict:
    return json.loads(
        Path(
            "loss-adjustment-format-study/analysis/examples/example-disease-benefit.json"
        ).read_text(encoding="utf-8")
    )


def _clear_model_review_flags(document: dict) -> None:
    def walk(value):
        if isinstance(value, dict):
            if "human_review_required" in value:
                value["human_review_required"] = False
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
    for issue in document["reasoning_issues"]:
        issue["disposition"] = "supported"
    for calculation in document["calculations"]:
        calculation["status"] = "complete"


def _schema() -> Path:
    return Path(
        "loss-adjustment-format-study/analysis/loss-adjustment-report.schema.json"
    )


def test_validate_example_accepts_review_gated_document():
    assert validator.validate_document(_load_example(), _schema()) == []


def test_validate_document_rejects_unknown_evidence_reference():
    document = _load_example()
    document["sections"]["summary"]["statements"][0]["evidence_refs"] = ["E999"]

    errors = validator.validate_document(document, _schema())

    assert any("unknown evidence reference E999" in error for error in errors)


def test_validate_document_blocks_approval_when_human_review_is_required():
    document = _load_example()
    document["review_gates"]["finalization"] = "approved"
    document["review_gates"]["medical"] = "passed"

    errors = validator.validate_document(document, _schema())

    assert any("cannot be approved" in error for error in errors)


def test_validate_document_blocks_professional_gates_for_review_required_issue():
    document = _load_example()
    _clear_model_review_flags(document)
    for _, value in validator._walk(document):
        if not isinstance(value, dict):
            continue
        if value.get("support_type") == "professional_judgment":
            value["support_type"] = "documentary"
        if isinstance(value.get("unresolved_items"), list):
            value["unresolved_items"] = []
    document["review_gates"]["medical"] = "passed"
    document["review_gates"]["legal"] = "passed"
    document["reasoning_issues"][0]["disposition"] = "supported"

    baseline_errors = validator.validate_document(document, _schema())

    assert not any(
        "professional review gate cannot pass" in error for error in baseline_errors
    )

    document["reasoning_issues"][0]["disposition"] = "human_review_required"

    errors = validator.validate_document(document, _schema())

    assert any("professional review gate cannot pass" in error for error in errors)


def test_validate_document_rejects_out_of_order_components():
    document = _load_example()
    components = document["document_profile"]["ordered_components"]
    components[3], components[8] = components[8], components[3]

    errors = validator.validate_document(document, _schema())

    assert any("canonical order" in error for error in errors)


def test_validate_document_rejects_unavailable_referenced_evidence():
    document = _load_example()
    document["evidence_registry"][0]["available"] = False

    errors = validator.validate_document(document, _schema())

    assert any("references unavailable evidence E1" in error for error in errors)


def test_validate_document_reconciles_final_benefit_amount():
    document = _load_example()
    document["calculations"][0]["status"] = "complete"
    document["final_assessment"]["outcome"] = "payable"
    document["final_assessment"]["amount"] = {
        "value": 9000000,
        "currency": "KRW",
    }
    document["final_assessment"]["net_calculation_ref"] = "C1"

    errors = validator.validate_document(document, _schema())

    assert any("does not reconcile" in error for error in errors)


def test_validate_document_rejects_payable_outcome_with_resolved_denial_issue():
    document = _load_example()
    _clear_model_review_flags(document)
    calculation = document["calculations"][0]
    calculation["status"] = "complete"
    document["reasoning_issues"][0]["outcome_effect"] = "denies_payment"
    document["final_assessment"].update(
        {
            "outcome": "payable",
            "amount": {"value": calculation["result"], "currency": "KRW"},
            "net_calculation_ref": calculation["calculation_id"],
        }
    )

    errors = validator.validate_document(document, _schema())

    assert any("denying issue requires a not_payable outcome" in error for error in errors)


def test_validate_document_rejects_resolved_outcome_with_unresolved_issue():
    document = _load_example()
    calculation = document["calculations"][0]
    calculation["status"] = "complete"
    document["final_assessment"].update(
        {
            "outcome": "payable",
            "amount": {"value": calculation["result"], "currency": "KRW"},
            "net_calculation_ref": calculation["calculation_id"],
        }
    )

    errors = validator.validate_document(document, _schema())

    assert any(
        "unresolved issue requires an unresolved final outcome" in error
        for error in errors
    )


def test_validate_document_treats_undetermined_issue_as_unresolved():
    document = _load_example()
    _clear_model_review_flags(document)
    calculation = document["calculations"][0]
    calculation["status"] = "complete"
    document["reasoning_issues"][0]["disposition"] = "undetermined"
    document["final_assessment"].update(
        {
            "outcome": "payable",
            "amount": {"value": calculation["result"], "currency": "KRW"},
            "net_calculation_ref": calculation["calculation_id"],
        }
    )

    errors = validator.validate_document(document, _schema())

    assert any("unresolved issue requires an unresolved final outcome" in error for error in errors)


def test_validate_document_requires_not_payable_for_resolved_denial():
    document = _load_example()
    _clear_model_review_flags(document)
    document["reasoning_issues"][0]["outcome_effect"] = "denies_payment"
    document["final_assessment"].update(
        {"outcome": "human_review_required", "amount": None}
    )

    errors = validator.validate_document(document, _schema())

    assert any("denying issue requires a not_payable outcome" in error for error in errors)


def test_validate_document_enforces_family_specific_requirements():
    document = _load_example()
    document["document_profile"]["family"] = "liability_damages"

    errors = validator.validate_document(document, _schema())

    assert any("does not contain items matching" in error for error in errors)
    assert any("mechanism does not match" in error for error in errors)


def test_validate_document_rejects_approved_authoring_status():
    document = _load_example()
    document["review_gates"]["finalization"] = "approved"

    errors = validator.validate_document(document, _schema())

    assert any("model-authored document cannot claim final approval" in error for error in errors)


def test_model_authored_document_cannot_self_assert_final_approval():
    document = _load_example()
    _clear_model_review_flags(document)
    document["review_gates"].update(
        {
            "evidence": "passed",
            "calculation": "passed",
            "medical": "passed",
            "legal": "not_applicable",
            "finalization": "approved",
            "human_approval_records": [
                {
                    "approval_id": "A1",
                    "reviewer_role": "final",
                    "status": "approved",
                    "reviewer_identifier": "fabricated-reviewer",
                    "recorded_at": "not-a-date",
                    "scope": "fabricated final approval",
                    "record_source": "model-authored",
                }
            ],
        }
    )

    errors = validator.validate_document(document, _schema())

    assert any("model-authored document cannot claim final approval" in error for error in errors)


def test_model_authored_professional_gates_cannot_self_clear_open_judgment():
    document = _load_example()
    document["review_gates"]["medical"] = "passed"
    document["review_gates"]["legal"] = "passed"

    errors = validator.validate_document(document, _schema())

    assert any("professional review gate cannot pass" in error for error in errors)


def test_open_professional_judgment_cannot_be_not_applicable():
    document = _load_example()
    document["review_gates"]["medical"] = "not_applicable"

    errors = validator.validate_document(document, _schema())

    assert any("professional review gate must remain open" in error for error in errors)


def test_validate_document_recomputes_declared_calculation():
    document = _load_example()
    document["calculations"][0]["inputs"][1]["value"] = "0.5"

    errors = validator.validate_document(document, _schema())

    assert any("result does not match deterministic recomputation" in error for error in errors)


def test_validate_document_rejects_dimensionally_invalid_money_multiplication():
    document = _load_example()
    document["calculations"][0]["inputs"][1]["unit"] = "KRW"

    errors = validator.validate_document(document, _schema())

    assert any("dimensionally invalid" in error for error in errors)


def test_validate_document_recomputes_large_decimals_exactly():
    document = _load_example()
    _clear_model_review_flags(document)
    calculation = document["calculations"][0]
    left = 123456789012345678901234567890
    right = 9
    expected = left * right
    calculation["inputs"][0]["value"] = str(left)
    calculation["inputs"][1]["value"] = str(right)
    calculation["result"] = expected
    calculation["status"] = "complete"
    document["final_assessment"]["outcome"] = "payable"
    document["final_assessment"]["amount"] = {
        "value": expected,
        "currency": "KRW",
    }
    document["final_assessment"]["net_calculation_ref"] = "C1"

    errors = validator.validate_document(document, _schema())

    assert errors == []


def test_validate_document_rejects_negative_exact_result_hidden_by_truncation():
    document = _load_example()
    calculation = document["calculations"][0]
    calculation["operation"] = "identity"
    calculation["inputs"] = [calculation["inputs"][0]]
    calculation["inputs"][0]["value"] = "-0.4"
    calculation["rounding_rule"] = {"mode": "truncate", "unit": 1}
    calculation["result"] = 0
    calculation["status"] = "complete"

    errors = validator.validate_document(document, _schema())

    assert any("negative before rounding" in error for error in errors)


def test_validate_document_requires_none_rounding_result_to_match_unit():
    document = _load_example()
    calculation = document["calculations"][0]
    calculation["operation"] = "identity"
    calculation["inputs"] = [calculation["inputs"][0]]
    calculation["inputs"][0]["value"] = "15"
    calculation["rounding_rule"] = {"mode": "none", "unit": 10}
    calculation["result"] = 15
    calculation["status"] = "complete"

    errors = validator.validate_document(document, _schema())

    assert any("multiple of the declared unit" in error for error in errors)


def test_validate_document_rejects_payable_with_relevant_provisional_calculation():
    document = _load_example()
    complete = document["calculations"][0]
    complete["result"] = 10000000
    complete["status"] = "complete"
    provisional = json.loads(json.dumps(complete))
    provisional["calculation_id"] = "C2"
    provisional["status"] = "provisional"
    provisional["result"] = None
    document["calculations"].append(provisional)
    document["final_assessment"]["outcome"] = "payable"
    document["final_assessment"]["amount"] = {
        "value": 10000000,
        "currency": "KRW",
    }

    errors = validator.validate_document(document, _schema())

    assert any("every declared calculation to be complete" in error for error in errors)


def test_validate_document_rejects_complete_calculation_with_provisional_parent():
    document = _load_example()
    parent = document["calculations"][0]
    parent["status"] = "provisional"
    child = json.loads(json.dumps(parent))
    child.update(
        {
            "calculation_id": "C2",
            "operation": "identity",
            "inputs": [
                {
                    "input_type": "calculation_ref",
                    "calculation_ref": "C1",
                }
            ],
            "status": "complete",
        }
    )
    document["calculations"].append(child)
    document["status"] = "review_required"
    document["final_assessment"].update(
        {
            "outcome": "human_review_required",
            "amount": None,
            "net_calculation_ref": None,
        }
    )

    errors = validator.validate_document(document, _schema())

    assert any("complete calculation must reference only complete calculations" in error for error in errors)


def test_validate_document_allows_provisional_calculation_with_provisional_parent():
    document = _load_example()
    parent = document["calculations"][0]
    parent["status"] = "provisional"
    child = json.loads(json.dumps(parent))
    child.update(
        {
            "calculation_id": "C2",
            "operation": "identity",
            "inputs": [
                {
                    "label": "provisional parent",
                    "input_type": "calculation_ref",
                    "calculation_ref": "C1",
                }
            ],
            "status": "provisional",
        }
    )
    document["calculations"].append(child)

    errors = validator.validate_document(document, _schema())

    assert not any("unknown or invalid calculation_ref" in error for error in errors)
    assert not any(
        "complete calculation must reference only complete calculations" in error
        for error in errors
    )


def test_validate_document_requires_known_denial_basis_for_not_payable():
    document = _load_example()
    calculation = document["calculations"][0]
    calculation["result"] = 10000000
    calculation["status"] = "complete"
    document["final_assessment"]["outcome"] = "not_payable"
    document["final_assessment"]["amount"] = {"value": 0, "currency": "KRW"}

    errors = validator.validate_document(document, _schema())
    assert any("denial_basis_issue_refs" in error for error in errors)

    document["final_assessment"]["denial_basis_issue_refs"] = ["I999"]
    errors = validator.validate_document(document, _schema())
    assert any("unknown denial basis issue I999" in error for error in errors)

    document["final_assessment"]["denial_basis_issue_refs"] = ["I1"]
    errors = validator.validate_document(document, _schema())
    assert any("does not deny payment" in error for error in errors)

    document["reasoning_issues"][0]["disposition"] = "not_supported"
    document["reasoning_issues"][0]["outcome_effect"] = "denies_payment"
    errors = validator.validate_document(document, _schema())
    assert not any("denial_basis_issue_refs" in error for error in errors)

    document["reasoning_issues"][0]["disposition"] = "supported"
    errors = validator.validate_document(document, _schema())
    assert not any("denial_basis_issue_refs" in error for error in errors)


def test_validate_document_rejects_payable_outcome_without_amount():
    document = _load_example()
    document["final_assessment"]["outcome"] = "payable"
    document["final_assessment"]["amount"] = None

    errors = validator.validate_document(document, _schema())

    assert any("payable outcome requires an exact KRW amount" in error for error in errors)


def test_validate_document_requires_explicit_net_calculation_reference():
    document = _load_example()
    calculation = document["calculations"][0]
    calculation["status"] = "complete"
    document["final_assessment"]["outcome"] = "payable"
    document["final_assessment"]["amount"] = {
        "value": calculation["result"],
        "currency": "KRW",
    }

    errors = validator.validate_document(document, _schema())

    assert any("net_calculation_ref" in error for error in errors)


def test_validate_document_accepts_typed_calculation_dependency():
    document = _load_example()
    _clear_model_review_flags(document)
    gross = document["calculations"][0]
    gross["status"] = "complete"
    for item in gross["inputs"]:
        item["input_type"] = "literal"
    net = {
        "calculation_id": "C2",
        "category": "benefit_amount",
        "label": "net payable",
        "operation": "identity",
        "inputs": [
            {
                "label": "gross benefit",
                "input_type": "calculation_ref",
                "calculation_ref": "C1",
            }
        ],
        "result": gross["result"],
        "currency": "KRW",
        "rounding_rule": {"mode": "none", "unit": 1},
        "status": "complete",
    }
    document["calculations"].append(net)
    document["final_assessment"].update(
        {
            "outcome": "payable",
            "amount": {"value": net["result"], "currency": "KRW"},
            "net_calculation_ref": "C2",
        }
    )

    assert validator.validate_document(document, _schema()) == []


def test_validate_document_rejects_unlinked_complete_calculation():
    document = _load_example()
    net = document["calculations"][0]
    net["status"] = "complete"
    document["calculations"].append(
        {
            "calculation_id": "C2",
            "category": "benefit_amount",
            "label": "unlinked complete amount",
            "operation": "identity",
            "inputs": [
                {
                    "label": "standalone amount",
                    "input_type": "literal",
                    "value": "1",
                    "unit": "KRW",
                    "evidence_refs": ["E1"],
                }
            ],
            "result": 1,
            "currency": "KRW",
            "rounding_rule": {"mode": "none", "unit": 1},
            "status": "complete",
        }
    )
    document["final_assessment"].update(
        {
            "outcome": "payable",
            "amount": {"value": net["result"], "currency": "KRW"},
            "net_calculation_ref": "C1",
        }
    )

    errors = validator.validate_document(document, _schema())

    assert any("net calculation graph" in error for error in errors)


def test_validate_document_rejects_zero_value_payable_outcome():
    document = _load_example()
    calculation = document["calculations"][0]
    calculation["inputs"][0]["value"] = "0"
    calculation["result"] = 0
    calculation["status"] = "complete"
    document["final_assessment"]["outcome"] = "payable"
    document["final_assessment"]["amount"] = {"value": 0, "currency": "KRW"}

    errors = validator.validate_document(document, _schema())

    assert any("payable outcome requires a positive amount" in error for error in errors)


def test_validate_document_rejects_free_form_formula_text():
    document = _load_example()
    document["calculations"][0]["formula"] = "1 + 1 = 2"

    errors = validator.validate_document(document, _schema())

    assert any("formula" in error and "unexpected" in error for error in errors)


def test_validate_document_rejects_unresolved_outcome_with_final_amount():
    document = _load_example()
    document["final_assessment"]["amount"] = {
        "value": 10000000,
        "currency": "KRW",
    }

    errors = validator.validate_document(document, _schema())

    assert any("unresolved outcome must not assert a final amount" in error for error in errors)


def test_validate_document_rejects_outcome_irrelevant_reference_fields():
    document = _load_example()
    document["final_assessment"]["net_calculation_ref"] = "C999"
    document["final_assessment"]["denial_basis_issue_refs"] = ["I999"]

    errors = validator.validate_document(document, _schema())

    assert any("net_calculation_ref is allowed only for payable outcomes" in error for error in errors)
    assert any("denial_basis_issue_refs is allowed only for not_payable" in error for error in errors)


def test_validate_document_requires_all_full_mode_components():
    document = _load_example()
    document["document_profile"]["ordered_components"].remove("cover")

    errors = validator.validate_document(document, _schema())

    assert any("full mode requires all 12 canonical components" in error for error in errors)


def test_auto_self_injury_without_disability_claim_does_not_require_disability_issue():
    document = _load_example()
    document["document_profile"].update(
        {
            "family": "automobile_self_injury",
            "claim_mechanism": "automobile_policy_benefit",
        }
    )
    document["case_reference"].update(
        {"event_type": "accident", "disability_benefit_claimed": False}
    )
    document["reasoning_issues"] = [document["reasoning_issues"][0]]

    errors = validator.validate_document(document, _schema())

    assert errors == []


def test_auto_self_injury_disability_claim_requires_disability_issue():
    document = _load_example()
    document["document_profile"].update(
        {
            "family": "automobile_self_injury",
            "claim_mechanism": "automobile_policy_benefit",
        }
    )
    document["case_reference"].update(
        {"event_type": "accident", "disability_benefit_claimed": True}
    )
    document["reasoning_issues"] = [document["reasoning_issues"][0]]

    errors = validator.validate_document(document, _schema())

    assert any("disability benefit claim requires a disability issue" in error for error in errors)


def test_auto_self_injury_requires_benefit_amount_calculation():
    document = _load_example()
    document["document_profile"].update(
        {
            "family": "automobile_self_injury",
            "claim_mechanism": "automobile_policy_benefit",
        }
    )
    document["case_reference"].update(
        {"event_type": "accident", "disability_benefit_claimed": False}
    )
    document["reasoning_issues"] = [document["reasoning_issues"][0]]
    document["calculations"][0]["category"] = "other"

    errors = validator.validate_document(document, _schema())

    assert any("automobile self-injury requires a benefit amount calculation" in error for error in errors)


def test_validate_document_rejects_family_incompatible_event_type():
    document = _load_example()
    document["case_reference"]["event_type"] = "accident"

    errors = validator.validate_document(document, _schema())

    assert any("event type does not match document family" in error for error in errors)
