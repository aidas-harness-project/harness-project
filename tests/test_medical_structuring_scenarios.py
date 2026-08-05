"""Fresh synthetic acceptance scenarios for medical structuring v0.1."""
from __future__ import annotations

from copy import deepcopy

from medical_contracts import build_timeline_index, validate_medical_variables
from test_medical_semantics import enabled_config, minimal_data, semantic_inputs


def _point(value: str) -> dict:
    return {
        "kind": "point",
        "precision": "day",
        "value": value,
        "raw_text": value,
    }


def _endpoint(value: str) -> dict:
    return {"precision": "day", "value": value}


def _observation(
    number: int,
    *,
    quote: str,
    observed_at: dict,
    value_type: str = "text",
    value=None,
) -> dict:
    observation = {
        "observation_id": f"MO_{number:04d}",
        "value_state": "asserted",
        "value_type": value_type,
        "observed_at": observed_at,
        "evidence": [{
            "locator_id": f"MEV_{number:04d}",
            "document_id": "DOC_001",
            "page": 1,
            "quote": quote,
            "chunk_id": "CHUNK_001",
        }],
        "extraction_method": "model_extracted",
    }
    if value_type == "coded":
        observation["coded_value"] = value
    elif value_type == "text":
        observation["text_value"] = value
    elif value_type == "quantity":
        observation["quantity_value"] = value
    else:
        raise AssertionError(f"unsupported synthetic value_type: {value_type}")
    return observation


def _variable(
    number: int,
    *,
    domain: str,
    kind: str,
    label: str,
    observation: dict,
    related: list[str] | None = None,
) -> dict:
    return {
        "variable_id": f"MV_{number:04d}",
        "domain_id": f"MD_{domain}",
        "variable_kind": kind,
        "label": label,
        "related_variable_ids": related or [],
        "observations": [observation],
    }


def _domain(code: str, label: str) -> dict:
    return {
        "domain_id": f"MD_{code}",
        "domain_code": code,
        "label": label,
        "config_version": "medical_structuring.v0.1",
    }


def _validate(data: dict, config: dict, *, conflict_ledger: dict | None = None):
    manifest, page_chunks, default_conflicts = semantic_inputs()
    quotes = [
        locator["quote"]
        for variable in data["variables"]
        for observation in variable["observations"]
        for locator in observation["evidence"]
        if "quote" in locator
    ]
    page_chunks["chunks"][0]["text"] = "\n".join(quotes)
    if len(data["source_coverage"]) > 1:
        manifest["documents"].append({
            "document_id": "DOC_002",
            "file_name": "synthetic-human-only.pdf",
            "file_path": "data/raw/CASE_9001/DOC_002/synthetic.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "pages": 1,
            "ocr_status": "pending",
            "cross_validation_status": "pending",
            "document_type": "expert_opinion",
            "downstream_disposition": "expert_review_only",
        })
    return validate_medical_variables(
        data,
        manifest=manifest,
        page_chunks=page_chunks,
        conflict_ledger=conflict_ledger or default_conflicts,
        config=config,
        canonical_case_type="기타",
    )


