"""Unit tests for the mechanical, pre-attempt policy UID preflight."""
import hashlib
import json

import dao
import run_policy_preflight as preflight


def test_cli_controlled_runtime_error_returns_one_and_writes_stderr(
        monkeypatch, capsys):
    monkeypatch.setattr(
        preflight, "run",
        lambda *args: (_ for _ in ()).throw(RuntimeError("controlled preflight failure")),
    )

    assert preflight.main([
        "CASE_001", "--held-by", "test", "--run-id", "RUN_20260813_001",
    ]) == 1
    captured = capsys.readouterr()
    assert captured.err == "controlled preflight failure\n"
    assert "NameError" not in captured.err


def test_preflight_holds_run_state_lock_through_uid_activation(monkeypatch):
    events = []
    lock_held = [False]

    monkeypatch.setattr(preflight.dao, "run_state_path", lambda case_id: case_id)
    monkeypatch.setattr(
        preflight.dao, "acquire_lock_blocking",
        lambda target, held_by, run_id, purpose:
            events.append("acquire") or lock_held.__setitem__(0, True) or None,
    )
    monkeypatch.setattr(
        preflight.dao, "release_lock",
        lambda target: events.append("release") or lock_held.__setitem__(0, False),
    )
    monkeypatch.setattr(preflight.dao, "read_contract_data", lambda case_id, filename:
                        {"stages": []} if filename == "_run_state.json" else
                        {"documents": [{"document_id": "DOC_001",
                                        "document_type": "insurance_policy",
                                        "downstream_disposition": "automated_text_pipeline"}]})
    monkeypatch.setattr(preflight.dao, "load_revision_index",
                        lambda case_id: {"documents": []})
    monkeypatch.setattr(
        preflight.dao, "record_source_digest",
        lambda *args: events.append("digest") or {"success": True, "messages": []},
    )
    def _enable(*args, **kwargs):
        assert lock_held[0], "the policy attempt guard must remain held"
        assert kwargs["run_state_lock_already_held"] is True
        events.append("enable")
        return {"success": True, "messages": [], "invalidated_stages": []}
    monkeypatch.setattr(preflight.dao, "enable_canonical_uids", _enable)

    result = preflight.run("CASE_001", "test", "RUN_1")

    assert result["promoted_document_ids"] == ["DOC_001"]
    assert events == ["acquire", "digest", "enable", "release"]
    assert not lock_held[0]


def test_preflight_failure_does_not_report_failed_uid_transition(monkeypatch):
    """A failed invalidation is fail-closed: no document is reported promoted."""
    monkeypatch.setattr(preflight.dao, "run_state_path", lambda case_id: case_id)
    monkeypatch.setattr(preflight.dao, "acquire_lock_blocking",
                        lambda *args: None)
    monkeypatch.setattr(preflight.dao, "release_lock", lambda target: None)
    monkeypatch.setattr(preflight.dao, "read_contract_data", lambda case_id, filename:
                        {"stages": []} if filename == "_run_state.json" else
                        {"documents": [{"document_id": "DOC_001",
                                        "document_type": "insurance_policy",
                                        "downstream_disposition": "automated_text_pipeline"}]})
    monkeypatch.setattr(preflight.dao, "load_revision_index",
                        lambda case_id: {"documents": []})
    monkeypatch.setattr(preflight.dao, "record_source_digest",
                        lambda *args: {"success": True, "messages": []})
    monkeypatch.setattr(preflight.dao, "enable_canonical_uids", lambda *args, **kwargs:
                        {"success": False, "messages": ["FAIL: invalidation failed"]})

    import pytest
    with pytest.raises(RuntimeError, match="enable-canonical-uids failed"):
        preflight.run("CASE_001", "test", "RUN_1")


def test_preflight_promotes_legacy_policy_and_releases_all_locks(
        isolated_dao, make_args, monkeypatch):
    """The full in-process migration uses only an isolated case fixture."""
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)
    case_id = "CASE_001"
    doc_id = "DOC_001"
    case = isolated_dao / "outputs" / case_id
    case.mkdir(parents=True)
    (case / "document_manifest.json").write_text(json.dumps({
        "case_id": case_id,
        "documents": [{
            "document_id": doc_id,
            "file_name": f"{doc_id}.pdf",
            "file_path": f"data/raw/{case_id}/{doc_id}.pdf",
            "file_format": "pdf", "file_size_bytes": 10,
            "ocr_status": "completed", "document_type": "insurance_policy",
            "downstream_disposition": "automated_text_pipeline",
            "extraction_method": "embedded_text",
        }],
    }), encoding="utf-8")
    raw = isolated_dao / "data" / "raw" / case_id
    raw.mkdir(parents=True)
    source = b"isolated immutable policy source"
    (raw / f"{doc_id}.pdf").write_bytes(source)
    text_file = isolated_dao / "redacted.md"
    text_file.write_text("<<<PAGE page=1>>>\npolicy text", encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id=case_id, doc_id=doc_id, text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260813_001")) == 0

    result = preflight.run(case_id, "orchestrator", "RUN_20260813_001")

    assert result["status"] == "promoted"
    assert result["promoted_document_ids"] == [doc_id]
    manifest = dao.read_contract_data(case_id, "document_manifest.json")
    assert manifest["documents"][0]["source_pdf_sha256"] == hashlib.sha256(source).hexdigest()
    assert dao.uid_scheme_for(case_id, doc_id) == "canonical_v1"
    for target in (dao.run_state_path(case_id), dao.revision_index_path(case_id),
                   case / "document_manifest.json"):
        assert not dao.lock_path(target).exists()


def test_scope_selects_only_text_processed_noncanonical_policy_documents():
    manifest = {"documents": [
        {"document_id": "DOC_001", "document_type": "insurance_policy",
         "downstream_disposition": "automated_text_pipeline"},
        {"document_id": "DOC_002", "document_type": "insurance_policy",
         "downstream_disposition": "text_only_no_normalization"},
        {"document_id": "DOC_003", "document_type": "insurance_policy",
         "downstream_disposition": "expert_review_only"},
        {"document_id": "DOC_004", "document_type": "medical_record",
         "downstream_disposition": "automated_text_pipeline"},
    ]}
    revisions = {"documents": [
        {"document_id": "DOC_001", "uid_scheme": "canonical_v1"},
        {"document_id": "DOC_002", "uid_scheme": "legacy"},
    ]}

    selected, canonical = preflight._in_scope_doc_ids(manifest, revisions)

    assert selected == ["DOC_002"]
    assert canonical == ["DOC_001"]
