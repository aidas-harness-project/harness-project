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
    acquired = threading.Event()

    def concurrent_write_then_release():
        assert dao.acquire_lock(
            target, "someone-else", "RUN_OTHER", "holding briefly"
        ) is None
        acquired.set()
        time.sleep(0.06)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["documents"][0]["pages"] = 99  # the "concurrent" change
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        dao.release_lock(target)

    holder = threading.Thread(target=concurrent_write_then_release)
    holder.start()
    assert acquired.wait(timeout=1)

    ok, message = dao.patch_manifest_document("CASE_009", "DOC_001", {"ocr_status": "completed"}, "me", "RUN_MINE")
    holder.join(timeout=1)

    assert ok, message
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))["documents"][0]
    assert doc["ocr_status"] == "completed", "this call's own patch must have landed"
    assert doc["pages"] == 99, \
        "must have read the manifest AFTER the lock was released, not a stale copy from before the wait"


def test_patch_with_stage_marks_run_state_in_progress_not_passed(isolated_dao):
    """A single manifest patch is progress within document_processing, not the
    whole stage finalizing. It marks the stage in_progress -- passing a stage
    is a deliberate finalize-stage call (snapshot + passed, atomic), never a
    side effect of one field write. (Part 1: atomic passed-with-snapshot.)"""
    _seed_manifest(isolated_dao)

    ok, message = dao.patch_manifest_document(
        "CASE_009", "DOC_001", {"ocr_status": "completed"}, "tester", "RUN_20260713_001", stage="document_processing")

    assert ok, message
    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["stage_name"] == "document_processing"
    assert state["stages"][0]["status"] == "in_progress", \
        "a single patch must not finalize the stage -- passing is finalize-stage's job"
    assert state["stages"][0]["backup_path"] is None


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


# ------------------------------------------------------- lock ordering (T3) --

def test_run_state_is_updated_after_the_manifest_lock_is_released(isolated_dao, monkeypatch):
    """The manifest lock must NOT still be held while run-state is updated.

    patch_manifest_document held the manifest lock across its
    _update_run_state call, which acquires a SECOND lock (_run_state.json).
    Holding one lock while requesting another is the hold-and-wait condition:
    with document-level workers (T8), one worker holding the manifest lock and
    waiting for run-state, while another holds run-state and waits for the
    manifest, is a deadlock. P5 polls for 15 minutes before giving up, so it
    would surface as a long stall rather than a crash -- and only sometimes,
    which is the worst shape to debug.

    Asserted as a property (was the lock file present?) rather than by racing
    two threads, so it cannot pass by winning a timing coin flip.
    """
    _seed_manifest(isolated_dao)
    manifest_lock = dao.case_dir("CASE_009") / "document_manifest.json.lock"
    observed = {}

    real_update = dao._update_run_state

    def spy(*args, **kwargs):
        observed["manifest_lock_held"] = manifest_lock.exists()
        return real_update(*args, **kwargs)

    monkeypatch.setattr(dao, "_update_run_state", spy)

    ok, message = dao.patch_manifest_document(
        "CASE_009", "DOC_001", {"ocr_status": "completed"}, "tester",
        "RUN_20260713_001", stage="document_processing")

    assert ok, message
    assert observed.get("manifest_lock_held") is False, (
        "run-state was updated while the manifest lock was still held -- "
        "that is the lock-order inversion T3 exists to remove")


def test_run_state_marker_still_lands_after_the_reorder(isolated_dao):
    """Moving the call must not silently drop the progress marker."""
    _seed_manifest(isolated_dao)

    ok, message = dao.patch_manifest_document(
        "CASE_009", "DOC_001", {"ocr_status": "completed"}, "tester",
        "RUN_20260713_001", stage="document_processing")

    assert ok, message
    state = json.loads(dao.run_state_path("CASE_009").read_text(encoding="utf-8"))
    stages = {s["stage_name"]: s for s in state["stages"]}
    assert stages["document_processing"]["status"] == "in_progress", (
        "the soft progress marker must still be recorded")


def test_two_concurrent_patches_both_complete(isolated_dao, monkeypatch):
    """The scenario T8 creates: two document workers patching two different
    documents at the same time. Both must finish, and both edits must survive
    -- neither may be lost to the other's read-modify-write."""
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 20)
    other = {
        "document_id": "DOC_002", "file_name": "DOC_002.pdf",
        "file_path": "data/raw/CASE_009/DOC_002.pdf", "file_format": "pdf",
        "file_size_bytes": 2000, "ocr_status": "pending",
    }
    manifest_path = _seed_manifest(isolated_dao, extra_docs=[other])
    results = {}

    def worker(doc_id):
        results[doc_id] = dao.patch_manifest_document(
            "CASE_009", doc_id, {"ocr_status": "completed"}, f"worker-{doc_id}",
            "RUN_20260713_001", stage="document_processing")

    threads = [threading.Thread(target=worker, args=(d,))
               for d in ("DOC_001", "DOC_002")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "a patch never returned -- deadlock"
    assert all(ok for ok, _ in results.values()), results
    docs = {d["document_id"]: d
            for d in json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]}
    assert docs["DOC_001"]["ocr_status"] == "completed"
    assert docs["DOC_002"]["ocr_status"] == "completed", "one worker's edit was lost"
