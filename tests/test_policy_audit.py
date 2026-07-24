"""Part 7: version-bound policy audits and downstream UID resolution."""
import hashlib
import json

import dao
import policy_audit


def _write_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _audit(hashes, findings=None):
    return {
        "case_id": "CASE_030",
        "run_id": "RUN_20260724_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_001",
        **hashes,
        "audit_scope": {
            "source_completeness": True,
            "semantic_buckets": True,
            "evidence_support": True,
            "reference_tables": True,
            "downstream_addresses": True,
        },
        "auditor_id": "policy-auditor",
        "audited_at": "2026-07-24T10:00:00+09:00",
        "findings": findings or [],
    }


def _seed_contracts(isolated_dao):
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    normalized = {
        "clauses": [{
            "clause_uid": "PC-1111111111111111",
            "source_boundary_uids": ["PB-1111111111111111"],
            "review_required": False,
            "payout_conditions": [{
                "condition_uid": "CI-1111111111111111",
                "review_required": False,
            }],
        }],
    }
    inventory = {
        "boundaries": [{
            "boundary_uid": "PB-1111111111111111",
            "disposition": "normalized",
            "normalized_mappings": [{
                "clause_uid": "PC-1111111111111111",
            }],
        }],
    }
    normalized_path = out / "normalized_policy_clause_DOC_001.json"
    inventory_path = out / "policy_boundary_inventory_DOC_001.json"
    _write_json(normalized_path, normalized)
    _write_json(inventory_path, inventory)
    _write_json(out / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_030/DOC_001.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "ocr_status": "completed",
            "document_type": "insurance_policy",
            "downstream_disposition": "automated_text_pipeline",
        }],
    })
    hashes = {
        "normalized_sha256":
            hashlib.sha256(normalized_path.read_bytes()).hexdigest(),
        "inventory_sha256":
            hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
        "reference_table_sha256": None,
    }
    return out, normalized, inventory, hashes


def _open_finding():
    return {
        "finding_uid": "PA-1111111111111111",
        "category": "misbucketed_clause",
        "severity": "high",
        "status": "open",
        "description": "고지의무가 지급조건으로 분류됨",
        "remediation": "obligations 버킷으로 이동",
        "clause_uid": "PC-1111111111111111",
        "condition_uid": "CI-1111111111111111",
        "boundary_uid": None,
        "table_uid": None,
        "evidence_references": [],
        "artifact_refs": ["normalized_policy_clause_DOC_001.json"],
        "resolution_note": None,
        "resolved_by": None,
        "resolution_actor_type": None,
    }


def test_clean_current_audit_passes(isolated_dao):
    _, normalized, inventory, hashes = _seed_contracts(isolated_dao)
    errors = policy_audit.check_policy_audit(
        _audit(hashes),
        "policy_audit_result_DOC_001.json",
        hashes,
        normalized,
        inventory,
        None,
    )
    assert errors == []


def test_stale_normalized_hash_is_rejected(isolated_dao):
    _, normalized, inventory, hashes = _seed_contracts(isolated_dao)
    audit = _audit(hashes)
    audit["normalized_sha256"] = "0" * 64
    errors = policy_audit.check_policy_audit(
        audit,
        "policy_audit_result_DOC_001.json",
        hashes,
        normalized,
        inventory,
        None,
    )
    assert any("normalized_sha256 is stale" in error for error in errors)


def test_empty_findings_cannot_hide_dangling_source_boundary(isolated_dao):
    _, normalized, inventory, hashes = _seed_contracts(isolated_dao)
    normalized["clauses"][0]["source_boundary_uids"] = [
        "PB-9999999999999999"]
    errors = policy_audit.check_policy_audit(
        _audit(hashes),
        "policy_audit_result_DOC_001.json",
        hashes,
        normalized,
        inventory,
        None,
    )
    assert any("deterministic audit" in error and "does not exist" in error
               for error in errors), errors


