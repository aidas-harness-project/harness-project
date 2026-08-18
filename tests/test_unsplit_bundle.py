"""Withdrawing a split so a bundle can be re-segmented.

`replace_manifest_documents` only appends, and `reset_document_processing`
refuses a segmented case outright ("a segmented child cannot be reconstructed
from intake-owned fields alone"). Correct for a cold reset of the whole stage,
but it left no way to redo just the split.

Encountered on CASE_047 (2026-08-18): a fork inherited CASE_050's 55 children,
an improved boundary rule proposed 30 different ones, and `split` refused with
`inconsistent_existing_split`. The alternatives were re-running 97 OCR calls on
a fresh case, or keeping boundaries already known to be wrong.

Because this DELETES manifest entries, most of what follows asserts on what it
REFUSES to do.
"""
from __future__ import annotations

import json

import dao


CASE, RUN, BUNDLE = "CASE_9300", "RUN_20260818_9", "DOC_001"


def _manifest(isolated_dao, documents):
    root = dao.case_dir(CASE)
    root.mkdir(parents=True, exist_ok=True)
    (root / "document_manifest.json").write_text(
        json.dumps({"case_id": CASE, "documents": documents}, ensure_ascii=False),
        encoding="utf-8")
    return root / "document_manifest.json"


# Modelled on CASE_047's real entries: the schema conditionally requires most
# of these, and several reject null, so a minimal dict does not validate.
_PROPOSAL = f"outputs/{CASE}/segmentation_proposal_DOC_001.json"


def _bundle(**overrides):
    entry = {
        "document_id": BUNDLE, "file_name": "DOC_001.pdf",
        "file_path": f"data/raw/{CASE}/DOC_001.pdf", "file_format": "pdf",
        "file_size_bytes": 13523848, "pre_flagged_type": None, "pages": 97,
        "ocr_status": "not_applicable", "segmentation_status": "completed",
        "segmentation_reviewed_by": None, "segmentation_reviewed_at": None,
        "segmentation_review_note": None, "ocr_text_path": None,
        "ocr_quality": "low", "uncertain_region_count": 0,
        "cross_validation_status": "single_reader_no_cross_validation",
        "redacted_text_path": None, "document_type": None,
        "classification_confidence": None, "source_total_pages": 97,
        "extraction_method": "ocr", "non_text_verification": None,
        "downstream_disposition": "superseded_bundle",
        "segmentation_proposal_path": _PROPOSAL,
    }
    entry.update(overrides)
    return entry


def _child(doc_id, start, end, **overrides):
    entry = {
        "document_id": doc_id, "file_name": f"{doc_id}.pdf",
        "document_role": "physical", "file_path": None, "file_format": "pdf",
        "file_size_bytes": None, "pre_flagged_type": None,
        "provisional_document_type": None, "source_file_name": "DOC_001.pdf",
        "source_page_start": start, "source_page_end": end,
        "segmentation_proposal_path": _PROPOSAL,
        "segmentation_status": "completed", "segmentation_reviewed_by": None,
        "segmentation_reviewed_at": None, "segmentation_review_note": None,
        "pages": end - start + 1, "ocr_status": "completed",
        "ocr_text_path": None, "ocr_quality": "low",
        "uncertain_region_count": 0,
        "cross_validation_status": "single_reader_no_cross_validation",
        "redacted_text_path": f"data/processed/{CASE}/{doc_id}/redacted_text.md",
        "document_type": "receipt", "classification_confidence": 0.95,
        "source_total_pages": 97, "extraction_method": "ocr",
        "downstream_disposition": "automated_text_pipeline",
        "non_text_verification": None,
    }
    entry.update(overrides)
    return entry


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_children_are_removed_and_the_bundle_becomes_splittable_again(isolated_dao):
    path = _manifest(isolated_dao, [
        _bundle(),
        _child("DOC_002", 1, 10),
        _child("DOC_003", 11, 20),
    ])
    ok, message = dao.unsplit_bundle(CASE, BUNDLE, held_by="orchestrator",
                                     run_id=RUN, confirm_case_id=CASE)
    assert ok, message

    documents = _read(path)["documents"]
    assert [d["document_id"] for d in documents] == [BUNDLE]
    bundle = documents[0]
    # Splittable again: a re-propose has to be able to run, and `split` refuses
    # a retained superseded_bundle.
    assert bundle["downstream_disposition"] == "automated_text_pipeline"
    assert bundle["segmentation_status"] == "required"
    # The re-decision is attributable rather than blanked: the schema requires
    # it, and a withdrawn split is a human bundle/non-bundle call being made
    # again -- crediting the original reviewer would be a false record.
    assert bundle["segmentation_reviewed_by"] == "orchestrator"
    assert bundle["segmentation_reviewed_at"]
    assert "unsplit-bundle" in bundle["segmentation_review_note"]
    # The approved proposal described the boundaries just removed.
    assert bundle["segmentation_proposal_path"] is None
    # The expensive part survives -- keeping it is the entire reason this
    # command exists rather than re-intaking the case.
    assert bundle["pages"] == 97


