"""DAO-governed cold restart for document_processing."""
from __future__ import annotations

import json

import dao


def _seed_case(tmp_path, *, segmented: bool = False):
    case = dao.case_dir("CASE_942")
    document = {
        "document_id": "DOC_001",
        "file_name": "DOC_001.pdf",
        "file_path": "data/raw/CASE_942/DOC_001.pdf",
        "file_format": "pdf",
        "file_size_bytes": 100,
        "pre_flagged_type": None,
        "pages": 2,
        "ocr_status": "completed",
        "segmentation_status": "completed" if segmented else "pending_review",
        "segmentation_reviewed_by": None,
        "segmentation_reviewed_at": None,
        "segmentation_review_note": None,
        "ocr_text_path": "data/processed/CASE_942/DOC_001",
        "ocr_quality": "low",
        "uncertain_region_count": 0,
        "cross_validation_status": "single_reader_no_cross_validation",
        "redacted_text_path": "data/processed/CASE_942/DOC_001/redacted_text.md",
        "document_type": "diagnosis_certificate",
        "classification_confidence": 0.9,
        "source_total_pages": 2,
        "extraction_method": "ocr",
        "downstream_disposition": (
            "superseded_bundle" if segmented else "automated_text_pipeline"),
        "non_text_verification": None,
    }
    if segmented:
        document["source_file_name"] = "bundle.pdf"
    dao.atomic_write_json(case / "document_manifest.json", {
        "case_id": "CASE_942", "created_at": dao.now_iso(),
        "updated_at": dao.now_iso(), "documents": [document],
    })
    dao.atomic_write_json(case / "_run_state.json", {
        "case_id": "CASE_942", "run_id": "RUN_OLD",
        "created_at": dao.now_iso(), "updated_at": dao.now_iso(),
        "stages": [{
            "stage_name": "document_processing", "status": "failed",
            "started_at": None, "completed_at": dao.now_iso(),
            "attempt_count": 0, "backup_path": None,
        }], "human_input_status": [],
    })
    for name in ("ocr_result_DOC_001.json",
                 "classification_result_DOC_001.json",
                 "redaction_result_DOC_001.json"):
        (case / name).write_text(json.dumps({"old": name}), encoding="utf-8")
    processed = tmp_path / "data" / "processed" / "CASE_942" / "DOC_001"
    processed.mkdir(parents=True)
    (processed / "page_001.md").write_text("old page", encoding="utf-8")
    return case


def test_reset_archives_contracts_and_issues_fresh_state(
        isolated_dao, monkeypatch):
    monkeypatch.setattr(dao, "ROOT", isolated_dao)
    case = _seed_case(isolated_dao)

    result = dao.reset_document_processing(
        "CASE_942", "RUN_20260812_942", "orchestrator", "CASE_942")

    assert result["status"] == "reset"
    assert not (case / "ocr_result_DOC_001.json").exists()
    backup = isolated_dao / "_stage2_reset_backups" / "CASE_942"
    assert list(backup.glob("*/outputs/ocr_result_DOC_001.json"))
    manifest = json.loads((case / "document_manifest.json").read_text(encoding="utf-8"))
    doc = manifest["documents"][0]
    assert doc["ocr_status"] == "pending"
    assert doc["segmentation_status"] == "pending_review"
    assert doc["redacted_text_path"] is None
    assert "downstream_disposition" not in doc
    state = json.loads((case / "_run_state.json").read_text(encoding="utf-8"))
    assert state["run_id"] == "RUN_20260812_942"
    assert state["stages"] == []
    assert (isolated_dao / "data" / "processed" / "CASE_942" /
            "DOC_001" / "page_001.md").exists()


def test_reset_requires_exact_case_confirmation(isolated_dao, monkeypatch):
    monkeypatch.setattr(dao, "ROOT", isolated_dao)
    case = _seed_case(isolated_dao)

    result = dao.reset_document_processing(
        "CASE_942", "RUN_NEW", "orchestrator", "CASE_943")

    assert result["status"] == "refused"
    assert (case / "ocr_result_DOC_001.json").exists()


def test_reset_refuses_segmented_case(isolated_dao, monkeypatch):
    monkeypatch.setattr(dao, "ROOT", isolated_dao)
    case = _seed_case(isolated_dao, segmented=True)

    result = dao.reset_document_processing(
        "CASE_942", "RUN_NEW", "orchestrator", "CASE_942")

    assert result["status"] == "refused"
    assert "segmented" in result["reason"]
    assert (case / "ocr_result_DOC_001.json").exists()
