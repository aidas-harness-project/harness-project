"""dao.py's _source_ledger.json operations (D2's per-file intake gate):
set-ledger-status, check-source-ledger-clear.
"""
import dao


def _seed_ledger(case_dir, files):
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), {
        "case_id": "CASE_009", "source_dir": "x", "created_at": dao.now_iso(), "updated_at": dao.now_iso(),
        "files": [{"file_name": f, "classification": "raw", "review_status": "pending",
                   "reviewed_by": None, "reviewed_at": None, "rejection_reason": None} for f in files],
    })


def test_set_ledger_status_approved_requires_reviewer(isolated_dao, make_args):
    _seed_ledger(isolated_dao, ["a.pdf"])
    rc = dao.cmd_set_ledger_status(make_args(file_name="a.pdf", status="approved", reviewer=None))
    assert rc == 1


def test_set_ledger_status_rejected_requires_reason(isolated_dao, make_args):
    _seed_ledger(isolated_dao, ["a.pdf"])
    rc = dao.cmd_set_ledger_status(make_args(file_name="a.pdf", status="rejected", reviewer="human", reason=None))
    assert rc == 1


def test_set_ledger_status_approved_records_reviewer_and_clears_reason(isolated_dao, make_args):
    _seed_ledger(isolated_dao, ["a.pdf"])
    rc = dao.cmd_set_ledger_status(make_args(file_name="a.pdf", status="approved", reviewer="human"))
    assert rc == 0

    ledger = dao.load_json(dao.source_ledger_path("CASE_009"))
    entry = ledger["files"][0]
    assert entry["review_status"] == "approved"
    assert entry["reviewed_by"] == "human"
    assert entry["rejection_reason"] is None


def test_set_ledger_status_unknown_file_fails(isolated_dao, make_args):
    _seed_ledger(isolated_dao, ["a.pdf"])
    rc = dao.cmd_set_ledger_status(make_args(file_name="does_not_exist.pdf", status="approved", reviewer="human"))
    assert rc == 1


def test_check_source_ledger_clear_blocks_on_pending(isolated_dao, make_args):
    _seed_ledger(isolated_dao, ["a.pdf", "b.pdf"])
    dao.cmd_set_ledger_status(make_args(file_name="a.pdf", status="approved", reviewer="human"))
    # b.pdf still pending

    rc = dao.cmd_check_source_ledger_clear(make_args())
    assert rc == 1


def test_check_source_ledger_clear_blocks_on_any_single_rejection(isolated_dao, make_args):
    """D2: a single rejected file blocks the whole case, not just itself."""
    _seed_ledger(isolated_dao, ["a.pdf", "b.pdf"])
    dao.cmd_set_ledger_status(make_args(file_name="a.pdf", status="approved", reviewer="human"))
    dao.cmd_set_ledger_status(make_args(file_name="b.pdf", status="rejected", reviewer="human", reason="answer-key contamination"))

    rc = dao.cmd_check_source_ledger_clear(make_args())
    assert rc == 1


def test_check_source_ledger_clear_passes_when_all_approved(isolated_dao, make_args):
    _seed_ledger(isolated_dao, ["a.pdf", "b.pdf"])
    dao.cmd_set_ledger_status(make_args(file_name="a.pdf", status="approved", reviewer="human"))
    dao.cmd_set_ledger_status(make_args(file_name="b.pdf", status="approved", reviewer="human"))

    rc = dao.cmd_check_source_ledger_clear(make_args())
    assert rc == 0
