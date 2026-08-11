"""Case classification must select an evidence-backed report profile.

The broad coverage/loss axes are not enough to choose a report form.  A new
classification records the family, claim mechanism, presentation mode, and
support level that the draft stage will consume.  Historical results without
this additive field remain readable.
"""

import copy
import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance
from validate_loss_adjustment_report import validate_document


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_NAME = "case_type_result.schema.json"
TEMPLATE_REGISTRY = ROOT / "templates" / "registry.json"
FORMAT_SCHEMA = (
    ROOT
    / "loss-adjustment-format-study"
    / "analysis"
    / "loss-adjustment-report.schema.json"
)
BASE_EXAMPLE = (
    ROOT
    / "loss-adjustment-format-study"
    / "analysis"
    / "examples"
    / "example-disease-benefit.json"
)
CONFORMANCE_MATRIX = (
    ROOT
    / "loss-adjustment-format-study"
    / "analysis"
    / "examples"
    / "report-profile-conformance.json"
)


def _validate(instance: dict) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, SCHEMA_NAME, schemas, registry)


def _result(**overrides) -> dict:
    result = {
        "case_id": "CASE_009",
        "component": "claim-analysis",
        "status": "success",
        "case_type": "배상책임",
        "coverage_basis": "배상책임",
        "loss_type": "후유장해",
        "case_type_source": "inferred",
        "template_id": "배상책임_후유장해형",
        "report_profile": {
            "format_contract_version": "loss_adjustment_report.v1",
            "family": "liability_damages",
            "claim_mechanism": "insured_liability",
            "mode": "full",
            "support_status": "supported",
        },
        "confidence": 0.9,
        "evidence_references": [{"quote": "시설소유자배상책임"}],
        "review_required": False,
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize(
    ("template_id", "coverage_basis", "loss_type", "profile"),
    [
        (
            "자동차보험_대인배상_간이형",
            "자동차보험",
            "후유장해",
            {
                "format_contract_version": "loss_adjustment_report.v1",
                "family": "automobile_compensation",
                "claim_mechanism": "statutory_or_policy_auto_compensation",
                "mode": "compact",
                "support_status": "supported",
            },
        ),
        (
            "개인보험_후유장해형",
            "개인보험",
            "후유장해",
            {
                "format_contract_version": "loss_adjustment_report.v1",
                "family": "personal_accident_benefit",
                "claim_mechanism": "personal_accident_policy_benefit",
                "mode": "full",
                "support_status": "supported",
            },
        ),
        (
            "진단수술비형",
            "개인보험",
            "진단·수술비",
            {
                "format_contract_version": "loss_adjustment_report.v1",
                "family": "disease_benefit",
                "claim_mechanism": "disease_policy_benefit",
                "mode": "full",
                "support_status": "supported",
            },
        ),
    ],
)
def test_supported_profile_selects_matching_template(
    template_id: str, coverage_basis: str, loss_type: str, profile: dict
):
    assert _validate(
        _result(
            template_id=template_id,
            coverage_basis=coverage_basis,
            loss_type=loss_type,
            report_profile=profile,
        )
    ) == []


def test_profile_cannot_select_a_template_for_another_family():
    result = _result(template_id="진단수술비형")

    errors = _validate(result)

    assert any("does not match report_profile" in error for error in errors)


def test_unsupported_indemnity_case_fails_closed_without_a_template():
    result = _result(
        case_type="실손",
        coverage_basis="개인보험",
        loss_type="실손",
        template_id=None,
        report_profile={
            "format_contract_version": "loss_adjustment_report.v1",
            "family": "other_review_required",
            "claim_mechanism": "other_review_required",
            "mode": "full",
            "support_status": "unsupported",
        },
        review_required=True,
        reviewer_role="손해사정사",
    )

    assert _validate(result) == []


def test_unsupported_case_cannot_borrow_the_closest_template():
    result = _result(
        case_type="실손",
        coverage_basis="개인보험",
        loss_type="실손",
        report_profile={
            "format_contract_version": "loss_adjustment_report.v1",
            "family": "other_review_required",
            "claim_mechanism": "other_review_required",
            "mode": "full",
            "support_status": "unsupported",
        },
        template_id="진단수술비형",
        review_required=True,
        reviewer_role="손해사정사",
    )

    assert _validate(result) != []


def test_provisional_self_injury_profile_requires_review():
    result = _result(
        coverage_basis="자동차보험",
        template_id="자동차보험_자기신체사고형",
        report_profile={
            "format_contract_version": "loss_adjustment_report.v1",
            "family": "automobile_self_injury",
            "claim_mechanism": "automobile_policy_benefit",
            "mode": "full",
            "support_status": "provisional",
        },
    )

    assert _validate(result) != []

    result.update(review_required=True, reviewer_role="손해사정사")
    assert _validate(result) == []


def test_every_draft_template_declares_its_report_profile_contract():
    templates = json.loads(TEMPLATE_REGISTRY.read_text(encoding="utf-8"))["templates"]
    draft_templates = {
        key: value for key, value in templates.items() if key != "screening_report"
    }

    assert draft_templates
    for key, template in draft_templates.items():
        assert template["report_families"], key
        assert template["claim_mechanisms"], key
        assert template["mode"] in {"full", "compact"}, key
        assert template["support_status"] in {"supported", "provisional"}, key
        assert len(template["render_headings"]) == len(template["heading_patterns"]), key
        assert len(template["structured_section_groups"]) == len(
            template["heading_patterns"]
        ), key


def _conformance_scenarios() -> list[dict]:
    return json.loads(CONFORMANCE_MATRIX.read_text(encoding="utf-8"))["scenarios"]


def test_conformance_matrix_covers_every_pipeline_family_and_mechanism():
    format_schema = json.loads(FORMAT_SCHEMA.read_text(encoding="utf-8"))
    mapping = format_schema["$defs"]["documentProfile"]["properties"][
        "claim_mechanism"
    ]["x-family-mechanisms"]
    expected = {
        (family, mechanism)
        for family, mechanisms in mapping.items()
        if family != "other_review_required"
        for mechanism in mechanisms
    }
    actual = {
        (scenario["family"], scenario["claim_mechanism"])
        for scenario in _conformance_scenarios()
    }

    assert actual == expected


def test_each_conformance_scenario_selects_a_compatible_registry_template():
    templates = json.loads(TEMPLATE_REGISTRY.read_text(encoding="utf-8"))["templates"]

    for scenario in _conformance_scenarios():
        template = templates[scenario["template_id"]]
        assert scenario["family"] in template["report_families"]
        assert scenario["claim_mechanism"] in template["claim_mechanisms"]
        assert scenario["mode"] == template["mode"]
        assert scenario["support_status"] == template["support_status"]

        result = _result(
            case_type=scenario["case_type"],
            coverage_basis=scenario["coverage_basis"],
            loss_type=scenario["loss_type"],
            template_id=scenario["template_id"],
            report_profile={
                "format_contract_version": "loss_adjustment_report.v1",
                "family": scenario["family"],
                "claim_mechanism": scenario["claim_mechanism"],
                "mode": scenario["mode"],
                "support_status": scenario["support_status"],
            },
            review_required=scenario["support_status"] == "provisional",
            reviewer_role=(
                "손해사정사"
                if scenario["support_status"] == "provisional"
                else None
            ),
        )
        if result["reviewer_role"] is None:
            del result["reviewer_role"]
        assert _validate(result) == [], scenario["scenario_id"]


def test_each_conformance_scenario_can_form_a_valid_structured_report():
    base = json.loads(BASE_EXAMPLE.read_text(encoding="utf-8"))

    for scenario in _conformance_scenarios():
        document = copy.deepcopy(base)
        profile = document["document_profile"]
        profile.update(
            family=scenario["family"],
            claim_mechanism=scenario["claim_mechanism"],
            mode=scenario["mode"],
        )
        if scenario["mode"] == "compact":
            profile["ordered_components"] = [
                "summary",
                "assignment_contract",
                "facts",
                "governing_basis",
                "analysis",
                "calculations",
                "conclusion",
                "evidence_index",
            ]
        document["case_reference"].update(
            case_id=f"CASE_{scenario['scenario_id'].upper()}",
            event_type=scenario["event_type"],
            disability_benefit_claimed=False,
        )
        required_issues = scenario["required_issue_kinds"]
        for index, issue in enumerate(document["reasoning_issues"]):
            issue["issue_kind"] = (
                required_issues[index] if index < len(required_issues) else "other"
            )
        document["calculations"][0]["category"] = scenario[
            "required_calculation_categories"
        ][0]

        assert validate_document(document, FORMAT_SCHEMA) == [], scenario[
            "scenario_id"
        ]
