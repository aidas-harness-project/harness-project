"""Durable idempotency contract for medical-review mutations."""
from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest

import dao
import medical_review_ledger


MUTATION_COMMANDS = (
    ["open-medical-review-item", "CASE_9001", "--issue-id", "MCI_0001", "--decision-owner", "human", "--held-by", "tester", "--run-id", "RUN_20260723_001"],
    ["record-medical-referral-decision", "CASE_9001", "MRI_0001", "decision.json", "--held-by", "tester", "--run-id", "RUN_20260723_001"],
    ["provide-medical-review-information", "CASE_9001", "MRI_0001", "--reason", "new evidence", "--held-by", "tester", "--run-id", "RUN_20260723_001"],
    ["transition-medical-review", "CASE_9001", "MRI_0001", "--action", "close", "--reason", "complete", "--held-by", "tester", "--run-id", "RUN_20260723_001"],
    ["reconcile-medical-review-waits", "CASE_9001", "--held-by", "tester", "--run-id", "RUN_20260723_001"],
    ["set-ledger-status", "CASE_9001", "document.pdf", "approved", "--reviewer", "human", "--held-by", "tester", "--run-id", "RUN_20260723_001"],
    ["add-conflict-entry", "CASE_9001", "--stage", "claim_analysis", "--topic", "diagnosis", "--sources-file", "sources.json", "--held-by", "tester", "--run-id", "RUN_20260723_001"],
    ["set-conflict-verdict", "CASE_9001", "CONFLICT_1", "resolved", "--note", "reviewed", "--held-by", "tester", "--run-id", "RUN_20260723_001"],
)


@pytest.mark.parametrize("argv", MUTATION_COMMANDS)
def test_medical_mutation_cli_requires_operation_id(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["dao.py", *argv])
    with pytest.raises(SystemExit) as exited:
        dao.main()
    assert exited.value.code == 2


def test_medical_operation_fingerprint_is_stable_and_rejects_collisions():
    args = SimpleNamespace(
        case_id="CASE_9001",
        run_id="RUN_20260723_001",
        review_item_id="MRI_0001",
        issue_id=None,
        decision_owner=None,
        reason="completed",
        operation_id="medical:test-close-0001",
    )
    actor = {
        "actor_id": "coordinator-1",
        "operator_actor_id": "OP_TEST",
        "attested_at": "2026-08-10T01:00:00+09:00",
    }
    ledger = {"events": []}

    request_sha = medical_review_ledger.medical_operation_request_sha256(
        dao, args, actor, "close", None, ledger,
    )
    actor["attested_at"] = "2026-08-10T02:00:00+09:00"
    assert medical_review_ledger.medical_operation_request_sha256(
        dao, args, actor, "close", None, ledger,
    ) == request_sha

    result = {
        "action": "close",
        "review_item_id": "MRI_0001",
        "state": "closed",
        "decision_id": None,
    }
    ledger["events"].append({
        "operation_id": args.operation_id,
        "operation_request_sha256": request_sha,
        "operation_result": result,
    })
    assert medical_review_ledger.committed_medical_operation_result(
        ledger, args.operation_id, request_sha,
    ) == result

    args.reason = "different request"
    with pytest.raises(ValueError, match="different medical request"):
        medical_review_ledger.medical_operation_request_sha256(
            dao, args, actor, "close", None, ledger,
        )
