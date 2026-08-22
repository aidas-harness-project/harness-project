"""`fork_case.py --through-stage document_processing`: the V4 cold-fork mode.

A completed source case carries every downstream contract, so an ordinary
fork of it arrives WARM -- the driver would return `reused` and an agent
would see a finished contract, which makes a cold comparison impossible.
`--from-step` cannot help a source with no P10 backup.

These tests use isolated tmp_path roots only. No real case is forked.
"""
import json

import pytest

import dao
import fork_case as fc
import stage_dependencies


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    data = tmp_path / "data"
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    monkeypatch.setattr(dao, "DATA", data)
    monkeypatch.setattr(fc, "OUTPUTS", outputs)
    monkeypatch.setattr(fc, "DATA", data)
    return tmp_path


DOCS = ["DOC_001", "DOC_002"]


def _seed_completed_case(tmp_path, case_id="CASE_140", *,
                         document_processing="passed", with_redacted=True,
                         legacy_ledger=False):
    """A source shaped like a real completed case: stage-2 state PLUS every
    later contract, driver receipts, traces and attempt history."""
    out = tmp_path / "outputs" / case_id
    out.mkdir(parents=True)

    manifest = {"case_id": case_id, "documents": [
        {"document_id": doc, "file_name": f"{doc}.pdf",
         "file_path": f"data/raw/{case_id}/{doc}.pdf",
         "file_format": "pdf", "file_size_bytes": 1024,
         "ocr_status": "completed",
         "source_file_name": f"{doc}.pdf",
         "source_page_start": 1, "source_page_end": 1,
         "document_type": "diagnosis_certificate",
         "downstream_disposition": "automated_text_pipeline"}
        for doc in DOCS]}
    (out / "document_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    entries = [{"file_name": "a.pdf", "classification": "raw",
                "review_status": "approved", "reviewed_by": "human",
                "reviewed_at": "2026-07-01T00:00:00+09:00", "rejection_reason": None}]
    # Built literally from source_ledger.schema.json's own $defs rather than a
    # dao helper: `dao.make_history_boundary` does not exist (the schema
    # requires the field but the DAO was never given a builder), which is what
    # the pre-existing tests/test_fork_case.py failures are about.
    ledger = {
        "ledger_version": "source_ledger.v0.4", "case_id": case_id,
        "source_dir": "x", "files": entries,
        "history_boundary": {
            "mode": "legacy_snapshot",
            "established_at": "2026-07-01T00:00:00+09:00",
            "baseline_sha256": "b" * 64,
            "baseline_state": entries,
        },
        "operations": [],
    }
    if legacy_ledger:
        for field in ("ledger_version", "history_boundary", "operations"):
            del ledger[field]
    (out / "_source_ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

    def _backup(stage):
        return f"outputs/{case_id}/_backups/step_01_{stage}"

    stages = [{"stage_name": "intake", "status": "passed", "attempt_count": 1,
               "backup_path": _backup("intake")},
              {"stage_name": "document_processing", "status": document_processing,
               "attempt_count": 2,
               "backup_path": (_backup("document_processing")
                               if document_processing == "passed" else None)}]
    for later in ("policy_clause_processing", "claim_analysis", "screening_report"):
        stages.append({"stage_name": later, "status": "passed", "attempt_count": 1,
                       "backup_path": _backup(later)})
    (out / "_run_state.json").write_text(json.dumps({
        "run_state_version": "run_state.v0.3", "case_id": case_id,
        "run_id": "RUN_20260701_001", "created_at": "2026-07-01T00:00:00+09:00",
        "updated_at": "2026-07-02T00:00:00+09:00",
        "medical_review_adopted": False, "stages": stages,
        "human_input_status": [],
    }), encoding="utf-8")

    (out / "page_chunks.json").write_text(json.dumps(
        {"case_id": case_id, "component": "document-pipeline", "status": "success",
         "created_at": "2026-07-01T00:00:00+09:00",
         "chunks": [{"chunk_id": f"CHUNK_{index}", "document_id": doc,
                     "page_start": 1, "page_end": 1, "text": "redacted"}
                    for index, doc in enumerate(DOCS, start=1)]}),
        encoding="utf-8")
    (out / "_revision_index.json").write_text(json.dumps(
        {"case_id": case_id, "documents": [
            {"document_id": doc, "current_revision_sha256": "a" * 64,
             "uid_scheme": "legacy",
             "revisions": [{"revision_sha256": "a" * 64,
                            "registered_at": "2026-07-01T00:00:00+09:00",
                            "registered_by": "document-pipeline",
                            "uid_stability": "deterministic_source",
                            "extractor": {"extraction_method": "embedded_text",
                                          "cross_validation_mode": "single_technology_weak_p8_poc",
                                          "tool": "write-redacted-text",
                                          "profile_sha256": "d" * 64},
                            "page_text_sha256": [{"page": 1, "sha256": "c" * 64}],
                            "supersedes": None}]}
            for doc in DOCS]}),
        encoding="utf-8")

    for doc in DOCS:
        (out / f"ocr_result_{doc}.json").write_text(json.dumps({
            "case_id": case_id, "document_id": doc, "component": "document-pipeline",
            "status": "success", "created_at": "2026-07-01T00:00:00+09:00",
            "ocr_engine": "claude-cli", "vision_model_name": "fixture-model",
            "uncertain_confidence_threshold": 0.7, "extraction_method": "embedded_text",
            "ocr_status": "completed", "pages": [], "ocr_quality": "high",
            "cross_validation_status": "agreed",
            "cross_validation_mode": "single_technology_weak_p8_poc",
            "cross_validation_note": "fixture",
        }), encoding="utf-8")
        (out / f"classification_result_{doc}.json").write_text(json.dumps({
            "case_id": case_id, "document_id": doc, "component": "document-pipeline",
            "status": "success", "created_at": "2026-07-01T00:00:00+09:00",
            "predicted_document_type": "diagnosis_certificate", "confidence": 0.9,
            "evidence_references": [{"document_id": doc, "page": 1, "quote": "진단서"}],
            "review_required": False,
        }), encoding="utf-8")
        (out / f"redaction_result_{doc}.json").write_text(json.dumps({
            "case_id": case_id, "document_id": doc, "component": "document-pipeline",
            "status": "success", "created_at": "2026-07-01T00:00:00+09:00",
            "method": "llm_span_substitution",
            "redacted_text_path": f"data/processed/{case_id}/{doc}/redacted_text.md",
            "items_redacted": 0, "review_required": False,
        }), encoding="utf-8")

    # Everything the cut must NOT carry over.
    for later in ("extracted_claim_fields.json", "coverage_result.json",
                  "case_type_result.json", "requirement_matching_result.json",
                  "denial_reason_result.json", "screening_report.json",
                  "critic_result_v1.json", "draft_report_metadata_v1.json"):
        (out / later).write_text(json.dumps({"case_id": case_id}), encoding="utf-8")
    (out / "_timing_summary.json").write_text(json.dumps({"case_id": case_id}),
                                              encoding="utf-8")
    (out / "draft_report_v1.md").write_text("draft", encoding="utf-8")

    receipts = out / "_driver_receipts" / "denial_response" / "initial_extraction"
    receipts.mkdir(parents=True)
    (receipts.parent / "initial_extraction.json").write_text(
        json.dumps({"case_id": case_id}), encoding="utf-8")
    (receipts / "DOC_001.json").write_text(json.dumps({"case_id": case_id}),
                                           encoding="utf-8")
    (out / "_medical_review").mkdir()
    (out / "_medical_review" / "ledger.json").write_text("{}", encoding="utf-8")
    (out / "_human_review").mkdir()
    (out / "_human_review" / "handoff.json").write_text("{}", encoding="utf-8")
    (out / "_trace").mkdir()
    (out / "_trace" / "shard.json").write_text("{}", encoding="utf-8")
    (out / "_backups").mkdir()
    (out / "_backups" / "step_01").mkdir()
    (out / "_backups" / "step_01" / "source_backup_only.json").write_text(
        json.dumps({"case_id": case_id, "source_only": True}), encoding="utf-8")

    if with_redacted:
        for doc in DOCS:
            d = tmp_path / "data" / "processed" / case_id / doc
            d.mkdir(parents=True)
            (d / "redacted_text.md").write_text(
                f"<<<PAGE page=1>>>\nredacted {doc}\n", encoding="utf-8")
    return out


def _snapshot(root):
    """Content map of a tree, for proving the source is untouched."""
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _snapshot_without_backups(root):
    """P10 snapshots contain the live tree, except they never nest backups."""
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and "_backups" not in p.relative_to(root).parts}


