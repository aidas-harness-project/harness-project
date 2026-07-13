"""dao.py's patch-manifest-document -- the one atomic read-modify-write
path for document_manifest.json (harness-guardrails P5, known-gaps.md
item 7). Unlike every other write-contract target, this file's schema
allows a single stage to touch only its own owner fields on one document
entry inside a shared array -- patch_manifest_document() reads fresh
*after* the lock is held, merges just the given fields into just the named
document, and validates the whole file before writing, closing the
read-before-lock gap write-contract still has for every other target.
"""
import json
import threading
import time

import dao


def _seed_manifest(base, case_id="CASE_009", extra_docs=()):
    out_dir = base / "outputs" / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "case_id": case_id,
        "created_at": dao.now_iso(),
        "documents": [
            {
                "document_id": "DOC_001", "file_name": "DOC_001.pdf",
                "file_path": f"data/raw/{case_id}/DOC_001.pdf", "file_format": "pdf",
                "file_size_bytes": 1000, "ocr_status": "pending",
            },
            *extra_docs,
        ],
    }
    dao.atomic_write_json(out_dir / "document_manifest.json", manifest)
    return out_dir / "document_manifest.json"


def test_patch_merges_given_fields_without_touching_unrelated_ones(isolated_dao):
    manifest_path = _seed_manifest(isolated_dao)

    ok, message = dao.patch_manifest_document(
        "CASE_009", "DOC_001", {"ocr_status": "completed", "pages": 4}, "tester", "RUN_20260713_001")

    assert ok, message
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))["documents"][0]
    assert doc["ocr_status"] == "completed"
    assert doc["pages"] == 4
    assert doc["file_name"] == "DOC_001.pdf", "fields not passed to the patch must survive untouched"


def test_patch_does_not_touch_other_documents_in_the_array(isolated_dao):
    other_doc = {
        "document_id": "DOC_002", "file_name": "DOC_002.pdf",
        "file_path": "data/raw/CASE_009/DOC_002.pdf", "file_format": "pdf",
        "file_size_bytes": 2000, "ocr_status": "pending",
    }
    manifest_path = _seed_manifest(isolated_dao, extra_docs=[other_doc])

    ok, message = dao.patch_manifest_document("CASE_009", "DOC_001", {"ocr_status": "completed"}, "tester", "RUN_20260713_001")

    assert ok, message
    docs = {d["document_id"]: d for d in json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]}
    assert docs["DOC_001"]["ocr_status"] == "completed"
    assert docs["DOC_002"]["ocr_status"] == "pending", "a patch to one document must not touch a sibling entry"


def test_patch_unknown_document_id_fails_and_writes_nothing(isolated_dao):
    manifest_path = _seed_manifest(isolated_dao)
    before = manifest_path.read_text(encoding="utf-8")

    ok, message = dao.patch_manifest_document("CASE_009", "DOC_999", {"ocr_status": "completed"}, "tester", "RUN_20260713_001")

    assert not ok
    assert "DOC_999" in message
    assert manifest_path.read_text(encoding="utf-8") == before, "an unknown document_id must not modify the file at all"


def test_patch_schema_invalid_field_value_rejected_and_not_written(isolated_dao):
    manifest_path = _seed_manifest(isolated_dao)
    before = manifest_path.read_text(encoding="utf-8")

    ok, message = dao.patch_manifest_document(
        "CASE_009", "DOC_001", {"ocr_status": "not_a_real_status"}, "tester", "RUN_20260713_001")

    assert not ok
    assert "FAIL" in message
    assert manifest_path.read_text(encoding="utf-8") == before, \
        "schema-invalid merge result must leave the on-disk file untouched, same contract as write-contract"


def test_patch_no_manifest_file_fails_cleanly(isolated_dao):
    ok, message = dao.patch_manifest_document("CASE_009", "DOC_001", {"ocr_status": "completed"}, "tester", "RUN_20260713_001")

    assert not ok
    assert "NOT_FOUND" not in message  # this path's own message, not read-contract's convention
    assert "no document_manifest.json" in message.lower() or "FAIL" in message


def test_patch_waits_for_lock_then_reads_data_fresh_as_of_release(isolated_dao, monkeypatch):
    """The whole point of this subcommand: read-contract + write-contract's
    read happens *before* waiting for the lock, so a write that lands
    during the wait is silently overwritten. patch_manifest_document must
    not have that gap -- simulates another writer changing a sibling field
    while this call is blocked on the lock, then confirms the eventual
    write preserves that concurrent change instead of clobbering it with
    stale data captured before the wait began."""
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 2.0)
    manifest_path = _seed_manifest(isolated_dao)
    target = manifest_path
    dao.acquire_lock(target, "someone-else", "RUN_OTHER", "holding briefly")

    def concurrent_write_then_release():
        time.sleep(0.06)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["documents"][0]["pages"] = 99  # the "concurrent" change
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        dao.release_lock(target)

    threading.Thread(target=concurrent_write_then_release).start()

    ok, message = dao.patch_manifest_document("CASE_009", "DOC_001", {"ocr_status": "completed"}, "me", "RUN_MINE")

    assert ok, message
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))["documents"][0]
    assert doc["ocr_status"] == "completed", "this call's own patch must have landed"
    assert doc["pages"] == 99, \
        "must have read the manifest AFTER the lock was released, not a stale copy from before the wait"


def test_patch_with_stage_updates_run_state_to_passed(isolated_dao):
    _seed_manifest(isolated_dao)

    ok, message = dao.patch_manifest_document(
        "CASE_009", "DOC_001", {"ocr_status": "completed"}, "tester", "RUN_20260713_001", stage="document_processing")

    assert ok, message
    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["stage_name"] == "document_processing"
    assert state["stages"][0]["status"] == "passed"


def test_cli_wrapper_reads_fields_from_file(isolated_dao, make_args, tmp_path):
    _seed_manifest(isolated_dao)
    fields_file = tmp_path / "fields.json"
    fields_file.write_text(json.dumps({"ocr_status": "completed", "pages": 2}), encoding="utf-8")
    args = make_args(doc_id="DOC_001", fields_file=str(fields_file))

    rc = dao.cmd_patch_manifest_document(args)

    assert rc == 0
    doc = json.loads((isolated_dao / "outputs" / "CASE_009" / "document_manifest.json").read_text(encoding="utf-8"))["documents"][0]
    assert doc["ocr_status"] == "completed"
    assert doc["pages"] == 2
