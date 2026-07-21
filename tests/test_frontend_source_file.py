"""D1: /source-file must never serve a ground-truth (answer-key) file (fleet C2)."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # frontend deps optional in some lanes

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "frontend" / "backend"))

import dao  # noqa: E402
import main  # noqa: E402


def _ledger(monkeypatch, entries):
    monkeypatch.setattr(dao, "load_json", lambda *_a, **_k: {"files": entries, "source_dir": None})
    monkeypatch.setattr(dao, "source_ledger_path", lambda case_id: Path("/nonexistent"))
    monkeypatch.setattr(main, "_require_case", lambda case_id: None)
    monkeypatch.setattr(main, "_known_ledger_file_name", lambda case_id, name: None)


def test_ground_truth_file_refused(monkeypatch):
    _ledger(monkeypatch, [{"file_name": "GT_report.pdf", "classification": "ground_truth"}])
    with pytest.raises(main.HTTPException) as exc:
        main.source_file("CASE_021", "GT_report.pdf")
    assert exc.value.status_code == 403


def test_unknown_file_refused(monkeypatch):
    _ledger(monkeypatch, [{"file_name": "raw.pdf", "classification": "raw"}])
    with pytest.raises(main.HTTPException) as exc:
        main.source_file("CASE_021", "not_in_ledger.pdf")
    assert exc.value.status_code == 403


def test_raw_file_passes_classification_gate(monkeypatch):
    # A raw entry passes the D1 gate; it then 404s only because no file is on
    # disk here -- proving the gate itself let it through (not a 403).
    _ledger(monkeypatch, [{"file_name": "raw.pdf", "classification": "raw"}])
    with pytest.raises(main.HTTPException) as exc:
        main.source_file("CASE_021", "raw.pdf")
    assert exc.value.status_code == 404
