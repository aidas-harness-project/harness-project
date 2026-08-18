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


# --- the driver that runs the stage, not just the gate that closes it -------
#
# The gate above and the driver below must agree on one case. They did not:
# `_policy_completion_blockers` accepted a declaration while the driver
# refused the same case outright, so a declared case could never reach the
# gate willing to pass it (found on CASE_050, 2026-08-18 -- 56 documents, no
# 약관). Testing the DAO alone is what let that through, so these assert the
# driver's own verdict.

def _driver_reads(*, run_state_stage="policy_clause_processing", documents,
                  declaration):
    """A `_dao_json` stub returning one case's reads, declaration included."""
    def fake_read(args, *, allow_missing=False):
        if args[0] == "read-contract" and args[2] == "_run_state.json":
            return {"stages": [{"stage_name": run_state_stage,
                                "status": "in_progress"}]}
        if args[0] == "read-contract" and args[2] == "document_manifest.json":
            return {"case_id": CASE, "documents": documents}
        if args[0] == "read-contract" and args[2] == "_no_policy_documents.json":
            return declaration
        raise AssertionError(args)
    return fake_read


def test_driver_passes_a_declared_case_the_dao_gate_would_also_pass(monkeypatch):
    """Reintroducing the defect -- dropping the declaration read, so the
    driver sees only the manifest -- turns this into the RuntimeError below."""
    import run_policy_pipeline_driver as driver

    documents = [{"document_id": "DOC_055",
                  "document_type": "insurance_certificate",
                  "downstream_disposition": "text_only_no_normalization"}]
    monkeypatch.setattr(driver, "_dao_json", _driver_reads(
        documents=documents, declaration={"reviewer": "pyun", "note": "no 약관"}))
    monkeypatch.setattr(driver, "_build_document_index",
                        lambda case_id, held_by, run_id: {"status": "built"})

    result = driver.run(case_id=CASE, held_by="orchestrator", run_id=RUN)
    assert result["status"] == "noop_no_policy_documents"
    assert result["text_only_policy_count"] == 0


def test_driver_still_blocks_an_undeclared_case_with_no_policy(monkeypatch):
    """The declaration is the whole difference: without one, 'no policy text'
    remains indistinguishable from skipped policy work and must block."""
    import run_policy_pipeline_driver as driver

    documents = [{"document_id": "DOC_055",
                  "document_type": "insurance_certificate",
                  "downstream_disposition": "text_only_no_normalization"}]
    monkeypatch.setattr(driver, "_dao_json", _driver_reads(
        documents=documents, declaration=None))

    with pytest.raises(RuntimeError, match="no active text-processed insurance policy"):
        driver.run(case_id=CASE, held_by="orchestrator", run_id=RUN)


def test_driver_processes_policy_documents_rather_than_taking_the_waiver(monkeypatch):
    """A declaration must never short-circuit a case that HAS policy text --
    the waiver branch is reachable only when the manifest has none."""
    import run_policy_pipeline_driver as driver

    documents = [{"document_id": "DOC_014", "document_type": "insurance_policy",
                  "downstream_disposition": "text_only_no_normalization"}]
    reads = _driver_reads(documents=documents,
                          declaration={"reviewer": "pyun", "note": "stale"})

    def fake_read(args, *, allow_missing=False):
        if args[0] == "read-driver-receipt":
            return None
        return reads(args, allow_missing=allow_missing)

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    monkeypatch.setattr(driver, "_dao_write", lambda args: None)
    monkeypatch.setattr(driver, "_build_document_index",
                        lambda case_id, held_by, run_id: {"status": "built"})

    result = driver.run(case_id=CASE, held_by="orchestrator", run_id=RUN)
    assert result["status"] == "noop"
    assert result["text_only_policy_count"] == 1
