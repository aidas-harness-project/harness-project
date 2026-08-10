"""dao.py's _conflict_ledger.json operations: add-conflict-entry,
set-conflict-verdict, check-conflicts-clear (P6's actual halt mechanism).
"""
import json

import dao


def _sources_file(tmp_path, sources):
    p = tmp_path / "sources.json"
    p.write_text(json.dumps(sources), encoding="utf-8")
    return str(p)


def test_add_conflict_entry_assigns_sequential_ids(isolated_dao, make_args, tmp_path):
    sources = [
        {"document_id": "DOC_001", "value": "2024-03-12", "quote": "사고일 2024-03-12"},
        {"document_id": "DOC_002", "value": "2024-03-15", "quote": "내원일 2024-03-15"},
    ]
    args1 = make_args(stage="consistency_check", topic="accident_date", sources_file=_sources_file(tmp_path, sources))
    args2 = make_args(stage="consistency_check", topic="diagnosis", sources_file=_sources_file(tmp_path, sources))

    assert dao.cmd_add_conflict_entry(args1) == 0
    assert dao.cmd_add_conflict_entry(args2) == 0

    ledger = dao.load_conflict_ledger("CASE_009")
    ids = [c["conflict_id"] for c in ledger["conflicts"]]
    assert ids == ["CONFLICT_1", "CONFLICT_2"]
    assert all(c["verdict"] == "pending" for c in ledger["conflicts"])


def test_conflict_mutations_exact_retry_and_collision(
    isolated_dao, make_args, tmp_path
):
    sources = [
        {"document_id": "DOC_001", "value": "a", "quote": "q1"},
        {"document_id": "DOC_002", "value": "b", "quote": "q2"},
    ]
    add_args = make_args(
        stage="claim_analysis",
        topic="diagnosis",
        sources_file=_sources_file(tmp_path, sources),
        operation_id="conflict:test-add-0001",
    )
    assert dao.cmd_add_conflict_entry(add_args) == 0
    committed_add = dao.conflict_ledger_path("CASE_009").read_bytes()
    assert dao.cmd_add_conflict_entry(add_args) == 0
    assert dao.conflict_ledger_path("CASE_009").read_bytes() == committed_add
    add_args.topic = "different topic"
    assert dao.cmd_add_conflict_entry(add_args) == 1

    verdict_args = make_args(
        conflict_id="CONFLICT_1",
        verdict="resolved",
        note="human confirmed",
        operation_id="conflict:test-verdict-0001",
    )
    assert dao.cmd_set_conflict_verdict(verdict_args) == 0
    committed_verdict = dao.conflict_ledger_path("CASE_009").read_bytes()
    assert dao.cmd_set_conflict_verdict(verdict_args) == 0
    assert dao.conflict_ledger_path("CASE_009").read_bytes() == committed_verdict
    verdict_args.note = "different decision"
    assert dao.cmd_set_conflict_verdict(verdict_args) == 1


def test_conflict_ledger_rejects_duplicate_operation_receipt(
    isolated_dao, make_args, tmp_path
):
    sources = [
        {"document_id": "DOC_001", "value": "a", "quote": "q1"},
        {"document_id": "DOC_002", "value": "b", "quote": "q2"},
    ]
    assert dao.cmd_add_conflict_entry(make_args(
        stage="claim_analysis",
        topic="diagnosis",
        sources_file=_sources_file(tmp_path, sources),
    )) == 0
    assert dao.cmd_set_conflict_verdict(make_args(
        conflict_id="CONFLICT_1",
        verdict="resolved",
        note="human confirmed",
    )) == 0
    ledger = dao.load_conflict_ledger("CASE_009")
    ledger["operations"].append(ledger["operations"][0].copy())
    dao.atomic_write_json(dao.conflict_ledger_path("CASE_009"), ledger)

    assert dao.cmd_check_conflicts_clear(make_args()) == 1


def test_conflict_ledger_rejects_schema_valid_forged_receipt(
    isolated_dao, make_args, tmp_path
):
    sources = [
        {"document_id": "DOC_001", "value": "a", "quote": "q1"},
        {"document_id": "DOC_002", "value": "b", "quote": "q2"},
    ]
    args = make_args(
        stage="claim_analysis",
        topic="diagnosis",
        sources_file=_sources_file(tmp_path, sources),
        operation_id="conflict:test-forgery-0001",
    )
    assert dao.cmd_add_conflict_entry(args) == 0
    ledger = dao.load_conflict_ledger("CASE_009")
    ledger["operations"][0]["result"].update({
        "target_id": "CONFLICT_999",
        "status": "resolved",
    })
    dao.atomic_write_json(dao.conflict_ledger_path("CASE_009"), ledger)

    assert dao.cmd_add_conflict_entry(args) == 1