def _run_cut(tmp_path, monkeypatch, source="CASE_140", target="CASE_301",
             through="document_processing", extra=None):
    argv = ["fork_case.py", source, "--through-stage", through,
            "--label", "V4a cold fork", "--held-by", "orchestrator",
            "--run-id", "RUN_20260813_200", "--new-case-id", target]
    monkeypatch.setattr(fc.sys, "argv", argv + (extra or []))
    fc.main()
    return tmp_path / "outputs" / target


def _run_reconstruction(tmp_path, monkeypatch, source="CASE_140", target="CASE_304",
                        extra=None):
    argv = ["fork_case.py", source, "--reconstruct-through-stage", "document_processing",
            "--label", "V4a reconstructed Stage-2 source", "--held-by", "orchestrator",
            "--run-id", "RUN_20260813_204", "--new-case-id", target]
    monkeypatch.setattr(fc.sys, "argv", argv + (extra or []))
    fc.main()
    return tmp_path / "outputs" / target


# --------------------------------------------------------------- source safety


def test_source_case_is_byte_for_byte_unchanged(tmp_path, monkeypatch, capsys):
    src = _seed_completed_case(tmp_path)
    before = _snapshot(src)
    before_processed = _snapshot(tmp_path / "data" / "processed" / "CASE_140")

    _run_cut(tmp_path, monkeypatch)

    assert _snapshot(src) == before
    assert _snapshot(tmp_path / "data" / "processed" / "CASE_140") == before_processed


