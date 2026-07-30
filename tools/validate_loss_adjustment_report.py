"""Validate structured loss-adjustment report content and cross-field gates."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator


def _walk(value: Any, path: tuple[str | int, ...] = ()) -> Iterator[tuple[tuple[str | int, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, index))


def _format_path(path: tuple[str | int, ...]) -> str:
    if not path:
        return "$"
    return "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in path
    )


def _duplicate_values(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _recompute_calculation(
    calculation: dict[str, Any], index: int, errors: list[str]
) -> Decimal | None:
    path = f"$.calculations[{index}]"
    operation = calculation.get("operation")
    inputs = calculation.get("inputs")
    if operation not in {"identity", "sum", "subtract", "multiply", "divide"}:
        return None
    if not isinstance(inputs, list):
        return None
    expected_counts = {
        "identity": (1, 1),
        "sum": (1, None),
        "subtract": (2, None),
        "multiply": (2, None),
        "divide": (2, None),
    }
    minimum, maximum = expected_counts[operation]
    if len(inputs) < minimum or (maximum is not None and len(inputs) > maximum):
        errors.append(f"{path}.inputs: invalid operand count for {operation}")
        return None

    operands: list[Fraction] = []
    try:
        for item in inputs:
            value = item.get("value") if isinstance(item, dict) else None
            if not isinstance(value, str):
                return None
            operand = Decimal(value)
            if not operand.is_finite():
                errors.append(f"{path}.inputs: operands must be finite decimals")
                return None
            operands.append(Fraction(operand))
    except InvalidOperation:
        return None

    try:
        if operation == "identity":
            computed = operands[0]
        elif operation == "sum":
            computed = sum(operands, Fraction(0))
        elif operation == "subtract":
            computed = operands[0]
            for operand in operands[1:]:
                computed -= operand
        elif operation == "multiply":
            computed = Fraction(1)
            for operand in operands:
                computed *= operand
        else:
            computed = operands[0]
            for operand in operands[1:]:
                if operand == 0:
                    errors.append(f"{path}.inputs: division by zero")
                    return None
                computed /= operand
    except InvalidOperation:
        errors.append(f"{path}.inputs: arithmetic operation is invalid")
        return None

    if computed < 0:
        errors.append(f"{path}.result: exact KRW result cannot be negative before rounding")
        return None

    rounding_rule = calculation.get("rounding_rule")
    if not isinstance(rounding_rule, dict):
        return None
    mode = rounding_rule.get("mode")
    unit_value = rounding_rule.get("unit")
    if not isinstance(unit_value, int) or isinstance(unit_value, bool) or unit_value < 1:
        return None
    unit = Fraction(unit_value)
    if mode == "none":
        if computed.denominator != 1:
            errors.append(
                f"{path}.rounding_rule: non-integral KRW result requires explicit rounding"
            )
            return None
        recomputed = computed
    elif mode == "truncate":
        recomputed = Fraction(int(computed / unit) * unit_value)
    elif mode == "half_up":
        scaled = computed / unit
        sign = -1 if scaled < 0 else 1
        quotient, remainder = divmod(abs(scaled.numerator), scaled.denominator)
        if remainder * 2 >= scaled.denominator:
            quotient += 1
        recomputed = Fraction(sign * quotient * unit_value)
    else:
        return None

    if recomputed < 0:
        errors.append(f"{path}.result: recomputed KRW result cannot be negative")
        return None
    declared = calculation.get("result")
    if declared is None:
        if calculation.get("status") == "complete":
            errors.append(f"{path}.result: complete calculation requires a result")
        return None
    if not isinstance(declared, int) or isinstance(declared, bool):
        return None
    if Fraction(declared) != recomputed:
        errors.append(
            f"{path}.result: result does not match deterministic recomputation"
        )
        return None
    return Decimal(recomputed.numerator)


def validate_document(document: dict[str, Any], schema_path: Path) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = [
        f"{_format_path(tuple(error.absolute_path))}: {error.message}"
        for error in sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda item: list(item.absolute_path),
        )
    ]

    evidence = document.get("evidence_registry", [])
    evidence_ids = [item.get("evidence_id") for item in evidence if isinstance(item, dict)]
    evidence_ids = [item for item in evidence_ids if isinstance(item, str)]
    for duplicate in sorted(_duplicate_values(evidence_ids)):
        errors.append(f"$.evidence_registry: duplicate evidence_id {duplicate}")
    known_evidence = set(evidence_ids)
    evidence_availability = {
        item["evidence_id"]: item.get("available") is True
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }

    for path, value in _walk(document):
        if path and path[-1] == "evidence_refs" and isinstance(value, list):
            for evidence_ref in value:
                if isinstance(evidence_ref, str) and evidence_ref not in known_evidence:
                    errors.append(
                        f"{_format_path(path)}: unknown evidence reference {evidence_ref}"
                    )
                elif (
                    isinstance(evidence_ref, str)
                    and evidence_availability.get(evidence_ref) is False
                ):
                    errors.append(
                        f"{_format_path(path)}: references unavailable evidence {evidence_ref}"
                    )

    for collection_name, id_name in (
        ("reasoning_issues", "issue_id"),
        ("calculations", "calculation_id"),
    ):
        collection = document.get(collection_name, [])
        identifiers: list[str] = []
        for item in collection:
            if not isinstance(item, dict):
                continue
            identifier = item.get(id_name)
            if isinstance(identifier, str):
                identifiers.append(identifier)
        for duplicate in sorted(_duplicate_values(identifiers)):
            errors.append(f"$.{collection_name}: duplicate {id_name} {duplicate}")
    known_issue_ids = {
        issue.get("issue_id")
        for issue in document.get("reasoning_issues", [])
        if isinstance(issue, dict) and isinstance(issue.get("issue_id"), str)
    }

    components = document.get("document_profile", {}).get("ordered_components", [])
    if isinstance(components, list):
        component_schema = schema["$defs"]["documentProfile"]["properties"][
            "ordered_components"
        ]
        canonical_order = component_schema["items"]["enum"]
        order = {component: index for index, component in enumerate(canonical_order)}
        known_components = [component for component in components if component in order]
        if known_components != sorted(known_components, key=order.__getitem__):
            errors.append(
                "$.document_profile.ordered_components: components are not in canonical order"
            )
        if (
            document.get("document_profile", {}).get("mode") == "full"
            and components != canonical_order
        ):
            errors.append(
                "$.document_profile.ordered_components: full mode requires all 12 canonical components"
            )

    family = document.get("document_profile", {}).get("family")
    mechanism = document.get("document_profile", {}).get("claim_mechanism")
    mechanism_schema = schema["$defs"]["documentProfile"]["properties"][
        "claim_mechanism"
    ]
    allowed_mechanisms = mechanism_schema["x-family-mechanisms"].get(family, [])
    if mechanism not in allowed_mechanisms:
        errors.append(
            "$.document_profile.claim_mechanism: mechanism does not match document family"
        )
    event_type = document.get("case_reference", {}).get("event_type")
    family_event_types = {
        "automobile_compensation": {"accident", "mixed"},
        "automobile_self_injury": {"accident", "mixed"},
        "personal_accident_benefit": {"accident", "mixed"},
        "liability_damages": {"accident", "mixed", "other"},
        "disease_benefit": {"disease", "mixed"},
        "other_review_required": {"mixed", "other"},
    }
    if event_type not in family_event_types.get(family, set()):
        errors.append(
            "$.case_reference.event_type: event type does not match document family"
        )

    if family == "automobile_self_injury":
        issue_kinds = {
            issue.get("issue_kind")
            for issue in document.get("reasoning_issues", [])
            if isinstance(issue, dict)
        }
        if (
            document.get("case_reference", {}).get("disability_benefit_claimed")
            is True
            and "disability" not in issue_kinds
        ):
            errors.append(
                "$.reasoning_issues: disability benefit claim requires a disability issue"
            )
        if not any(
            isinstance(calculation, dict)
            and calculation.get("category") == "benefit_amount"
            for calculation in document.get("calculations", [])
        ):
            errors.append(
                "$.calculations: automobile self-injury requires a benefit amount calculation"
            )
    calculations = document.get("calculations", [])
    recomputed_results: dict[str, Decimal] = {}
    for index, calculation in enumerate(calculations):
        if not isinstance(calculation, dict):
            continue
        recomputed = _recompute_calculation(calculation, index, errors)
        calculation_id = calculation.get("calculation_id")
        if recomputed is not None and isinstance(calculation_id, str):
            recomputed_results[calculation_id] = recomputed

    final_assessment = document.get("final_assessment", {})
    final_amount = final_assessment.get("amount")
    outcome = final_assessment.get("outcome")
    if outcome in {"payable", "partially_payable"} and not (
        isinstance(final_amount, dict)
        and isinstance(final_amount.get("value"), int)
        and not isinstance(final_amount.get("value"), bool)
    ):
        errors.append(
            "$.final_assessment.amount: payable outcome requires an exact KRW amount"
        )
    elif (
        outcome in {"payable", "partially_payable"}
        and isinstance(final_amount, dict)
        and isinstance(final_amount.get("value"), int)
        and final_amount["value"] <= 0
    ):
        errors.append(
            "$.final_assessment.amount.value: payable outcome requires a positive amount"
        )
    if outcome in {"undetermined", "human_review_required"} and final_amount is not None:
        errors.append(
            "$.final_assessment.amount: unresolved outcome must not assert a final amount"
        )
    if outcome == "not_payable" and not (
        isinstance(final_amount, dict) and final_amount.get("value") == 0
    ):
        errors.append(
            "$.final_assessment.amount: not_payable outcome requires a zero KRW amount"
        )
    denial_basis_issue_refs = final_assessment.get("denial_basis_issue_refs")
    if outcome == "not_payable":
        if not isinstance(denial_basis_issue_refs, list) or not denial_basis_issue_refs:
            errors.append(
                "$.final_assessment.denial_basis_issue_refs: not_payable outcome requires at least one denying issue"
            )
        else:
            for issue_ref in denial_basis_issue_refs:
                if isinstance(issue_ref, str) and issue_ref not in known_issue_ids:
                    errors.append(
                        f"$.final_assessment.denial_basis_issue_refs: unknown denial basis issue {issue_ref}"
                    )

    category = (
        "benefit_amount"
        if family
        in {
            "automobile_self_injury",
            "personal_accident_benefit",
            "disease_benefit",
        }
        else "total_damages"
        if family in {"automobile_compensation", "liability_damages"}
        else None
    )
    if category and isinstance(final_amount, dict) and outcome in {
        "payable",
        "partially_payable",
    }:
        relevant_calculations = [
            calculation
            for calculation in calculations
            if isinstance(calculation, dict)
            and calculation.get("category") == category
        ]
        if any(
            calculation.get("status") != "complete"
            or calculation.get("calculation_id") not in recomputed_results
            for calculation in relevant_calculations
        ):
            errors.append(
                "$.calculations: payable outcome requires all relevant typed calculations to be complete"
            )
        relevant_results: list[Decimal] = []
        for calculation in relevant_calculations:
            calculation_id = calculation.get("calculation_id")
            if (
                calculation.get("status") == "complete"
                and calculation_id in recomputed_results
            ):
                relevant_results.append(recomputed_results[calculation_id])
        expected_amount: int | None = None
        if category == "benefit_amount" and relevant_results:
            expected_amount = sum(int(result) for result in relevant_results)
        elif category == "total_damages" and len(relevant_results) == 1:
            expected_amount = int(relevant_results[0])
        actual_amount = final_amount.get("value")
        if expected_amount is None:
            errors.append(
                "$.calculations: payable outcome requires recomputed complete typed calculations"
            )
        elif isinstance(actual_amount, int) and actual_amount != expected_amount:
            errors.append(
                "$.final_assessment.amount.value: amount does not reconcile with the typed calculation total"
            )

    gates = document.get("review_gates", {})
    if (
        gates.get("finalization") == "approved"
        or "human_approval_records" in gates
    ):
        errors.append(
            "$.review_gates: model-authored document cannot claim final approval and cannot be approved by this authoring validator; trusted release attestation belongs to a separate human-controlled workflow"
        )
    calculation_review = any(
        isinstance(calculation, dict) and calculation.get("status") != "complete"
        for calculation in calculations
    )
    if calculation_review and gates.get("calculation") == "passed":
        errors.append(
            "$.review_gates.calculation: provisional calculation cannot pass the calculation gate"
        )

    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(
            "loss-adjustment-format-study/analysis/loss-adjustment-report.schema.json"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    document = json.loads(args.document.read_text(encoding="utf-8"))
    errors = validate_document(document, args.schema)
    result = {"valid": not errors, "errors": errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
