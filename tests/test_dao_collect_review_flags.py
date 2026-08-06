"""P3's batch-review aggregation point.

P3 says an inference-bearing claim is hedged and flagged, and that flagged
claims "surface together at the aggregation/report stage for batch human
review" -- explicitly WITHOUT halting the stage that raised them.

Only the flagging half was ever built. Four agent specs set review_required and
common_component_output.schema.json forces a reviewer_role alongside it, but no
code in the repo read the field except the classification gate, which reads one
contract family. CASE_909 finished carrying 18 flags, and seeing them meant
opening eighteen files by hand.

These tests pin the collector AND the property that makes it the right shape:
it reports and returns success even with flags wide open. A gate here would
demand a human decision per flag, which is the opposite of what P3 routes them
to batch review for -- and most of CASE_909's flags record a limit of the
material (a date blanked by redaction), where a reviewer can add nothing.
"""
import json

import pytest

import dao
import human_review


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


TARGET_KEY = "classification_text_source:raw_page_text"


def _contract(component="claim-analysis", status="success",
              review_required=True, warnings=None):
    out = {
        "case_id": "CASE_009",
        "component": component,
        "status": status,
        "review_required": review_required,
        "warnings": list(warnings or []),
    }
    if review_required:
        out["reviewer_role"] = "손해사정사"
    return out


def _classification(doc_id="DOC_001", text_source="raw_page_text"):
    return {
        "case_id": "CASE_009",
        "component": "document-pipeline",
        "status": "success",
        "document_id": doc_id,
        "predicted_document_type": "insurer_response",
        "confidence": 0.93,
        "evidence_references": [{"page": 1, "quote": "제목: 협조요청"}],
        "review_required": True,
        "reviewer_role": "손해사정사",
        "classification_text_source": text_source,
    }


def _write(isolated_dao, name, data):
    case = isolated_dao / "outputs" / "CASE_009"
    case.mkdir(parents=True, exist_ok=True)
    (case / name).write_text(json.dumps(data, ensure_ascii=False),
                             encoding="utf-8")
    return case / name


# ------------------------------------------------------- what it collects ---

def test_collects_a_flagged_contract_with_its_stated_reason(isolated_dao):
    _write(isolated_dao, "coverage_result.json", _contract(
        warnings=["applicable:true records SCOPE, not that the payout "
                  "condition is satisfied"]))
    result = dao.collect_review_flags("CASE_009")
    assert result["unresolved_count"] == 1
    entry = result["unresolved"][0]
    assert entry["contract"] == "coverage_result.json"
    assert entry["component"] == "claim-analysis"
    # The agent's own account is the content a reviewer actually needs;
    # dropping it would leave a list of filenames.
    assert "SCOPE" in entry["warnings"][0]


def test_an_unflagged_contract_is_not_collected(isolated_dao):
    _write(isolated_dao, "coverage_result.json",
           _contract(review_required=False))
    assert dao.collect_review_flags("CASE_009")["unresolved_count"] == 0


def test_bookkeeping_files_are_not_component_output(isolated_dao):
    """_run_state/_conflict_ledger are the DAO's own records, and a sidecar
    carries citations rather than judgments. Sweeping them in would report
    the harness's plumbing as pending review work."""
    _write(isolated_dao, "_run_state.json", _contract())
    _write(isolated_dao, "draft_report_v1.evidence.json", _contract())
    assert dao.collect_review_flags("CASE_009")["unresolved_count"] == 0


def test_partial_status_is_marked_as_not_retryable(isolated_dao):
    """CASE_909's extracted_claim_fields: the dates are blanked by redaction,
    so a P9 retry produces the identical result. The flag is a report, not a
    task, and the distinction is what keeps a reviewer from re-running it."""
    _write(isolated_dao, "extracted_claim_fields.json",
           _contract(status="partial", warnings=["accident_date is null"]))
    entry = dao.collect_review_flags("CASE_009")["unresolved"][0]
    assert entry["retry_would_not_help"] is True


def test_success_status_is_not_marked_retryable(isolated_dao):
    _write(isolated_dao, "coverage_result.json", _contract())
    entry = dao.collect_review_flags("CASE_009")["unresolved"][0]
    assert "retry_would_not_help" not in entry


def test_counts_are_grouped_by_reviewer_role(isolated_dao):
    """reviewer_role is the real routing value (손해사정사/의사/법률전문가), so a
    batch review is split by who can actually answer."""
    _write(isolated_dao, "coverage_result.json", _contract())
    medical = _contract()
    medical["reviewer_role"] = "의사"
    _write(isolated_dao, "requirement_matching_result.json", medical)
    result = dao.collect_review_flags("CASE_009")
    assert result["unresolved_by_reviewer_role"] == {"손해사정사": 1, "의사": 1}


# ------------------------------------------- resolved vs merely flagged -----