# ------------------------------------------------------------ retained inputs


def test_fork_keeps_manifest_and_processed_input_digests(tmp_path, monkeypatch, capsys):
    _seed_completed_case(tmp_path)
    dest = _run_cut(tmp_path, monkeypatch)

    source_manifest = json.loads(
        (tmp_path / "outputs" / "CASE_140" / "document_manifest.json").read_text(encoding="utf-8"))
    forked_manifest = json.loads((dest / "document_manifest.json").read_text(encoding="utf-8"))
    assert forked_manifest["case_id"] == "CASE_301"
    assert forked_manifest["documents"] == source_manifest["documents"]

    for doc in DOCS:
        original = (tmp_path / "data" / "processed" / "CASE_140" / doc / "redacted_text.md").read_bytes()
        copied = (tmp_path / "data" / "processed" / "CASE_301" / doc / "redacted_text.md").read_bytes()
        assert copied == original

    # Reviewed intake state carries over so the fork need not repeat D2 review.
    assert (dest / "_source_ledger.json").exists()
    assert (dest / "_revision_index.json").exists()
    assert (dest / "page_chunks.json").exists()
    for doc in DOCS:
        assert (dest / f"ocr_result_{doc}.json").exists()
        assert (dest / f"redaction_result_{doc}.json").exists()
        assert (dest / f"classification_result_{doc}.json").exists()


# ----------------------------------------------------------- excluded warmth


def test_fork_has_no_claim_analysis_or_downstream_artifacts(tmp_path, monkeypatch, capsys):
    _seed_completed_case(tmp_path)
    dest = _run_cut(tmp_path, monkeypatch)

    for warm in ("extracted_claim_fields.json", "coverage_result.json",
                 "case_type_result.json", "requirement_matching_result.json",
                 "denial_reason_result.json", "screening_report.json",
                 "critic_result_v1.json", "draft_report_metadata_v1.json",
                 "draft_report_v1.md", "_timing_summary.json"):
        assert not (dest / warm).exists(), f"{warm} leaked into the cold fork"

    assert not (dest / "_driver_receipts").exists()
    assert not (dest / "_trace").exists()
    assert not (dest / "_medical_review").exists()
    assert not (dest / "_human_review").exists()
    assert not list(dest.rglob("*.lock"))
    # The source's backup history is NOT inherited: the only backup present is
    # the fork's own origin snapshot, which its passed stages point at.
    assert [p.name for p in (dest / "_backups").iterdir()] == ["step_01_fork_origin"]
    assert not (dest / "_backups" / "step_01_fork_origin" /
                "source_backup_only.json").exists()


# --------------------------------------------------------------- run state