def test_empty_findings_cannot_hide_review_required_table(isolated_dao):
    _, normalized, inventory, hashes = _seed_contracts(isolated_dao)
    reference_table = {
        "tables": [{
            "table_uid": "RT-1111111111111111",
            "review_required": True,
            "rows": [{
                "row_uid": "RR-1111111111111111",
                "cells": [{
                    "cell_uid": "RC-1111111111111111",
                    "review_required": True,
                }],
            }],
        }],
    }
    errors = policy_audit.check_policy_audit(
        _audit(hashes),
        "policy_audit_result_DOC_001.json",
        hashes,
        normalized,
        inventory,
        reference_table,
    )
    assert any("table" in error and "review_required" in error
               for error in errors), errors
    assert any("cell" in error and "review_required" in error
               for error in errors), errors


def test_open_finding_blocks_clear_audit(isolated_dao):
    _, _, _, hashes = _seed_contracts(isolated_dao)
    blockers = policy_audit.unresolved_findings(
        _audit(hashes, [_open_finding()]))
    assert any("고지의무" in blocker for blocker in blockers)


def test_accepted_risk_requires_human_actor(isolated_dao):
    _, _, _, hashes = _seed_contracts(isolated_dao)
    finding = _open_finding()
    finding.update({
        "status": "accepted_risk",
        "resolution_note": "운영상 수용",
        "resolved_by": "agent",
        "resolution_actor_type": "automated",
    })
    errors = dao._schema_check(
        _audit(hashes, [finding]),
        "policy_audit_result.schema.json",
    )
    assert any("human" in error for error in errors)


def test_downstream_reference_requires_current_clear_audit(isolated_dao):
    out, _, _, hashes = _seed_contracts(isolated_dao)
    data = {
        "coverages": [{
            "matched_clause_ref": {
                "document_id": "DOC_001",
                "clause_uid": "PC-1111111111111111",
            },
        }],
    }
    assert any(
        "policy audit is missing" in error
        for error in dao._downstream_policy_ref_errors(
            "CASE_030", "coverage_result.schema.json", data))
    _write_json(
        out / "policy_audit_result_DOC_001.json",
        _audit(hashes),
    )
    assert dao._downstream_policy_ref_errors(
        "CASE_030", "coverage_result.schema.json", data) == []


def test_downstream_condition_uid_must_resolve_within_clause(isolated_dao):
    out, _, _, hashes = _seed_contracts(isolated_dao)
    _write_json(
        out / "policy_audit_result_DOC_001.json",
        _audit(hashes),
    )
    data = {
        "coverage_requirements": [{
            "requirements": [{
                "clause_ref": {
                    "document_id": "DOC_001",
                    "clause_uid": "PC-1111111111111111",
                    "condition_uid": "CI-9999999999999999",
                },
            }],
        }],
    }
    errors = dao._downstream_policy_ref_errors(
        "CASE_030", "requirement_matching_result.schema.json", data)
    assert any("condition_uid" in error and "does not resolve" in error
               for error in errors)


def test_dao_writes_only_current_version_bound_audit(
        isolated_dao, make_args):
    out, _, _, hashes = _seed_contracts(isolated_dao)
    data_file = isolated_dao / "audit.json"
    _write_json(data_file, _audit(hashes))
    args = make_args(
        case_id="CASE_030",
        filename="policy_audit_result_DOC_001.json",
        data_file=str(data_file),
        schema_name="policy_audit_result.schema.json",
        held_by="policy-pipeline",
        run_id="RUN_20260724_001",
        stage="policy_clause_processing",
    )
    assert dao.cmd_write_contract(args) == 0
    assert (out / "policy_audit_result_DOC_001.json").exists()

    stale = _audit(hashes)
    stale["inventory_sha256"] = "0" * 64
    _write_json(data_file, stale)
    assert dao.cmd_write_contract(args) == 1
