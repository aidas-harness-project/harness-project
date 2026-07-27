"""Part 11A: human provenance for policy decisions is DAO-backed, not self-declared.

A machine-authored contract can no longer claim 'a human decided X' with a bare
name string. accepted_risk / resolution_actor_type=human on an audit finding,
and an administrative_excluded unpaged physical page, each require a
human_review_uid that resolves to a real record in _human_review_ledger.json --
a ledger written ONLY through `dao.py record-human-review`, whose hash the DAO
computes itself from the reviewed artifact's canonical bytes.
"""
import hashlib
import json

import dao
import human_review


def _write_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _finding(status="accepted_risk", actor="human", uid=None, review_uid=None):
    return {
        "finding_uid": uid or "PA-1111111111111111",
        "category": "semantic_mismatch",
        "severity": "medium",
        "status": status,
        "description": "잔여 위험 수용 대상 소견",
        "remediation": "수동 검토 결과 수용",
        "clause_uid": None,
        "condition_uid": None,
        "boundary_uid": None,
        "table_uid": None,
        "evidence_references": [],
        "artifact_refs": ["normalized_policy_clause_DOC_001.json"],
        "resolution_note": "운영상 수용" if status != "open" else None,
        "resolved_by": "홍길동" if status != "open" else None,
        "resolution_actor_type": actor if status != "open" else None,
        "human_review_uid": review_uid,
    }


def _audit(findings):
    return {
        "case_id": "CASE_030",
        "run_id": "RUN_20260724_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_005",
        "normalized_sha256": "a" * 64,
        "inventory_sha256": "b" * 64,
        "reference_table_sha256": None,
        "audit_scope": {
            "source_completeness": True,
            "semantic_buckets": True,
            "evidence_support": True,
            "reference_tables": True,
            "downstream_addresses": True,
        },
        "auditor_id": "policy-auditor",
        "audited_at": "2026-07-24T10:00:00+09:00",
        "findings": findings,
    }


# ---- schema-level: the JSON alone cannot self-declare a human decision ----

def test_fake_human_name_without_uid_is_schema_rejected():
    """FAKE-HUMAN-NAME + actor_type=human, no human_review_uid -> rejected."""
    finding = _finding(actor="human", review_uid=None)
    finding["resolved_by"] = "FAKE-HUMAN-NAME"
    errors = dao._schema_check(_audit([finding]), "policy_audit_result.schema.json")
    assert any("human_review_uid" in e for e in errors), errors


def test_accepted_risk_without_uid_is_schema_rejected():
    finding = _finding(status="accepted_risk", actor="human", review_uid=None)
    errors = dao._schema_check(_audit([finding]), "policy_audit_result.schema.json")
    assert any("human_review_uid" in e for e in errors), errors


def test_automated_actor_cannot_be_accepted_risk():
    """accepted_risk forces actor_type=human; an automated actor is rejected."""
    finding = _finding(status="accepted_risk", actor="automated",
                       review_uid="HR-0123456789abcdef")
    errors = dao._schema_check(_audit([finding]), "policy_audit_result.schema.json")
    assert any("human" in e for e in errors), errors


# ---- verifier-level: the referenced UID must resolve, match, and be allowed --

def test_nonexistent_review_uid_is_rejected():
    finding = _finding(review_uid="HR-ffffffffffffffff")
    audit = _audit([finding])
    ledger = {"case_id": "CASE_030", "records": []}
    errors = human_review.check_audit_human_provenance(audit, ledger, "DOC_005")
    assert any("does not exist" in e for e in errors), errors


def test_uid_issued_for_different_artifact_hash_is_rejected():
    """A UID recorded against DIFFERENT bytes than the finding now carries."""
    finding = _finding(review_uid="HR-0000000000000000")
    audit = _audit([finding])
    ledger = {
        "case_id": "CASE_030",
        "records": [{
            "review_uid": "HR-0000000000000000",
            "reviewer": "홍길동",
            "artifact_kind": "policy_audit_finding",
            "artifact_id": "DOC_005",
            "target_key": "PA-1111111111111111",
            "artifact_sha256": "9" * 64,  # not the audit's current canonical hash
            "decision": "accepted_risk",
            "reviewed_at": "2026-07-24T10:00:00+09:00",
            "note": "검토함",
        }],
    }
    errors = human_review.check_audit_human_provenance(audit, ledger, "DOC_005")
    assert any("changed after review" in e or "hashes to" in e for e in errors), errors


def test_uid_for_different_finding_is_rejected():
    finding = _finding(review_uid="HR-0000000000000000")
    audit = _audit([finding])
    current = human_review.canonical_artifact_sha256(audit)
    ledger = {
        "case_id": "CASE_030",
        "records": [{
            "review_uid": "HR-0000000000000000",
            "reviewer": "홍길동",
            "artifact_kind": "policy_audit_finding",
            "artifact_id": "DOC_005",
            "target_key": "PA-9999999999999999",  # a different finding
            "artifact_sha256": current,
            "decision": "accepted_risk",
            "reviewed_at": "2026-07-24T10:00:00+09:00",
            "note": "검토함",
        }],
    }
    errors = human_review.check_audit_human_provenance(audit, ledger, "DOC_005")
    assert any("recorded for target" in e for e in errors), errors


