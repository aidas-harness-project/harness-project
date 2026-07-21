"""Stage-2 segmentation preflight and its human review write path.

Every test uses the isolated DAO roots from conftest.py; no real case data or
pipeline output is read or written.
"""
import json
import threading
import time

import dao


def _seed(base, documents, case_id="CASE_930"):
    out_dir = base / "outputs" / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(out_dir / "document_manifest.json", {
        "case_id": case_id,
        "created_at": dao.now_iso(),
        "documents": documents,
    })
    return out_dir / "document_manifest.json"


def _pdf(doc_id, **extra):
    document = {
        "document_id": doc_id,
        "file_name": f"{doc_id}.pdf",
        "file_path": f"data/raw/CASE_930/{doc_id}.pdf",
        "file_format": "pdf",
        "file_size_bytes": 100,
        "ocr_status": "pending",
    }
    document.update(extra)
    return document


def test_legacy_pdf_without_status_fails_closed(isolated_dao):
    _seed(isolated_dao, [_pdf("DOC_001")])
    result = dao.check_segmentation_ready("CASE_930", "DOC_001")
    assert result["clear"] is False
    assert result["blockers"] == [{
        "document_id": "DOC_001",
        "segmentation_status": "pending_review",
        "reason": "PDF bundle decision has not been reviewed",
    }]


def test_one_required_bundle_blocks_the_entire_case_stage2(isolated_dao):
    _seed(isolated_dao, [
        _pdf("DOC_001", segmentation_status="not_required",
             segmentation_reviewed_by="reviewer", segmentation_reviewed_at=dao.now_iso()),
        _pdf("DOC_002", segmentation_status="required",
             segmentation_reviewed_by="reviewer", segmentation_reviewed_at=dao.now_iso()),
    ])
    result = dao.check_segmentation_ready("CASE_930", "DOC_001")
    assert result["clear"] is False
    assert [b["document_id"] for b in result["blockers"]] == ["DOC_002"]


def test_human_not_required_decision_clears_preflight(isolated_dao):
    manifest_path = _seed(
        isolated_dao, [_pdf("DOC_001", segmentation_status="pending_review")]
    )
    ok, message = dao.set_segmentation_status(
        "CASE_930", "DOC_001", "not_required", "Kim", "single form",
        "tester", "RUN_20260721_001",
    )
    assert ok, message
    result = dao.check_segmentation_ready("CASE_930", "DOC_001")
    assert result["clear"] is True
    document = json.loads(manifest_path.read_text(encoding="utf-8"))["documents"][0]
    assert document["segmentation_status"] == "not_required"
    assert document["segmentation_reviewed_by"] == "Kim"
    assert document["segmentation_review_note"] == "single form"


def test_split_children_are_ready_but_superseded_bundle_is_not_a_valid_target(isolated_dao):
    proposal = "outputs/CASE_930/segmentation_proposal_DOC_001.json"
    _seed(isolated_dao, [
        _pdf(
            "DOC_001", segmentation_status="completed", ocr_status="not_applicable",
            downstream_disposition="superseded_bundle",
            segmentation_proposal_path=proposal, redacted_text_path=None,
        ),
        _pdf(
            "DOC_002", segmentation_status="completed", source_file_name="bundle.pdf",
            source_page_start=1, source_page_end=3,
            segmentation_proposal_path=proposal,
        ),
    ])
    assert dao.check_segmentation_ready("CASE_930", "DOC_002")["clear"] is True
    blocked = dao.check_segmentation_ready("CASE_930", "DOC_001")
    assert blocked["clear"] is False
    assert "superseded bundle" in blocked["blockers"][0]["reason"]


def test_non_pdf_is_not_subject_to_pdf_segmentation(isolated_dao):
    document = _pdf("DOC_001")
    document.update({
        "file_name": "DOC_001.txt", "file_path": "data/raw/CASE_930/DOC_001.txt",
        "file_format": "text",
    })
    _seed(isolated_dao, [document])
    assert dao.check_segmentation_ready("CASE_930", "DOC_001")["clear"] is True


def test_human_decision_reads_fresh_after_manifest_lock(isolated_dao, monkeypatch):
    """A split landing while the review command waits must be seen after the
    lock clears; the human command must not overwrite a superseded bundle."""
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 2.0)
    manifest_path = _seed(
        isolated_dao, [_pdf("DOC_001", segmentation_status="pending_review")]
    )
    dao.acquire_lock(manifest_path, "splitter", "RUN_SPLIT", "splitting bundle")

    def supersede_then_release():
        time.sleep(0.06)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["documents"][0].update({
            "segmentation_status": "completed",
            "downstream_disposition": "superseded_bundle",
            "ocr_status": "not_applicable",
            "redacted_text_path": None,
            "segmentation_proposal_path": (
                "outputs/CASE_930/segmentation_proposal_DOC_001.json"
            ),
        })
        dao.atomic_write_json(manifest_path, manifest)
        dao.release_lock(manifest_path)

    thread = threading.Thread(target=supersede_then_release)
    thread.start()
    ok, message = dao.set_segmentation_status(
        "CASE_930", "DOC_001", "not_required", "Kim", None,
        "reviewer", "RUN_REVIEW",
    )
    thread.join()

    assert not ok
    assert "already a superseded bundle" in message
    document = json.loads(manifest_path.read_text(encoding="utf-8"))["documents"][0]
    assert document["segmentation_status"] == "completed"
