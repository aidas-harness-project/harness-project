"""Part 7: version-bound policy audits and downstream UID resolution."""
import hashlib
import json

import dao
import policy_audit


def _write_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _pass_policy_stage(out):
    """Part 11I: a downstream reference is only resolvable while the policy
    stage itself is currently `passed`."""
    _write_json(out / "_run_state.json", {
        "case_id": "CASE_030",
        "run_id": "RUN_20260724_001",
        "stages": [{
            "stage_name": "policy_clause_processing",
            "status": "passed",
            "attempt_count": 1,
            "backup_path": "outputs/CASE_030/_backups/step_01_policy_clause_processing",
        }],
    })


def _with_snapshot(data, doc_ids=("DOC_001",)):
    """Part 11I: attach the upstream policy snapshot a policy-referencing
    artifact must carry. Taken from the DAO so the fixture tracks the real
    digest inputs instead of freezing a subset."""
    data["upstream_policy_snapshot"] = dao.policy_snapshot_for(
        "CASE_030", doc_ids)
    return data


def _audit(hashes, findings=None, binding=None):
    data = {
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
    if binding is not None:
        # Part 11J: a canonical_v1 document's policy-layer contract must state
        # which registered revision it was derived from. Only the canonically
        # seeded tests need it -- the checker-only ones never reach that gate.
        data["source_text_revision"] = binding
    return data


# P0-3: any test that reaches a real WRITE path, or that resolves a downstream
# policy reference, needs DOC_001 to be genuinely canonical_v1 -- a legacy or
# unregistered document is refused before the audit layer under test is
# reached. Those tests pass `canonical=(make_args, isolated_dao, canonicalize)`
# and get UIDs derived from the registered source. The checker-only tests
# (which call policy_audit.check_policy_audit directly, never the DAO write
# path) keep the cheap non-canonical seed: they are about the audit's own
# version binding, and canonicalizing them would test the UID layer twice
# while making the binding harder to see.
QUOTE = "제3조(보험금의 지급) 회사는 보험금을 지급합니다."
TEXT = f"<<<PAGE page=1>>>\n{QUOTE}\n"
RAW = b"%PDF-1.7 immutable policy source for the audit tests"
FAKE_UIDS = {
    "boundary": "PB-1111111111111111",
    "clause": "PC-1111111111111111",
    "condition": "CI-1111111111111111",
}


def _derived_uids():
    """The UIDs the DAO will recompute for the seeded source, from the same
    production helper it recomputes with."""
    import policy_uid

    pdf = dao.read_contract_data(
        "CASE_030", "document_manifest.json")["documents"][0][
            "source_pdf_sha256"]
    ps = policy_uid.compute_uid(
        "span", source_pdf_sha256=pdf, physical_page=1, span_text=QUOTE,
        ordinal=1)
    pb = policy_uid.compute_uid(
        "boundary", source_pdf_sha256=pdf, physical_page=1, span_text=ps,
        ordinal=1)
    pc = policy_uid.compute_uid(
        "clause", source_pdf_sha256=pdf, physical_page=1, span_text=ps,
        ordinal=1, parent_uid=pb)
    ci = policy_uid.compute_uid(
        "condition", source_pdf_sha256=pdf, physical_page=1, span_text=ps,
        ordinal=1, parent_uid=pc)
    return {"span": ps, "boundary": pb, "clause": pc, "condition": ci}


def _binding():
    return {"documents": [{
        "document_id": "DOC_001",
        "revision_sha256": dao.revision_entry_for(
            "CASE_030", "DOC_001")["current_revision_sha256"],
    }]}


def _seed_contracts(isolated_dao, canonical=None):
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
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
            "extraction_method": "embedded_text",
        }],
    })

    uids = FAKE_UIDS
    span = None
    if canonical is not None:
        make_args, isolated, canonicalize = canonical
        raw = isolated / "data" / "raw" / "CASE_030"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "DOC_001.pdf").write_bytes(RAW)
        canonicalize(make_args, isolated, "CASE_030", "DOC_001", TEXT)
        uids = _derived_uids()
        span = {"page": 1, "start_char": 0, "end_char": len(QUOTE),
                "quote": QUOTE}

    normalized = {
        "clauses": [{
            "clause_uid": uids["clause"],
            "source_boundary_uids": [uids["boundary"]],
            "evidence_references": [{
                "document_id": "DOC_001", "page": 1, "quote": QUOTE,
            }],
            "review_required": False,
            "payout_conditions": [{
                "condition_uid": uids["condition"],
                "evidence_references": [{
                    "document_id": "DOC_001", "page": 1, "quote": QUOTE,
                }],
                "review_required": False,
            }],
        }],
    }
    inventory = {
        "boundaries": [{
            "boundary_uid": uids["boundary"],
            "disposition": "normalized",
            "normalized_mappings": [{
                "clause_uid": uids["clause"],
            }],
        }],
    }
    if span is not None:
        normalized["clauses"][0]["source_span_uids"] = [span]
        normalized["clauses"][0]["payout_conditions"][0][
            "source_span_uids"] = [span]
        # P0-7: under canonical_v1 evidence must pin the same exact range as
        # the identity span. The legacy branch deliberately leaves the
        # offset-free evidence above alone -- that shape stays readable.
        evidence = dict(span)
        evidence["document_id"] = "DOC_001"
        normalized["clauses"][0]["evidence_references"] = [evidence]
        normalized["clauses"][0]["payout_conditions"][0][
            "evidence_references"] = [dict(evidence)]
        inventory["page_spans"] = [{
            "span_uid": uids["span"], "page": 1, "start_char": 0,
            "end_char": len(QUOTE), "quote": QUOTE,
            "disposition": "boundary", "boundary_uid": uids["boundary"],
        }]
    normalized_path = out / "normalized_policy_clause_DOC_001.json"
    inventory_path = out / "policy_boundary_inventory_DOC_001.json"
    _write_json(normalized_path, normalized)
    _write_json(inventory_path, inventory)
    # Take the binding digests from the DAO itself rather than recomputing a
    # subset by hand: Part 11F widened the binding to the processed source text,
    # the manifest entry/page_map, and the parent coverage, and a fixture that
    # hardcodes only the contract hashes silently stops covering the rest.
    hashes, _, _, _ = dao._policy_audit_context("CASE_030", "DOC_001")
    assert hashlib.sha256(normalized_path.read_bytes()).hexdigest() == \
        hashes["normalized_sha256"]
    assert hashlib.sha256(inventory_path.read_bytes()).hexdigest() == \
        hashes["inventory_sha256"]
    _UIDS.clear()
    _UIDS.update(uids)
    return out, normalized, inventory, hashes