def test_diagnosis_imaging_treatment_form_one_fixed_containment_hierarchy():
    data = minimal_data()
    config = enabled_config()
    data["domains"] = [
        _domain("diagnosis", "Diagnosis"),
        _domain("objective_evidence", "Objective evidence"),
        _domain("treatment", "Treatment"),
    ]
    config["variable_kinds"] = [
        {"code": "diagnosis_code", "domain_code": "diagnosis", "label": "Synthetic diagnosis"},
        {"code": "imaging_finding", "domain_code": "objective_evidence", "label": "Synthetic imaging"},
        {"code": "treatment_event", "domain_code": "treatment", "label": "Synthetic treatment"},
    ]
    data["variables"] = [
        _variable(
            1,
            domain="diagnosis",
            kind="diagnosis_code",
            label="Documented diagnosis",
            observation=_observation(
                1,
                quote="Synthetic diagnosis documented.",
                observed_at=_point("2026-01-03"),
                value_type="coded",
                value={"system": "synthetic", "code": "DX-1", "display": "Synthetic diagnosis"},
            ),
            related=["MV_0002", "MV_0003"],
        ),
        _variable(
            2,
            domain="objective_evidence",
            kind="imaging_finding",
            label="Documented image finding",
            observation=_observation(
                2,
                quote="Synthetic image finding documented.",
                observed_at=_point("2026-01-02"),
                value="Synthetic image finding",
            ),
            related=["MV_0001"],
        ),
        _variable(
            3,
            domain="treatment",
            kind="treatment_event",
            label="Documented treatment",
            observation=_observation(
                3,
                quote="Synthetic treatment documented.",
                observed_at=_point("2026-01-04"),
                value="Synthetic treatment",
            ),
            related=["MV_0001"],
        ),
    ]
    data["timeline_observation_ids"] = build_timeline_index(data)

    assert _validate(data, config) == []
    assert data["timeline_observation_ids"] == ["MO_0002", "MO_0001", "MO_0003"]
    assert {variable["domain_id"] for variable in data["variables"]} == {
        "MD_diagnosis",
        "MD_objective_evidence",
        "MD_treatment",
    }


def test_contradiction_keeps_both_observations_and_human_only_source_unconsumed():
    data = minimal_data()
    config = enabled_config()
    first = data["variables"][0]["observations"][0]
    first["coded_value"] = {
        "system": "synthetic",
        "code": "DX-A",
        "display": "Synthetic diagnosis A",
    }
    second = deepcopy(first)
    second.update({"observation_id": "MO_0002"})
    second["coded_value"] = {
        "system": "synthetic",
        "code": "DX-B",
        "display": "Synthetic diagnosis B",
    }
    second["evidence"][0].update({
        "locator_id": "MEV_0002",
        "quote": "Synthetic conflicting diagnosis documented.",
    })
    data["variables"][0]["observations"] = [first, second]
    data["medical_issues"] = [{
        "issue_id": "MCI_0001",
        "issue_category": "diagnosis",
        "question": "Which documented synthetic code remains unresolved?",
        "evidence_links": [
            {
                "issue_evidence_link_id": "MIEL_0001",
                "observation_id": "MO_0001",
                "relation": "supports",
                "materiality": "core",
                "reason": "First documented synthetic value.",
                "classification_version": "medical_structuring.v0.1",
            },
            {
                "issue_evidence_link_id": "MIEL_0002",
                "observation_id": "MO_0002",
                "relation": "conflicts",
                "materiality": "core",
                "reason": "Second documented synthetic value conflicts with the first.",
                "classification_version": "medical_structuring.v0.1",
            },
        ],
        "config_version": "medical_structuring.v0.1",
    }]
    data["importance_assignments"] = [
        {
            "importance_id": "MIA_0001",
            "issue_id": "MCI_0001",
            "observation_id": "MO_0001",
            "tier": "A",
            "reason": "Core only for this synthetic question.",
            "config_version": "medical_structuring.v0.1",
        },
        {
            "importance_id": "MIA_0002",
            "issue_id": "MCI_0001",
            "observation_id": "MO_0002",
            "tier": "A",
            "reason": "Competing core value for this synthetic question.",
            "config_version": "medical_structuring.v0.1",
        },
    ]
    data["contradiction_groups"] = [{
        "contradiction_group_id": "MCG_0001",
        "observation_ids": ["MO_0001", "MO_0002"],
        "conflict_id": "CONFLICT_1",
    }]
    data["timeline_observation_ids"] = build_timeline_index(data)
    data["source_coverage"].append({
        "document_id": "DOC_002",
        "document_role": "expert_opinion",
        "coverage_status": "human_only_not_consumed",
        "reason": "Synthetic human-only source is listed but not consumed.",
        "processing_contract_versions": {
            "document_manifest": "document_manifest.v0.1",
            "page_chunks": "page_chunks.v0.1",
        },
    })
    conflict_ledger = {
        "case_id": "CASE_9001",
        "conflicts": [{"conflict_id": "CONFLICT_1", "verdict": "pending"}],
    }

    assert _validate(data, config, conflict_ledger=conflict_ledger) == []
    assert len(data["variables"][0]["observations"]) == 2
    assert data["source_coverage"][1]["coverage_status"] == "human_only_not_consumed"