def test_valid_dao_recorded_reference_passes():
    finding = _finding(review_uid="PLACEHOLDER")
    audit = _audit([finding])
    # The record binds to the canonical (UID-stripped) hash, so compute it with
    # the placeholder in place -- stripping makes the placeholder irrelevant.
    current = human_review.canonical_artifact_sha256(audit)
    review_uid = human_review.compute_review_uid(
        "CASE_030", "policy_audit_finding", "DOC_005",
        "PA-1111111111111111", current, "2026-07-24T10:00:00+09:00")
    audit["findings"][0]["human_review_uid"] = review_uid
    ledger = {
        "case_id": "CASE_030",
        "records": [{
            "review_uid": review_uid,
            "reviewer": "홍길동",
            "artifact_kind": "policy_audit_finding",
            "artifact_id": "DOC_005",
            "target_key": "PA-1111111111111111",
            "artifact_sha256": current,
            "decision": "accepted_risk",
            "reviewed_at": "2026-07-24T10:00:00+09:00",
            "note": "검토함",
        }],
    }
    errors = human_review.check_audit_human_provenance(audit, ledger, "DOC_005")
    assert errors == [], errors


# ---- unpaged administrative exclusion: same rule -------------------------

def _coverage_with_unpaged(review_uid):
    return {
        "case_id": "CASE_030",
        "parent_document_id": "DOC_001",
        "unpaged_physical_pages": [{
            "physical_page": 3,
            "disposition": "administrative_excluded",
            "reason": "뒤표지",
            "verified_by": "홍길동",
            "verified_at": "2026-07-24T10:00:00+09:00",
            "human_review_uid": review_uid,
        }],
    }


def test_unpaged_without_uid_is_schema_rejected():
    data = _coverage_with_unpaged(None)
    # Minimal shape: schema requires more top-level fields, but the unpaged
    # conditional fires on disposition regardless -- assert the uid requirement.
    errors = dao._schema_check(data, "policy_parent_coverage.schema.json")
    assert any("human_review_uid" in e for e in errors), errors


def test_unpaged_nonexistent_uid_is_rejected():
    data = _coverage_with_unpaged("HR-ffffffffffffffff")
    ledger = {"case_id": "CASE_030", "records": []}
    errors = human_review.check_unpaged_human_provenance(data, ledger, "DOC_001")
    assert any("does not exist" in e for e in errors), errors


def test_unpaged_valid_reference_passes():
    data = _coverage_with_unpaged("PLACEHOLDER")
    current = human_review.canonical_artifact_sha256(data)
    review_uid = human_review.compute_review_uid(
        "CASE_030", "unpaged_physical_exclusion", "DOC_001",
        "physical:3", current, "2026-07-24T10:00:00+09:00")
    data["unpaged_physical_pages"][0]["human_review_uid"] = review_uid
    ledger = {
        "case_id": "CASE_030",
        "records": [{
            "review_uid": review_uid,
            "reviewer": "홍길동",
            "artifact_kind": "unpaged_physical_exclusion",
            "artifact_id": "DOC_001",
            "target_key": "physical:3",
            "artifact_sha256": current,
            "decision": "verified",
            "reviewed_at": "2026-07-24T10:00:00+09:00",
            "note": "표지 확인",
        }],
    }
    errors = human_review.check_unpaged_human_provenance(data, ledger, "DOC_001")
    assert errors == [], errors


def test_rejected_decision_never_satisfies_reference():
    """A 'rejected' human review is recorded but does not clear the gate."""
    data = _coverage_with_unpaged("PLACEHOLDER")
    current = human_review.canonical_artifact_sha256(data)
    review_uid = human_review.compute_review_uid(
        "CASE_030", "unpaged_physical_exclusion", "DOC_001",
        "physical:3", current, "2026-07-24T10:00:00+09:00")
    data["unpaged_physical_pages"][0]["human_review_uid"] = review_uid
    ledger = {
        "case_id": "CASE_030",
        "records": [{
            "review_uid": review_uid,
            "reviewer": "홍길동",
            "artifact_kind": "unpaged_physical_exclusion",
            "artifact_id": "DOC_001",
            "target_key": "physical:3",
            "artifact_sha256": current,
            "decision": "rejected",
            "reviewed_at": "2026-07-24T10:00:00+09:00",
            "note": "실제로는 규범 페이지임",
        }],
    }
    errors = human_review.check_unpaged_human_provenance(data, ledger, "DOC_001")
    assert any("only 'verified'" in e for e in errors), errors


# ---- end-to-end: the DAO command records a hash-bound review --------------

def test_record_human_review_binds_hash_and_round_trips(isolated_dao, make_args):
    """The genuine workflow: the finding is reviewed while still `open`
    (schema-valid), the DAO records the review binding the canonical hash it
    computes itself, then the agent flips status to accepted_risk and writes
    the returned UID. The disposition-stripped hash is invariant to that
    transition, so the record stays valid."""
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    finding = _finding(status="open")
    audit = _audit([finding])
    _write_json(out / "policy_audit_result_DOC_005.json", audit)

    rc = dao.cmd_record_human_review(make_args(
        case_id="CASE_030", artifact_kind="policy_audit_finding",
        artifact_id="DOC_005", target_key="PA-1111111111111111",
        decision="accepted_risk", reviewer="홍길동",
        note="수동 검토 후 잔여 위험 수용", run_id="RUN_20260724_001",
        held_by="test-agent"))
    assert rc == 0

    ledger = dao.load_human_review_ledger("CASE_030")
    assert ledger is not None and len(ledger["records"]) == 1
    record = ledger["records"][0]
    # The DAO computed the hash itself, binding it to the canonical bytes.
    assert record["artifact_sha256"] == human_review.canonical_artifact_sha256(audit)
    assert record["decision"] == "accepted_risk"

    # The agent now flips the finding to accepted_risk and writes the UID.
    resolved = _finding(status="accepted_risk", actor="human",
                        review_uid=record["review_uid"])
    resolved_audit = _audit([resolved])
    errors = human_review.check_audit_human_provenance(
        resolved_audit, ledger, "DOC_005")
    assert errors == [], errors