# The UIDs the most recent _seed_contracts produced -- fabricated for the
# checker-only tests, source-derived for the canonical ones. Module-level so
# the small payload builders below need no extra plumbing; reset on every seed.
_UIDS: dict = dict(FAKE_UIDS)


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
        "human_review_uid": None,
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


def test_downstream_reference_requires_current_clear_audit(
        isolated_dao, make_args, canonicalize):
    """Canonical seed since P0-3: a downstream reference to a non-canonical
    document is refused before the audit's own currency is ever consulted, so
    the clean-resolution assertion at the end would otherwise be asserting the
    wrong refusal's absence."""
    out, _, _, hashes = _seed_contracts(
        isolated_dao, canonical=(make_args, isolated_dao, canonicalize))
    _pass_policy_stage(out)
    data = _with_snapshot({
        "coverages": [{
            "matched_clause_ref": {
                "document_id": "DOC_001",
                "clause_uid": _UIDS["clause"],
            },
        }],
    })
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
    _pass_policy_stage(out)
    _write_json(
        out / "policy_audit_result_DOC_001.json",
        _audit(hashes),
    )
    data = _with_snapshot({
        "coverage_requirements": [{
            "requirements": [{
                "clause_ref": {
                    "document_id": "DOC_001",
                    "clause_uid": "PC-1111111111111111",
                    "condition_uid": "CI-9999999999999999",
                },
            }],
        }],
    })
    errors = dao._downstream_policy_ref_errors(
        "CASE_030", "requirement_matching_result.schema.json", data)
    assert any("condition_uid" in error and "does not resolve" in error
               for error in errors)


def test_dao_writes_only_current_version_bound_audit(
        isolated_dao, make_args, canonicalize):
    """Canonical seed since P0-3: this is a real write-path test, and a
    policy-layer write against a non-canonical document is now refused before
    the version binding under test is reached."""
    out, _, _, hashes = _seed_contracts(
        isolated_dao, canonical=(make_args, isolated_dao, canonicalize))
    data_file = isolated_dao / "audit.json"
    _write_json(data_file, _audit(hashes, binding=_binding()))
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

    stale = _audit(hashes, binding=_binding())
    stale["inventory_sha256"] = "0" * 64
    _write_json(data_file, stale)
    assert dao.cmd_write_contract(args) == 1
