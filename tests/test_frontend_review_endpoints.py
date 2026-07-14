"""frontend/backend/main.py -- the in-UI review/decision endpoints and the
honest run-status lifecycle.

Covers what the pipeline-viewer wiring pass added:
- source-file serving (read the actual object under D2 review, in the UI)
- the P8 OCR-review queue + resolve endpoint (validation and delegation;
  resolve_from_raw_ocr itself is covered by tests/test_run_checkpoint1.py)
- the P7/D1 human-review gate endpoints
- run-status telling the truth: crashed (non-zero exit) is not "finished",
  a zombie child is not "running", and a run whose Popen handle was lost to
  a backend restart is "ended_unknown", never dressed up as success.

No real dao.py subprocess or claude call is made anywhere here.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "frontend" / "backend"))

import pytest
from fastapi import HTTPException

import dao
import main


@pytest.fixture
def isolated_main(tmp_path, monkeypatch):
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "_uploads")
    monkeypatch.setattr(main, "SCRATCH_DIR", tmp_path / "_ocr_scratch")
    monkeypatch.setattr(main, "RUNS_FILE", tmp_path / "_runs.json")
    monkeypatch.setattr(main, "_PROCS", {})
    assert main.dao is dao
    return tmp_path


def _seed_case(base, case_id="CASE_009", files=("doc.pdf",), source_dir=None):
    out_dir = base / "outputs" / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(out_dir / "_source_ledger.json", {
        "case_id": case_id, "source_dir": source_dir or "source-cases/x", "created_at": dao.now_iso(),
        "updated_at": dao.now_iso(),
        "files": [{"file_name": f, "classification": "raw", "review_status": "pending",
                   "reviewed_by": None, "reviewed_at": None, "rejection_reason": None} for f in files],
    })
    return out_dir


# ---------------------------------------------------------- validators --

@pytest.mark.parametrize("doc_id", ["--held-by", "DOC_x", "doc_001", "", "DOC_1; rm"])
def test_valid_doc_id_rejects_non_matching(doc_id):
    with pytest.raises(HTTPException) as exc:
        main._valid_doc_id(doc_id)
    assert exc.value.status_code == 400


def test_valid_doc_id_accepts_real_pattern():
    main._valid_doc_id("DOC_001")  # must not raise


@pytest.mark.parametrize("name", ["", "   ", "-x", "--held-by", "  --run-id"])
def test_valid_actor_name_rejects_empty_and_flag_shaped(name):
    with pytest.raises(HTTPException) as exc:
        main._valid_actor_name(name)
    assert exc.value.status_code == 400


def test_valid_actor_name_accepts_a_real_name():
    main._valid_actor_name("김태윤")  # must not raise


# --------------------------------------------------------- source-file --

def test_source_file_unknown_name_rejected(isolated_main):
    _seed_case(isolated_main)
    with pytest.raises(HTTPException) as exc:
        main.source_file("CASE_009", "not_in_ledger.pdf")
    assert exc.value.status_code == 400


def test_source_file_served_from_staged_uploads(isolated_main):
    _seed_case(isolated_main)
    staged = isolated_main / "_uploads" / "CASE_009"
    staged.mkdir(parents=True)
    (staged / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    resp = main.source_file("CASE_009", "doc.pdf")
    assert Path(resp.path).read_bytes() == b"%PDF-1.4 fake"
    assert resp.media_type == "application/pdf"


def test_source_file_falls_back_to_ledger_source_dir(isolated_main, monkeypatch):
    src_dir = isolated_main / "source-cases" / "somewhere"
    src_dir.mkdir(parents=True)
    (src_dir / "doc.pdf").write_bytes(b"original bytes")
    monkeypatch.setattr(main, "ROOT", isolated_main)
    _seed_case(isolated_main, source_dir="source-cases/somewhere")
    resp = main.source_file("CASE_009", "doc.pdf")
    assert Path(resp.path).read_bytes() == b"original bytes"


def test_source_file_missing_everywhere_404s(isolated_main):
    _seed_case(isolated_main)
    with pytest.raises(HTTPException) as exc:
        main.source_file("CASE_009", "doc.pdf")
    assert exc.value.status_code == 404


def test_source_file_slash_in_ledger_name_rejected(isolated_main):
    # A ledger poisoned with a path-shaped name must not become a read
    # primitive -- the forbidden-chars check runs even for known entries.
    _seed_case(isolated_main, files=("../../../etc/passwd",))
    with pytest.raises(HTTPException) as exc:
        main.source_file("CASE_009", "../../../etc/passwd")
    assert exc.value.status_code == 400


# ----------------------------------------------------------- ocr-review --

def _seed_ocr_result(out_dir, doc_id="DOC_001", pages=None):
    dao.atomic_write_json(out_dir / f"ocr_result_{doc_id}.json", {
        "case_id": "CASE_009", "document_id": doc_id,
        "cross_validation_status": "disagreed_pending_review",
        "review_reason": "Page(s) [2]: disagree",
        "pages": pages if pages is not None else [
            {"page": 1, "cross_validation": {"agreement": "agreed"}},
            {"page": 2, "cross_validation": {"agreement": "disagreed", "vision_model_reading": "B-side text"}},
        ],
    })


def test_ocr_review_returns_unresolved_pages_with_both_readings(isolated_main):
    out_dir = _seed_case(isolated_main)
    _seed_ocr_result(out_dir)
    scratch = isolated_main / "_ocr_scratch"
    scratch.mkdir()
    (scratch / "CASE_009_DOC_001_raw.json").write_text(json.dumps({
        "document_path": "x", "pages": [
            {"page": 1, "reading_a": "a1", "reading_b": "b1", "agreement": "agreed", "disagreement_details": []},
            {"page": 2, "reading_a": "a2", "reading_b": "b2", "agreement": "disagreed",
             "disagreement_details": ["DISAGREE: dates differ"]},
        ],
    }), encoding="utf-8")

    result = main.ocr_review("CASE_009")
    assert len(result["documents"]) == 1
    doc = result["documents"][0]
    assert doc["doc_id"] == "DOC_001" and doc["raw_available"] is True
    assert [p["page"] for p in doc["pages"]] == [2]  # agreed page 1 excluded
    assert doc["pages"][0]["reading_a"] == "a2"
    assert doc["pages"][0]["reading_b"] == "b2"
    assert doc["pages"][0]["disagreement_details"] == ["DISAGREE: dates differ"]


def test_ocr_review_without_raw_scratch_still_lists_the_block(isolated_main):
    out_dir = _seed_case(isolated_main)
    _seed_ocr_result(out_dir)
    result = main.ocr_review("CASE_009")
    doc = result["documents"][0]
    assert doc["raw_available"] is False
    assert doc["pages"][0]["reading_a"] is None
    assert doc["pages"][0]["reading_b"] == "B-side text"  # falls back to the persisted reading


def test_ocr_review_skips_resolved_documents(isolated_main):
    out_dir = _seed_case(isolated_main)
    dao.atomic_write_json(out_dir / "ocr_result_DOC_001.json", {
        "case_id": "CASE_009", "document_id": "DOC_001",
        "cross_validation_status": "disagreed_resolved", "pages": [],
    })
    assert main.ocr_review("CASE_009") == {"documents": []}


# ---------------------------------------------------------- ocr-resolve --

def _resolve_body(**overrides):
    kwargs = dict(doc_id="DOC_001", page=2, chosen_reading="reading_a", reviewer="김태윤", note="checked raw page")
    kwargs.update(overrides)
    return main.OcrResolveBody(**kwargs)


def test_ocr_resolve_rejects_flag_shaped_doc_id(isolated_main):
    _seed_case(isolated_main)
    with pytest.raises(HTTPException) as exc:
        main.ocr_resolve("CASE_009", _resolve_body(doc_id="--held-by"))
    assert exc.value.status_code == 400


def test_ocr_resolve_rejects_bad_reading_and_empty_note(isolated_main):
    _seed_case(isolated_main)
    with pytest.raises(HTTPException):
        main.ocr_resolve("CASE_009", _resolve_body(chosen_reading="reading_c"))
    with pytest.raises(HTTPException):
        main.ocr_resolve("CASE_009", _resolve_body(note="   "))


def test_ocr_resolve_missing_raw_scratch_is_409_not_a_guess(isolated_main):
    _seed_case(isolated_main)
    with pytest.raises(HTTPException) as exc:
        main.ocr_resolve("CASE_009", _resolve_body())
    assert exc.value.status_code == 409


def test_ocr_resolve_delegates_to_run_checkpoint1(isolated_main, monkeypatch):
    _seed_case(isolated_main)
    scratch = isolated_main / "_ocr_scratch"
    scratch.mkdir()
    raw = {"document_path": "x", "pages": [{"page": 2, "reading_a": "a", "reading_b": "b",
                                             "agreement": "disagreed", "disagreement_details": []}]}
    (scratch / "CASE_009_DOC_001_raw.json").write_text(json.dumps(raw), encoding="utf-8")

    calls = {}
    def fake_resolve(case_id, doc_id, ocr_data, page, chosen_reading, resolved_by, note, held_by, run_id):
        calls.update(case_id=case_id, doc_id=doc_id, ocr_data=ocr_data, page=page,
                     chosen_reading=chosen_reading, resolved_by=resolved_by, note=note,
                     held_by=held_by, run_id=run_id)
        return {"status": "resolved"}
    monkeypatch.setattr(main.run_checkpoint1, "resolve_from_raw_ocr", fake_resolve)

    result = main.ocr_resolve("CASE_009", _resolve_body())
    assert result == {"status": "resolved"}
    assert calls["ocr_data"] == raw
    assert calls["chosen_reading"] == "reading_a"
    assert calls["resolved_by"] == calls["held_by"] == "김태윤"


def test_ocr_resolve_surfaces_a_sys_exit_as_400(isolated_main, monkeypatch):
    _seed_case(isolated_main)
    scratch = isolated_main / "_ocr_scratch"
    scratch.mkdir()
    (scratch / "CASE_009_DOC_001_raw.json").write_text(json.dumps({"pages": []}), encoding="utf-8")
    def exploding(*a, **k):
        sys.exit("error: no page 2 in this OCR result")
    monkeypatch.setattr(main.run_checkpoint1, "resolve_from_raw_ocr", exploding)
    with pytest.raises(HTTPException) as exc:
        main.ocr_resolve("CASE_009", _resolve_body())
    assert exc.value.status_code == 400
    assert "no page 2" in exc.value.detail


# --------------------------------------------------- human-review gate --

def test_human_review_reports_gate_state_per_version(isolated_main):
    out_dir = _seed_case(isolated_main)
    (out_dir / "draft_report_v1_reviewed.md").write_text("draft", encoding="utf-8")
    (out_dir / "expert_review_v1.json").write_text("{}", encoding="utf-8")
    dao.atomic_write_json(dao.human_review_flag_path("CASE_009", "v1"), {
        "case_id": "CASE_009", "version": "v1", "reviewer": "Dev", "marked_complete_at": dao.now_iso(),
    })
    state = main.human_review("CASE_009")
    assert state["v1"] == {
        "reviewed_draft_exists": True, "expert_review_exists": True,
        "review_complete": True, "completed_by": "Dev",
        "completed_at": state["v1"]["completed_at"],
    }
    assert state["v2"]["review_complete"] is False
    assert state["v2"]["expert_review_exists"] is False


def test_human_review_complete_validates_before_any_subprocess(isolated_main, monkeypatch):
    _seed_case(isolated_main)
    called = []
    monkeypatch.setattr(main, "_run_dao_cli", lambda args, held_by: called.append((args, held_by)) or {"ok": True})

    with pytest.raises(HTTPException):
        main.human_review_complete("CASE_009", main.HumanReviewCompleteBody(version="v3", reviewer="Dev"))
    with pytest.raises(HTTPException):
        main.human_review_complete("CASE_009", main.HumanReviewCompleteBody(version="v1", reviewer="--held-by"))
    assert called == []

    main.human_review_complete("CASE_009", main.HumanReviewCompleteBody(version="v1", reviewer="Dev"))
    args, held_by = called[0]
    assert args == ["mark-human-review-complete", "CASE_009", "v1", "--reviewer", "Dev"]
    assert held_by == "Dev"


# ------------------------------------------------- run-status lifecycle --

class _FakeProc:
    def __init__(self, pid, exit_code):
        self.pid = pid
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code


def _seed_run(base, case_id="CASE_009", pid=12345, log_text="log line\n"):
    log_path = base / f"{case_id}.log"
    log_path.write_text(log_text, encoding="utf-8")
    main._save_runs({case_id: {"pid": pid, "log_path": str(log_path),
                               "started_at": time.time() - 60, "status": "running"}})
    return log_path


def test_run_status_nonzero_exit_is_crashed_not_finished(isolated_main):
    _seed_run(isolated_main)
    main._PROCS["CASE_009"] = _FakeProc(12345, exit_code=2)
    status = main.run_status("CASE_009")
    assert status["status"] == "crashed"
    assert status["exit_code"] == 2
    assert main._load_runs()["CASE_009"]["status"] == "crashed"  # persisted


def test_run_status_zero_exit_is_finished(isolated_main):
    _seed_run(isolated_main)
    main._PROCS["CASE_009"] = _FakeProc(12345, exit_code=0)
    status = main.run_status("CASE_009")
    assert status["status"] == "finished"
    assert status["exit_code"] == 0


def test_run_status_still_running_while_child_alive(isolated_main):
    _seed_run(isolated_main)
    main._PROCS["CASE_009"] = _FakeProc(12345, exit_code=None)
    status = main.run_status("CASE_009")
    assert status["status"] == "running"
    assert status["log_size"] is not None and status["log_age_seconds"] is not None
    assert "log line" in status["log_tail"]


def test_run_status_lost_handle_and_dead_pid_is_ended_unknown(isolated_main, monkeypatch):
    # Backend restarted since launch: no Popen handle, pid gone -- the exit
    # code is unknowable, and the status must say so rather than claim success.
    _seed_run(isolated_main, pid=999999)
    monkeypatch.setattr(main, "_pid_alive", lambda pid: False)
    status = main.run_status("CASE_009")
    assert status["status"] == "ended_unknown"


def test_run_status_not_started(isolated_main):
    assert main.run_status("CASE_777") == {"status": "not_started"}