def test_a_cleared_classification_is_reported_as_resolved(
        isolated_dao, make_args):
    """classification_result is the one family with a resolution path. A flag
    the document_processing gate has already closed must not be re-reported as
    outstanding work."""
    _write(isolated_dao, "classification_result_DOC_001.json",
           _classification())
    assert dao.cmd_record_human_review(make_args(
        artifact_kind="classification_review", artifact_id="DOC_001",
        target_key=TARGET_KEY, decision="verified", reviewer="pyun",
        note="quote survives redaction")) == 0
    ledger = dao.load_human_review_ledger("CASE_009")
    uid = ledger["records"][-1]["review_uid"]
    data = _classification()
    data["human_review_uid"] = uid
    _write(isolated_dao, "classification_result_DOC_001.json", data)

    result = dao.collect_review_flags("CASE_009")
    assert result["resolved_count"] == 1
    assert result["unresolved_count"] == 0
    assert result["resolved"][0]["human_review_uid"] == uid


def test_an_uncleared_classification_stays_unresolved(isolated_dao):
    _write(isolated_dao, "classification_result_DOC_001.json",
           _classification())
    result = dao.collect_review_flags("CASE_009")
    assert result["unresolved_count"] == 1
    entry = result["unresolved"][0]
    assert entry["resolution_path"] == "human_review_uid"
    assert entry["resolution_errors"]


def test_a_forged_review_uid_does_not_count_as_resolved(isolated_dao):
    """The collector must not be a softer check than the gate: it runs the same
    hash-bound verification, so a made-up UID cannot launder a flag."""
    data = _classification()
    data["human_review_uid"] = "HR-deadbeefdeadbeef"
    _write(isolated_dao, "classification_result_DOC_001.json", data)
    result = dao.collect_review_flags("CASE_009")
    assert result["resolved_count"] == 0
    assert result["unresolved_count"] == 1


def test_a_contract_with_no_resolution_path_says_so(isolated_dao):
    """denial_reason_result and the rest have nowhere to route a decision. The
    collector states that rather than implying a review is merely missing."""
    _write(isolated_dao, "denial_reason_result.json",
           _contract(component="denial-response"))
    entry = dao.collect_review_flags("CASE_009")["unresolved"][0]
    assert entry["resolution_path"] is None


# --------------------------------------------------- it reports, not gates --

def test_open_flags_still_exit_zero(isolated_dao, make_args, capsys):
    """The load-bearing property. P3: a flagged claim "does not halt the
    current stage". If this ever returns non-zero, a caller wiring it into a
    finalize step turns every judgment contract into a blocking human review --
    exactly what batch routing exists to avoid."""
    _write(isolated_dao, "coverage_result.json", _contract())
    _write(isolated_dao, "denial_reason_result.json",
           _contract(component="denial-response"))
    rc = dao.cmd_collect_review_flags(make_args(case_id="CASE_009"))
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["unresolved_count"] == 2


def test_it_never_writes(isolated_dao, make_args):
    """Read-only in the strong sense: no contract, ledger, or run-state file is
    touched. A collector that mutated state could not be run freely, which is
    the whole point of having one."""
    path = _write(isolated_dao, "coverage_result.json", _contract())
    before = path.read_bytes()
    listing_before = sorted(
        p.name for p in (isolated_dao / "outputs" / "CASE_009").iterdir())
    dao.cmd_collect_review_flags(make_args(case_id="CASE_009"))
    assert path.read_bytes() == before
    listing_after = sorted(
        p.name for p in (isolated_dao / "outputs" / "CASE_009").iterdir())
    assert listing_after == listing_before


def test_an_unreadable_contract_is_reported_not_skipped(isolated_dao):
    """A corrupt file must never look like an absent flag -- that would report
    a clean sweep over a case the command could not actually read."""
    _write(isolated_dao, "coverage_result.json", _contract())
    (isolated_dao / "outputs" / "CASE_009" / "broken.json").write_text(
        "{not json", encoding="utf-8")
    result = dao.collect_review_flags("CASE_009")
    assert [u["contract"] for u in result["unreadable"]] == ["broken.json"]
    assert result["unresolved_count"] == 1


def test_unreadable_contracts_make_the_command_fail(isolated_dao, make_args):
    """Non-zero is reserved for "the collection itself cannot be trusted".
    Open flags are normal; a file the sweep could not parse is not."""
    (isolated_dao / "outputs" / "CASE_009").mkdir(parents=True, exist_ok=True)
    (isolated_dao / "outputs" / "CASE_009" / "broken.json").write_text(
        "{not json", encoding="utf-8")
    assert dao.cmd_collect_review_flags(make_args(case_id="CASE_009")) == 1


# ------------------------------------------------------------- the CLI ------

def test_the_real_cli_subcommand_runs(isolated_dao, capsys):
    """Drives main() rather than the helper. Every other test here would pass
    with the subcommand computed but never registered -- the exact
    "implemented but not wired" shape this whole line of work exists to catch.
    """
    _write(isolated_dao, "coverage_result.json", _contract())
    parser = dao.build_parser()
    args = parser.parse_args(["collect-review-flags", "CASE_009"])
    assert args.fn(args) == 0
    assert json.loads(capsys.readouterr().out)["unresolved_count"] == 1


def test_family_naming_strips_a_document_suffix():
    """classification_result_DOC_004.json must resolve to the family that owns
    the resolution path, or every per-document contract loses it."""
    assert dao._contract_family(
        "classification_result_DOC_004.json") == "classification_result"
    assert dao._contract_family(
        "coverage_result.json") == "coverage_result"
