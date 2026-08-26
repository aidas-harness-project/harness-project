"""fork_case.py -- lets a branch reuse already-completed OCR/redaction/
chunking work under a fresh case_id instead of re-running it, since P10's
snapshot-backup only versions outputs/ (never data/) and case_id is the
primary key almost everywhere in the DAO (no run_id-scoped branching).
"""
import json
import re
from pathlib import Path

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
        # run_state.schema.json requires both of these; the fixture predates
        # them and only passed while dao.py's writer did not produce them
        # either (merge 3569d50 kept parent1's dao.py against a newer schema).
        "run_state_version": "run_state.v0.3",
        "medical_review_adopted": False,
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


def test_a_minted_id_always_satisfies_the_strictest_schema(tmp_path):
    """The real invariant: never mint an id some schema will later refuse.

    `human_review_ledger.schema.json` carries the tightest case_id pattern in
    the repo, so it is read here rather than restated -- a test naming a width
    would have to be edited every time the pattern moves, which is exactly how
    it drifted before.

    Until 2026-08-22 that pattern was ^CASE_[0-9]{3}$ and CASE_999 on disk made
    max+1 return CASE_1000, discovered only AFTER every file was copied and the
    case_id rewritten into each: the fork "succeeded" and left a case that could
    never accept a human-review write. The guard against that was to fall back
    to the lowest free id, which became the normal path once CASE_9001/9200/9401
    existed -- so every fork silently reused a gap (CASE_054, 2026-08-22).

    The pattern is now ^CASE_[0-9]{3,4}$ and the fallback is gone. What must
    still hold is this: whatever id is minted, the ledger accepts it.
    """
    # From this file, not from dao's roots -- conftest repoints those at
    # tmp_path, which holds no schemas.
    schema_path = (Path(__file__).resolve().parent.parent / "schemas"
                   / "human_review_ledger.schema.json")
    pattern = json.loads(schema_path.read_text(encoding="utf-8"))[
        "properties"]["case_id"]["pattern"]

    (tmp_path / "outputs" / "CASE_999").mkdir(parents=True)
    (tmp_path / "outputs" / "CASE_001").mkdir(parents=True)
    got = fc.next_free_case_id()

    assert re.fullmatch(pattern, got), (
        f"{got} does not satisfy {pattern}; the human-review ledger would "
        "refuse it the moment that ledger is written"
    )


def test_passing_the_old_ceiling_keeps_counting_up(tmp_path):
    """CASE_999 now yields CASE_1000, not a reused low gap.

    Chronological order is the point: a fork's id should sit above everything
    on disk so the numbering says when it was made. Reusing a gap can also
    resurrect an id with history attached -- CASE_002 is free in the real tree
    only because its files were rejected in the D1 incident.
    """
    (tmp_path / "outputs" / "CASE_999").mkdir(parents=True)
    (tmp_path / "outputs" / "CASE_001").mkdir(parents=True)

    assert fc.next_free_case_id() == "CASE_1000"


def test_three_digit_ids_stay_zero_padded(tmp_path):
    """Backward compatibility: CASE_002, never CASE_2.

    Every id on disk is zero-padded to three, and a bare `str(n)` would make
    the fork's id inconsistent with the whole existing tree.
    """
    (tmp_path / "outputs" / "CASE_001").mkdir(parents=True)

    assert fc.next_free_case_id() == "CASE_002"


def test_next_free_case_id_takes_max_plus_one_over_a_gap(tmp_path):
    """max+1 always, never gap-filling.

    Reusing a gap loses chronological ordering and can resurrect an id that
    has history attached -- CASE_002 is free in the real tree only because
    its files were rejected in the D1 incident. This was the rule below 999
    and the ceiling fallback broke it above; since 2026-08-22 there is no
    fallback and no ceiling below 9999, so it is simply the rule.
    """
    (tmp_path / "outputs" / "CASE_001").mkdir(parents=True)
    (tmp_path / "outputs" / "CASE_009").mkdir(parents=True)
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


def test_next_case_id_counts_the_ground_truth_root(tmp_path):
    """Inverted 2026-08-26. It used to assert `ground_truth` was IGNORED, which
    is the opposite of what the tool now does deliberately: `next_free_case_id`
    scans it alongside outputs/raw/processed so a fork cannot be handed an id
    that already names answer-key material. Observed 2026-08-22, when a fork of
    CASE_701 was assigned CASE_054 -- a collision with a real case.

    Kept and inverted rather than deleted, because collision avoidance is worth
    pinning; only the expectation was stale.
    """
    _seed_source_case(tmp_path, "CASE_005")
    (tmp_path / "data" / "ground_truth" / "CASE_999").mkdir(parents=True)

    assert fc.next_free_case_id() == "CASE_1000"


