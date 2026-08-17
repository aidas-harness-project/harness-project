"""A child that inherits the parent's pages gets no PDF of its own.

Under the post-2026-08-05 order the bundle is OCR'd and redacted BEFORE
segmentation, so each child is handed the parent's pages and nothing ever opens
a child file: run_document_stage.select_documents skips any entry whose
ocr_status is already completed, which an inheriting child always is.

Writing them anyway cost 44MB against an 11.4MB intake on CASE_142. The
arithmetic is the giveaway: every single-page child of the 19-page DOC_005
weighed 1.34MB -- the same as the whole parent -- because page selection keeps
the parent's unreferenced fonts and images and save() was not asked to collect
them. Twelve children, twelve copies of one scan.

The older order (split first, OCR each child afterwards) still needs the file,
because there the child IS the OCR input. Both must keep working: already
processed cases are not reprocessed.
"""
import json

import pytest

import segment_case as sc


class _FakeDao:
    def __init__(self):
        self.calls = []

    def replace_manifest_documents(self, case_id, bundle_id, bundle_fields,
                                   new_documents, held_by, run_id, **kw):
        self.calls.append({"new_documents": new_documents,
                           "bundle_fields": bundle_fields})
        return True, "PASS"


def _bundle_pdf(tmp_path, pages):
    import fitz

    path = tmp_path / "bundle.pdf"
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {n + 1}")
    doc.save(path)
    doc.close()
    return path


def _proposal(segments, *, page_count=12):
    return {
        "case_id": "CASE_900", "source_document_id": "DOC_001",
        "source_file_name": "bundle.pdf",
        "source_file_path": "data/raw/CASE_900/DOC_001.pdf",
        "source_page_count": page_count,
        "created_at": "2026-08-14T00:00:00Z", "updated_at": "2026-08-14T00:00:00Z",
        "review_status": "approved",
        "reviewed_by": "R", "reviewed_at": "2026-08-14T00:00:00Z",
        "rejection_reason": None,
        "method": {"ocr_performed": False, "method_version": "v",
                   "mode": "vision_proposal", "crop_ratio": 0.33,
                   "grid_cols": 3, "grid_rows": 4},
        "segments": segments,
        "unassigned_pages": [],
        "warnings": [],
    }


def _seg(index, start, end):
    return {"segment_index": index, "page_start": start, "page_end": end,
            "review_status": "approved", "provisional_document_type": None,
            "provisional_type_label": None, "confidence": None,
            "boundary_evidence": None, "needs_full_page": False,
            "orientation_suspect": False, "assigned_document_id": None}


def _manifest():
    return {"case_id": "CASE_900", "documents": [{
        "document_id": "DOC_001", "file_name": "DOC_001.pdf",
        "file_path": "data/raw/CASE_900/DOC_001.pdf", "file_format": "pdf",
        "file_size_bytes": 1234, "ocr_status": "completed",
        "source_file_name": "bundle.pdf",
    }]}


def _write_parent_ocr(tmp_path, pages):
    """The record whose existence means 'the bundle was OCR'd first', plus the
    processed page files it points at -- redistribution verifies the two agree
    rather than trusting the record alone."""
    outputs = tmp_path / "outputs" / "CASE_900"
    outputs.mkdir(parents=True, exist_ok=True)
    processed = tmp_path / "data" / "processed" / "CASE_900" / "DOC_001"
    processed.mkdir(parents=True, exist_ok=True)
    for n in range(pages):
        (processed / f"page_{n + 1:03d}.md").write_text(
            f"<<<PAGE page={n + 1}>>>\npage {n + 1}\n", encoding="utf-8")
    (outputs / "ocr_result_DOC_001.json").write_text(json.dumps({
        "case_id": "CASE_900", "document_id": "DOC_001",
        "pages": [{"page": n + 1, "cross_validation": "agreed",
                   "text_path": f"data/processed/CASE_900/DOC_001/page_{n + 1:03d}.md"}
                  for n in range(pages)],
    }), encoding="utf-8")


def _run_split(tmp_path, pdf, prop, dao):
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        return sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest(), held_by="R", run_id="RUN_1", dao=dao)
    finally:
        sc.ROOT = orig_root


# --- the inheriting order: no child PDFs ----------------------------------

