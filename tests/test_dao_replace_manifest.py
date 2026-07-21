"""dao.py's replace-manifest-documents -- segmentation's split step.

Unlike patch-manifest-document (one existing entry) or write-contract (whole
file, read unlocked before the lock), this replaces ONE bundle entry with a
modified bundle PLUS N appended per-document entries, as a single atomic unit
under one lock hold, reading fresh after the lock. A half-applied split would
either lose the bundle's audit record or leave DOC_XXX entries with no bundle
to trace back to -- so it is all-or-nothing and validates before writing.
"""
import json
import threading
import time
import types

import dao


def _seed_manifest(base, case_id="CASE_900"):
    out_dir = base / "outputs" / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "case_id": case_id, "created_at": dao.now_iso(),
        "documents": [{
            "document_id": "DOC_001", "file_name": "DOC_001.pdf",
            "file_path": f"data/raw/{case_id}/DOC_001.pdf", "file_format": "pdf",
            "file_size_bytes": 5000, "ocr_status": "pending",
            "source_file_name": "bundle.pdf",
        }],
    }
    dao.atomic_write_json(out_dir / "document_manifest.json", manifest)
    return out_dir / "document_manifest.json"


def _new_doc(doc_id, start, end):
    return {
        "document_id": doc_id, "file_name": f"{doc_id}.pdf",
        "file_path": f"data/raw/CASE_900/{doc_id}.pdf", "file_format": "pdf",
        "file_size_bytes": 100, "ocr_status": "pending",
        "source_file_name": "bundle.pdf", "source_page_start": start,
        "source_page_end": end,
        "segmentation_proposal_path": "outputs/CASE_900/segmentation_proposal_DOC_001.json",
        "document_type": None,
    }


_SUPERSEDE = {
    "downstream_disposition": "superseded_bundle", "ocr_status": "not_applicable",
    "redacted_text_path": None,
    "segmentation_proposal_path": "outputs/CASE_900/segmentation_proposal_DOC_001.json",
}


def test_replace_supersedes_the_bundle_and_appends_new_entries(isolated_dao):
    manifest_path = _seed_manifest(isolated_dao)
    ok, message = dao.replace_manifest_documents(
        "CASE_900", "DOC_001", _SUPERSEDE,
        [_new_doc("DOC_002", 1, 6), _new_doc("DOC_003", 7, 12)],
        "tester", "RUN_20260721_001")
    assert ok, message
    docs = {d["document_id"]: d for d in
            json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]}
    assert docs["DOC_001"]["downstream_disposition"] == "superseded_bundle"
    assert docs["DOC_001"]["ocr_status"] == "not_applicable"
    assert set(docs) == {"DOC_001", "DOC_002", "DOC_003"}
    assert docs["DOC_002"]["source_page_start"] == 1


def test_replace_rejects_an_id_that_collides_with_an_existing_one(isolated_dao):
    manifest_path = _seed_manifest(isolated_dao)
    before = manifest_path.read_text(encoding="utf-8")
    ok, message = dao.replace_manifest_documents(
        "CASE_900", "DOC_001", _SUPERSEDE,
        [_new_doc("DOC_001", 1, 12)],  # collides with the bundle's own id
        "tester", "RUN_20260721_001")
    assert not ok
    assert "already exist" in message
    assert manifest_path.read_text(encoding="utf-8") == before


def test_replace_rejects_duplicate_ids_within_the_new_documents(isolated_dao):
    _seed_manifest(isolated_dao)
    ok, message = dao.replace_manifest_documents(
        "CASE_900", "DOC_001", _SUPERSEDE,
        [_new_doc("DOC_002", 1, 6), _new_doc("DOC_002", 7, 12)],
        "tester", "RUN_20260721_001")
    assert not ok
    assert "duplicate" in message