def test_fork_run_state_resumes_after_document_processing(tmp_path, monkeypatch, capsys):
    _seed_completed_case(tmp_path)
    dest = _run_cut(tmp_path, monkeypatch)
    state = json.loads((dest / "_run_state.json").read_text(encoding="utf-8"))

    statuses = {s["stage_name"]: s["status"] for s in state["stages"]}
    assert statuses == {"intake": "passed", "document_processing": "passed"}
    assert state["case_id"] == "CASE_301"
    assert state["run_id"] == "RUN_20260813_200"
    assert "CASE_140" not in json.dumps(state)
    # No inherited attempt history, and no fabricated human review.
    assert "human_input_status" not in state
    assert all("current_attempt_started_at" not in s for s in state["stages"])
    # PRESENT and null, not absent. The intent here is unchanged -- the fork
    # inherited this stage's output and did not run it, so it claims no start
    # or completion time -- but absence was the wrong way to say it: the stage
    # item declares both nullable and names no `required` list, so an entry
    # missing them validated, and `_update_run_state` then read
    # `entry["started_at"]` directly and raised KeyError on the first
    # `in_progress` transition of every forked case (CASE_054, 2026-08-22).
    # Null says the same thing and survives the round trip.
    assert all(s["started_at"] is None and s["completed_at"] is None
               for s in state["stages"])
    # The source recorded 2 attempts for document_processing; the fork has
    # made none of them, so P9's budget is not pre-spent.
    assert {s["stage_name"]: s["attempt_count"] for s in state["stages"]} == {
        "intake": 1, "document_processing": 1}

    # The cut genuinely unblocks the next stage rather than merely claiming to.
    frontier = stage_dependencies.parallel_frontier(state)
    assert "policy_clause_processing" in frontier
    assert not stage_dependencies.check_dependencies(
        "policy_clause_processing", "in_progress", state)
    # claim_analysis stays blocked on its real prerequisite -- documented, not a bug.
    assert stage_dependencies.check_dependencies("claim_analysis", "in_progress", state)


def test_the_fork_survives_its_first_real_stage_transition(
        tmp_path, monkeypatch, capsys):
    """The gap every other test in this file left open.

    They all read the written file and assert its shape. None of them fed that
    file back to the DAO, so nothing noticed that `_update_run_state` reads
    `entry["started_at"]` unguarded: the fork reported success, every artifact
    validated, and the failure surfaced later in an unrelated command --

        File "tools/dao.py", line 7008, in _update_run_state
          entry["started_at"] = entry["started_at"] or now
        KeyError: 'started_at'

    -- which blocked EVERY forked case at its first `in_progress` transition
    (CASE_054, 2026-08-22). A shape assertion cannot catch a shape the writer
    and the reader disagree about; only the round trip can.

    The stage re-opened here is an INHERITED one. A fresh stage gets a
    default-constructed entry that carries the key either way, so opening one
    of those passes with the defect in place and proves nothing; only a
    rebuilt entry exercises the disagreement.
    """
    _seed_completed_case(tmp_path)
    dest = _run_cut(tmp_path, monkeypatch)
    case_id = dest.name

    dao._update_run_state(
        case_id, "RUN_20260813_200", "document_processing",
        "in_progress", "orchestrator")

    state = json.loads((dest / "_run_state.json").read_text(encoding="utf-8"))
    reopened = next(s for s in state["stages"]
                    if s["stage_name"] == "document_processing")
    assert reopened["status"] == "in_progress"
    assert reopened["started_at"], "an opened attempt must carry a start time"


def test_backup_path_points_inside_the_fork_and_the_snapshot_exists(
        tmp_path, monkeypatch, capsys):
    """P10: a passed stage must be restorable from the snapshot it names.

    The first implementation copied the SOURCE's backup_path, which satisfied
    the schema's non-null rule while naming `outputs/CASE_140/_backups/...` --
    a directory the fork does not contain.
    """
    _seed_completed_case(tmp_path)
    dest = _run_cut(tmp_path, monkeypatch)
    state = json.loads((dest / "_run_state.json").read_text(encoding="utf-8"))

    for stage in state["stages"]:
        path = stage["backup_path"]
        assert path is not None, f"{stage['stage_name']} passed with no snapshot"
        # Names the NEW case, never the source.
        assert path.startswith("outputs/CASE_301/_backups/"), path
        assert "CASE_140" not in path
        # And the directory it names actually exists on disk.
        assert (tmp_path / path).is_dir(), f"{path} does not exist"


