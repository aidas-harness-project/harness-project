"""First reconstruction tracer for the approved medical fail-closed path.

This test is intentionally synthetic and pure.  It establishes the contract/gate seam
before any medical implementation is ported into the clean reconstruction branch.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from medical_contracts import validate_medical_variables
from medical_review_ledger import clearance_blockers
import dao
from test_medical_review_outcomes import _publish_no_issue, _run_state


def _variables() -> dict:
    return {
        "case_id": "CASE_9001",
        "case_type": "synthetic",
        "run_id": "RUN_20260804_001",
        "component": "claim-analysis",
        "status": "success",
        "schema_version": "medical_variables.v0.1",
        "config_version": "medical_structuring.v0.1",
        "source_coverage": [{
            "document_id": "DOC_001",
            "document_role": "medical_record",
            "coverage_status": "consumed",
            "reason": "Synthetic validated medical record.",
            "processing_contract_versions": {
                "document_manifest": "document_manifest.v0.1",
                "page_chunks": "page_chunks.v0.1",
            },
        }],
        "domains": [{
            "domain_id": "MD_diagnosis",
            "domain_code": "diagnosis",
            "label": "Diagnosis",
            "config_version": "medical_structuring.v0.1",
        }],
        "medical_issues": [{
            "issue_id": "MCI_0001",
            "issue_category": "diagnosis",
            "question": "How should the synthetic finding be interpreted?",
            "evidence_links": [{
                "issue_evidence_link_id": "MIEL_0001",
                "observation_id": "MO_0001",
                "relation": "supports",
                "materiality": "core",
                "reason": "The observation establishes the synthetic issue.",
                "classification_version": "medical_structuring.v0.1",
            }],
            "config_version": "medical_structuring.v0.1",
        }],
        "variables": [{
            "variable_id": "MV_0001",
            "domain_id": "MD_diagnosis",
            "variable_kind": "diagnosis_code",
            "label": "Documented diagnosis",
            "related_variable_ids": [],
            "observations": [{
                "observation_id": "MO_0001",
                "value_state": "asserted",
                "value_type": "coded",
                "coded_value": {
                    "system": "synthetic",
                    "code": "SYN-001",
                    "display": "Synthetic finding",
                },
                "observed_at": {
                    "kind": "unknown",
                    "reason": "No occurrence date in the synthetic source.",
                },
                "evidence": [{
                    "locator_id": "MEV_0001",
                    "document_id": "DOC_001",
                    "page": 1,
                    "quote": "Synthetic finding documented.",
                    "chunk_id": "CHUNK_001",
                }],
                "extraction_method": "model_extracted",
            }],
        }],
        "contradiction_groups": [],
        "importance_assignments": [{
            "importance_id": "MIA_0001",
            "issue_id": "MCI_0001",
            "observation_id": "MO_0001",
            "tier": "A",
            "reason": "Synthetic importance for contract testing only.",
            "config_version": "medical_structuring.v0.1",
        }],
        "quantity_summaries": [],
        "timeline_observation_ids": ["MO_0001"],
    }


def _manifest() -> dict:
    return {
        "case_id": "CASE_9001",
        "documents": [{
            "document_id": "DOC_001",
            "document_type": "medical_record",
            "pages": 1,
            "ocr_status": "completed",
            "cross_validation_status": "agreed",
            "downstream_disposition": "automated_text_pipeline",
        }],
    }


def _page_chunks() -> dict:
    return {
        "case_id": "CASE_9001",
        "chunks": [{
            "chunk_id": "CHUNK_001",
            "document_id": "DOC_001",
            "page_start": 1,
            "page_end": 1,
            "text": "Synthetic finding documented.",
        }],
    }


def _config() -> dict:
    return {
        "schema_version": "medical_structuring_config.v0.1",
        "config_version": "medical_structuring.v0.1",
        "behavior_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "authority_role": "test-fixture",
            "decision_record": "tests/test_medical_reconstruction_tracer.py",
            "approved_at": "2026-08-04T00:00:00+09:00",
            "scope": "Synthetic test only; no clinical activation.",
        },
        "enabled_case_types": ["synthetic"],
        "enabled_document_roles": ["medical_record"],
        "domains": [{
            "code": "diagnosis",
            "label": "Diagnosis",
        }],
        "variable_kinds": [{
            "code": "diagnosis_code",
            "domain_code": "diagnosis",
            "label": "Diagnosis code",
        }],
        "importance_tiers": [
            {"tier": "A", "description": "Core synthetic evidence."},
            {"tier": "B", "description": "Supporting synthetic evidence."},
            {"tier": "C", "description": "Context-only synthetic evidence."},
        ],
        "units": [],
        "quantity_rules": [],
    }


def test_qg_med_1_contract_revision_and_gate_are_fail_closed() -> None:
    variables = _variables()
    errors = validate_medical_variables(
        variables,
        manifest=_manifest(),
        page_chunks=_page_chunks(),
        conflict_ledger={"case_id": "CASE_9001", "conflicts": []},
        config=_config(),
        canonical_case_type="synthetic",
    )
    assert errors == []

    canonical = json.dumps(
        variables,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    revision = hashlib.sha256(canonical).hexdigest()
    assert len(revision) == 64

    no_issue_variables = deepcopy(variables)
    no_issue_variables["medical_issues"] = []
    no_issue_variables["importance_assignments"] = []
    empty_ledger = {"review_items": []}
    assert clearance_blockers(empty_ledger, no_issue_variables) == ([], [])

    pending_ledger = {
        "review_items": [{
            "review_item_id": "MRI_0001",
            "issue_id": "MCI_0001",
            "state": "decision_pending",
            "target_medical_variables_revision": revision,
        }],
    }
    blockers, coverage_errors = clearance_blockers(pending_ledger, variables)
    assert coverage_errors == []
    assert blockers == ["MRI_0001"]

    resolved_ledger = {
        "review_items": [{
            "review_item_id": "MRI_0001",
            "issue_id": "MCI_0001",
            "state": "do_not_refer",
            "target_medical_variables_revision": revision,
        }],
    }
    assert clearance_blockers(resolved_ledger, variables) == ([], [])


def test_qg_med_1_real_dao_no_issue_vertical_loop(
    isolated_dao, tmp_path, monkeypatch, make_args, capsys
):
    case_id, run_id = _publish_no_issue(
        tmp_path,
        monkeypatch,
        make_args,
        capsys,
    )
    assert dao.cmd_check_medical_reviews_clear(
        make_args(case_id=case_id)
    ) == 0

    dao.save_run_state(case_id, _run_state(case_id, run_id, "in_progress"))
    capsys.readouterr()
    assert dao.cmd_read_medical_review_outcomes(make_args(
        case_id=case_id,
        caller_stage="screening_report",
        run_id=run_id,
    )) == 0
    outcome = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert outcome["schema_version"] == "medical_review_outcomes.v0.1"
    assert outcome["case_id"] == case_id
    assert outcome["medical_variables_revision"]["run_id"] == run_id
    assert outcome["review_items"] == []
