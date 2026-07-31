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
    calculation: dict[str, Any],
    index: int,
    errors: list[str],
    prior_results: dict[str, Decimal],
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
    units: list[str] = []
    try:
        for item in inputs:
            input_type = item.get("input_type") if isinstance(item, dict) else None
            if input_type == "calculation_ref":
                calculation_ref = item.get("calculation_ref")
                if calculation_ref not in prior_results:
                    errors.append(
                        f"{path}.inputs: calculation reference must identify an earlier complete calculation"
                    )
                    return None
                operands.append(Fraction(prior_results[calculation_ref]))
                units.append("KRW")
                continue
            if input_type != "literal":
                return None
            value = item.get("value") if isinstance(item, dict) else None
            unit = item.get("unit") if isinstance(item, dict) else None
            if not isinstance(value, str):
                return None
            if unit not in {"KRW", "ratio"}:
                errors.append(f"{path}.inputs: operand unit must be KRW or ratio")
                return None
            operand = Decimal(value)
            if not operand.is_finite():
                errors.append(f"{path}.inputs: operands must be finite decimals")
                return None
            operands.append(Fraction(operand))
            units.append(unit)
    except InvalidOperation:
        return None

    dimensionally_valid = (
        (operation in {"identity", "sum", "subtract"} and set(units) == {"KRW"})
        or (
            operation == "multiply"
            and units.count("KRW") == 1
            and units.count("ratio") == len(units) - 1
        )
        or (
            operation == "divide"
            and units[0] == "KRW"
            and all(unit == "ratio" for unit in units[1:])
        )
    )
    if not dimensionally_valid:
        errors.append(f"{path}.inputs: operation is dimensionally invalid for a KRW result")
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
        if computed.numerator % unit_value != 0:
            errors.append(
                f"{path}.rounding_rule: result must be a multiple of the declared unit"
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
    known_issues = {
        issue["issue_id"]: issue
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
    calculation_dependencies: dict[str, set[str]] = {}
    calculation_statuses: dict[str, str] = {}
    for index, calculation in enumerate(calculations):
        if not isinstance(calculation, dict):
            continue
        if calculation.get("status") == "complete":
            for input_index, item in enumerate(calculation.get("inputs", [])):
                if (
                    isinstance(item, dict)
                    and item.get("input_type") == "calculation_ref"
                    and isinstance(
                        calculation_ref := item.get("calculation_ref"), str
                    )
                    and calculation_statuses.get(calculation_ref) != "complete"
                ):
                    errors.append(
                        f"$.calculations[{index}].inputs[{input_index}]: complete calculation must reference only complete calculations"
                    )
        recomputed = _recompute_calculation(
            calculation, index, errors, recomputed_results
        )
        calculation_id = calculation.get("calculation_id")
        if isinstance(calculation_id, str):
            status = calculation.get("status")
            if isinstance(status, str):
                calculation_statuses[calculation_id] = status
            calculation_dependencies[calculation_id] = {
                item["calculation_ref"]
                for item in calculation.get("inputs", [])
                if isinstance(item, dict)
                and item.get("input_type") == "calculation_ref"
                and isinstance(item.get("calculation_ref"), str)
            }
            if recomputed is not None:
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
    net_calculation_ref = final_assessment.get("net_calculation_ref")
    if outcome in {"payable", "partially_payable"}:
        if not isinstance(net_calculation_ref, str):
            errors.append(
                "$.final_assessment.net_calculation_ref: payable outcome requires an explicit net calculation reference"
            )
        elif net_calculation_ref not in recomputed_results:
            errors.append(
                "$.final_assessment.net_calculation_ref: net calculation must reference a complete recomputed calculation"
            )
        elif (
            isinstance(final_amount, dict)
            and isinstance(final_amount.get("value"), int)
            and final_amount["value"] != int(recomputed_results[net_calculation_ref])
        ):
            errors.append(
                "$.final_assessment.amount.value: amount does not reconcile with net_calculation_ref"
            )
        if isinstance(net_calculation_ref, str) and net_calculation_ref in recomputed_results:
            reachable: set[str] = set()
            pending = [net_calculation_ref]
            while pending:
                calculation_id = pending.pop()
                if calculation_id in reachable:
                    continue
                reachable.add(calculation_id)
                pending.extend(calculation_dependencies.get(calculation_id, set()))
            declared_ids = set(calculation_dependencies)
            if reachable != declared_ids:
                errors.append(
                    "$.calculations: every calculation must be reachable from the net calculation graph"
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
                elif isinstance(issue_ref, str):
                    issue = known_issues[issue_ref]
                    if not (
                        issue.get("disposition")
                        in {"supported", "not_supported", "partially_supported"}
                        and issue.get("outcome_effect") == "denies_payment"
                    ):
                        errors.append(
                            f"$.final_assessment.denial_basis_issue_refs: issue {issue_ref} does not deny payment"
                        )

    unresolved_issue_ids = [
        issue_id
        for issue_id, issue in known_issues.items()
        if issue.get("disposition")
        in {"undetermined", "human_review_required"}
    ]
    if unresolved_issue_ids and outcome not in {
        "undetermined",
        "human_review_required",
    }:
        errors.append(
            "$.final_assessment.outcome: unresolved issue requires an unresolved final outcome "
            f"({', '.join(sorted(unresolved_issue_ids))})"
        )

    denying_issue_ids = [
        issue_id
        for issue_id, issue in known_issues.items()
        if issue.get("disposition")
        in {"supported", "not_supported", "partially_supported"}
        and issue.get("outcome_effect") == "denies_payment"
    ]
    if denying_issue_ids and outcome != "not_payable":
        errors.append(
            "$.final_assessment.outcome: resolved denying issue requires a "
            f"not_payable outcome ({', '.join(denying_issue_ids)})"
        )

    net_ref = final_assessment.get("net_calculation_ref")
    if net_ref is not None and outcome not in {"payable", "partially_payable"}:
        errors.append(
            "$.final_assessment.net_calculation_ref: net_calculation_ref is "
            "allowed only for payable outcomes"
        )
    denial_refs = final_assessment.get("denial_basis_issue_refs")
    if denial_refs is not None and outcome != "not_payable":
        errors.append(
            "$.final_assessment.denial_basis_issue_refs: "
            "denial_basis_issue_refs is allowed only for not_payable"
        )

    if isinstance(final_amount, dict) and outcome in {
        "payable",
        "partially_payable",
    }:
        if any(
            calculation.get("status") != "complete"
            or calculation.get("calculation_id") not in recomputed_results
            for calculation in calculations
            if isinstance(calculation, dict)
        ):
            errors.append(
                "$.calculations: payable outcome requires every declared calculation to be complete and recomputable"
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
    open_professional_judgment = any(
        isinstance(value, dict)
        and (
            value.get("human_review_required") is True
            or value.get("disposition") == "human_review_required"
            or value.get("support_type") == "professional_judgment"
            or (
                isinstance(value.get("unresolved_items"), list)
                and bool(value["unresolved_items"])
            )
        )
        for _, value in _walk(document)
    )
    if open_professional_judgment and any(
        gates.get(name) == "passed" for name in ("medical", "legal")
    ):
        errors.append(
            "$.review_gates: professional review gate cannot pass while professional judgment or unresolved items remain open"
        )
    if open_professional_judgment and any(
        gates.get(name) == "not_applicable" for name in ("medical", "legal")
    ):
        errors.append(
            "$.review_gates: professional review gate must remain open and cannot "
            "be not_applicable while professional judgment or unresolved items remain open"
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