def test_fork_origin_snapshot_content_matches_retained_artifacts(
        tmp_path, monkeypatch, capsys):
    """The snapshot must hold the fork's own retained set -- not the warm tree.

    Restoring from an inherited source backup would resurrect exactly the
    downstream contracts the cut removed.
    """
    _seed_completed_case(tmp_path)
    dest = _run_cut(tmp_path, monkeypatch)
    snapshot = dest / "_backups" / "step_01_fork_origin"

    assert _snapshot(snapshot) == _snapshot_without_backups(dest)

    # Warm artifacts must not reappear through the snapshot.
    for warm in ("extracted_claim_fields.json", "coverage_result.json",
                 "case_type_result.json", "requirement_matching_result.json",
                 "screening_report.json", "_timing_summary.json"):
        assert not (snapshot / warm).exists(), f"{warm} restorable from snapshot"

    # Snapshot contents carry the NEW case id, not the source's.
    snap_manifest = json.loads((snapshot / "document_manifest.json").read_text(encoding="utf-8"))
    assert snap_manifest["case_id"] == "CASE_301"
    # And the recorded run state inside the snapshot is the finalized one.
    snap_state = json.loads((snapshot / "_run_state.json").read_text(encoding="utf-8"))
    assert snap_state == json.loads((dest / "_run_state.json").read_text(encoding="utf-8"))

    # The snapshot itself is not nested inside the snapshot.
    assert not (snapshot / "_backups").exists()


# --------------------------------------------------------- existing fork modes


def test_default_and_from_step_forks_still_copy_the_requested_state(
        tmp_path, monkeypatch, capsys):
    """Stage-cut dispatch must not alter the established default/step paths."""
    source = tmp_path / "outputs" / "CASE_140"
    source.mkdir(parents=True)
    (source / "artifact.json").write_text(
        json.dumps({"case_id": "CASE_140", "value": "current"}), encoding="utf-8")
    step = source / "_backups" / "step_01_intake"
    step.mkdir(parents=True)
    (step / "artifact.json").write_text(
        json.dumps({"case_id": "CASE_140", "value": "snapshot"}), encoding="utf-8")

    base = ["fork_case.py", "CASE_140", "--label", "regression", "--held-by",
            "orchestrator", "--run-id", "RUN_20260813_200"]
    monkeypatch.setattr(fc.sys, "argv", base + ["--new-case-id", "CASE_302"])
    fc.main()
    assert json.loads((tmp_path / "outputs" / "CASE_302" / "artifact.json").read_text(
        encoding="utf-8")) == {"case_id": "CASE_302", "value": "current"}

    monkeypatch.setattr(fc.sys, "argv", base + ["--from-step", "1", "--new-case-id", "CASE_303"])
    fc.main()
    assert json.loads((tmp_path / "outputs" / "CASE_303" / "artifact.json").read_text(
        encoding="utf-8")) == {"case_id": "CASE_303", "value": "snapshot"}


def test_rebuilt_run_state_validates_against_its_schema(tmp_path, monkeypatch, capsys):
    from _validation import load_registry, validate_instance
    _seed_completed_case(tmp_path)
    dest = _run_cut(tmp_path, monkeypatch)
    state = json.loads((dest / "_run_state.json").read_text(encoding="utf-8"))
    schemas, registry = load_registry()
    assert validate_instance(state, "run_state.schema.json", schemas, registry) == []


# -------------------------------------------------------- reconstructed cut


