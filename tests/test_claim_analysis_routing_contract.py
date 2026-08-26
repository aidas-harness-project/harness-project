"""Synthetic contract tests for selective Claim Analysis routing v0.1.

These tests read only version-controlled schemas and configuration. They never
read source-cases, outputs, data, or ground truth.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _validation import load_registry, validate_instance
import dao
import medical_document_routing as routing
import run_checkpoint1 as checkpoint1


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = (
    ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
)


def _errors(instance: dict, schema_name: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _classification() -> dict:
    return {
        "case_id": "CASE_9001",
        "run_id": "RUN_20260819_1",
        "component": "document-pipeline",
        "status": "success",
        "document_id": "DOC_001",
        "predicted_document_type": "diagnosis_certificate",
        "confidence": 0.99,
        "evidence_references": [{"page": 1, "quote": "진 단 서"}],
        "review_required": False,
    }


def _resolved_medical_block() -> dict:
    return {
        "schema_version": "medical_document_classification.v0.1",
        "status": "deterministic_title",
        "kind": "diagnosis_certificate",
        "roles": ["diagnosis_source"],
        "candidates": [],
        "evidence_references": [{"page": 1, "quote": "진 단 서"}],
    }


def test_routing_registry_is_schema_valid_and_activated() -> None:
    """The shipped registry validates and is the ACTIVE lane.

    Inverted 2026-08-20 alongside the legacy spine's deletion. This previously
    asserted `behavior_enabled is False` and `activation is None`, describing a
    registry that had not been switched on yet; both became false when the lane
    was activated (approved 2026-08-20T09:15+09:00) and there is no longer any
    other lane for the flag to hand off to. Schema validity is the part worth
    keeping, so it stays and the activation assertions flip to match.
    """
    config = _config()
    assert _errors(config, "claim_analysis_routing_config.schema.json") == []
    assert config["behavior_enabled"] is True
    assert config["activation"] is not None


def test_registry_has_exactly_the_eight_accepted_medical_domains() -> None:
    config = _config()
    expected = {
        "event_timeline",
        "diagnosis",
        "diagnosis_basis",
        "treatment",
        "clinical_course_outcome",
        "prior_history_influences",
        "complications_new_problems",
        "disability",
    }
    domains = {row["code"]: row for row in config["domains"]}
    medical = {code for code, row in domains.items()
               if row.get("domain_kind", "medical") == "medical"}
    assert medical == expected, "the eight medical domains are fixed"
    legal = {code for code, row in domains.items()
             if row.get("domain_kind") == "legal_factual"}
    assert legal == {"liability_basis"}
    assert "effective_first_required_grade" not in domains["liability_basis"], (
        "a legal domain carries no medical grade ceiling")
    assert domains["event_timeline"]["effective_first_required_grade"] == "B"
    assert domains["complications_new_problems"]["effective_first_required_grade"] == "B"


def test_active_fields_obey_the_b_ceiling_except_named_operational_overrides() -> None:
    config = _config()
    allowed_override_basis = {"routing_override", "consistency_override"}
    for field in config["fields"]:
        if field["extraction_wave"] not in {"A", "B"}:
            continue
        # Medical fields only: the B ceiling is a clinical-priority rule, and
        # a `legal_factual` field carries `priority_grade` instead (added
        # 2026-08-21 -- 과실비율 has no medical grade to be ceilinged).
        if "medical_advisory_grade" not in field:
            assert field["priority_grade"] in {"A", "B", "C"}
            continue
        if field["medical_advisory_grade"] in {"A", "B"}:
            continue
        assert field["medical_advisory_grade"] == "C"
        assert field["extraction_wave"] == "B"
        assert field["activation_basis"] in allowed_override_basis

    deferred_ids = {
        field["field_id"]
        for field in config["fields"]
        if field.get("medical_advisory_grade") == "D"
    }
    assert deferred_ids == {"symptom_fixation_judgment", "final_disability_rate"}
    assert all(
        field["extraction_wave"] == "deferred"
        for field in config["fields"]
        if field["field_id"] in deferred_ids
    )


def test_registry_references_are_closed_and_cost_documents_are_presence_only() -> None:
    config = _config()
    kinds = {row["kind"] for row in config["document_kinds"]}
    domains = {row["code"] for row in config["domains"]}
    routes = {row["route_id"] for row in config["source_routes"]}

    assert len(kinds) == len(config["document_kinds"])
    assert len(routes) == len(config["source_routes"])
    for route in config["source_routes"]:
        assert {
            kind for group in route["priority_groups"] for kind in group
        } <= kinds
    for field in config["fields"]:
        assert field["domain_code"] in domains
        if field["source_route_id"] is not None:
            assert field["source_route_id"] in routes

    cost_kinds = {
        "medical_expense_receipt",
        "medical_expense_itemization",
        "pharmacy_payment_confirmation",
    }
    rows = {row["kind"]: row for row in config["document_kinds"]}
    for kind in cost_kinds:
        assert rows[kind]["claim_analysis_read_mode"] == "presence_only"
        assert rows[kind]["default_roles"] == ["required_document_presence_only"]
    assert not any(
        kind in cost_kinds
        for route in config["source_routes"]
        for group in route["priority_groups"]
        for kind in group
    )

    invalid = deepcopy(config)
    cost = next(
        row for row in invalid["document_kinds"]
        if row["kind"] == "medical_expense_receipt"
    )
    cost["claim_analysis_read_mode"] = "content"
    cost["default_roles"] = ["diagnosis_source"]
    assert _errors(invalid, "claim_analysis_routing_config.schema.json") != []


def test_routing_semantics_reject_active_d_grade_and_unnamed_c_grade() -> None:
    d_grade = _config()
    field = next(row for row in d_grade["fields"]
                 if row["field_id"] == "final_disability_rate")
    field.update({
        "extraction_wave": "B",
        "activation_basis": "routing_override",
        "source_route_id": "disability",
    })
    assert _errors(d_grade, "claim_analysis_routing_config.schema.json") != []

    c_grade = _config()
    field = next(row for row in c_grade["fields"]
                 if row["field_id"] == "accident_mechanism")
    field["activation_basis"] = "advisory_priority"
    assert _errors(c_grade, "claim_analysis_routing_config.schema.json") != []


def test_routing_semantics_reject_duplicate_ids_and_dangling_routes() -> None:
    duplicate = _config()
    copied = deepcopy(duplicate["source_routes"][0])
    copied["priority_groups"] = deepcopy(
        duplicate["source_routes"][1]["priority_groups"]
    )
    duplicate["source_routes"].append(copied)
    assert _errors(duplicate, "claim_analysis_routing_config.schema.json") != []

    dangling = _config()
    dangling["fields"][0]["source_route_id"] = "missing_route"
    assert _errors(dangling, "claim_analysis_routing_config.schema.json") != []


def test_four_case_types_are_independent_multi_label_checks() -> None:
    rules = _config()["case_type_rules"]
    assert {row["case_type"] for row in rules} == {
        "personal_insurance",
        "traffic_accident",
        "industrial_accident",
        "liability",
    }
    assert all(row["evaluation_mode"] == "independent_parallel" for row in rules)
    assert all(row["absence_means_false"] is False for row in rules)
    assert all(row["evidence_required"] is True for row in rules)


def test_critical_comparison_and_filing_status_cost_limits_are_fixed() -> None:
    config = _config()
    policy = config["extraction_policy"]
    assert policy["critical_field_additional_comparison_limit"] == 1
    assert policy["stop_on_first_trusted_value"] is True
    assert policy["missing_mention_is_conflict"] is False
    assert policy["direct_conflict_canonical_value"] is None
    assert config["filing_status_policy"] == {
        "intake_value_authoritative": True,
        "default_when_missing": "unknown",
        "search_when_missing": "opportunistic_in_already_read_documents",
        "extra_read_for_status": False,
    }


def test_legacy_classification_remains_valid_without_fine_grained_block() -> None:
    assert _errors(_classification(), "classification_result.schema.json") == []


def test_resolved_fine_grained_classification_requires_kind_roles_and_evidence() -> None:
    instance = _classification()
    instance["medical_classification"] = _resolved_medical_block()
    assert _errors(instance, "classification_result.schema.json") == []

    for field in ("kind", "roles", "evidence_references"):
        bad = deepcopy(instance)
        if field == "kind":
            bad["medical_classification"][field] = None
        else:
            bad["medical_classification"][field] = []
        assert _errors(bad, "classification_result.schema.json") != []


def test_ambiguous_fine_grained_classification_preserves_candidates() -> None:
    instance = _classification()
    instance["medical_classification"] = {
        "schema_version": "medical_document_classification.v0.1",
        "status": "ambiguous",
        "kind": None,
        "roles": [],
        "candidates": [
            {"kind": "progress_record", "confidence": 0.56},
            {"kind": "outpatient_record", "confidence": 0.44},
        ],
        "ambiguity_reason": "The printed title only states a generic record genre.",
        "evidence_references": [{"page": 1, "quote": "진료기록"}],
    }
    assert _errors(instance, "classification_result.schema.json") == []

    bad = deepcopy(instance)
    bad["medical_classification"]["candidates"] = []
    assert _errors(bad, "classification_result.schema.json") != []


def test_manifest_accepts_the_same_additive_medical_block() -> None:
    manifest = {
        "case_id": "CASE_9001",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "synthetic.pdf",
            "file_path": "data/raw/CASE_9001/DOC_001/synthetic.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "pages": 1,
            "ocr_status": "completed",
            "document_type": "diagnosis_certificate",
            "classification_confidence": 0.99,
            "medical_classification": _resolved_medical_block(),
        }],
    }
    assert _errors(manifest, "document_manifest.schema.json") == []


@pytest.mark.parametrize(("title", "expected"), [
    ("진 단 서", "diagnosis_certificate"),
    ("초 진 기 록 지", "initial_visit_record"),
    ("수 술 기 록", "surgery_procedure_record"),
    ("영상 판독지", "imaging_interpretation"),
    ("진료비 세부산정내역", "medical_expense_itemization"),
    ("약제비 납입확인서", "pharmacy_payment_confirmation"),
    ("후유장애 진단서(Mc Bride)", "disability_assessment"),
])
def test_printed_medical_form_title_maps_without_an_llm(title: str, expected: str) -> None:
    assert routing.medical_kind_from_title(title) == expected


@pytest.mark.parametrize("title", ["REPORT", "기록지", "판독지", "요약", "의무기록"])
def test_generic_medical_genre_title_stays_ambiguous_for_llm_fallback(title: str) -> None:
    assert routing.medical_kind_from_title(title) is None


def test_unsafe_or_out_of_scope_titles_do_not_inherit_a_medical_kind() -> None:
    assert routing.medical_kind_from_title("사망진단서") is None
    assert routing.medical_kind_from_title("routine검사") is None
    assert routing.medical_kind_from_title("응급실기록지") == "emergency_record"
    assert routing.medical_kind_from_title("상해진단서") == "diagnosis_certificate"


def test_model_medical_kind_uses_registry_roles_not_model_invented_roles() -> None:
    config = _config()
    block = routing.classification_from_model({
        "predicted_document_type": "medical_record",
        "medical_document_kind": "progress_record",
        "medical_kind_candidates": [],
        "quote": "경과기록",
    }, config)
    expected_roles = next(
        row["default_roles"] for row in config["document_kinds"]
        if row["kind"] == "progress_record"
    )
    assert block["status"] == "llm_classified"
    assert block["roles"] == expected_roles


def test_model_ambiguity_preserves_up_to_three_candidates() -> None:
    block = routing.classification_from_model({
        "predicted_document_type": "medical_record",
        "medical_document_kind": None,
        "medical_kind_candidates": [
            {"kind": "progress_record", "confidence": 0.55},
            {"kind": "outpatient_record", "confidence": 0.45},
        ],
        "medical_ambiguity_reason": "Generic chart title.",
        "quote": "진료기록",
    }, _config())
    assert block["status"] == "ambiguous"
    assert [row["kind"] for row in block["candidates"]] == [
        "progress_record", "outpatient_record"
    ]

    missing_confidence = {
        "predicted_document_type": "medical_record",
        "medical_document_kind": None,
        "medical_kind_candidates": [{"kind": "progress_record"}],
        "quote": "진료기록",
    }
    with pytest.raises(KeyError):
        routing.classification_from_model(missing_confidence, _config())


def test_deterministic_not_medical_path_never_fabricates_a_quote() -> None:
    without_title = routing.not_medical_classification()
    assert without_title["evidence_references"] == []
    with_title = routing.not_medical_classification(quote="무배당 보통약관")
    assert with_title["evidence_references"] == [
        {"page": 1, "quote": "무배당 보통약관"}
    ]


def test_enabled_classifier_adds_fine_kind_to_the_existing_single_call() -> None:
    config = _config()
    config["behavior_enabled"] = True
    response = {
        "predicted_document_type": "medical_record",
        "document_type_label": "경과기록",
        "confidence": 0.91,
        "quote": "경과기록",
        "medical_document_kind": "progress_record",
        "medical_kind_candidates": [],
        "medical_ambiguity_reason": None,
    }

    class FakeClassifier:
        def __init__(self) -> None:
            self.calls = []

        def classify_document(self, prompt: str, prompt_version: str):
            self.calls.append((prompt, prompt_version))
            return SimpleNamespace(
                text=json.dumps(response, ensure_ascii=False),
                metadata=lambda: {},
            )

    classifier = FakeClassifier()
    parsed = checkpoint1.classify_document(
        "경과기록", classifier=classifier, routing_config=config
    )
    assert parsed["medical_document_kind"] == "progress_record"
    assert len(classifier.calls) == 1
    assert classifier.calls[0][1] == checkpoint1.MEDICAL_CLASSIFICATION_PROMPT_VERSION
    assert "medical_document_kind" in classifier.calls[0][0]


def _canonical_observation(number: int, value: str, quote: str) -> dict:
    return {
        "observation_id": f"CAO_{number:04d}",
        "value_state": "asserted",
        "value": value,
        "source_document_kind": "diagnosis_certificate",
        "source_priority_rank": 1,
        "extraction_wave": "A",
        "evidence_references": [{
            "document_id": "DOC_001",
            "page": 1,
            "quote": quote,
            "start_char": 0,
            "end_char": len(quote),
        }],
    }


def _selective_result() -> dict:
    quote = "우측 요골 골절"
    evidence = {
        "document_id": "DOC_001", "page": 1, "quote": quote,
        "start_char": 0, "end_char": len(quote),
    }
    case_types = []
    for case_type in (
        "personal_insurance", "traffic_accident", "industrial_accident", "liability"
    ):
        supported = case_type == "personal_insurance"
        case_types.append({
            "case_type": case_type,
            "status": "applicable" if supported else "uncertain",
            "triggered_field_ids": ["injury_event_present"] if supported else [],
            "conflicting_field_ids": [],
            "filing_status": "unknown",
            "reason": "External injury is documented." if supported else "No decisive fact was found.",
            "evidence_references": [evidence] if supported else [],
        })
    return {
        "case_id": "CASE_9001",
        "run_id": "RUN_20260819_1",
        "component": "claim-analysis",
        "status": "success",
        "schema_version": "claim_analysis_result.v0.1",
        "config_version": "claim_analysis_routing.v0.1",
        "medical_projection_status": "not_configured",
        "claim_facts": [{
            "field_id": "primary_diagnosis",
            "domain_code": "diagnosis",
            "priority_grade": "A",
            "authority": "source_document_extraction",
            "resolution_status": "asserted",
            "selected_observation_ids": ["CAO_0001"],
            "observations": [_canonical_observation(1, "우측 요골 골절", "우측 요골 골절")],
            "conflict_candidate_ids": [],
            "stop_reason": "trusted_value_found",
        }],
        "case_type_assessment": case_types,
        "policy_links": [],
        "required_document_checklist": [{
            "document_kind": "diagnosis_certificate",
            "status": "available",
            "required_for_case_types": ["personal_insurance"],
            "document_ids": ["DOC_001"],
            "reason": "A fine-grained Stage 2 classification is present.",
        }],
        "conflict_candidates": [],
    }


def test_compact_selective_claim_analysis_result_validates() -> None:
    assert _errors(_selective_result(), "claim_analysis_result.schema.json") == []


def test_conflict_keeps_both_observations_and_forbids_a_selected_value() -> None:
    result = _selective_result()
    field = result["claim_facts"][0]
    field.update({
        "resolution_status": "conflict",
        "selected_observation_ids": [],
        "observations": [
            _canonical_observation(1, "우측 요골 골절", "우측 요골 골절"),
            _canonical_observation(2, "좌측 요골 골절", "좌측 요골 골절"),
        ],
        "conflict_candidate_ids": ["CAC_0001"],
        "stop_reason": "conflict_found",
        "resolution_reason": "The case sources directly disagree on laterality.",
    })
    result["conflict_candidates"] = [{
        "conflict_candidate_id": "CAC_0001",
        "field_id": "primary_diagnosis",
        "observation_ids": ["CAO_0001", "CAO_0002"],
        "reason": "The case sources directly disagree on laterality.",
        "consistency_status": "pending_consistency_check",
    }]
    assert _errors(result, "claim_analysis_result.schema.json") == []

    bad = deepcopy(result)
    bad["claim_facts"][0]["selected_observation_ids"] = ["CAO_0001"]
    assert _errors(bad, "claim_analysis_result.schema.json") != []


def test_claim_analysis_semantics_reject_unknown_assertion_duplicates_and_dangling_ids() -> None:
    unknown = _selective_result()
    field = unknown["claim_facts"][0]
    field.update({
        "resolution_status": "unavailable",
        "selected_observation_ids": [],
        "conflict_candidate_ids": [],
        "stop_reason": "sources_exhausted",
        "resolution_reason": "No trusted source was available.",
        "unavailable_reason": "not_mentioned",
    })
    # The retained observation still asserts a value, which an unavailable
    # field may not carry -- that mismatch is the defect this asserts on.
    assert _errors(unknown, "claim_analysis_result.schema.json") != []

    duplicate_types = _selective_result()
    duplicate_types["case_type_assessment"] = [
        deepcopy(duplicate_types["case_type_assessment"][0]) for _ in range(4)
    ]
    assert _errors(duplicate_types, "claim_analysis_result.schema.json") != []

    dangling = _selective_result()
    dangling["claim_facts"][0]["selected_observation_ids"] = ["CAO_9999"]
    assert _errors(dangling, "claim_analysis_result.schema.json") != []


def test_conflict_candidate_must_reference_observations_owned_by_its_field() -> None:
    result = _selective_result()
    result["conflict_candidates"] = [{
        "conflict_candidate_id": "CAC_0001",
        "field_id": "primary_diagnosis",
        "observation_ids": ["CAO_0001", "CAO_9999"],
        "reason": "Synthetic dangling observation.",
        "consistency_status": "pending_consistency_check",
    }]
    result["claim_facts"][0]["conflict_candidate_ids"] = ["CAC_0001"]
    assert _errors(result, "claim_analysis_result.schema.json") != []


def test_exact_evidence_verifier_checks_the_claimed_character_range() -> None:
    from claim_analysis_contracts import verify_exact_evidence_references

    result = _selective_result()
    assert verify_exact_evidence_references(
        result, lambda doc_id, page: "우측 요골 골절"
    ) == []
    errors = verify_exact_evidence_references(
        result, lambda doc_id, page: "좌측 요골 골절"
    )
    assert errors
    assert all("exactly match" in error for error in errors)


def test_dao_refuses_claim_analysis_result_with_a_false_exact_range(
    isolated_dao, make_args, monkeypatch, tmp_path
) -> None:
    result = _selective_result()
    data_file = tmp_path / "claim-analysis.json"
    data_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(dao, "_load_medical_revision", lambda case_id, sha: ({
        "case_id": case_id,
        "run_id": "RUN_20260819_1",
        "schema_version": "medical_variables.v0.1",
        "config_version": "medical_structuring.v0.1",
    }, None))
    monkeypatch.setattr(dao, "read_redacted_text_bundle_data", lambda case_id, doc_ids: {
        "case_id": case_id,
        "documents": [{
            "document_id": "DOC_001",
            "pages": [{"page": 1, "text": "좌측 요골 골절"}],
        }],
    })
    args = make_args(
        case_id="CASE_9001",
        run_id="RUN_20260819_1",
        filename="claim_analysis_result.json",
        data_file=str(data_file),
        schema_name="claim_analysis_result.schema.json",
    )

    assert dao.cmd_write_contract(args) == 1
    assert not (
        isolated_dao / "outputs" / "CASE_9001" / "claim_analysis_result.json"
    ).exists()


def test_dao_refuses_a_declared_medical_revision_context_that_cannot_resolve(
    isolated_dao, make_args, monkeypatch, tmp_path
) -> None:
    """A recorded context must be real. Declaring it is what invites the check.

    The lane may omit `medical_revision_context` entirely -- it claims no
    canonical authority. What it may not do is name a revision the case does
    not have, which would dress a source-grounded reading in canonical
    provenance.
    """
    result = _selective_result()
    result["medical_revision_context"] = {
        "sha256": "a" * 64,
        "run_id": "RUN_20260819_1",
        "schema_version": "medical_variables.v0.1",
        "config_version": "medical_structuring.v0.1",
    }
    data_file = tmp_path / "claim-analysis.json"
    data_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        dao, "_load_medical_revision",
        lambda case_id, sha: (None, "medical variable contract or revision not found"),
    )
    monkeypatch.setattr(
        dao, "read_redacted_text_bundle_data",
        lambda *args: pytest.fail("evidence must not be read before the context resolves"),
    )
    args = make_args(
        case_id="CASE_9001",
        run_id="RUN_20260819_1",
        filename="claim_analysis_result.json",
        data_file=str(data_file),
        schema_name="claim_analysis_result.schema.json",
    )

    assert dao.cmd_write_contract(args) == 1
    assert not (
        isolated_dao / "outputs" / "CASE_9001" / "claim_analysis_result.json"
    ).exists()


def test_dao_never_requires_a_medical_revision_for_the_selective_lane(
    isolated_dao, make_args, monkeypatch, tmp_path
) -> None:
    """No canonical revision on the case must not block the write.

    This is the separation the lane exists to have: a source-grounded result
    stands on its own citations. If the absence of a canonical revision refused
    the write, the lane could never run on a case that has no medical review --
    which is most of them.
    """
    result = _selective_result()
    assert "medical_revision_context" not in result
    data_file = tmp_path / "claim-analysis.json"
    data_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        dao, "_load_medical_revision",
        lambda case_id, sha: pytest.fail(
            "the selective lane must not consult the canonical revision"),
    )
    monkeypatch.setattr(
        dao, "read_redacted_text_bundle_data",
        lambda case_id, doc_ids: {"documents": [{
            "document_id": "DOC_001",
            "pages": [{"page": 1, "text": "우측 요골 골절"}],
        }]},
    )
    args = make_args(
        case_id="CASE_9001",
        run_id="RUN_20260819_1",
        filename="claim_analysis_result.json",
        data_file=str(data_file),
        schema_name="claim_analysis_result.schema.json",
    )

    assert dao.cmd_write_contract(args) == 0


def test_dao_rejects_a_projection_authority_without_consulting_any_revision(
    isolated_dao, make_args, monkeypatch, tmp_path
) -> None:
    """A projection label is refused ON ITS OWN TERMS, with no fall-through.

    The old path answered a mislabelled authority with a complaint about a
    missing medical revision -- an error message about the wrong thing. The
    label is the defect, so the label is what the refusal names.
    """
    result = _selective_result()
    result["claim_facts"][0]["authority"] = "medical_variables_projection"
    data_file = tmp_path / "claim-analysis.json"
    data_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        dao, "_load_medical_revision",
        lambda case_id, sha: pytest.fail(
            "a mislabelled authority must not fall through to the revision check"),
    )
    args = make_args(
        case_id="CASE_9001",
        run_id="RUN_20260819_1",
        filename="claim_analysis_result.json",
        data_file=str(data_file),
        schema_name="claim_analysis_result.schema.json",
    )

    assert dao.cmd_write_contract(args) == 1
    assert not (
        isolated_dao / "outputs" / "CASE_9001" / "claim_analysis_result.json"
    ).exists()


def test_internal_trace_is_separate_and_caps_comparisons_at_one() -> None:
    trace = {
        "case_id": "CASE_9001",
        "run_id": "RUN_20260819_1",
        "schema_version": "claim_analysis_trace.v0.1",
        "config_version": "claim_analysis_routing.v0.1",
        "documents": [{
            "document_id": "DOC_001",
            "medical_document_kind": "diagnosis_certificate",
            "disposition": "read",
            "wave": "A",
            "reason": "Highest-priority source for primary diagnosis.",
            "field_ids": ["primary_diagnosis"],
        }],
        "field_stops": [{
            "field_id": "primary_diagnosis",
            "stop_reason": "trusted_value_found",
            "documents_read": 1,
            "additional_comparisons": 1,
        }],
        "metrics": {
            "documents_considered": 1,
            "documents_read": 1,
            "documents_presence_only": 0,
            "a_stop_fields": 1,
            "b_fallback_fields": 0,
            "provider_calls": 1,
            "input_tokens": None,
            "output_tokens": None,
            "wall_time_seconds": None,
        },
    }
    assert _errors(trace, "claim_analysis_trace.schema.json") == []
    trace["field_stops"][0]["additional_comparisons"] = 2
    assert _errors(trace, "claim_analysis_trace.schema.json") != []


# ------------------------------- 기왕증 기여율: a printed row with no field --
# Korean disability certificates print a 전병력 block whose rows the catalogue
# had no receiver for. Read from CASE_7061 DOC_003 and CASE_7071 DOC_003
# (both McBride 후유장애진단서, 2026-08-26) -- the same four rows in the same
# order, so this is the form's standard block and not one case's quirk:
#
#   | 기왕증 | |
#   | 기왕증의 현재 장애에 대한 기여율 | |      <- no field received this
#   | 기존장애 및 장애율 | |
#   | 기왕증과 교통사고 상당 인과관계 유/무 | 무 |  <- nor this
#
# Contribution rate decides how much of a disability the insurer pays for, so
# a printed figure that reaches no field is a silent loss of the number the
# whole apportionment turns on.
#
# It is deliberately NOT the deferred D-grade `final_disability_rate`. That one
# is a JUDGMENT the adjuster makes and the PoC excludes (`scope.final_eligibility:
# out_of_scope`). These two only TRANSCRIBE what a doctor already wrote, which
# is exactly the line `documented_disability_rate` already sits on -- its own
# note says "Report the printed number; the pipeline does not calculate or
# endorse a final rate of its own".

def test_the_catalogue_receives_the_printed_contribution_rate() -> None:
    config = _config()
    by_id = {f["field_id"]: f for f in config["fields"]}
    field = by_id.get("documented_prior_condition_contribution_rate")
    assert field is not None, (
        "기왕증 기여율 is printed on the disability certificate and reaches no "
        "field, so the figure apportionment turns on is silently dropped")
    # Same shape as the precedent it mirrors.
    precedent = by_id["documented_disability_rate"]
    assert field["source_route_id"] == precedent["source_route_id"] == "disability"
    assert field["medical_advisory_grade"] == "B"
    assert field["extraction_wave"] == "B"
    assert field["domain_code"] == "prior_history_influences"


def test_the_catalogue_receives_the_printed_causation_verdict() -> None:
    config = _config()
    by_id = {f["field_id"]: f for f in config["fields"]}
    field = by_id.get("documented_prior_condition_causation")
    assert field is not None, (
        "'기왕증과 ... 상당 인과관계 유/무' is a printed verdict with no receiver")
    assert field["source_route_id"] == "disability"
    assert field["domain_code"] == "prior_history_influences"


def test_transcribing_a_printed_rate_is_not_the_deferred_judgment() -> None:
    """The PoC excludes CALCULATING a final rate, not reading one off a form.

    If these ever became D/deferred they would be indistinguishable from
    `final_disability_rate`, and the printed figure would go missing again for
    a reason that does not apply to it.
    """
    config = _config()
    by_id = {f["field_id"]: f for f in config["fields"]}
    for field_id in ("documented_prior_condition_contribution_rate",
                     "documented_prior_condition_causation"):
        assert by_id[field_id]["medical_advisory_grade"] != "D"
        assert by_id[field_id]["extraction_wave"] != "deferred"
    # The deferred set stays exactly what it was; this change adds no judgment.
    deferred = {f["field_id"] for f in config["fields"]
                if f.get("medical_advisory_grade") == "D"}
    assert deferred == {"symptom_fixation_judgment", "final_disability_rate"}
