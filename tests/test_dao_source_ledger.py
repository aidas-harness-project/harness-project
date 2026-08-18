"""dao.py's _source_ledger.json operations (D2's per-file intake gate):
set-ledger-status, check-source-ledger-clear.
"""
import dao


def _seed_ledger(case_dir, files):
    entries = [{"file_name": f, "classification": "raw", "review_status": "pending",
                "reviewed_by": None, "reviewed_at": None, "rejection_reason": None} for f in files]
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), {
        "ledger_version": "source_ledger.v0.4",
        "case_id": "CASE_009", "source_dir": "x", "created_at": dao.now_iso(), "updated_at": dao.now_iso(),
        "files": entries,
        "history_boundary": dao.make_history_boundary(entries, mode="native"),
        "operations": [],
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


def test_set_ledger_status_exact_retry_and_collision(
    isolated_dao, make_args
):
    _seed_ledger(isolated_dao, ["a.pdf"])
    args = make_args(
        file_name="a.pdf",
        status="approved",
        reviewer="human",
        operation_id="ledger:test-approve-0001",
    )
    assert dao.cmd_set_ledger_status(args) == 0
    committed = dao.source_ledger_path("CASE_009").read_bytes()
    assert dao.cmd_set_ledger_status(args) == 0
    assert dao.source_ledger_path("CASE_009").read_bytes() == committed
    args.status = "rejected"
    args.reason = "different request"
    assert dao.cmd_set_ledger_status(args) == 1
    assert dao.source_ledger_path("CASE_009").read_bytes() == committed


def test_source_ledger_rejects_tampered_operation_receipt(
    isolated_dao, make_args
):
    _seed_ledger(isolated_dao, ["a.pdf"])
    args = make_args(
        file_name="a.pdf",
        status="approved",
        reviewer="human",
        operation_id="ledger:test-tamper-0001",
    )
    assert dao.cmd_set_ledger_status(args) == 0
    ledger = dao.load_json(dao.source_ledger_path("CASE_009"))
    del ledger["operations"][0]["result"]["status"]
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), ledger)

    assert dao.cmd_set_ledger_status(args) == 1
    assert dao.cmd_check_source_ledger_clear(make_args()) == 1


def test_repair_source_ledger_binding_repairs_only_a_stale_digest(
    isolated_dao, make_args
):
    _seed_ledger(isolated_dao, ["a.pdf"])
    args = make_args(
        file_name="a.pdf", status="approved", reviewer="human",
        operation_id="ledger:test-repair-0001",
    )
    assert dao.cmd_set_ledger_status(args) == 0
    ledger = dao.load_json(dao.source_ledger_path("CASE_009"))
    operation = ledger["operations"][0]
    old = operation["request_sha256"]
    operation["request_sha256"] = "0" * 64
    ledger["history_boundary"]["baseline_sha256"] = "0" * 64
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), ledger)

    repair = make_args(
        operation_id="ledger:test-repair-0001", reviewer="human",
        note="authorized repair of stale digest",
    )
    assert dao.cmd_repair_source_ledger_binding(repair) == 0
    repaired = dao.load_json(dao.source_ledger_path("CASE_009"))
    assert repaired["operations"][0]["request_sha256"] == old
    assert repaired["binding_repairs"] == [{
        "operation_id": "ledger:test-repair-0001",
        "old_request_sha256": "0" * 64,
        "new_request_sha256": old,
        "old_baseline_sha256": "0" * 64,
        "new_baseline_sha256": dao.hashlib.sha256(
            dao._canonical_json_bytes(repaired["history_boundary"]["baseline_state"])
        ).hexdigest(),
        "reviewer": "human",
        "note": "authorized repair of stale digest",
        "run_id": repair.run_id,
        "repaired_at": repaired["binding_repairs"][0]["repaired_at"],
    }]
    assert dao.cmd_check_source_ledger_clear(make_args()) == 0


def test_source_ledger_rejects_schema_valid_forged_receipt(
    isolated_dao, make_args
):
    _seed_ledger(isolated_dao, ["a.pdf", "b.pdf"])
    args = make_args(
        file_name="a.pdf",
        status="approved",
        reviewer="human",
        operation_id="ledger:test-forgery-0001",
    )
    assert dao.cmd_set_ledger_status(args) == 0
    ledger = dao.load_json(dao.source_ledger_path("CASE_009"))
    ledger["operations"][0]["result"].update({
        "target_id": "b.pdf",
        "status": "rejected",
    })
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), ledger)

    assert dao.cmd_set_ledger_status(args) == 1


def test_legacy_source_ledger_upgrades_on_first_locked_mutation(
    isolated_dao, make_args
):
    _seed_ledger(isolated_dao, ["a.pdf"])
    legacy = dao.load_json(dao.source_ledger_path("CASE_009"))
    legacy.pop("operations")
    legacy.pop("ledger_version", None)
    legacy.pop("history_boundary", None)
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), legacy)

    assert dao.cmd_set_ledger_status(make_args(
        file_name="a.pdf", status="approved", reviewer="human",
    )) == 0
    upgraded = dao.load_json(dao.source_ledger_path("CASE_009"))
    assert upgraded["ledger_version"] == "source_ledger.v0.4"
    assert upgraded["history_boundary"]["mode"] == "legacy_snapshot"
    assert upgraded["history_boundary"]["baseline_state"][0]["review_status"] == "pending"
    assert len(upgraded["operations"]) == 1

    upgraded["files"][0]["review_status"] = "rejected"
    upgraded["files"][0]["rejection_reason"] = "direct edit"
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), upgraded)
    assert dao.cmd_check_source_ledger_clear(make_args()) == 1