def test_legacy_conflict_state_is_recorded_as_a_replayable_boundary(
    isolated_dao, make_args, tmp_path
):
    path = dao.conflict_ledger_path("CASE_009")
    legacy = {
        "case_id": "CASE_009",
        "conflicts": [{
            "conflict_id": "CONFLICT_1",
            "raised_by_stage": "claim_analysis",
            "field_or_topic": "legacy diagnosis",
            "sources": [
                {"document_id": "DOC_001", "value": "a", "quote": "q1"},
                {"document_id": "DOC_002", "value": "b", "quote": "q2"},
            ],
            "verdict": "resolved",
            "resolution_note": "legacy human decision",
            "resolved_at": dao.now_iso(),
        }],
    }
    dao.atomic_write_json(path, legacy)
    sources = [
        {"document_id": "DOC_001", "value": "c", "quote": "q3"},
        {"document_id": "DOC_002", "value": "d", "quote": "q4"},
    ]
    assert dao.cmd_add_conflict_entry(make_args(
        stage="claim_analysis",
        topic="new diagnosis",
        sources_file=_sources_file(tmp_path, sources),
    )) == 0
    upgraded = dao.load_json(path)
    assert upgraded["history_boundary"]["mode"] == "legacy_snapshot"
    assert upgraded["history_boundary"]["baseline_state"][0]["verdict"] == "resolved"

    upgraded["conflicts"][0]["verdict"] = "pending"
    upgraded["conflicts"][0]["resolution_note"] = None
    upgraded["conflicts"][0]["resolved_at"] = None
    dao.atomic_write_json(path, upgraded)
    assert dao.cmd_check_conflicts_clear(make_args()) == 1


def test_immediate_predecessor_conflict_ledger_establishes_absorbed_boundary(
    isolated_dao, make_args, tmp_path
):
    sources = [
        {"document_id": "DOC_001", "value": "a", "quote": "q1"},
        {"document_id": "DOC_002", "value": "b", "quote": "q2"},
    ]
    request = {
        "case_id": "CASE_009",
        "operation_id": "conflict:predecessor-add-0001",
        "action": "add",
        "payload": {
            "stage": "claim_analysis", "topic": "diagnosis",
            "sources": sources,
        },
    }
    predecessor = {
        "ledger_version": "conflict_ledger.v0.2",
        "case_id": "CASE_009",
        "conflicts": [{
            "conflict_id": "CONFLICT_1",
            "raised_by_stage": "claim_analysis",
            "field_or_topic": "diagnosis",
            "sources": sources,
            "verdict": "pending",
            "resolution_note": None,
            "resolved_at": None,
        }],
        "operations": [{
            "operation_id": request["operation_id"], "request": request,
            "request_sha256": dao.hashlib.sha256(
                dao._canonical_json_bytes(request)
            ).hexdigest(),
            "result": {"action": "add", "target_id": "CONFLICT_1", "status": "pending"},
            "completed_at": dao.now_iso(),
        }],
    }
    dao.atomic_write_json(dao.conflict_ledger_path("CASE_009"), predecessor)

    migrated = dao.validated_conflict_ledger("CASE_009")
    assert migrated["ledger_version"] == "conflict_ledger.v0.3"
    assert migrated["history_boundary"]["mode"] == "legacy_snapshot"
    assert migrated["history_boundary"]["absorbed_operation_count"] == 1
    assert len(migrated["operations"]) == 1

    retry = make_args(
        stage="claim_analysis", topic="diagnosis",
        sources_file=_sources_file(tmp_path, sources),
        operation_id="conflict:predecessor-add-0001",
    )
    before = dao.conflict_ledger_path("CASE_009").read_bytes()
    assert dao.cmd_add_conflict_entry(retry) == 0
    assert dao.conflict_ledger_path("CASE_009").read_bytes() == before


