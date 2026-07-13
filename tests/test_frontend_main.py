"""frontend/backend/main.py -- argument-injection guards on the two
endpoints that shell out to tools/dao.py's CLI (set_ledger_status,
set_conflict_verdict). Both conflict_id (a URL path parameter) and
file_name (a request-body field) previously flowed unvalidated into a
subprocess argv as positional arguments -- a value exactly matching a
known dao.py flag (e.g. "--held-by") would be consumed by argparse as
that OPTION instead of the intended positional, desyncing the rest of the
parse. Found by an automated security review of the commit that first
added main.py; fixed with _valid_conflict_id (pattern match, mirrors
conflict_ledger.schema.json) and _known_ledger_file_name (must already be
a real entry in the case's own ledger).

No real dao.py subprocess is invoked here -- these tests only exercise
the validation that runs BEFORE _run_dao_cli, confirming the attack is
rejected before it ever reaches a subprocess call.
"""
import sys
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
    assert main.dao is dao, "main.py must share the same dao module object for this monkeypatch to take effect"
    return tmp_path


def _seed_ledger(base, case_id="CASE_009", files=("real_file.pdf",)):
    out_dir = base / "outputs" / case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(out_dir / "_source_ledger.json", {
        "case_id": case_id, "source_dir": "source-cases/x", "created_at": dao.now_iso(),
        "updated_at": dao.now_iso(),
        "files": [{"file_name": f, "classification": "raw", "review_status": "pending",
                   "reviewed_by": None, "reviewed_at": None, "rejection_reason": None} for f in files],
    })
    dao.atomic_write_json(out_dir / "document_manifest.json", {
        "case_id": case_id, "created_at": dao.now_iso(), "documents": [],
    })


# ------------------------------------------------ _valid_conflict_id --

@pytest.mark.parametrize("conflict_id", ["--held-by", "--run-id", "-x", "CONFLICT_abc", "", "CONFLICT_1; rm -rf"])
def test_valid_conflict_id_rejects_non_matching_values(conflict_id):
    with pytest.raises(HTTPException) as exc:
        main._valid_conflict_id(conflict_id)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("conflict_id", ["CONFLICT_1", "CONFLICT_42", "CONFLICT_0"])
def test_valid_conflict_id_accepts_real_pattern(conflict_id):
    main._valid_conflict_id(conflict_id)  # must not raise


# ------------------------------------------------ _known_ledger_file_name --

def test_known_ledger_file_name_rejects_a_flag_shaped_value(isolated_main):
    _seed_ledger(isolated_main, files=["real_file.pdf"])

    with pytest.raises(HTTPException) as exc:
        main._known_ledger_file_name("CASE_009", "--held-by")
    assert exc.value.status_code == 400


def test_known_ledger_file_name_accepts_a_real_entry(isolated_main):
    _seed_ledger(isolated_main, files=["real_file.pdf"])

    main._known_ledger_file_name("CASE_009", "real_file.pdf")  # must not raise


def test_known_ledger_file_name_rejects_an_unknown_but_flag_free_name(isolated_main):
    """Not every rejection here is an injection attempt -- a genuine typo
    must also be rejected, with a clear message rather than reaching
    dao.py's own deeper NOT_FOUND."""
    _seed_ledger(isolated_main, files=["real_file.pdf"])

    with pytest.raises(HTTPException) as exc:
        main._known_ledger_file_name("CASE_009", "typo_file.pdf")
    assert exc.value.status_code == 400
    assert "typo_file.pdf" in exc.value.detail


def test_known_ledger_file_name_no_ledger_at_all_rejects_cleanly(isolated_main):
    with pytest.raises(HTTPException):
        main._known_ledger_file_name("CASE_009", "anything.pdf")


# ---------------------------------------- full endpoint-level attack simulation --

def test_set_ledger_status_endpoint_blocks_flag_smuggling_via_file_name(isolated_main):
    _seed_ledger(isolated_main, files=["real_file.pdf"])
    body = main.LedgerStatusBody(file_name="--held-by", status="approved", reviewer="attacker")

    with pytest.raises(HTTPException) as exc:
        main.set_ledger_status("CASE_009", body)
    assert exc.value.status_code == 400


def test_set_conflict_verdict_endpoint_blocks_flag_smuggling_via_conflict_id(isolated_main):
    _seed_ledger(isolated_main)
    body = main.ConflictVerdictBody(verdict="resolved", note="attacker note")

    with pytest.raises(HTTPException) as exc:
        main.set_conflict_verdict("CASE_009", "--held-by", body)
    assert exc.value.status_code == 400