def test_replace_unknown_bundle_fails_and_writes_nothing(isolated_dao):
    manifest_path = _seed_manifest(isolated_dao)
    before = manifest_path.read_text(encoding="utf-8")
    ok, message = dao.replace_manifest_documents(
        "CASE_900", "DOC_999", _SUPERSEDE, [_new_doc("DOC_002", 1, 12)],
        "tester", "RUN_20260721_001")
    assert not ok
    assert "DOC_999" in message
    assert manifest_path.read_text(encoding="utf-8") == before


def test_replace_schema_invalid_result_rejected_and_not_written(isolated_dao):
    """A superseded bundle missing its required segmentation_proposal_path must
    fail the schema conditional and leave the file untouched."""
    manifest_path = _seed_manifest(isolated_dao)
    before = manifest_path.read_text(encoding="utf-8")
    bad_supersede = {"downstream_disposition": "superseded_bundle",
                     "ocr_status": "not_applicable"}  # no segmentation_proposal_path
    ok, message = dao.replace_manifest_documents(
        "CASE_900", "DOC_001", bad_supersede, [_new_doc("DOC_002", 1, 12)],
        "tester", "RUN_20260721_001")
    assert not ok
    assert "FAIL" in message
    assert manifest_path.read_text(encoding="utf-8") == before


def test_replace_reads_fresh_after_the_lock(isolated_dao, monkeypatch):
    """Same read-after-lock guarantee patch_manifest_document has: a concurrent
    write landing during the lock wait must survive, not be clobbered."""
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 2.0)
    manifest_path = _seed_manifest(isolated_dao)
    dao.acquire_lock(manifest_path, "someone-else", "RUN_OTHER", "holding briefly")

    def concurrent_write_then_release():
        time.sleep(0.06)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["documents"][0]["file_size_bytes"] = 99999  # concurrent change
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        dao.release_lock(manifest_path)

    threading.Thread(target=concurrent_write_then_release).start()

    ok, message = dao.replace_manifest_documents(
        "CASE_900", "DOC_001", _SUPERSEDE, [_new_doc("DOC_002", 1, 12)],
        "me", "RUN_MINE")
    assert ok, message
    docs = {d["document_id"]: d for d in
            json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]}
    assert docs["DOC_001"]["downstream_disposition"] == "superseded_bundle"
    assert docs["DOC_001"]["file_size_bytes"] == 99999, \
        "must have read the manifest after the lock released, not a stale pre-wait copy"


def test_replace_with_stage_updates_run_state(isolated_dao):
    # Segmentation is the tail of intake, so it records under the "intake" stage.
    # (A dedicated stage name lands with the step-8 pipeline integration, added
    # to run_state's enum in the same change per the D4 rule.)
    _seed_manifest(isolated_dao)
    ok, message = dao.replace_manifest_documents(
        "CASE_900", "DOC_001", _SUPERSEDE, [_new_doc("DOC_002", 1, 12)],
        "tester", "RUN_20260721_001", stage="intake")
    assert ok, message
    state = dao.load_run_state("CASE_900")
    assert state["stages"][0]["stage_name"] == "intake"
    assert state["stages"][0]["status"] == "passed"


def test_cli_wrapper_reads_both_files(isolated_dao, tmp_path):
    _seed_manifest(isolated_dao)
    bundle_fields_file = tmp_path / "bundle.json"
    bundle_fields_file.write_text(json.dumps(_SUPERSEDE), encoding="utf-8")
    new_docs_file = tmp_path / "new.json"
    new_docs_file.write_text(json.dumps([_new_doc("DOC_002", 1, 12)]), encoding="utf-8")

    args = types.SimpleNamespace(
        case_id="CASE_900", bundle_id="DOC_001",
        bundle_fields_file=str(bundle_fields_file),
        new_documents_file=str(new_docs_file),
        held_by="tester", run_id="RUN_20260721_001", purpose=None, stage=None)

    assert dao.cmd_replace_manifest_documents(args) == 0
    docs = json.loads(
        (isolated_dao / "outputs" / "CASE_900" / "document_manifest.json").read_text(encoding="utf-8"))["documents"]
    assert {d["document_id"] for d in docs} == {"DOC_001", "DOC_002"}
