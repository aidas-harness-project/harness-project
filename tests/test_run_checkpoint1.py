"""run_checkpoint1.py -- the checkpoint-1 automation wrapper (OCR + classify,
stopping at a P8 disagreement; resolve_from_raw_ocr() continues past one
once a human decides). All claude subprocess calls are mocked -- these
tests never shell out to a real CLI.
"""
import json

import pytest

import dao
import run_checkpoint1 as rc1


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    data = tmp_path / "data"
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    monkeypatch.setattr(dao, "DATA", data)
    monkeypatch.setattr(rc1, "ROOT", tmp_path)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)
    return tmp_path


def _seed_manifest(tmp_path, case_id, doc_id):
    out_dir = tmp_path / "outputs" / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(out_dir / "document_manifest.json", {
        "case_id": case_id, "created_at": dao.now_iso(),
        "documents": [{"document_id": doc_id, "file_name": f"{doc_id}.pdf", "file_path": f"data/raw/{case_id}/{doc_id}.pdf",
                       "file_format": "pdf", "file_size_bytes": 1000, "ocr_status": "pending"}],
    })


def _mock_ocr(monkeypatch, pages):
    """pages: list of (reading_a, reading_b, agreement) tuples."""
    def fake_run_ocr(case_id, doc_id, pdf_path, progress=None):
        return {"document_path": str(pdf_path), "pages": [
            {"page": i, "reading_a": a, "reading_b": b, "agreement": agree,
             "disagreement_details": [] if agree == "agreed" else ["DISAGREE: mock"]}
            for i, (a, b, agree) in enumerate(pages, start=1)
        ]}
    monkeypatch.setattr(rc1, "run_ocr", fake_run_ocr)


def _mock_classify(monkeypatch, doc_type="insurer_response", label="보험사 회신"):
    def fake_classify(text):
        return {"predicted_document_type": doc_type, "document_type_label": label,
                "confidence": 0.9, "quote": text[:20]}
    monkeypatch.setattr(rc1, "classify_document", fake_classify)


def test_all_agreed_passes_through_to_classification(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("page one text", "page one text b", "agreed"),
                            ("page two text", "page two text b", "agreed")])
    _mock_classify(monkeypatch)

    result = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    assert result["status"] == "passed"
    assert result["document_type"] == "insurer_response"
    ocr_result = json.loads((tmp_path / "outputs" / "CASE_009" / "ocr_result_DOC_001.json").read_text(encoding="utf-8"))
    assert ocr_result["cross_validation_status"] == "agreed"
    classification = json.loads((tmp_path / "outputs" / "CASE_009" / "classification_result_DOC_001.json").read_text(encoding="utf-8"))
    assert classification["predicted_document_type"] == "insurer_response"
    manifest = json.loads((tmp_path / "outputs" / "CASE_009" / "document_manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"][0]["ocr_status"] == "completed"
    assert manifest["documents"][0]["document_type"] == "insurer_response"
    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["status"] == "passed"


def test_agreed_pages_written_to_processed_layer(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("real page text", "real page text b", "agreed")])
    _mock_classify(monkeypatch)

    rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    page_path = tmp_path / "data" / "processed" / "CASE_009" / "DOC_001" / "page_001.md"
    assert page_path.read_text(encoding="utf-8") == "real page text"


def test_disagreement_blocks_and_does_not_classify(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("page one A", "page one B", "agreed"),
                            ("page two A", "page two B", "disagreed")])
    classify_called = []
    monkeypatch.setattr(rc1, "classify_document", lambda text: classify_called.append(text) or {})

    result = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    assert result["status"] == "blocked_disagreement"
    assert result["disagreed_pages"] == [2]
    assert classify_called == [], "must not classify while a disagreement is unresolved"
    assert not (tmp_path / "outputs" / "CASE_009" / "classification_result_DOC_001.json").exists()
    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["status"] == "failed", "run-state must reflect the real block, not stay untouched"