def test_reconstruction_validates_then_rebuilds_an_in_progress_stage2_boundary(
        tmp_path, monkeypatch, capsys):
    src = _seed_completed_case(tmp_path, document_processing="in_progress")
    before_outputs = _snapshot(src)
    before_processed = _snapshot(tmp_path / "data" / "processed" / "CASE_140")

    dest = _run_reconstruction(tmp_path, monkeypatch)
    state = json.loads((dest / "_run_state.json").read_text(encoding="utf-8"))
    record = json.loads((dest / "_fork_record.json").read_text(encoding="utf-8"))

    assert _snapshot(src) == before_outputs
    assert _snapshot(tmp_path / "data" / "processed" / "CASE_140") == before_processed
    assert {entry["stage_name"]: entry["status"] for entry in state["stages"]} == {
        "intake": "passed", "document_processing": "passed"}
    assert record["forked_at_step"] == "reconstructed_through_stage:document_processing"
    assert record["reconstruction"]["source_document_processing_status"] == "in_progress"
    assert "legacy_source_ledger_migration" not in record
    assert len(record["reconstruction"]["retained_boundary_sha256"]) == 64
    assert "CASE_140" not in json.dumps(state)
    assert _snapshot(dest / "_backups" / "step_01_fork_origin") == \
        _snapshot_without_backups(dest)
    frontier = stage_dependencies.parallel_frontier(state)
    assert "policy_clause_processing" in frontier
    assert stage_dependencies.check_dependencies("claim_analysis", "in_progress", state)


def test_reconstruction_adapts_only_a_complete_approved_legacy_source_ledger(
        tmp_path, monkeypatch, capsys):
    from _validation import load_registry, validate_instance
    src = _seed_completed_case(tmp_path, document_processing="in_progress", legacy_ledger=True)
    source_ledger_bytes = (src / "_source_ledger.json").read_bytes()
    dest = _run_reconstruction(tmp_path, monkeypatch)
    ledger = json.loads((dest / "_source_ledger.json").read_text(encoding="utf-8"))
    record = json.loads((dest / "_fork_record.json").read_text(encoding="utf-8"))
    schemas, registry = load_registry()

    assert (src / "_source_ledger.json").read_bytes() == source_ledger_bytes
    assert ledger["ledger_version"] == "source_ledger.v0.4"
    assert ledger["operations"] == []
    assert ledger["history_boundary"]["mode"] == "legacy_snapshot"
    assert ledger["files"] == json.loads(source_ledger_bytes)["files"]
    assert validate_instance(ledger, "source_ledger.schema.json", schemas, registry) == []
    migration = record["legacy_source_ledger_migration"]
    assert migration["migration_kind"] == "legacy_source_ledger_snapshot"
    assert migration["source_ledger_sha256"] == __import__("hashlib").sha256(source_ledger_bytes).hexdigest()
    assert migration["target_history_boundary_sha256"] == ledger["history_boundary"]["baseline_sha256"]
    assert migration["migrated_fields"] == ["ledger_version", "history_boundary", "operations"]
    assert set(migration) == {"migration_kind", "source_ledger_sha256",
                              "target_history_boundary_sha256", "migrated_fields", "migrated_at"}


def test_canonical_history_boundary_is_lossless_and_detached():
    entries = [{"file_name": "a.pdf", "classification": "raw", "review_status": "approved"}]
    boundary = dao.make_history_boundary(entries, mode="legacy_snapshot")
    entries[0]["review_status"] = "rejected"
    assert boundary["mode"] == "legacy_snapshot"
    assert boundary["baseline_state"][0]["review_status"] == "approved"
    assert len(boundary["baseline_sha256"]) == 64


