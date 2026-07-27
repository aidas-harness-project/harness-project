"""Part 11I: downstream artifacts record the policy snapshot they read.

A coverage_result that points at `PC-1111…` is not just a pointer, it is an
assertion: "as of the policy layer I read, this clause says what I claim it
says." Nothing recorded which policy layer that was. So a clause could be
renormalized, a boundary inventory rewritten, a parent's page map corrected --
and the downstream artifact would keep resolving cleanly, because every check
ran against the NEW bytes and silently agreed with itself.

`upstream_policy_snapshot` closes that: the artifact carries the digest set it
was derived from, the DAO recomputes it at write time, and a mismatch is a
refusal rather than a warning. Being unable to detect staleness is the
condition this removes, so the field is mandatory the moment a policy
reference exists.
"""
import json

import pytest

import dao


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


NORMALIZED = {
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
INVENTORY = {
    "boundaries": [{
        "boundary_uid": "PB-1111111111111111",
        "disposition": "normalized",
        "normalized_mappings": [{"clause_uid": "PC-1111111111111111"}],
    }],
}


def _audit(hashes):
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
        "findings": [],
    }


@pytest.fixture
def policy_case(isolated_dao):
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    _write_json(out / "normalized_policy_clause_DOC_001.json", NORMALIZED)
    _write_json(out / "policy_boundary_inventory_DOC_001.json", INVENTORY)
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
    hashes, _, _, _ = dao._policy_audit_context("CASE_030", "DOC_001")
    _write_json(out / "policy_audit_result_DOC_001.json", _audit(hashes))
    _write_json(out / "_run_state.json", {
        "case_id": "CASE_030",
        "run_id": "RUN_20260724_001",
        "stages": [{
            "stage_name": "policy_clause_processing",
            "status": "passed",
            "attempt_count": 1,
            "backup_path": "outputs/CASE_030/_backups/step_01_policy",
        }],
    })
    return out


def _coverage(snapshot=None):
    data = {
        "coverages": [{
            "matched_clause_ref": {
                "document_id": "DOC_001",
                "clause_uid": "PC-1111111111111111",
            },
        }],
    }
    if snapshot is not None:
        data["upstream_policy_snapshot"] = snapshot
    return data


def _errors(data):
    return dao._downstream_policy_ref_errors(
        "CASE_030", "coverage_result.schema.json", data)


def test_a_current_snapshot_resolves_cleanly(policy_case):
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    assert _errors(_coverage(snapshot)) == []


def test_a_policy_reference_without_a_snapshot_is_refused(policy_case):
    errors = _errors(_coverage())
    assert any("upstream_policy_snapshot is missing" in e for e in errors), errors


def test_an_artifact_with_no_policy_reference_needs_no_snapshot(policy_case):
    assert _errors({"coverages": [{"matched_clause_ref": None}]}) == []


def test_rewriting_the_normalized_contract_makes_the_snapshot_stale(policy_case):
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    assert _errors(_coverage(snapshot)) == []
    renormalized = json.loads(json.dumps(NORMALIZED))
    renormalized["clauses"][0]["payout_conditions"][0]["review_required"] = True
    _write_json(policy_case / "normalized_policy_clause_DOC_001.json",
                renormalized)
    errors = _errors(_coverage(snapshot))
    assert any("is stale" in e for e in errors), errors


def test_rewriting_the_boundary_inventory_makes_the_snapshot_stale(policy_case):
    """The clause bytes did not move, but the accounting that justifies them
    did -- and the downstream artifact was derived from both."""
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    rewritten = json.loads(json.dumps(INVENTORY))
    rewritten["boundaries"][0]["disposition"] = "excluded"
    _write_json(policy_case / "policy_boundary_inventory_DOC_001.json",
                rewritten)
    assert any("is stale" in e for e in _errors(_coverage(snapshot)))


def test_editing_the_manifest_page_map_makes_the_snapshot_stale(policy_case):
    """Every page-local evidence claim is expressed against the page map, so
    moving it moves the meaning of clauses that never changed a byte."""
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    manifest = json.loads(
        (policy_case / "document_manifest.json").read_text(encoding="utf-8"))
    manifest["documents"][0]["page_map"] = [
        {"logical_page": 1, "physical_page": 8}]
    _write_json(policy_case / "document_manifest.json", manifest)
    assert any("is stale" in e for e in _errors(_coverage(snapshot)))


def test_a_snapshot_may_not_omit_or_pad_the_referenced_documents(policy_case):
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    empty = {"documents": [], "snapshot_sha256": snapshot["snapshot_sha256"]}
    assert any("does not cover referenced document DOC_001" in e
               for e in _errors(_coverage(empty)))
    padded = json.loads(json.dumps(snapshot))
    padded["documents"].append(
        {"document_id": "DOC_999", "digest_sha256": "0" * 64})
    assert any("does not reference" in e for e in _errors(_coverage(padded)))


def test_a_forged_snapshot_digest_is_refused(policy_case):
    """The recorded digest is recomputed, never trusted -- a downstream author
    cannot mint agreement by writing whatever hash it likes."""
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    snapshot["documents"][0]["digest_sha256"] = "0" * 64
    assert any("is stale" in e for e in _errors(_coverage(snapshot)))


