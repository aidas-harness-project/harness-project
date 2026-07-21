"""run_scenario_matrix.py -- branches a real P8 disagreement into
reading_a / reading_b / unresolved outcomes via fork_case.py, running real
OCR only once. Provider calls are mocked; these tests never shell out to a
real CLI or call an external API.
"""
import json

import pytest

import dao
import fork_case as fc
import run_checkpoint1 as rc1
import run_scenario_matrix as rsm


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    data = tmp_path / "data"
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    monkeypatch.setattr(dao, "DATA", data)
    monkeypatch.setattr(rc1, "ROOT", tmp_path)
    monkeypatch.setattr(fc, "OUTPUTS", outputs)
    monkeypatch.setattr(fc, "DATA", data)
    monkeypatch.setattr(rsm, "ROOT", tmp_path)
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


def _mock_ocr_once(monkeypatch, pages):
    calls = []

    def fake_run_ocr(case_id, doc_id, pdf_path, progress=None, **kwargs):
        calls.append((case_id, doc_id))
        return {"document_path": str(pdf_path), "pages": [
            {"page": i, "reading_a": a, "reading_b": b, "agreement": agree,
             "disagreement_details": [] if agree == "agreed" else ["DISAGREE: mock"]}
            for i, (a, b, agree) in enumerate(pages, start=1)
        ]}
    monkeypatch.setattr(rc1, "run_ocr", fake_run_ocr)
    return calls


def _mock_classify(monkeypatch):
    monkeypatch.setattr(rc1, "classify_document", lambda text, classifier=None: {
        "predicted_document_type": "insurer_response", "document_type_label": "회신",
        "confidence": 0.9, "quote": text[:20],
    })


def test_no_disagreement_reports_and_does_not_fork(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr_once(monkeypatch, [("clean text", "clean text b", "agreed")])
    _mock_classify(monkeypatch)

    matrix = rsm.run_matrix("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    assert matrix["disagreement_found"] is False
    assert not (tmp_path / "outputs" / "CASE_010").exists(), "no fork should have happened"


def test_real_disagreement_produces_three_forks_ocr_runs_once(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    ocr_calls = _mock_ocr_once(monkeypatch, [("A version", "B version", "disagreed")])
    _mock_classify(monkeypatch)

    matrix = rsm.run_matrix("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    assert matrix["disagreement_found"] is True
    assert len(ocr_calls) == 1, "real OCR must run exactly once no matter how many scenarios follow"
    assert set(matrix["scenarios"].keys()) == {"reading_a", "reading_b", "unresolved"}

    fork_ids = {r["fork_case_id"] for r in matrix["scenarios"].values()}
    assert len(fork_ids) == 3, "each scenario must get its own distinct fork"
    assert "CASE_009" not in fork_ids, "scenarios fork off the baseline, not overwrite it"


def test_reading_a_and_reading_b_scenarios_pass_with_different_text(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr_once(monkeypatch, [("the A text", "the B text", "disagreed")])
    _mock_classify(monkeypatch)

    matrix = rsm.run_matrix("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    a_result = matrix["scenarios"]["reading_a"]
    b_result = matrix["scenarios"]["reading_b"]
    assert a_result["status"] == "passed"
    assert b_result["status"] == "passed"

    a_page = tmp_path / "data" / "processed" / a_result["fork_case_id"] / "DOC_001" / "page_001.md"
    b_page = tmp_path / "data" / "processed" / b_result["fork_case_id"] / "DOC_001" / "page_001.md"
    assert a_page.read_text(encoding="utf-8") == "the A text"
    assert b_page.read_text(encoding="utf-8") == "the B text"


def test_unresolved_scenario_stays_blocked(tmp_path, monkeypatch):
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr_once(monkeypatch, [("A", "B", "disagreed")])
    _mock_classify(monkeypatch)

    matrix = rsm.run_matrix("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    unresolved = matrix["scenarios"]["unresolved"]
    assert unresolved["status"] == "left_unresolved"
    assert unresolved["document_manifest_ocr_status"] == "failed", \
        "the fork's manifest is the copied blocked state -- correctly reset to failed by " \
        "run_checkpoint1's manifest-staleness fix, not left showing a stale prior value"
    fork_id = unresolved["fork_case_id"]
    assert not (tmp_path / "outputs" / fork_id / "classification_result_DOC_001.json").exists()


def test_scenario_resolution_notes_are_marked_as_automated_not_verified(tmp_path, monkeypatch):
    """Important distinction from a real resolution (like the I67/I67.8
    one) -- this is forcing an outcome to observe behavior, not a genuine
    human verification. The note must say so, so nobody mistakes a
    scenario-matrix fork for a trustworthy resolved case."""
    _seed_manifest(tmp_path, "CASE_009", "DOC_001")
    _mock_ocr_once(monkeypatch, [("A", "B", "disagreed")])
    _mock_classify(monkeypatch)

    matrix = rsm.run_matrix("CASE_009", "DOC_001", "fake.pdf", "tester", "RUN_20260713_001")

    fork_id = matrix["scenarios"]["reading_a"]["fork_case_id"]
    ocr_result = json.loads((tmp_path / "outputs" / fork_id / "ocr_result_DOC_001.json").read_text(encoding="utf-8"))
    note = ocr_result["pages"][0]["cross_validation"]["resolution"]["note"]
    assert "not a real verified resolution" in note or "Do not treat" in note
