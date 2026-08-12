"""Checkpoint 2 across a case's documents, with document-level parallelism.

Redaction already ran PAGES concurrently, but documents were driven one
`redact_document.py` invocation at a time -- the same sequential pattern
`run_document_stage` was built to remove for checkpoint 1. These cover the
selector (which documents the stage should touch) and the isolation property
that matters when it runs them together: one document failing must not take
down the rest, or a case is only as complete as its worst document.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import run_document_stage as rds  # noqa: E402


def _doc(doc_id: str, **kw) -> dict:
    base = {"document_id": doc_id, "ocr_status": "completed",
            "redacted_text_path": None}
    base.update(kw)
    return base


class TestSelectRedactionDocuments:
    def test_takes_a_completed_unredacted_document(self) -> None:
        manifest = {"documents": [_doc("DOC_001")]}
        assert [d["document_id"] for d in
                rds.select_redaction_documents(manifest)] == ["DOC_001"]

    def test_skips_already_redacted(self) -> None:
        """Re-redacting is not merely wasteful -- it re-pays a provider call
        for a page whose result is already committed."""
        manifest = {"documents": [
            _doc("DOC_001", redacted_text_path="data/processed/x/DOC_001/redacted_text.md"),
            _doc("DOC_002"),
        ]}
        assert [d["document_id"] for d in
                rds.select_redaction_documents(manifest)] == ["DOC_002"]

    def test_skips_superseded_bundle(self) -> None:
        """Its children carry its pages; redacting it would process every page
        twice and produce a document nothing downstream should read.

        The bundle is identified by `downstream_disposition`. This test used to
        set `segmentation_status="superseded_bundle"` -- not a permitted value
        of that field -- and still passed, because a real bundle is also
        excluded by the later ocr_status/redacted_text_path filters. The dead
        line therefore looked like it worked here while the identical mistake
        in select_documents was actively breaking checkpoint 1.
        """
        manifest = {"documents": [
            _doc("DOC_005", downstream_disposition="superseded_bundle"),
            _doc("DOC_006"),
        ]}
        assert [d["document_id"] for d in
                rds.select_redaction_documents(manifest)] == ["DOC_006"]

    def test_bundle_skip_does_not_depend_on_the_later_filters(self) -> None:
        """The disposition alone must exclude it.

        Constructed so every other filter would ADMIT the document: OCR
        completed, no redacted_text_path yet. Only the disposition can
        exclude it, so this fails if the bundle test is dropped or points at
        the wrong field again.
        """
        manifest = {"documents": [
            _doc("DOC_005", downstream_disposition="superseded_bundle",
                 ocr_status="completed", redacted_text_path=None),
        ]}
        assert rds.select_redaction_documents(manifest) == []

    def test_skips_expert_review_only(self) -> None:
        """checkpoint 2 is not applicable to non-text visual evidence, and
        feeding the raw image to the text redactor is what that disposition
        exists to forbid."""
        manifest = {"documents": [
            _doc("DOC_001", downstream_disposition="expert_review_only"),
        ]}
        assert rds.select_redaction_documents(manifest) == []

    def test_skips_failed_ocr(self) -> None:
        """No page text exists to redact."""
        manifest = {"documents": [_doc("DOC_001", ocr_status="failed")]}
        assert rds.select_redaction_documents(manifest) == []

    def test_only_filter(self) -> None:
        manifest = {"documents": [_doc("DOC_001"), _doc("DOC_002")]}
        got = rds.select_redaction_documents(manifest, only=["DOC_002"])
        assert [d["document_id"] for d in got] == ["DOC_002"]


class TestRunRedactionStage:
    @pytest.fixture
    def two_docs(self, monkeypatch):
        manifest = {"documents": [_doc("DOC_001"), _doc("DOC_002")]}
        monkeypatch.setattr(rds._dao, "read_contract_data",
                            lambda case_id, name: manifest)
        monkeypatch.setattr(rds.redact_document_mod, "_redactor_for",
                            lambda *a, **k: object())
        return manifest

    def test_runs_every_selected_document(self, two_docs, monkeypatch) -> None:
        seen = []
        monkeypatch.setattr(
            rds.redact_document_mod, "redact_document",
            lambda case_id, doc_id, *a, **k: seen.append(doc_id) or {"pages": 3})
        result = rds.run_redaction_stage(
            "CASE_999", "t", "RUN_20260812_001", doc_workers=2,
            progress=lambda _m: None)
        assert sorted(seen) == ["DOC_001", "DOC_002"]
        assert result["status"] == "success"
        assert result["blocked"] == []

    def test_one_failure_does_not_stop_the_others(self, two_docs, monkeypatch) -> None:
        """The property that makes running them together safe.

        A document blocked by the cross-validation gate or a detected leak must
        report itself while its siblings finish -- otherwise a case is only as
        complete as its worst document, and the parallelism costs correctness.
        """
        def flaky(case_id, doc_id, *a, **k):
            if doc_id == "DOC_001":
                raise RuntimeError("checkpoint 2 blocked: simulated")
            return {"pages": 3}

        monkeypatch.setattr(rds.redact_document_mod, "redact_document", flaky)
        result = rds.run_redaction_stage(
            "CASE_999", "t", "RUN_20260812_001", doc_workers=2,
            progress=lambda _m: None)

        assert result["status"] == "partial"
        assert [b["doc_id"] for b in result["blocked"]] == ["DOC_001"]
        ok = [d for d in result["documents"] if d["status"] == "success"]
        assert [d["doc_id"] for d in ok] == ["DOC_002"], (
            "the healthy document must still complete"
        )

    def test_sequential_mode_matches(self, two_docs, monkeypatch) -> None:
        """doc_workers=1 restores the strictly sequential loop with the same
        result shape, so the parallel path is a wall-time change only."""
        monkeypatch.setattr(rds.redact_document_mod, "redact_document",
                            lambda *a, **k: {"pages": 3})
        result = rds.run_redaction_stage(
            "CASE_999", "t", "RUN_20260812_001", doc_workers=1,
            progress=lambda _m: None)
        assert result["status"] == "success"
        assert result["document_workers"] == 1
        assert len(result["documents"]) == 2

    def test_no_eligible_documents_is_success(self, monkeypatch) -> None:
        monkeypatch.setattr(rds._dao, "read_contract_data",
                            lambda case_id, name: {"documents": []})
        result = rds.run_redaction_stage(
            "CASE_999", "t", "RUN_20260812_001", progress=lambda _m: None)
        assert result["status"] == "success"
        assert "no documents required" in result["note"]