def test_snapshot_sha256_must_match_its_own_documents_list(policy_case):
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    snapshot["snapshot_sha256"] = "1" * 64
    assert any("does not match its own documents list" in e
               for e in _errors(_coverage(snapshot)))


def test_a_failed_policy_stage_blocks_every_downstream_reference(policy_case):
    """Per-contract soundness is not the same question as 'did the policy
    stage clear'. CASE_030's shape exactly: clean-looking individual contracts
    inside a stage that never passed."""
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    state = json.loads(
        (policy_case / "_run_state.json").read_text(encoding="utf-8"))
    state["stages"][0]["status"] = "failed"
    state["stages"][0].pop("backup_path", None)
    _write_json(policy_case / "_run_state.json", state)
    errors = _errors(_coverage(snapshot))
    assert any("policy_clause_processing is failed" in e for e in errors), errors


def test_write_contract_refuses_a_stale_snapshot(policy_case, isolated_dao,
                                                 make_args):
    """End to end through the real write path, not just the checker."""
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    payload = _coverage(snapshot)
    payload.update({
        "case_id": "CASE_030", "run_id": "RUN_20260724_001",
        "component": "claim-analysis", "status": "success",
    })
    payload["coverages"][0].update({
        "coverage_name": "테스트담보",
        "standardized_coverage_name": "test_benefit",
        "applicable": True,
        "review_required": False,
        "confidence": 0.9,
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": "지급합니다",
        }],
    })
    data_file = isolated_dao / "coverage.json"
    _write_json(data_file, payload)
    args = make_args(
        case_id="CASE_030", filename="coverage_result.json",
        data_file=str(data_file), schema_name="coverage_result.schema.json",
        held_by="claim-analysis", run_id="RUN_20260724_001", stage=None)
    assert dao.cmd_write_contract(args) == 0

    renormalized = json.loads(json.dumps(NORMALIZED))
    renormalized["clauses"][0]["review_required"] = True
    _write_json(policy_case / "normalized_policy_clause_DOC_001.json",
                renormalized)
    assert dao.cmd_write_contract(args) == 1


def _claim_analysis_passed(policy_case):
    state = json.loads(
        (policy_case / "_run_state.json").read_text(encoding="utf-8"))
    state["stages"].append({
        "stage_name": "claim_analysis", "status": "passed",
        "attempt_count": 1,
        "backup_path": "outputs/CASE_030/_backups/step_02_claim_analysis",
    })
    _write_json(policy_case / "_run_state.json", state)


def _claim_analysis_status(policy_case):
    after = json.loads(
        (policy_case / "_run_state.json").read_text(encoding="utf-8"))
    return {s["stage_name"]: s["status"] for s in after["stages"]}[
        "claim_analysis"]


def _rewrite_audit_args(policy_case, isolated_dao, make_args, **changes):
    """A real, schema-valid, cross-contract-clean rewrite of a policy-layer
    contract. The audit is used rather than the normalized contract because a
    valid rewrite of it is cheap to construct -- the point under test is the
    cascade, and inventing a hundred lines of clause fixture to reach it would
    only make the cascade harder to see."""
    hashes, _, _, _ = dao._policy_audit_context("CASE_030", "DOC_001")
    audit = _audit(hashes)
    audit.update(changes)
    data_file = isolated_dao / "audit.json"
    _write_json(data_file, audit)
    return make_args(
        case_id="CASE_030",
        filename="policy_audit_result_DOC_001.json",
        data_file=str(data_file),
        schema_name="policy_audit_result.schema.json",
        held_by="policy-pipeline", run_id="RUN_20260724_001", stage=None)


def test_rewriting_a_policy_contract_invalidates_downstream_stages(
        policy_case, isolated_dao, make_args):
    """The cascade fires from the policy layer too, not only from run-state
    demotions: rewriting a policy contract un-grounds claim_analysis."""
    _claim_analysis_passed(policy_case)
    args = _rewrite_audit_args(
        policy_case, isolated_dao, make_args,
        audited_at="2026-07-25T09:00:00+09:00")
    assert dao.cmd_write_contract(args) == 0
    assert _claim_analysis_status(policy_case) == "failed"


def test_rewriting_identical_bytes_invalidates_nothing(
        policy_case, isolated_dao, make_args):
    """Only a real change un-grounds downstream work. Re-writing a contract
    with the bytes it already had must not demote a passed stage -- a cascade
    that fires on every write would train everyone to ignore it."""
    _claim_analysis_passed(policy_case)
    args = _rewrite_audit_args(policy_case, isolated_dao, make_args)
    assert dao.cmd_write_contract(args) == 0
    assert _claim_analysis_status(policy_case) == "passed"


def test_policy_snapshot_cli_refuses_a_document_with_no_contract(
        policy_case, make_args, capsys):
    args = make_args(case_id="CASE_030", document_id=["DOC_404"])
    assert dao.cmd_policy_snapshot(args) == 1
    assert "BLOCKED" in capsys.readouterr().out

    args = make_args(case_id="CASE_030", document_id=["DOC_001"])
    assert dao.cmd_policy_snapshot(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == dao.policy_snapshot_for("CASE_030", ["DOC_001"])