def test_blocked_run_resets_manifest_instead_of_leaving_it_stale(tmp_path, monkeypatch):
    """Real bug, found by actually running the scenario matrix against a
    forked case that had PREVIOUSLY passed: without this reset, a fresh
    run that newly finds a disagreement would leave document_manifest.json
    showing the OLD completed/passed values, directly contradicting the
    fresh ocr_result.json that says disagreed_pending_review. Not a
    fork-specific issue -- the same staleness would hit any genuine re-run
    that newly fails after a prior success."""
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    manifest_path = tmp_path / "outputs" / "CASE_009" / "document_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"][0].update({
        "ocr_status": "completed", "ocr_quality": "high", "uncertain_region_count": 0,
        "cross_validation_status": "agreed", "redacted_text_path": "data/processed/CASE_009/DOC_001/redacted_text.md",
        "document_type": "insurer_response", "classification_confidence": 0.9,
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    dao._update_run_state("CASE_009", "RUN_OLD", "document_processing", "passed", "tester")

    _mock_ocr(monkeypatch, [("A", "B", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text: {})

    rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_002")

    fresh_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc = fresh_manifest["documents"][0]
    assert doc["ocr_status"] == "failed"
    assert doc["cross_validation_status"] == "disagreed_pending_review"
    assert doc["redacted_text_path"] is None, "stale redaction path must not survive a fresh extraction failure"
    assert doc["document_type"] is None
    assert doc["classification_confidence"] is None
    state = dao.load_run_state("CASE_009")
    assert state["stages"][-1]["status"] == "failed"


def test_disagreed_page_has_no_text_path_agreed_page_does(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("agreed text", "agreed text b", "agreed"),
                            ("A version", "B version", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text: {})

    rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    ocr_result = json.loads((tmp_path / "outputs" / "CASE_009" / "ocr_result_DOC_001.json").read_text(encoding="utf-8"))
    pages = {p["page"]: p for p in ocr_result["pages"]}
    assert pages[1]["text_path"] is not None
    assert pages[2]["text_path"] is None


def test_raw_ocr_scratch_saved_when_blocked(tmp_path, monkeypatch):
    """This is what lets a *separate, later* process resolve the
    disagreement without re-running real OCR -- reading_a's full text
    isn't retained in ocr_result.json itself (only reading_b is)."""
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("the real reading_a text", "a different reading_b text", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text: {})

    result = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    raw_path = tmp_path / "_ocr_scratch" / "CASE_009_DOC_001_raw.json"
    assert raw_path.exists()
    assert result["raw_ocr_path"] == str(raw_path)
    saved = json.loads(raw_path.read_text(encoding="utf-8"))
    assert saved["pages"][0]["reading_a"] == "the real reading_a text"


def test_resolve_from_raw_ocr_completes_a_single_page_document(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("A reading", "B reading", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text: {})

    blocked = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")
    assert blocked["status"] == "blocked_disagreement"

    ocr_data = json.loads((tmp_path / "_ocr_scratch" / "CASE_009_DOC_001_raw.json").read_text(encoding="utf-8"))
    _mock_classify(monkeypatch, doc_type="medical_record", label="의무기록")

    result = rc1.resolve_from_raw_ocr("CASE_009", "DOC_001", ocr_data, page=1, chosen_reading="reading_b",
                                       resolved_by="Dev", note="verified against raw image",
                                       held_by="tester", run_id="RUN_20260713_002")

    assert result["status"] == "passed"
    assert result["document_type"] == "medical_record"
    page_path = tmp_path / "data" / "processed" / "CASE_009" / "DOC_001" / "page_001.md"
    assert page_path.read_text(encoding="utf-8") == "B reading"
    ocr_result = json.loads((tmp_path / "outputs" / "CASE_009" / "ocr_result_DOC_001.json").read_text(encoding="utf-8"))
    assert ocr_result["cross_validation_status"] == "disagreed_resolved"
    resolution = ocr_result["pages"][0]["cross_validation"]["resolution"]
    assert resolution == {"chosen_reading": "reading_b", "resolved_by": "Dev",
                           "resolved_at": resolution["resolved_at"], "note": "verified against raw image"}


def test_resolve_from_raw_ocr_partial_when_multiple_disagreements(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr(monkeypatch, [("p1 A", "p1 B", "disagreed"), ("p2 A", "p2 B", "disagreed")])
    monkeypatch.setattr(rc1, "classify_document", lambda text: {})

    blocked = rc1.run_checkpoint1("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")
    ocr_data = json.loads((tmp_path / "_ocr_scratch" / "CASE_009_DOC_001_raw.json").read_text(encoding="utf-8"))

    result = rc1.resolve_from_raw_ocr("CASE_009", "DOC_001", ocr_data, page=1, chosen_reading="reading_a",
                                       resolved_by="Dev", note="n", held_by="tester", run_id="RUN_20260713_002")

    assert result["status"] == "partially_resolved"
    assert result["still_unresolved"] == [2]
    assert not (tmp_path / "outputs" / "CASE_009" / "classification_result_DOC_001.json").exists()


def test_resolve_from_raw_ocr_rejects_bad_chosen_reading(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    ocr_data = {"pages": [{"page": 1, "reading_a": "a", "reading_b": "b"}]}
    with pytest.raises(SystemExit):
        rc1.resolve_from_raw_ocr("CASE_009", "DOC_001", ocr_data, page=1, chosen_reading="reading_c",
                                  resolved_by="Dev", note="n", held_by="tester", run_id="RUN_20260713_001")


def test_classify_document_parses_real_shaped_response(monkeypatch):
    from unittest import mock

    def fake_run(cmd, **kw):
        r = mock.Mock()
        r.returncode = 0
        r.stdout = '{"predicted_document_type": "insurer_response", "document_type_label": "회신", "confidence": 0.95, "quote": "sample"}'
        r.stderr = ""
        return r
    monkeypatch.setattr(rc1.subprocess, "run", fake_run)

    result = rc1.classify_document("some document text")
    assert result["predicted_document_type"] == "insurer_response"
    assert result["confidence"] == 0.95


def test_classify_document_fails_loud_on_unparseable_response(monkeypatch):
    from unittest import mock

    def fake_run(cmd, **kw):
        r = mock.Mock()
        r.returncode = 0
        r.stdout = "not json at all"
        r.stderr = ""
        return r
    monkeypatch.setattr(rc1.subprocess, "run", fake_run)

    with pytest.raises(SystemExit):
        rc1.classify_document("some text")


def test_classify_document_rejects_unknown_document_type(monkeypatch):
    from unittest import mock

    def fake_run(cmd, **kw):
        r = mock.Mock()
        r.returncode = 0
        r.stdout = '{"predicted_document_type": "not_a_real_type", "confidence": 0.9, "quote": "x"}'
        r.stderr = ""
        return r
    monkeypatch.setattr(rc1.subprocess, "run", fake_run)

    with pytest.raises(SystemExit):
        rc1.classify_document("some text")