def test_an_unrelated_bundle_and_its_children_are_untouched(isolated_dao):
    """Only the named bundle's children go. A case with two bundles must keep
    the other one intact."""
    path = _manifest(isolated_dao, [
        _bundle(),
        _child("DOC_002", 1, 10),
        _bundle(document_id="DOC_010", file_name="DOC_010.pdf", pages=20),
        _child("DOC_011", 1, 20, source_file_name="DOC_010.pdf",
               document_type="medical_record"),
    ])
    ok, message = dao.unsplit_bundle(CASE, BUNDLE, held_by="orchestrator",
                                     run_id=RUN, confirm_case_id=CASE)
    assert ok, message
    remaining = [d["document_id"] for d in _read(path)["documents"]]
    assert remaining == [BUNDLE, "DOC_010", "DOC_011"]


def test_a_document_that_was_never_split_is_refused(isolated_dao):
    """Asking to unsplit an ordinary document means the caller is confused
    about which document they are holding -- there are no children to remove."""
    path = _manifest(isolated_dao, [
        _bundle(downstream_disposition="automated_text_pipeline"),
        _child("DOC_002", 1, 10),
    ])
    before = _read(path)
    ok, message = dao.unsplit_bundle(CASE, BUNDLE, held_by="orchestrator",
                                     run_id=RUN, confirm_case_id=CASE)
    assert not ok
    assert "not a superseded_bundle" in message
    assert _read(path) == before


def test_a_nested_bundle_child_is_refused_rather_than_orphaned(isolated_dao):
    """A child that is itself a bundle owns children of its own; deleting it
    would leave them pointing at nothing."""
    path = _manifest(isolated_dao, [
        _bundle(),
        _child("DOC_002", 1, 10, downstream_disposition="superseded_bundle"),
        _child("DOC_003", 1, 5, source_file_name="DOC_002.pdf"),
    ])
    before = _read(path)
    ok, message = dao.unsplit_bundle(CASE, BUNDLE, held_by="orchestrator",
                                     run_id=RUN, confirm_case_id=CASE)
    assert not ok
    assert "themselves superseded bundles" in message
    assert _read(path) == before


def test_a_child_referenced_by_another_document_is_refused(isolated_dao):
    """`source_document_id` is a real derivation edge; removing its target
    silently orphans the deriving entry."""
    path = _manifest(isolated_dao, [
        _bundle(),
        _child("DOC_002", 1, 10),
        _child("DOC_020", 1, 3, source_file_name="DOC_020.pdf",
               source_document_id="DOC_002"),
    ])
    before = _read(path)
    ok, message = dao.unsplit_bundle(CASE, BUNDLE, held_by="orchestrator",
                                     run_id=RUN, confirm_case_id=CASE)
    assert not ok
    assert "DOC_020" in message and "orphan" in message
    assert _read(path) == before


def test_the_destructive_scope_confirmation_is_enforced(isolated_dao):
    """Same guard reset_document_processing takes: the case id must be typed
    back, so a command aimed at the wrong case cannot delete anything."""
    path = _manifest(isolated_dao, [_bundle(), _child("DOC_002", 1, 10)])
    before = _read(path)
    ok, message = dao.unsplit_bundle(CASE, BUNDLE, held_by="orchestrator",
                                     run_id=RUN, confirm_case_id="CASE_0000")
    assert not ok
    assert "--confirm-case-id" in message
    assert _read(path) == before


def test_a_bundle_with_no_children_is_refused(isolated_dao):
    """Nothing to do, and reporting success would suggest a split was undone."""
    path = _manifest(isolated_dao, [_bundle()])
    before = _read(path)
    ok, message = dao.unsplit_bundle(CASE, BUNDLE, held_by="orchestrator",
                                     run_id=RUN, confirm_case_id=CASE)
    assert not ok
    assert "no children" in message
    assert _read(path) == before


def test_a_held_lock_blocks_the_removal(isolated_dao, monkeypatch):
    """P5: the manifest lock is taken before anything is read or written, and a
    lock held by someone else halts rather than proceeding. The poll bounds are
    shortened because the real ones wait 15 minutes."""
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.03)
    path = _manifest(isolated_dao, [_bundle(), _child("DOC_002", 1, 10)])
    dao.acquire_lock(path, "someone-else", "RUN_OTHER", "mid-write")
    try:
        ok, message = dao.unsplit_bundle(CASE, BUNDLE, held_by="orchestrator",
                                         run_id=RUN, confirm_case_id=CASE)
        assert not ok
        assert "LOCKED" in message and "someone-else" in message
        assert len(_read(path)["documents"]) == 2
    finally:
        dao.release_lock(path)