def test_immediate_predecessor_source_ledger_establishes_absorbed_boundary(
    isolated_dao, make_args
):
    entries = [{
        "file_name": "a.pdf", "classification": "raw",
        "review_status": "approved", "reviewed_by": "human",
        "reviewed_at": dao.now_iso(), "rejection_reason": None,
    }]
    request = {
        "case_id": "CASE_009",
        "operation_id": "ledger:predecessor-0001",
        "action": "set_status",
        "payload": {
            "file_name": "a.pdf", "status": "approved",
            "reviewer": "human", "reason": None,
        },
    }
    predecessor = {
        "ledger_version": "source_ledger.v0.3",
        "case_id": "CASE_009", "source_dir": "x",
        "files": entries,
        "operations": [{
            "operation_id": request["operation_id"],
            "request": request,
            "request_sha256": dao.hashlib.sha256(
                dao._canonical_json_bytes(request)
            ).hexdigest(),
            "result": {
                "action": "set_status", "target_id": "a.pdf",
                "status": "approved",
            },
            "completed_at": entries[0]["reviewed_at"],
        }],
    }
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), predecessor)

    migrated = dao.validated_source_ledger("CASE_009")
    assert migrated["ledger_version"] == "source_ledger.v0.4"
    assert migrated["history_boundary"]["mode"] == "legacy_snapshot"
    assert migrated["history_boundary"]["absorbed_operation_count"] == 1
    assert "predecessor_state_sha256" in migrated["history_boundary"]
    assert migrated["files"][0]["reviewed_at"] == migrated["operations"][0][
        "completed_at"
    ]
    assert len(migrated["operations"]) == 1

    retry = make_args(
        file_name="a.pdf", status="approved", reviewer="human",
        operation_id="ledger:predecessor-0001",
    )
    before = dao.source_ledger_path("CASE_009").read_bytes()
    assert dao.cmd_set_ledger_status(retry) == 0
    assert dao.source_ledger_path("CASE_009").read_bytes() == before


def test_predecessor_source_final_state_must_match_latest_receipt(
    isolated_dao
):
    request = {
        "case_id": "CASE_009", "operation_id": "ledger:contradiction-0001",
        "action": "set_status", "payload": {
            "file_name": "a.pdf", "status": "approved",
            "reviewer": "human", "reason": None,
        },
    }
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), {
        "ledger_version": "source_ledger.v0.3",
        "case_id": "CASE_009", "source_dir": "x",
        "files": [{
            "file_name": "a.pdf", "classification": "raw",
            "review_status": "rejected", "reviewed_by": "human",
            "reviewed_at": dao.now_iso(), "rejection_reason": "direct edit",
        }],
        "operations": [{
            "operation_id": request["operation_id"], "request": request,
            "request_sha256": dao.hashlib.sha256(
                dao._canonical_json_bytes(request)
            ).hexdigest(),
            "result": {"action": "set_status", "target_id": "a.pdf", "status": "approved"},
            "completed_at": dao.now_iso(),
        }],
    })

    try:
        dao.validated_source_ledger("CASE_009")
    except ValueError as exc:
        assert "contradicts" in str(exc)
    else:
        raise AssertionError("contradictory predecessor state was accepted")


def test_predecessor_source_receipt_requires_authored_timestamp(isolated_dao):
    request = {
        "case_id": "CASE_009", "operation_id": "ledger:missing-time-0001",
        "action": "set_status", "payload": {
            "file_name": "a.pdf", "status": "approved",
            "reviewer": "human", "reason": None,
        },
    }
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), {
        "ledger_version": "source_ledger.v0.3",
        "case_id": "CASE_009", "source_dir": "x",
        "files": [{
            "file_name": "a.pdf", "classification": "raw",
            "review_status": "approved", "reviewed_by": "human",
            "reviewed_at": None, "rejection_reason": None,
        }],
        "operations": [{
            "operation_id": request["operation_id"], "request": request,
            "request_sha256": dao.hashlib.sha256(
                dao._canonical_json_bytes(request)
            ).hexdigest(),
            "result": {"action": "set_status", "target_id": "a.pdf", "status": "approved"},
            "completed_at": dao.now_iso(),
        }],
    })

    try:
        dao.validated_source_ledger("CASE_009")
    except ValueError as exc:
        assert "authored state timestamp" in str(exc)
    else:
        raise AssertionError("missing predecessor timestamp was accepted")

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


def test_schema_invalid_ledger_is_rejected_and_not_written(isolated_dao, make_args):
    """Regression: cmd_set_ledger_status used to write whatever it built
    with zero schema enforcement (found via a real fork_case.py smoke
    test). A pre-existing malformed entry (classification isn't
    raw|ground_truth) must still block the write, even though the
    approved/rejected status this call sets is itself always valid."""
    dao.atomic_write_json(dao.source_ledger_path("CASE_009"), {
        "case_id": "CASE_009", "source_dir": "x",
        "files": [{"file_name": "a.pdf", "classification": "not_a_real_type", "review_status": "pending",
                   "reviewed_by": None, "reviewed_at": None, "rejection_reason": None}],
    })

    rc = dao.cmd_set_ledger_status(make_args(file_name="a.pdf", status="approved", reviewer="human"))

    assert rc == 1
    ledger = dao.load_json(dao.source_ledger_path("CASE_009"))
    assert ledger["files"][0]["review_status"] == "pending", "unchanged -- the invalid write never happened"
