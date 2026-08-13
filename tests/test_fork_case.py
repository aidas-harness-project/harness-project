"""fork_case.py -- lets a branch reuse already-completed OCR/redaction/
chunking work under a fresh case_id instead of re-running it, since P10's
snapshot-backup only versions outputs/ (never data/) and case_id is the
primary key almost everywhere in the DAO (no run_id-scoped branching).
"""
import json

import pytest

import dao
import fork_case as fc


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    data = tmp_path / "data"
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    monkeypatch.setattr(dao, "DATA", data)
    monkeypatch.setattr(fc, "OUTPUTS", outputs)
    monkeypatch.setattr(fc, "DATA", data)
    return tmp_path


def _seed_source_case(tmp_path, case_id="CASE_005", extra_files=None):
    out_dir = tmp_path / "outputs" / case_id
    out_dir.mkdir(parents=True)
    (out_dir / "extracted_claim_fields.json").write_text(json.dumps({
        "case_id": case_id, "component": "claim-analysis", "status": "success",
        "fields": {"diagnosis_name": {"value": "test", "confidence": 0.9,
                    "evidence_references": [{"document_id": "DOC_001", "quote": "q"}], "review_required": False}},
    }), encoding="utf-8")
    source_entries = [{"file_name": "a.pdf", "classification": "raw", "review_status": "approved",
                       "reviewed_by": "human", "reviewed_at": "2026-07-01T00:00:00+09:00", "rejection_reason": None}]
    (out_dir / "_source_ledger.json").write_text(json.dumps({
        "ledger_version": "source_ledger.v0.4",
        "case_id": case_id, "source_dir": "x",
        "files": source_entries,
        "history_boundary": dao.make_history_boundary(source_entries, mode="legacy_snapshot"),
        "operations": [],
    }), encoding="utf-8")
    (out_dir / "_run_state.json").write_text(json.dumps({
        "case_id": case_id, "run_id": "RUN_20260701_001",
        "stages": [{"stage_name": "document_processing", "status": "passed", "attempt_count": 1,
                    "backup_path": "outputs/" + case_id + "/_backups/step_01_document_processing"}],
        "human_input_status": [],
    }), encoding="utf-8")
    if extra_files:
        for name, content in extra_files.items():
            (out_dir / name).write_text(content, encoding="utf-8")

    processed_dir = tmp_path / "data" / "processed" / case_id / "DOC_001"
    processed_dir.mkdir(parents=True)
    (processed_dir / "redacted_text.md").write_text("<<<PAGE page=1>>>\nredacted content\n", encoding="utf-8")

    raw_dir = tmp_path / "data" / "raw" / case_id
    raw_dir.mkdir(parents=True)
    (raw_dir / "DOC_001.pdf").write_bytes(b"fake pdf bytes")
    (raw_dir / "_intake_record.json").write_text(json.dumps({"case_id": case_id, "raw": []}), encoding="utf-8")

    gt_dir = tmp_path / "data" / "ground_truth" / case_id
    gt_dir.mkdir(parents=True)
    (gt_dir / "final_report.pdf").write_bytes(b"fake ground truth")

    return out_dir


# ------------------------------------------------------------ id assignment --

def test_next_free_case_id_scans_all_four_roots(tmp_path):
    (tmp_path / "outputs" / "CASE_001").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "CASE_002").mkdir(parents=True)
    (tmp_path / "data" / "processed" / "CASE_009").mkdir(parents=True)
    assert fc.next_free_case_id() == "CASE_010"


def test_next_free_case_id_ignores_non_numeric_dirs(tmp_path):
    (tmp_path / "outputs" / "CASE_001").mkdir(parents=True)
    (tmp_path / "outputs" / "CASE_DEMO").mkdir(parents=True)
    (tmp_path / "outputs" / "CASE_SMOKE").mkdir(parents=True)
    assert fc.next_free_case_id() == "CASE_002"


def test_next_free_case_id_starts_at_001_when_nothing_exists(tmp_path):
    assert fc.next_free_case_id() == "CASE_001"


# ---------------------------------------------------------------- basic fork --

def test_fork_copies_outputs_and_rewrites_case_id(tmp_path):
    _seed_source_case(tmp_path)

    warnings = fc.copy_outputs_and_rewrite_case_id(dao.case_dir("CASE_005"), "CASE_006")

    assert warnings == []
    new_fields = json.loads((tmp_path / "outputs" / "CASE_006" / "extracted_claim_fields.json").read_text(encoding="utf-8"))
    assert new_fields["case_id"] == "CASE_006"
    new_ledger = json.loads((tmp_path / "outputs" / "CASE_006" / "_source_ledger.json").read_text(encoding="utf-8"))
    assert new_ledger["case_id"] == "CASE_006"