# REMOVED 2026-08-26: test_data_tree_copier_rejects_ground_truth_namespace and
# test_ground_truth_copy_option_is_unavailable. Both asserted a contract the
# tool deliberately replaced: `copy_data_tree` no longer refuses the
# `ground_truth` namespace and `--include-ground-truth` is no longer rejected.
# That is the documented behaviour -- CLAUDE.md describes the flag as
# "deliberate, not default", and fork_case.py:971 calls
# `copy_data_tree("ground_truth", ...)` as its supported path, printing a
# WARNING that names D1 rather than blocking.
#
# Deleted rather than updated because there is nothing left for them to pin:
# inverting them would only assert that a copy happens, which
# test_fork_records_the_receipt already covers via `included_ground_truth`.
#
# D1 itself is NOT weakened by their removal and is not what they guarded.
# The answer key is protected at the READ gate: `dao.cmd_read_ground_truth`
# still denies any `caller_stage != "evaluation"` and still requires the
# human-review flag, and `test_dao_human_review` pins both. Copying answer-key
# material under a second case_id leaves it just as unreadable.


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


# ------------------------------------------------- case-root path rewriting --

def test_fork_rewrites_paths_under_every_case_root(tmp_path):
    """Only the `case_id` FIELD was rewritten until 2026-08-14, so a fork's
    manifest kept pointing at the source case's raw PDFs -- and the DAO read
    them, giving a branch that shared input files with its parent while its own
    copies sat unused on disk. Measured on a real fork: 773 processed paths, 22
    outputs paths, 10 raw paths.
    """
    _seed_source_case(tmp_path)
    manifest_path = tmp_path / "outputs" / "CASE_005" / "document_manifest.json"
    manifest_path.write_text(json.dumps({
        "case_id": "CASE_005",
        "documents": [{
            "document_id": "DOC_001", "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_005/DOC_001.pdf", "file_format": "pdf",
            "file_size_bytes": 10, "ocr_status": "completed",
            "redacted_text_path": "data/processed/CASE_005/DOC_001/redacted_text.md",
            "backup_path": "outputs/CASE_005/_backups/step_01_intake",
        }],
    }), encoding="utf-8")

    fc.copy_outputs_and_rewrite_case_id(
        dao.case_dir("CASE_005"), "CASE_006", "CASE_005")

    entry = json.loads(
        (tmp_path / "outputs" / "CASE_006" / "document_manifest.json")
        .read_text(encoding="utf-8"))["documents"][0]
    assert entry["file_path"] == "data/raw/CASE_006/DOC_001.pdf"
    assert entry["redacted_text_path"] == "data/processed/CASE_006/DOC_001/redacted_text.md"
    assert entry["backup_path"] == "outputs/CASE_006/_backups/step_01_intake"


def test_fork_leaves_the_fork_record_pointing_at_its_source(tmp_path):
    """`_fork_record.json` exists to say where the fork came from. Rewriting
    the id there would erase the one record of the lineage."""
    _seed_source_case(tmp_path)
    (tmp_path / "outputs" / "CASE_005" / "_fork_record.json").write_text(
        json.dumps({"case_id": "CASE_005", "forked_from": "CASE_004"}),
        encoding="utf-8")

    fc.copy_outputs_and_rewrite_case_id(
        dao.case_dir("CASE_005"), "CASE_006", "CASE_005")

    record = json.loads(
        (tmp_path / "outputs" / "CASE_006" / "_fork_record.json")
        .read_text(encoding="utf-8"))
    assert record["forked_from"] == "CASE_004"


def test_fork_does_not_rewrite_a_bare_case_id_inside_prose(tmp_path):
    """The rewrite is anchored on `<root>/<CASE_ID>/`. A reviewer's note that
    mentions the source case is a historical statement, not a path."""
    _seed_source_case(tmp_path)
    note = "compared against CASE_005 before approving"
    (tmp_path / "outputs" / "CASE_005" / "_conflict_ledger.json").write_text(
        json.dumps({"case_id": "CASE_005", "conflicts": [
            {"conflict_id": "CONFLICT_1", "resolution_note": note}]}),
        encoding="utf-8")

    fc.copy_outputs_and_rewrite_case_id(
        dao.case_dir("CASE_005"), "CASE_006", "CASE_005")

    ledger = json.loads(
        (tmp_path / "outputs" / "CASE_006" / "_conflict_ledger.json")
        .read_text(encoding="utf-8"))
    assert ledger["conflicts"][0]["resolution_note"] == note
    assert ledger["case_id"] == "CASE_006"


