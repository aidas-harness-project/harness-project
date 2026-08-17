"""D5: a case with no policy document may be DECLARED, never assumed.

Part of the PoC corpus arrives as a diagnosis certificate plus an insurer
letter with no 약관. Without a route through the policy-completeness gate,
`claim_analysis` can never start on such a case; with a global dev flag, the
same waiver would apply silently to cases whose policy work merely failed.
The allowance is therefore a per-case human declaration.
"""
from __future__ import annotations

import json

import pytest

import dao


CASE, RUN = "CASE_9200", "RUN_20260817_9"


def _case(tmp_path, monkeypatch, documents):
    root = tmp_path / "outputs" / CASE
    root.mkdir(parents=True)
    monkeypatch.setattr(dao, "case_dir", lambda case_id: tmp_path / "outputs" / case_id)
    (root / "document_manifest.json").write_text(
        json.dumps({"case_id": CASE, "documents": documents}, ensure_ascii=False),
        encoding="utf-8")
    return root


def test_declaration_lets_the_policy_gate_clear_that_one_blocker(tmp_path, monkeypatch):
    root = _case(tmp_path, monkeypatch, [
        {"document_id": "DOC_001", "document_type": "insurer_response",
         "downstream_disposition": "automated_text_pipeline"},
        {"document_id": "DOC_002", "document_type": "diagnosis_certificate",
         "downstream_disposition": "automated_text_pipeline"},
    ])
    # Before: the gate names the blocker AND the command that resolves it --
    # a refusal a caller cannot act on is how a gate gets routed around.
    blockers = dao._policy_completion_blockers(CASE)
    assert blockers and "no text-processed insurance_policy" in blockers[0]
    assert "declare-no-policy-documents" in blockers[0]

    ok, message = dao.declare_no_policy_documents(
        CASE, reviewer="pyun", note="corpus case ships without 약관",
        held_by="orchestrator", run_id=RUN)
    assert ok, message
    assert (root / "_no_policy_documents.json").exists()
    assert dao._policy_completion_blockers(CASE) == []


def test_declaration_is_refused_when_the_case_does_carry_a_policy(tmp_path, monkeypatch):
    """The waiver must never become 'skip policy processing'."""
    _case(tmp_path, monkeypatch, [
        {"document_id": "DOC_009", "document_type": "insurance_policy",
         "downstream_disposition": "automated_text_pipeline"},
    ])
    ok, message = dao.declare_no_policy_documents(
        CASE, reviewer="pyun", note="x", held_by="orchestrator", run_id=RUN)
    assert not ok
    assert "DOES carry policy documents" in message


def test_a_stale_declaration_blocks_instead_of_persisting(tmp_path, monkeypatch):
    """Declared first, policy documents added later: the declaration is now a
    false statement about the case, so the gate must refuse rather than let it
    stand."""
    root = _case(tmp_path, monkeypatch, [
        {"document_id": "DOC_001", "document_type": "insurer_response",
         "downstream_disposition": "automated_text_pipeline"},
    ])
    assert dao.declare_no_policy_documents(
        CASE, reviewer="pyun", note="none at intake",
        held_by="orchestrator", run_id=RUN)[0]
    assert dao._policy_completion_blockers(CASE) == []

    (root / "document_manifest.json").write_text(json.dumps({
        "case_id": CASE,
        "documents": [{"document_id": "DOC_009",
                       "document_type": "insurance_policy",
                       "downstream_disposition": "automated_text_pipeline"}],
    }, ensure_ascii=False), encoding="utf-8")
    blockers = dao._policy_completion_blockers(CASE)
    assert blockers and "stale" in blockers[0]


def test_declaration_requires_a_named_reviewer_and_a_reason(tmp_path, monkeypatch):
    """It is a human judgment, so it carries attribution. Without this the
    waiver is indistinguishable from an automated bypass."""
    _case(tmp_path, monkeypatch, [
        {"document_id": "DOC_001", "document_type": "insurer_response",
         "downstream_disposition": "automated_text_pipeline"},
    ])
    ok, message = dao.declare_no_policy_documents(
        CASE, reviewer="  ", note="x", held_by="o", run_id=RUN)
    assert not ok and "--reviewer" in message
    ok, message = dao.declare_no_policy_documents(
        CASE, reviewer="pyun", note="", held_by="o", run_id=RUN)
    assert not ok and "--note" in message