def test_inheriting_child_writes_no_pdf(tmp_path):
    pdf = _bundle_pdf(tmp_path, 12)
    _write_parent_ocr(tmp_path, 12)
    dao = _FakeDao()

    out = _run_split(tmp_path, pdf, _proposal([_seg(0, 1, 5), _seg(1, 6, 12)]), dao)

    assert out["status"] == "split"
    raw_dir = tmp_path / "data" / "raw" / "CASE_900"
    assert not (raw_dir / "DOC_002.pdf").exists()
    assert not (raw_dir / "DOC_003.pdf").exists()
    assert out["new_pdf_paths"] == []


def test_inheriting_child_records_null_path_and_size(tmp_path):
    pdf = _bundle_pdf(tmp_path, 12)
    _write_parent_ocr(tmp_path, 12)
    dao = _FakeDao()

    _run_split(tmp_path, pdf, _proposal([_seg(0, 1, 5), _seg(1, 6, 12)]), dao)

    entries = dao.calls[0]["new_documents"]
    for entry in entries:
        assert entry["file_path"] is None
        assert entry["file_size_bytes"] is None
        # Still a range of a real physical file, not a processed-text segment.
        assert entry["document_role"] == "physical"


def test_inheriting_child_is_still_identified_by_its_page_range(tmp_path):
    """With no file_path, the page range is the only thing that says which part
    of the parent this document is. It must survive."""
    pdf = _bundle_pdf(tmp_path, 12)
    _write_parent_ocr(tmp_path, 12)
    dao = _FakeDao()

    _run_split(tmp_path, pdf, _proposal([_seg(0, 1, 5), _seg(1, 6, 12)]), dao)

    first, second = dao.calls[0]["new_documents"]
    assert (first["source_file_name"], first["source_page_start"],
            first["source_page_end"]) == ("bundle.pdf", 1, 5)
    assert (second["source_file_name"], second["source_page_start"],
            second["source_page_end"]) == ("bundle.pdf", 6, 12)


def test_inheriting_manifest_entries_are_schema_valid(tmp_path):
    """The schema forbids a null file_path on a physical document -- except for
    exactly this shape. If that exception is ever narrowed, this fails."""
    import _validation

    pdf = _bundle_pdf(tmp_path, 12)
    _write_parent_ocr(tmp_path, 12)
    dao = _FakeDao()
    _run_split(tmp_path, pdf, _proposal([_seg(0, 1, 5), _seg(1, 6, 12)]), dao)

    manifest = _manifest()
    manifest["documents"] = dao.calls[0]["new_documents"]
    schemas, registry = _validation.load_registry()
    errors = _validation.validate_instance(
        manifest, "document_manifest.schema.json", schemas, registry)
    assert errors == [], errors


# --- the split-first order still writes files -----------------------------

def test_without_parent_ocr_child_pdfs_are_still_written(tmp_path):
    """Under the older order the child file IS the OCR input. Dropping it would
    leave those cases with nothing to read."""
    pdf = _bundle_pdf(tmp_path, 12)
    dao = _FakeDao()  # no parent OCR record written

    out = _run_split(tmp_path, pdf, _proposal([_seg(0, 1, 5), _seg(1, 6, 12)]), dao)

    raw_dir = tmp_path / "data" / "raw" / "CASE_900"
    assert (raw_dir / "DOC_002.pdf").exists()
    assert (raw_dir / "DOC_003.pdf").exists()
    assert len(out["new_pdf_paths"]) == 2

    entries = dao.calls[0]["new_documents"]
    for entry in entries:
        assert entry["file_path"].startswith("data/raw/CASE_900/")
        assert entry["file_size_bytes"] > 0


def test_split_first_children_keep_their_page_counts(tmp_path):
    """The page selection itself must not change -- only whether it runs."""
    import fitz

    pdf = _bundle_pdf(tmp_path, 12)
    dao = _FakeDao()
    _run_split(tmp_path, pdf, _proposal([_seg(0, 1, 5), _seg(1, 6, 12)]), dao)

    raw_dir = tmp_path / "data" / "raw" / "CASE_900"
    with fitz.open(raw_dir / "DOC_002.pdf") as d:
        assert d.page_count == 5
    with fitz.open(raw_dir / "DOC_003.pdf") as d:
        assert d.page_count == 7


def test_garbage_collection_does_not_lose_the_text_layer(tmp_path):
    """garbage=4 drops unreferenced objects. The text layer select() exists to
    preserve must survive it -- an child extracting 0 chars reads as a genuine
    scan downstream and silently routes to vision OCR."""
    import fitz

    pdf = _bundle_pdf(tmp_path, 6)
    dao = _FakeDao()
    _run_split(tmp_path, pdf, _proposal([_seg(0, 1, 3), _seg(1, 4, 6)]), dao)

    raw_dir = tmp_path / "data" / "raw" / "CASE_900"
    with fitz.open(raw_dir / "DOC_002.pdf") as child:
        texts = [child[n].get_text().strip() for n in range(child.page_count)]
    assert texts == ["page 1", "page 2", "page 3"]