@pytest.mark.parametrize("kind", ["pending", "rejected", "partial"])
def test_reconstruction_rejects_unsafe_or_ambiguous_legacy_ledgers_before_target(
        tmp_path, monkeypatch, capsys, kind):
    src = _seed_completed_case(tmp_path, document_processing="in_progress", legacy_ledger=True)
    ledger = json.loads((src / "_source_ledger.json").read_text(encoding="utf-8"))
    if kind == "partial":
        ledger["operations"] = []
    else:
        ledger["files"][0]["review_status"] = kind
    (src / "_source_ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_reconstruction(tmp_path, monkeypatch)
    assert not (tmp_path / "outputs" / "CASE_304").exists()


def test_reconstruction_rejects_invalid_redaction_before_creating_target(
        tmp_path, monkeypatch, capsys):
    src = _seed_completed_case(tmp_path, document_processing="in_progress")
    invalid = json.loads((src / "redaction_result_DOC_001.json").read_text(encoding="utf-8"))
    invalid["items_redacted"] = None
    (src / "redaction_result_DOC_001.json").write_text(json.dumps(invalid), encoding="utf-8")
    before = _snapshot(src)

    with pytest.raises(SystemExit) as exc:
        _run_reconstruction(tmp_path, monkeypatch)
    assert "items_redacted" in str(exc.value)
    assert not (tmp_path / "outputs" / "CASE_304").exists()
    assert _snapshot(src) == before


@pytest.mark.parametrize("failure", ["missing_text", "missing_classification",
                                      "missing_revision", "invalid_revision",
                                      "invalid_manifest", "lock"])
def test_reconstruction_fails_closed_before_target_for_incomplete_boundary(
        tmp_path, monkeypatch, capsys, failure):
    src = _seed_completed_case(tmp_path, document_processing="in_progress")
    if failure == "missing_text":
        (tmp_path / "data" / "processed" / "CASE_140" / DOCS[0] / "redacted_text.md").unlink()
    elif failure == "missing_classification":
        (src / f"classification_result_{DOCS[0]}.json").unlink()
    elif failure == "missing_revision":
        (src / "_revision_index.json").unlink()
    elif failure == "invalid_revision":
        revision = json.loads((src / "_revision_index.json").read_text(encoding="utf-8"))
        revision["documents"][0]["current_revision_sha256"] = "not-a-digest"
        (src / "_revision_index.json").write_text(json.dumps(revision), encoding="utf-8")
    elif failure == "invalid_manifest":
        (src / "document_manifest.json").write_text("{", encoding="utf-8")
    else:
        (src / "document_manifest.json.lock").write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit):
        _run_reconstruction(tmp_path, monkeypatch)
    assert not (tmp_path / "outputs" / "CASE_304").exists()


def test_reconstruction_is_mutually_exclusive_and_refuses_a_passed_source(
        tmp_path, monkeypatch, capsys):
    _seed_completed_case(tmp_path, document_processing="passed")
    with pytest.raises(SystemExit) as exc:
        _run_reconstruction(tmp_path, monkeypatch)
    assert "only for an unpassed" in str(exc.value)
    assert not (tmp_path / "outputs" / "CASE_304").exists()

    with pytest.raises(SystemExit) as exc:
        _run_reconstruction(tmp_path, monkeypatch, extra=["--through-stage", "document_processing"])
    assert "mutually exclusive" in str(exc.value)


# ------------------------------------------------------------- fail closed


def test_refuses_when_document_processing_is_not_passed(tmp_path, monkeypatch, capsys):
    """CASE_140's real shape: downstream contracts exist, run-state says in_progress."""
    _seed_completed_case(tmp_path, document_processing="in_progress")
    with pytest.raises(SystemExit) as exc:
        _run_cut(tmp_path, monkeypatch)
    assert "not 'passed'" in str(exc.value)
    assert not (tmp_path / "outputs" / "CASE_301").exists() or \
        not any((tmp_path / "outputs" / "CASE_301").iterdir())


def test_refuses_when_redacted_input_is_missing(tmp_path, monkeypatch, capsys):
    _seed_completed_case(tmp_path, with_redacted=False)
    with pytest.raises(SystemExit) as exc:
        _run_cut(tmp_path, monkeypatch)
    assert "no redacted text" in str(exc.value)


def test_refuses_when_run_state_is_absent(tmp_path, monkeypatch, capsys):
    src = _seed_completed_case(tmp_path)
    (src / "_run_state.json").unlink()
    with pytest.raises(SystemExit) as exc:
        _run_cut(tmp_path, monkeypatch)
    assert "_run_state.json" in str(exc.value)


def test_through_stage_and_from_step_are_mutually_exclusive(tmp_path, monkeypatch, capsys):
    _seed_completed_case(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run_cut(tmp_path, monkeypatch, extra=["--from-step", "1"])
    assert "mutually exclusive" in str(exc.value)


def test_unknown_through_stage_is_rejected_by_the_parser(tmp_path, monkeypatch, capsys):
    _seed_completed_case(tmp_path)
    monkeypatch.setattr(fc.sys, "argv", [
        "fork_case.py", "CASE_140", "--through-stage", "claim_analysis",
        "--label", "x", "--held-by", "o", "--run-id", "RUN_20260813_200"])
    with pytest.raises(SystemExit):
        fc.main()


def test_active_lock_still_refuses_a_stage_cut(tmp_path, monkeypatch, capsys):
    src = _seed_completed_case(tmp_path)
    (src / "document_manifest.json.lock").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _run_cut(tmp_path, monkeypatch)
    assert "lock" in str(exc.value)