def _quantity_scenario() -> tuple[dict, dict]:
    data = minimal_data()
    config = enabled_config()
    data["domains"] = [_domain("treatment", "Treatment")]
    config["variable_kinds"] = [{
        "code": "treatment_session",
        "domain_code": "treatment",
        "label": "Synthetic treatment session",
    }]
    config["units"] = ["visit"]
    config["quantity_rules"] = [{
        "rule_id": "QR_SYNTHETIC_VISITS",
        "variable_kind": "treatment_session",
        "counting_basis": "documented_encounters",
        "allowed_units": ["visit"],
    }]
    observations = []
    for number, date in ((1, "2026-01-02"), (2, "2026-01-05")):
        observations.append(_observation(
            number,
            quote=f"Synthetic treatment visit {number} documented.",
            observed_at=_point(date),
            value_type="quantity",
            value={
                "count": 1,
                "unit_status": "normalized",
                "unit_code": "visit",
                "period_start": _endpoint(date),
                "period_end": _endpoint(date),
                "counting_basis": "documented_encounters",
            },
        ))
    data["variables"] = [{
        "variable_id": "MV_0001",
        "domain_id": "MD_treatment",
        "variable_kind": "treatment_session",
        "label": "Documented synthetic visits",
        "related_variable_ids": [],
        "observations": observations,
    }]
    data["medical_issues"] = [{
        "issue_id": "MCI_0001",
        "issue_category": "treatment_pattern",
        "question": "What documented synthetic visit count is present?",
        "evidence_links": [
            {
                "issue_evidence_link_id": f"MIEL_{number:04d}",
                "observation_id": f"MO_{number:04d}",
                "relation": "supports",
                "materiality": "core" if number == 1 else "supporting",
                "reason": "Documented synthetic encounter.",
                "classification_version": "medical_structuring.v0.1",
            }
            for number in (1, 2)
        ],
        "config_version": "medical_structuring.v0.1",
    }]
    data["importance_assignments"] = [
        {
            "importance_id": f"MIA_{number:04d}",
            "issue_id": "MCI_0001",
            "observation_id": f"MO_{number:04d}",
            "tier": tier,
            "reason": "Issue-contextual synthetic importance only.",
            "config_version": "medical_structuring.v0.1",
        }
        for number, tier in ((1, "A"), (2, "B"))
    ]
    data["quantity_summaries"] = [{
        "quantity_id": "MQ_0001",
        "variable_id": "MV_0001",
        "source_observation_ids": ["MO_0001", "MO_0002"],
        "period": {
            "kind": "range",
            "start": _endpoint("2026-01-02"),
            "end": _endpoint("2026-01-05"),
            "raw_text": "2026-01-02 to 2026-01-05",
        },
        "count": 2,
        "unit_status": "normalized",
        "unit_code": "visit",
        "counting_basis": "documented_encounters",
        "coverage_status": "complete",
    }]
    data["timeline_observation_ids"] = build_timeline_index(data)
    return data, config


def test_quantity_timeline_and_importance_are_deterministic_not_clinical_inference():
    data, config = _quantity_scenario()
    assert _validate(data, config) == []
    assert data["timeline_observation_ids"] == ["MO_0001", "MO_0002"]
    assert [row["tier"] for row in data["importance_assignments"]] == ["A", "B"]

    data["quantity_summaries"][0]["count"] = 3
    errors = _validate(data, config)
    assert any("does not equal source observation count" in error for error in errors)