def test_fork_rewrites_windows_separator_paths(tmp_path):
    """backup_path is recorded by tools using os.path, so it arrives with
    backslashes on this platform."""
    _seed_source_case(tmp_path)
    (tmp_path / "outputs" / "CASE_005" / "_run_state.json").write_text(
        json.dumps({"case_id": "CASE_005", "stages": [
            {"stage_name": "intake", "status": "passed",
             "backup_path": r"outputs\CASE_005\_backups\step_01_intake\x.json"}]}),
        encoding="utf-8")

    fc.copy_outputs_and_rewrite_case_id(
        dao.case_dir("CASE_005"), "CASE_006", "CASE_005")

    state = json.loads(
        (tmp_path / "outputs" / "CASE_006" / "_run_state.json")
        .read_text(encoding="utf-8"))
    assert state["stages"][0]["backup_path"] == \
        r"outputs\CASE_006\_backups\step_01_intake\x.json"


def test_fork_without_a_source_id_keeps_the_old_field_only_behaviour(tmp_path):
    """The parameter is optional so existing callers keep working; they just
    do not get path rewriting."""
    _seed_source_case(tmp_path)
    manifest_path = tmp_path / "outputs" / "CASE_005" / "document_manifest.json"
    manifest_path.write_text(json.dumps({
        "case_id": "CASE_005",
        "documents": [{
            "document_id": "DOC_001", "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_005/DOC_001.pdf", "file_format": "pdf",
            "file_size_bytes": 10, "ocr_status": "completed"}],
    }), encoding="utf-8")

    fc.copy_outputs_and_rewrite_case_id(dao.case_dir("CASE_005"), "CASE_006")

    entry = json.loads(
        (tmp_path / "outputs" / "CASE_006" / "document_manifest.json")
        .read_text(encoding="utf-8"))["documents"][0]
    assert entry["file_path"] == "data/raw/CASE_005/DOC_001.pdf"


def test_a_fork_starts_under_the_medical_gate(tmp_path):
    """A pre-restore source is exempt from the medical clearance gate; its
    fork must not be.

    `medical_gate_status: never_evaluated` marks a run that FINISHED before
    the 2026-08-14 restore, and the scoping decision was not to retroactively
    fail those. A fork is a different thing: new run_id, stages about to
    execute again, check available now. Observed on CASE_022 (fork of the
    pre-restore CASE_142), where `check-medical-reviews-clear` exited 1 while
    `claim_analysis` finalized `passed` -- nothing bypassed a gate, the
    exemption simply propagated through a copy.
    """
    _seed_source_case(tmp_path)
    state_path = tmp_path / "outputs" / "CASE_005" / "_run_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["medical_gate_status"] = "never_evaluated"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    fc.copy_outputs_and_rewrite_case_id(
        dao.case_dir("CASE_005"), "CASE_006", "CASE_005")

    forked = json.loads(
        (tmp_path / "outputs" / "CASE_006" / "_run_state.json")
        .read_text(encoding="utf-8"))
    assert forked["medical_gate_status"] == "enforced"


def test_a_source_with_no_gate_status_still_yields_an_enforced_fork(tmp_path):
    """The commoner shape: every case created before 2026-08-14 carries no
    `medical_gate_status` at all and is stamped `never_evaluated` on first
    touch. Copying it unchanged would hand the fork that stamp."""
    _seed_source_case(tmp_path)
    state_path = tmp_path / "outputs" / "CASE_005" / "_run_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("medical_gate_status", None)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    fc.copy_outputs_and_rewrite_case_id(
        dao.case_dir("CASE_005"), "CASE_006", "CASE_005")

    forked = json.loads(
        (tmp_path / "outputs" / "CASE_006" / "_run_state.json")
        .read_text(encoding="utf-8"))
    assert forked["medical_gate_status"] == "enforced"


def test_a_stage_cut_fork_declares_the_gate_explicitly(tmp_path):
    """The cut path rebuilds run state rather than copying it, so it needs the
    field set outright -- omitting it is not neutral, since an absent value is
    stamped `never_evaluated` on first touch."""
    state = fc.build_stage_cut_run_state(
        new_case_id="CASE_006", run_id="RUN_20260816_001",
        through_stage="document_processing",
        source_state={"stages": [{"stage_name": "intake", "status": "passed"}]})

    assert state["medical_gate_status"] == "enforced"