def test_predecessor_conflict_timestamp_normalizes_to_retained_receipt(
    isolated_dao
):
    sources = [
        {"document_id": "DOC_001", "value": "a", "quote": "q1"},
        {"document_id": "DOC_002", "value": "b", "quote": "q2"},
    ]
    request = {
        "case_id": "CASE_009",
        "operation_id": "conflict:predecessor-verdict-0001",
        "action": "set_verdict",
        "payload": {
            "conflict_id": "CONFLICT_1", "verdict": "resolved",
            "note": "legacy human decision",
        },
    }
    authored_at = dao.now_iso()
    completed_at = dao.now_iso()
    dao.atomic_write_json(dao.conflict_ledger_path("CASE_009"), {
        "ledger_version": "conflict_ledger.v0.2",
        "case_id": "CASE_009",
        "conflicts": [{
            "conflict_id": "CONFLICT_1",
            "raised_by_stage": "claim_analysis",
            "field_or_topic": "diagnosis", "sources": sources,
            "verdict": "resolved", "resolution_note": "legacy human decision",
            "resolved_at": authored_at,
        }],
        "operations": [{
            "operation_id": request["operation_id"], "request": request,
            "request_sha256": dao.hashlib.sha256(
                dao._canonical_json_bytes(request)
            ).hexdigest(),
            "result": {"action": "set_verdict", "target_id": "CONFLICT_1", "status": "resolved"},
            "completed_at": completed_at,
        }],
    })

    migrated = dao.validated_conflict_ledger("CASE_009")
    assert migrated["conflicts"][0]["resolved_at"] == completed_at
    assert "predecessor_state_sha256" in migrated["history_boundary"]


def test_check_conflicts_clear_false_while_pending(isolated_dao, make_args, tmp_path):
    sources = [{"document_id": "DOC_001", "value": "a", "quote": "q1"},
               {"document_id": "DOC_002", "value": "b", "quote": "q2"}]
    dao.cmd_add_conflict_entry(make_args(stage="claim_analysis", topic="t", sources_file=_sources_file(tmp_path, sources)))

    rc = dao.cmd_check_conflicts_clear(make_args())

    assert rc == 1, "a pending conflict must block (non-zero exit is how the orchestrator's halt check reads this)"


def test_set_conflict_verdict_then_clear(isolated_dao, make_args, tmp_path):
    sources = [{"document_id": "DOC_001", "value": "a", "quote": "q1"},
               {"document_id": "DOC_002", "value": "b", "quote": "q2"}]
    dao.cmd_add_conflict_entry(make_args(stage="claim_analysis", topic="t", sources_file=_sources_file(tmp_path, sources)))

    rc = dao.cmd_set_conflict_verdict(make_args(conflict_id="CONFLICT_1", verdict="resolved", note="human confirmed DOC_001"))
    assert rc == 0

    ledger = dao.load_conflict_ledger("CASE_009")
    entry = ledger["conflicts"][0]
    assert entry["verdict"] == "resolved"
    assert entry["resolution_note"] == "human confirmed DOC_001"
    assert entry["resolved_at"] is not None

    assert dao.cmd_check_conflicts_clear(make_args()) == 0, "resolved -- nothing pending -- must clear"


def test_set_conflict_verdict_unknown_id_fails(isolated_dao, make_args):
    rc = dao.cmd_set_conflict_verdict(make_args(conflict_id="CONFLICT_99", verdict="resolved", note="n"))
    assert rc == 1


def test_conflict_sources_never_discarded_on_resolution(isolated_dao, make_args, tmp_path):
    """P6: a conflict is labeled, never silently deleted -- resolving it
    must not drop either side's value."""
    sources = [{"document_id": "DOC_001", "value": "2024-03-12", "quote": "q1"},
               {"document_id": "DOC_002", "value": "2024-03-15", "quote": "q2"}]
    dao.cmd_add_conflict_entry(make_args(stage="claim_analysis", topic="accident_date", sources_file=_sources_file(tmp_path, sources)))
    dao.cmd_set_conflict_verdict(make_args(conflict_id="CONFLICT_1", verdict="resolved", note="n"))

    ledger = dao.load_conflict_ledger("CASE_009")
    assert len(ledger["conflicts"][0]["sources"]) == 2


def test_schema_invalid_sources_rejected_and_not_written(isolated_dao, make_args, tmp_path):
    """Regression: cmd_add_conflict_entry used to write whatever it built
    with zero schema enforcement (found via a real fork_case.py smoke
    test). A malformed source (missing required 'quote') must block the
    write -- conflict_ledger.schema.json requires quote on every source."""
    bad_sources = [{"document_id": "DOC_001", "value": "a"}, {"document_id": "DOC_002", "value": "b", "quote": "q2"}]
    args = make_args(stage="claim_analysis", topic="t", sources_file=_sources_file(tmp_path, bad_sources))

    rc = dao.cmd_add_conflict_entry(args)

    assert rc == 1
    ledger = dao.load_conflict_ledger("CASE_009")
    assert ledger["conflicts"] == [], "nothing should have been written"
