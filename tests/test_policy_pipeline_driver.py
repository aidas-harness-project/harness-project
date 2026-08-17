"""Policy zero-normalization driver tests use no case files or providers."""
from __future__ import annotations

import json

import run_policy_pipeline_driver as driver


CASE, RUN = "CASE_9001", "RUN_20260813_1"


def _policy(doc_id, disposition):
    return {"document_id": doc_id, "document_type": "insurance_policy",
            "downstream_disposition": disposition}


def test_policy_documents_separates_opt_in_normalization():
    text_only, normalized = driver.policy_documents({"documents": [
        _policy("DOC_002", "text_only_no_normalization"),
        _policy("DOC_001", "automated_text_pipeline"),
        {"document_id": "DOC_003", "document_type": "other", "downstream_disposition": "text_only_no_normalization"},
    ]})

    assert [doc["document_id"] for doc in text_only] == ["DOC_002"]
    assert [doc["document_id"] for doc in normalized] == ["DOC_001"]


def test_zero_normalization_writes_receipt_without_model(monkeypatch):
    writes = {}
    manifest = {"documents": [_policy("DOC_001", "text_only_no_normalization")]}

    def fake_read(args, *, allow_missing=False):
        if args[0] == "read-contract" and args[2] == "_run_state.json":
            return {"stages": [{"stage_name": "policy_clause_processing", "status": "in_progress"}]}
        if args[0] == "read-contract" and args[2] == "document_manifest.json":
            return manifest
        if args[0] == "read-driver-receipt":
            return None
        raise AssertionError(args)

    def fake_write(args):
        writes["receipt"] = json.loads(open(args[args.index("--data-file") + 1], encoding="utf-8").read())

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    monkeypatch.setattr(driver, "_dao_write", fake_write)
    monkeypatch.setattr(driver, "_build_document_index",
                        lambda *a, **k: {"status": "built", "detail": "stub"})
    assert driver.run(case_id=CASE, held_by="policy-pipeline", run_id=RUN) == {
        "status": "noop", "text_only_policy_count": 1,
        "document_index": {"status": "built", "detail": "stub"},
    }
    assert writes["receipt"]["completed_contracts"] == []


def test_a_failed_index_build_does_not_fail_the_stage(monkeypatch):
    """The index is advisory: no contract references it, no gate reads it, and
    an agent without one falls back to the chunk scan it did before. A table
    detector crashing must not cost the stage its completion."""
    manifest = {"documents": [_policy("DOC_001", "text_only_no_normalization")]}

    def fake_read(args, *, allow_missing=False):
        if args[0] == "read-contract" and args[2] == "_run_state.json":
            return {"stages": [{"stage_name": "policy_clause_processing",
                                "status": "in_progress"}]}
        if args[0] == "read-contract" and args[2] == "document_manifest.json":
            return manifest
        if args[0] == "read-driver-receipt":
            return None
        raise AssertionError(args)

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    monkeypatch.setattr(driver, "_dao_write", lambda args: None)
    monkeypatch.setattr(driver, "_build_document_index",
                        lambda *a, **k: {"status": "failed", "detail": "boom"})

    result = driver.run(case_id=CASE, held_by="policy-pipeline", run_id=RUN)

    assert result["status"] == "noop"
    assert result["document_index"]["status"] == "failed"


def test_the_index_is_built_before_the_receipt_reuse_branch(monkeypatch):
    """A resumed run returns early on a matching receipt. Building the index
    after that branch would skip it on exactly the runs most likely to lack
    one -- and because the index is advisory, nothing downstream would report
    the absence."""
    calls = []
    manifest = {"documents": [_policy("DOC_001", "text_only_no_normalization")]}
    receipt = {"matches": True}

    def fake_read(args, *, allow_missing=False):
        if args[0] == "read-contract" and args[2] == "_run_state.json":
            return {"stages": [{"stage_name": "policy_clause_processing",
                                "status": "in_progress"}]}
        if args[0] == "read-contract" and args[2] == "document_manifest.json":
            return manifest
        if args[0] == "read-driver-receipt":
            return receipt
        raise AssertionError(args)

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    monkeypatch.setattr(driver.driver_runtime, "receipt_matches",
                        lambda *a, **k: True)
    monkeypatch.setattr(driver, "_build_document_index",
                        lambda *a, **k: calls.append("built") or
                        {"status": "built", "detail": ""})

    result = driver.run(case_id=CASE, held_by="policy-pipeline", run_id=RUN)

    assert result["status"] == "reused"
    assert calls == ["built"], "a reused run must still refresh the index"


def test_normalized_policy_is_explicitly_blocked(monkeypatch):
    """A manifest still recording the retired `automated_text_pipeline` value
    must hard-block, not finalize over the discrepancy.

    Unreachable for anything intaken after 2026-08-04, but live for the four
    legacy cases (CASE_112 is still forked from). Silently passing here would
    reproduce CASE_112's `manual_override`: a stage reading `passed` with the
    obligation neither met nor withdrawn. The message changed when
    normalization was retired; the refusal did not.
    """
    def fake_read(args, *, allow_missing=False):
        if args[0] == "read-contract" and args[2] == "_run_state.json":
            return {"stages": [{"stage_name": "policy_clause_processing", "status": "in_progress"}]}
        if args[0] == "read-contract" and args[2] == "document_manifest.json":
            return {"documents": [_policy("DOC_001", "automated_text_pipeline")]}
        raise AssertionError(args)

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    with __import__("pytest").raises(RuntimeError, match="normalization is retired"):
        driver.run(case_id=CASE, held_by="policy-pipeline", run_id=RUN)
