"""Stage-2 segmentation preflight and its human review write path.

Every test uses the isolated DAO roots from conftest.py; no real case data or
pipeline output is read or written.
"""
import json
import threading

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


def test_an_unreviewed_pdf_no_longer_blocks_processing(isolated_dao):
    """There is nothing left to review before processing starts.

    The pre-decision existed because segmentation ran BEFORE OCR: an unsplit
    bundle would be OCR'd and classified as one document, and undoing that
    meant paying for OCR twice. With OCR first, a bundle is the normal thing to
    read -- pages are pages -- and classification happens after the split, so
    the failure this gate prevented cannot occur. Whether a PDF is a bundle is
    now an OUTPUT of segmentation (one proposed boundary means one document),
    not an input a human has to supply before anything can run.
    """
    _seed(isolated_dao, [_pdf("DOC_001")])
    assert dao.check_segmentation_ready("CASE_930", "DOC_001")["clear"] is True


def test_an_unsplit_bundle_no_longer_blocks_the_case(isolated_dao):
    """A bundle awaiting its split is not a reason to stop reading the case."""
    _seed(isolated_dao, [
        _pdf("DOC_001", segmentation_status="not_required"),
        _pdf("DOC_002", segmentation_status="required"),
    ])
    assert dao.check_segmentation_ready("CASE_930", "DOC_001")["clear"] is True
    assert dao.check_segmentation_ready("CASE_930", "DOC_002")["clear"] is True


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

    # The split must land while the review command is genuinely blocked on the
    # lock. Sleeping a fixed 0.06s against a 0.02s poll was a race the loser
    # side of which reads as a real failure: under load the reviewer could
    # acquire first and legitimately see a non-superseded bundle. Waiting for
    # the reviewer to actually start polling makes the ordering the test
    # asserts an established fact rather than a timing bet.
    reviewer_is_waiting = threading.Event()
    real_acquire = dao.acquire_lock

    def signal_once_blocked(target, *args, **kwargs):
        result = real_acquire(target, *args, **kwargs)
        if result is not None and target == manifest_path:
            reviewer_is_waiting.set()   # someone else holds it: we are queued
        return result

    monkeypatch.setattr(dao, "acquire_lock", signal_once_blocked)

    def supersede_then_release():
        assert reviewer_is_waiting.wait(timeout=5), \
            "reviewer never blocked on the manifest lock"
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