def test_fork_resets_operation_receipts_to_an_explicit_fork_baseline(tmp_path):
    _seed_source_case(tmp_path)
    source_path = dao.source_ledger_path("CASE_005")
    source = dao.load_json(source_path)
    request = {
        "case_id": "CASE_005",
        "operation_id": "ledger:fork-source-0001",
        "action": "set_status",
        "payload": {
            "file_name": "a.pdf", "status": "approved",
            "reviewer": "human", "reason": None,
        },
    }
    source["operations"] = [{
        "operation_id": request["operation_id"],
        "request": request,
        "request_sha256": dao.hashlib.sha256(
            dao._canonical_json_bytes(request)
        ).hexdigest(),
        "result": {
            "action": "set_status", "target_id": "a.pdf",
            "status": "approved",
        },
        "completed_at": source["files"][0]["reviewed_at"],
    }]
    source["history_boundary"] = dao.make_history_boundary(
        [{
            **source["files"][0], "review_status": "pending",
            "reviewed_by": None, "reviewed_at": None,
        }],
        mode="native",
    )
    dao.atomic_write_json(source_path, source)

    assert fc.copy_outputs_and_rewrite_case_id(
        dao.case_dir("CASE_005"), "CASE_006"
    ) == []
    forked = dao.validated_source_ledger("CASE_006")
    assert forked["operations"] == []
    assert forked["history_boundary"]["mode"] == "legacy_snapshot"
    assert forked["history_boundary"]["forked_operation_count"] == 1


def test_fork_refuses_content_addressed_medical_state(tmp_path):
    out_dir = _seed_source_case(tmp_path)
    (out_dir / "_medical_variable_revisions").mkdir()

    with pytest.raises(ValueError, match="content-addressed medical revision"):
        fc.copy_outputs_and_rewrite_case_id(out_dir, "CASE_006")
    assert not any(dao.case_dir("CASE_006").iterdir())


def test_fork_preserves_source_ledger_approval_status_as_is(tmp_path):
    _seed_source_case(tmp_path)

    fc.copy_outputs_and_rewrite_case_id(dao.case_dir("CASE_005"), "CASE_006")

    new_ledger = json.loads((tmp_path / "outputs" / "CASE_006" / "_source_ledger.json").read_text(encoding="utf-8"))
    assert new_ledger["files"][0]["review_status"] == "approved", "carried forward as-is, not reset to pending"
    assert new_ledger["files"][0]["reviewed_by"] == "human"


def test_fork_excludes_backups_and_lock_files(tmp_path):
    out_dir = _seed_source_case(tmp_path)
    (out_dir / "_backups").mkdir()
    (out_dir / "_backups" / "step_01_x.json").write_text("{}", encoding="utf-8")
    (out_dir / "extracted_claim_fields.json.lock").write_text(json.dumps({
        "held_by": "someone", "run_id": "r", "started_at": "t", "purpose": "p"
    }), encoding="utf-8")
    nested = out_dir / "nested"
    nested.mkdir()
    (nested / "artifact.json").write_text("{}", encoding="utf-8")
    (nested / "artifact.json.lock").write_text("{}", encoding="utf-8")

    # Persistent unlocked lock inodes do not block, but are never copied.
    fc.copy_outputs_and_rewrite_case_id(out_dir, "CASE_006")

    dest = tmp_path / "outputs" / "CASE_006"
    assert not (dest / "_backups").exists()
    assert not (dest / "extracted_claim_fields.json.lock").exists()
    assert (dest / "nested" / "artifact.json").exists()
    assert not (dest / "nested" / "artifact.json.lock").exists()


def test_processed_data_always_copied(tmp_path):
    _seed_source_case(tmp_path)
    dest = fc.copy_data_tree("processed", "CASE_005", "CASE_006")
    assert dest == tmp_path / "data" / "processed" / "CASE_006"
    assert (dest / "DOC_001" / "redacted_text.md").read_text(encoding="utf-8") == "<<<PAGE page=1>>>\nredacted content\n"


def test_raw_not_copied_unless_requested_and_ground_truth_never_copied(tmp_path):
    _seed_source_case(tmp_path)
    assert not (tmp_path / "data" / "raw" / "CASE_006").exists()
    assert not (tmp_path / "data" / "ground_truth" / "CASE_006").exists()


def test_next_case_id_ignores_ground_truth_root(tmp_path):
    _seed_source_case(tmp_path, "CASE_005")
    (tmp_path / "data" / "ground_truth" / "CASE_999").mkdir(parents=True)

    assert fc.next_free_case_id() == "CASE_006"


def test_data_tree_copier_rejects_ground_truth_namespace(tmp_path):
    _seed_source_case(tmp_path)

    with pytest.raises(ValueError, match="unsupported fork data namespace"):
        fc.copy_data_tree("ground_truth", "CASE_005", "CASE_006")