# --- the availability probe -----------------------------------------------

def test_probe_agrees_with_the_redistribution_condition(tmp_path):
    """Both must key off the same record. If they disagreed, a case would
    either lose its child text or keep paying for files nothing opens, and the
    disagreement would stay invisible until a later stage failed."""
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        assert sc._parent_ocr_available("CASE_900", "DOC_001") is False
        _write_parent_ocr(tmp_path, 3)
        assert sc._parent_ocr_available("CASE_900", "DOC_001") is True
    finally:
        sc.ROOT = orig_root


def test_probe_treats_unreadable_record_as_absent(tmp_path):
    """The raise belongs to _redistribute_parent_ocr, which is the call that
    needs the contents. Reporting the same fault twice from two places would
    make the first report the misleading one."""
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        outputs = tmp_path / "outputs" / "CASE_900"
        outputs.mkdir(parents=True)
        (outputs / "ocr_result_DOC_001.json").write_text("{not json", encoding="utf-8")
        assert sc._parent_ocr_available("CASE_900", "DOC_001") is False
    finally:
        sc.ROOT = orig_root


# --- the digest a child without a file still owes ------------------------

def test_child_without_a_file_resolves_its_parents_digest(tmp_path, monkeypatch):
    """`source_pdf_sha256` is the first input to every canonical UID. A child
    with no PDF of its own must still answer 'which registered file am I made
    of', and the honest answer is the parent's file.

    Caught by running the real pipeline, not by reading: dropping child PDFs
    made `record-source-digest` return
    'no readable registered raw source for DOC_009', which blocks canonical UID
    activation and with it the whole policy stage.
    """
    import dao

    outputs = tmp_path / "outputs" / "CASE_900"
    outputs.mkdir(parents=True)
    raw = tmp_path / "data" / "raw" / "CASE_900"
    raw.mkdir(parents=True)
    (raw / "DOC_001.pdf").write_bytes(b"%PDF-1.4 parent bytes")

    manifest = {"case_id": "CASE_900", "documents": [
        {"document_id": "DOC_001", "file_name": "DOC_001.pdf",
         "file_path": "data/raw/CASE_900/DOC_001.pdf", "file_format": "pdf",
         "file_size_bytes": 21, "ocr_status": "not_applicable",
         "downstream_disposition": "superseded_bundle"},
        {"document_id": "DOC_002", "file_name": "DOC_002.pdf",
         "file_path": None, "file_format": "pdf", "file_size_bytes": None,
         "ocr_status": "completed", "source_file_name": "DOC_001.pdf",
         "source_page_start": 1, "source_page_end": 5},
        {"document_id": "DOC_003", "file_name": "DOC_003.pdf",
         "file_path": None, "file_format": "pdf", "file_size_bytes": None,
         "ocr_status": "completed", "source_file_name": "DOC_001.pdf",
         "source_page_start": 6, "source_page_end": 12},
    ]}
    (outputs / "document_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")

    parent = dao.registered_source_pdf_sha256("CASE_900", "DOC_001")
    child = dao.registered_source_pdf_sha256("CASE_900", "DOC_002")
    sibling = dao.registered_source_pdf_sha256("CASE_900", "DOC_003")

    assert parent is not None
    assert child == parent
    # Siblings share a digest because they ARE the same file; what separates
    # them is the page range, which the UID scheme carries separately.
    assert sibling == parent


def test_child_naming_no_parent_still_resolves_to_nothing(tmp_path, monkeypatch):
    """The fallback must not become 'find any file'. A document with neither a
    file nor a named parent has no registered source, and saying otherwise
    would mint a UID against a file the case never tied it to."""
    import dao

    outputs = tmp_path / "outputs" / "CASE_900"
    outputs.mkdir(parents=True)
    (tmp_path / "data" / "raw" / "CASE_900").mkdir(parents=True)

    manifest = {"case_id": "CASE_900", "documents": [
        {"document_id": "DOC_002", "file_name": "DOC_002.pdf",
         "file_path": None, "file_format": "pdf", "file_size_bytes": None,
         "ocr_status": "completed"},
    ]}
    (outputs / "document_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")

    assert dao.registered_source_pdf_sha256("CASE_900", "DOC_002") is None
