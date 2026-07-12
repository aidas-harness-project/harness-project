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
    args1 = make_args(stage="consistency-check", topic="accident_date", sources_file=_sources_file(tmp_path, sources))
    args2 = make_args(stage="consistency-check", topic="diagnosis", sources_file=_sources_file(tmp_path, sources))

    assert dao.cmd_add_conflict_entry(args1) == 0
    assert dao.cmd_add_conflict_entry(args2) == 0

    ledger = dao.load_conflict_ledger("CASE_009")
    ids = [c["conflict_id"] for c in ledger["conflicts"]]
    assert ids == ["CONFLICT_1", "CONFLICT_2"]
    assert all(c["verdict"] == "pending" for c in ledger["conflicts"])


def test_check_conflicts_clear_false_while_pending(isolated_dao, make_args, tmp_path):
    sources = [{"document_id": "DOC_001", "value": "a", "quote": "q1"},
               {"document_id": "DOC_002", "value": "b", "quote": "q2"}]
    dao.cmd_add_conflict_entry(make_args(stage="claim-analysis", topic="t", sources_file=_sources_file(tmp_path, sources)))

    rc = dao.cmd_check_conflicts_clear(make_args())

    assert rc == 1, "a pending conflict must block (non-zero exit is how the orchestrator's halt check reads this)"


def test_set_conflict_verdict_then_clear(isolated_dao, make_args, tmp_path):
    sources = [{"document_id": "DOC_001", "value": "a", "quote": "q1"},
               {"document_id": "DOC_002", "value": "b", "quote": "q2"}]
    dao.cmd_add_conflict_entry(make_args(stage="claim-analysis", topic="t", sources_file=_sources_file(tmp_path, sources)))

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
    dao.cmd_add_conflict_entry(make_args(stage="claim-analysis", topic="accident_date", sources_file=_sources_file(tmp_path, sources)))
    dao.cmd_set_conflict_verdict(make_args(conflict_id="CONFLICT_1", verdict="resolved", note="n"))

    ledger = dao.load_conflict_ledger("CASE_009")
    assert len(ledger["conflicts"][0]["sources"]) == 2