def test_raw_copy_rewrites_intake_record_case_id(tmp_path):
    _seed_source_case(tmp_path)
    fc.copy_data_tree("raw", "CASE_005", "CASE_006")
    record = json.loads((tmp_path / "data" / "raw" / "CASE_006" / "_intake_record.json").read_text(encoding="utf-8"))
    assert record["case_id"] == "CASE_006"


# --------------------------------------------------------------- lock check --

def test_refuses_to_fork_with_an_active_lock_present(tmp_path):
    out_dir = _seed_source_case(tmp_path)
    target = out_dir / "extracted_claim_fields.json"
    assert dao.acquire_lock(target, "someone", "r", "mid-write") is None
    try:
        with pytest.raises(SystemExit):
            fc.check_no_active_locks(out_dir)
    finally:
        dao.release_lock(target)


def test_persistent_unlocked_lock_inode_passes_the_check(tmp_path):
    out_dir = _seed_source_case(tmp_path)
    target = out_dir / "extracted_claim_fields.json"
    assert dao.acquire_lock(target, "someone", "r", "completed write") is None
    dao.release_lock(target)

    fc.check_no_active_locks(out_dir)


def test_no_lock_present_passes_the_check(tmp_path):
    out_dir = _seed_source_case(tmp_path)
    fc.check_no_active_locks(out_dir)  # should not raise


# ------------------------------------------------------------- backup steps --

def test_from_step_resolves_the_matching_backup_dir(tmp_path):
    out_dir = _seed_source_case(tmp_path)
    backups = out_dir / "_backups"
    backups.mkdir()
    step_dir = backups / "step_02_document-pipeline"
    step_dir.mkdir()
    (step_dir / "extracted_claim_fields.json").write_text(json.dumps({"case_id": "CASE_005"}), encoding="utf-8")

    resolved = fc.resolve_source_root("CASE_005", from_step=2)

    assert resolved == step_dir


def test_from_step_missing_exits(tmp_path):
    out_dir = _seed_source_case(tmp_path)
    (out_dir / "_backups").mkdir()
    with pytest.raises(SystemExit):
        fc.resolve_source_root("CASE_005", from_step=7)


def test_default_from_step_none_uses_current_state(tmp_path):
    out_dir = _seed_source_case(tmp_path)
    resolved = fc.resolve_source_root("CASE_005", from_step=None)
    assert resolved == out_dir


# ------------------------------------------------------------------- main() --

def _run_main(argv):
    import sys
    old = sys.argv
    sys.argv = ["fork_case.py"] + argv
    try:
        fc.main()
    finally:
        sys.argv = old


def test_full_fork_via_main_default_scope(tmp_path):
    _seed_source_case(tmp_path)

    _run_main(["CASE_005", "--label", "test branch", "--held-by", "tester", "--run-id", "RUN_X"])

    assert (tmp_path / "outputs" / "CASE_006").exists()
    assert (tmp_path / "data" / "processed" / "CASE_006").exists()
    assert not (tmp_path / "data" / "raw" / "CASE_006").exists()
    assert not (tmp_path / "data" / "ground_truth" / "CASE_006").exists()

    record = json.loads((tmp_path / "outputs" / "CASE_006" / "_fork_record.json").read_text(encoding="utf-8"))
    assert record["forked_from"] == "CASE_005"
    assert record["new_case_id"] == "CASE_006"
    assert record["label"] == "test branch"
    assert record["forked_at_step"] == "current"
    assert record["included_raw"] is False
    assert record["included_ground_truth"] is False


def test_ground_truth_copy_option_is_unavailable(tmp_path):
    _seed_source_case(tmp_path)

    with pytest.raises(SystemExit):
        _run_main(["CASE_005", "--label", "forbidden copy", "--include-ground-truth",
                   "--held-by", "tester", "--run-id", "RUN_X"])

    assert not (tmp_path / "data" / "ground_truth" / "CASE_006").exists()


def test_main_refuses_when_source_locked(tmp_path):
    out_dir = _seed_source_case(tmp_path)
    target = out_dir / "extracted_claim_fields.json"
    assert dao.acquire_lock(target, "someone", "r", "mid-write") is None

    try:
        with pytest.raises(SystemExit):
            _run_main(["CASE_005", "--label", "x", "--held-by", "tester", "--run-id", "RUN_X"])
    finally:
        dao.release_lock(target)

    assert not (tmp_path / "outputs" / "CASE_006").exists()


def test_main_rejects_malformed_source_case_id(tmp_path):
    with pytest.raises(SystemExit):
        _run_main(["not-a-real-id", "--label", "x", "--held-by", "tester", "--run-id", "RUN_X"])
