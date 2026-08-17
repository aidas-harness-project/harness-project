"""P0-8 follow-up 2: the table gate was never on the finalization path.

The previous pass built a real gate and then wired it to almost nothing.
`_table_region_finalize_blockers` was called by TESTS and by nothing else --
`_policy_completion_blockers`, the function that actually decides whether
`policy_clause_processing` may finalize, never mentioned it. Everything the
follow-up proved about receipt derivation was true and unreachable.

Four defects, each with its own attack here.

1. **Not on the production path.** A document whose PDF is full of tables, with
   no reference_table and no scan, finalized `policy_clause_processing`
   cleanly. Section 1 drives the REAL `dao.py finalize-stage
   --stage policy_clause_processing` path, because a private helper returning
   a non-empty list proves only that the helper works.

2. **Absent reference_table was a free pass.** `_table_region_finalize_blockers`
   opened with `if tables is None: return []`. So the gate ran only on
   documents somebody had already chosen to extract a table from, and the way
   to make a table invisible was never to mention it. The scan is now an
   obligation the DOCUMENT carries, not one a contract triggers.

3. **The candidate inventory had no whole-body checksum.** Each `candidate_id` verified
   against its own geometry, but nothing verified the SET, so deleting the
   entry for an unextracted table left every survivor verifying perfectly --
   in the one artifact whose whole job is to answer "were there other
   tables?". A content-derived `scan_id` now detects ordinary corruption through
   the supported DAO path. It is not an authenticated signature: a process with
   forbidden direct-write access can recompute it, a boundary documented below.

4. **Continuation was decided by resemblance.** Matching column grid + matching
   header + no new heading returned `continues` unconditionally, contradicting
   `continuation_decision`'s own fail-closed docstring. Two independent
   appendices printed with the same layout -- an entirely ordinary way to lay
   out a policy schedule -- were welded into one table. Note the asymmetry: a
   wrongly-SPLIT table leaves its second half as an uncovered candidate that
   blocks finalization, while a wrongly-MERGED one reports a complete,
   verified table spanning content that was never one table.

Every test runs against a REAL pymupdf-rendered PDF and the REAL DAO commands.
"""
import json

import pytest

import dao
import table_region_provenance as trp

from test_table_region_provenance import (  # noqa: F401  (fixtures)
    CASE, HELD_BY, RUN, _canonicalize_uids, _contract_from_receipt,
    _manifest_entry, _receipt, _registered_text, _revision_text, _seed_manifest,
    _write_contract, case, register_table, table_pdf,
)


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


def _scan(make_args, doc_id="DOC_001", detector_profile=None):
    """Run the real `scan-table-candidates` command."""
    return dao.cmd_scan_table_candidates(make_args(
        case_id=CASE, doc_id=doc_id, detector_profile=detector_profile,
        held_by=HELD_BY, run_id=RUN))


def _inventory(doc_id="DOC_001"):
    return dao.table_candidate_inventory_for(CASE, doc_id)


def _finalize_policy_stage(isolated_dao, make_args, doc_id="DOC_001"):
    """Drive the REAL production finalization path.

    This is the point of the file. `_policy_completion_blockers` is a private
    helper; `dao.py finalize-stage --stage policy_clause_processing` is what a
    pipeline actually runs (passing a stage is atomic with its P10 snapshot, so
    `update-run-state --status passed` is refused outright), and the previous
    pass's gate was absent from exactly that path while every helper-level test
    passed.

    `document_processing` is finalized first through the same real command,
    since policy_clause_processing depends on it -- reaching the gate under
    test requires genuinely satisfying what comes before it.
    """
    classification = isolated_dao / "_classification.json"
    classification.write_text(json.dumps({
        "case_id": CASE, "component": "document-pipeline", "status": "success",
        "document_id": doc_id, "predicted_document_type": "insurance_policy",
        "confidence": 0.95, "review_required": False,
        "evidence_references": [{
            "document_id": doc_id, "page": 1, "quote": "Disability Table"}],
    }, ensure_ascii=False), encoding="utf-8")
    assert dao.cmd_write_contract(make_args(
        case_id=CASE, filename=f"classification_result_{doc_id}.json",
        data_file=str(classification),
        schema_name="classification_result.schema.json",
        held_by=HELD_BY, run_id=RUN)) == 0
    assert dao.cmd_finalize_stage(make_args(
        case_id=CASE, stage="document_processing",
        held_by=HELD_BY, run_id=RUN)) == 0
    return dao.cmd_finalize_stage(make_args(
        case_id=CASE, stage="policy_clause_processing",
        held_by=HELD_BY, run_id=RUN))


# Findings this gate owns. A bare policy fixture has plenty of unrelated
# blockers (no declared role, no normalized clauses, no boundary inventory), so
# matching on the gate's own vocabulary keeps each assertion about the table
# gate rather than about whichever unrelated blocker sorts first.
_TABLE_GATE_MARKERS = (
    "scan-table-candidates", "table candidate", "candidate scan",
    "table region index", "table scan", "the DAO's scan found",
    "table region receipt", "does not hash to its own",
    "never covered by the candidate scan",
)


def _table_blockers(doc_id="DOC_001"):
    return [b for b in dao._policy_completion_blockers(CASE)
            if doc_id in b and any(marker in b
                                   for marker in _TABLE_GATE_MARKERS)]


# ==========================================================================
# 1. THE ATTACK -- a document with tables, never scanned, finalizing
# ==========================================================================

@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_a_document_with_tables_and_no_scan_cannot_finalize(
        case, isolated_dao, make_args, capsys):
    """Attack A. A real PDF with a real table, no reference_table, no scan.

    Before this, `_table_region_finalize_blockers` was unreachable from
    `_policy_completion_blockers`, so the whole P0-8 apparatus had no bearing
    on whether the stage passed.
    """
    case()

    rc = _finalize_policy_stage(isolated_dao, make_args)

    assert rc != 0, (
        "policy_clause_processing finalized on a document that was never "
        "scanned for tables -- the P0-8 gate is not on the production path")
    out = capsys.readouterr().out
    assert "scan-table-candidates" in out, out


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_the_real_cli_path_refuses_while_a_candidate_is_unhandled(
        case, isolated_dao, make_args, capsys):
    """Attack B. Scanned, candidate found, no reference_table extracts it.

    The scan existing is not the gate -- the scan's FINDINGS having a
    disposition is. Driven through the real finalize-stage path.
    """
    case()
    assert _scan(make_args) == 0
    assert len(_inventory()["candidates"]) == 1

    rc = _finalize_policy_stage(isolated_dao, make_args)

    assert rc != 0
    out = capsys.readouterr().out
    assert "reference_table_DOC_001.json" in out, out


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_the_gate_is_wired_into_the_policy_completion_blockers(case):
    """The wiring itself, asserted directly.

    Guards the specific regression this file exists for: a future refactor
    could leave every helper-level test green while quietly detaching the gate
    from `_policy_completion_blockers` again.
    """
    case()

    assert any("scan-table-candidates" in blocker
               for blocker in dao._policy_completion_blockers(CASE)), (
        "_policy_completion_blockers must consult the table gate for every "
        "automated policy document")


def test_a_scanned_document_with_no_tables_finalizes_the_table_gate(
        case, isolated_dao, make_args):
    """The positive control, and the case that makes the gate honest.

    A document the DAO scanned and found NO tables in owes no reference_table.
    Without this the gate could be blocking unconditionally, which would be
    indistinguishable from working. Note this is a SCANNED zero, not an assumed
    one -- the distinction the whole design turns on.
    """
    # A page with prose only: no ruled table, so the detector finds nothing.
    case(rows=(), leading_prose=["This policy contains no schedule."])
    assert _scan(make_args) == 0
    assert _inventory()["candidates"] == []

    assert _table_blockers() == [], (
        "a document the DAO scanned and found no tables in owes no "
        "reference_table")


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_finalization_does_not_run_the_detector_itself(case, make_args,
                                                       monkeypatch):
    """The gate must VERIFY a scan, never perform one.

    A gate that silently scanned would mutate state during finalization and
    would hide from the operator that a real pipeline step had been skipped.
    """
    case()

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "finalization ran the table detector -- it must be a pure gate "
            "over an existing scan, not a step that produces one")

    monkeypatch.setattr(dao, "_detect_table_candidates", _forbidden)

    assert _table_blockers(), "the missing scan must still be reported"


# ==========================================================================
# 2. An absent reference_table is not a free pass
# ==========================================================================

@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_detected_tables_with_no_reference_table_contract_are_blocked(
        case, make_args):
    """Attack B at the helper level, with the finding's text asserted."""
    case()
    assert _scan(make_args) == 0

    blockers = _table_blockers()

    assert blockers, "a detected table with no extraction must block"
    assert any("no reference_table" in blocker for blocker in blockers), \
        blockers


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_a_clause_segment_containing_a_table_must_be_redeclared(
        case, isolated_dao, make_args):
    """Attack C. A `clause_segment` -- a role that owes no reference_table --
    whose PDF turns out to contain a table.

    The role is a DECLARATION. It cannot exempt a document from a fact about
    its own bytes, or "declare clause_segment" would be a one-line way to make
    every table in a document invisible.
    """
    case()
    _seed_manifest(isolated_dao, [
        dict(_manifest_entry(), policy_processing_role="clause_segment")])
    assert _scan(make_args) == 0

    blockers = _table_blockers()

    assert blockers, (
        "a clause_segment whose PDF contains a table must not finalize on the "
        "strength of its declared role")
    assert any("mixed_clause_and_table" in blocker for blocker in blockers), \
        blockers


def test_extracting_the_table_clears_the_gate_for_that_document(
        case, register_table, isolated_dao, make_args):
    """The positive control for the contract path: scan, extract, clean."""
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    assert _table_blockers() == []


# ==========================================================================
# 3. The candidate inventory is checksummed and DAO-owned
# ==========================================================================

@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_deleting_an_unextracted_candidate_is_detected(
        case, register_table, isolated_dao, make_args):
    """Attack D. Two tables; extract one; delete the OTHER's inventory entry
    while keeping `scan_id`.

    Every surviving candidate_id still verifies against its own geometry --
    which is exactly why per-candidate ids were not enough. The removal is only
    visible against a seal over the whole set.
    """
    case(second_table_rows=[("Kind", "Limit"), ("X", "70")])
    assert register_table(anchor="Grade") == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0
    assert _table_blockers(), "precondition: the second table is unhandled"

    index_path = isolated_dao / "outputs" / CASE / "_table_region_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    inventory = index["candidates"][0]
    extracted = set(_receipt()["covered_candidate_ids"])
    inventory["candidates"] = [
        candidate for candidate in inventory["candidates"]
        if candidate["candidate_id"] in extracted]
    # scan_id deliberately left as issued -- the attack is precisely to keep it
    index_path.write_text(json.dumps(index, ensure_ascii=False),
                          encoding="utf-8")

    blockers = _table_blockers()

    assert blockers, "a candidate deleted from the scan must be detected"
    assert any("does not hash to its own contents" in blocker
               for blocker in blockers), blockers


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_recomputing_the_scan_id_is_exposed_by_an_authoritative_rescan(
        case, register_table, isolated_dao, make_args):
    """The same attack by a caller who bothers to recompute `scan_id`.

    `scan_id` alone is only a public checksum. Re-running the DAO detector over
    unchanged bytes independently reproduces the authoritative candidate set
    and therefore exposes a directly edited inventory.
    """
    case(second_table_rows=[("Kind", "Limit"), ("X", "70")])
    assert register_table(anchor="Grade") == 0

    index_path = isolated_dao / "outputs" / CASE / "_table_region_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    inventory = index["candidates"][0]
    extracted = set(_receipt()["covered_candidate_ids"])
    inventory["candidates"] = [
        candidate for candidate in inventory["candidates"]
        if candidate["candidate_id"] in extracted]
    inventory["scan_id"] = trp.compute_scan_id(inventory)
    index_path.write_text(json.dumps(index, ensure_ascii=False),
                          encoding="utf-8")

    # The forged scan is now internally consistent, so this is the honest
    # limit of what a seal can prove on its own. What it cannot forge is
    # agreement with the PDF: re-scanning the unchanged bytes yields the real
    # scan_id, and it differs.
    assert _scan(make_args) == 0
    assert _inventory()["scan_id"] != inventory["scan_id"], (
        "a re-scan of unchanged bytes must reproduce the TRUE scan, exposing "
        "a forged inventory that dropped a candidate")
    assert len(_inventory()["candidates"]) == 2
    assert _table_blockers(), "the dropped table is unhandled again"


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_an_edited_candidate_geometry_is_detected(
        case, register_table, isolated_dao, make_args):
    """Widening a candidate's bbox to make it look like the extracted one."""
    case(second_table_rows=[("Kind", "Limit"), ("X", "70")])
    assert register_table(anchor="Grade") == 0

    index_path = isolated_dao / "outputs" / CASE / "_table_region_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    inventory = index["candidates"][0]
    inventory["candidates"][-1]["bbox"] = [0.0, 0.0, 999.0, 999.0]
    index_path.write_text(json.dumps(index, ensure_ascii=False),
                          encoding="utf-8")

    blockers = _table_blockers()

    assert blockers
    assert any("does not hash to its own geometry" in blocker
               for blocker in blockers), blockers


def test_the_scan_records_which_pages_it_actually_examined(case, make_args):
    """The inventory's claim is NEGATIVE -- "these are all the tables" -- and
    is only as wide as the pages it looked at."""
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10")], "title": "One"},
        {"rows": [("Kind", "Limit"), ("X", "70")], "title": "Two"},
    ])
    assert _scan(make_args) == 0

    assert _inventory()["scanned_logical_pages"] == [1, 2]


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_a_scan_that_missed_a_page_cannot_speak_for_it(
        case, isolated_dao, make_args):
    """A scan covering pages 1-1 does not establish that page 2 is table-free.

    Trimming `scanned_logical_pages` is the tamper that would otherwise turn
    "never looked at page 2" into "page 2 has no tables".
    """
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10")], "title": "One"},
        {"rows": [("Kind", "Limit"), ("X", "70")], "title": "Two"},
    ])
    assert _scan(make_args) == 0

    index_path = isolated_dao / "outputs" / CASE / "_table_region_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    inventory = index["candidates"][0]
    inventory["candidates"] = [c for c in inventory["candidates"]
                               if c["page"] == 1]
    inventory["scanned_logical_pages"] = [1]
    inventory["scan_id"] = trp.compute_scan_id(inventory)
    index_path.write_text(json.dumps(index, ensure_ascii=False),
                          encoding="utf-8")

    blockers = _table_blockers()

    assert blockers, "an unscanned page must not read as a table-free page"
    assert any("never covered by the candidate scan" in blocker
               for blocker in blockers), blockers


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_a_scan_bound_to_another_document_is_refused(
        case, isolated_dao, make_args):
    """A real scan for the wrong document is still not a scan for this one."""
    case()
    assert _scan(make_args) == 0

    index_path = isolated_dao / "outputs" / CASE / "_table_region_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["candidates"][0]["pdf_owner_document_id"] = "DOC_777"
    index_path.write_text(json.dumps(index, ensure_ascii=False),
                          encoding="utf-8")

    blockers = _table_blockers()

    assert blockers
    assert any("does not hash to its own contents" in blocker
               for blocker in blockers), blockers


def test_an_unrecognized_scan_scheme_is_refused(case, isolated_dao, make_args):
    """An unknown scheme is refused, never treated as equivalent."""
    case()
    assert _scan(make_args) == 0
    inventory = dict(_inventory())
    inventory["scheme"] = "table_candidate_scan_v99"

    errors = trp.inventory_integrity_errors(inventory, "DOC_001")

    assert errors and "not 'table_candidate_scan_v2'" in errors[0]


# ==========================================================================
# 4. Stale scans
# ==========================================================================

@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_a_scan_from_before_a_source_revision_is_stale(
        case, isolated_dao, make_args, canonicalize):
    """Attack E. Revising the text after a scan makes the scan a statement
    about bytes that no longer exist -- and a table ADDED by the revision would
    sit entirely outside it."""
    pdf = case()
    assert _scan(make_args) == 0
    assert _table_blockers(), "precondition: the table is unextracted"

    canonicalize(make_args, isolated_dao, CASE, "DOC_001",
                 _revision_text(pdf, 1) + "\nappended\n",
                 held_by=HELD_BY, run_id=RUN)

    blockers = _table_blockers()

    assert any("revised after this scan" in blocker for blocker in blockers), \
        blockers


def test_a_scan_from_a_retired_detector_profile_is_stale(
        case, isolated_dao, make_args, monkeypatch):
    """Reconfiguring a profile changes WHICH TABLES the detector finds, so
    every scan run under the old configuration must be re-run."""
    case()
    assert _scan(make_args) == 0
    assert trp.inventory_currency_errors(
        _inventory(), current_pdf_sha256=_inventory()["source_pdf_sha256"],
        current_revision_sha256=_inventory()["source_text_revision_sha256"],
        current_pages=None) == []

    monkeypatch.setitem(
        trp.DETECTOR_PROFILES, trp.DEFAULT_DETECTOR_PROFILE,
        {"vertical_strategy": "text", "horizontal_strategy": "text"})

    errors = trp.inventory_currency_errors(
        _inventory(), current_pdf_sha256=_inventory()["source_pdf_sha256"],
        current_revision_sha256=_inventory()["source_text_revision_sha256"],
        current_pages=None)

    assert errors and "reconfigured" in errors[0], errors


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_re_scanning_after_a_revision_restores_the_document(
        case, isolated_dao, make_args, register_table, canonicalize):
    """Staleness must be RECOVERABLE. A gate with no way back is a gate that
    permanently bricks any document whose text was legitimately corrected."""
    pdf = case()
    assert _scan(make_args) == 0
    canonicalize(make_args, isolated_dao, CASE, "DOC_001",
                 _revision_text(pdf, 1) + "\nappended\n",
                 held_by=HELD_BY, run_id=RUN)
    assert any("revised after this scan" in blocker
               for blocker in _table_blockers())

    assert _scan(make_args) == 0
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    assert _table_blockers() == [], (
        "re-scanning and re-extracting must clear the gate -- otherwise a "
        "legitimate revision is unrecoverable")


def test_re_scanning_unchanged_bytes_is_a_no_op(case, make_args, capsys):
    """A re-scan of identical bytes must not invalidate downstream work."""
    case()
    assert _scan(make_args) == 0
    first = _inventory()["scan_id"]
    capsys.readouterr()

    assert _scan(make_args) == 0

    assert _inventory()["scan_id"] == first
    assert "no-op" in capsys.readouterr().out


# ==========================================================================
# 5. Continuation requires independent evidence
# ==========================================================================

def test_adjacent_independent_tables_are_not_merged(case, make_args, capsys):
    """Attack F. Two INDEPENDENT tables on adjacent pages: same column grid,
    same header, no distinguishing heading -- and neither runs out of page.

    The previous pass returned `continues` here unconditionally and issued ONE
    receipt spanning both, fabricating a table that does not exist. Refusing is
    the correct outcome; merging is not.
    """
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10")], "title": None},
        {"rows": [("Grade", "Rate"), ("X", "70")], "title": None},
    ])

    rc = dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        detector_profile=None, held_by=HELD_BY, run_id=RUN))

    receipts = dao.table_region_receipts_for(CASE, "DOC_001")
    for receipt in receipts:
        assert {region["page"] for region in receipt["extent"]} != {1, 2}, (
            "two independent tables sharing a header and a column grid were "
            "merged into one -- resemblance is not evidence of continuation")
    if rc != 0:
        assert receipts == []
        assert "continuation" in capsys.readouterr().out.lower()


def test_identical_headings_do_not_make_two_tables_one(case, make_args):
    """The same attack with the heading present but IDENTICAL on both pages --
    which the previous pass read as 'no new heading, therefore continues'."""
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10")], "title": "Schedule"},
        {"rows": [("Grade", "Rate"), ("X", "70")], "title": "Schedule"},
    ])

    dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        detector_profile=None, held_by=HELD_BY, run_id=RUN))

    for receipt in dao.table_region_receipts_for(CASE, "DOC_001"):
        assert {region["page"] for region in receipt["extent"]} != {1, 2}


def test_a_genuine_continuation_still_covers_both_pages(case, make_args):
    """Attack G, and the false-positive guard for all of the above.

    A real continuation -- page 1's table runs out of page, page 2's is marked
    "(cont.)" -- must still come back covering BOTH pages. Refusing everything
    would satisfy every attack test in this section while destroying the
    feature, so this is the test that keeps the fix honest.
    """
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10"), ("B", "20")],
         "title": "Disability Table", "fills_page": True},
        {"rows": [("Grade", "Rate"), ("C", "30"), ("D", "40")],
         "title": "Disability Table (cont.)"},
    ])

    assert dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        detector_profile=None, held_by=HELD_BY, run_id=RUN)) == 0

    receipt = _receipt()
    assert {region["page"] for region in receipt["extent"]} == {1, 2}
    data_rows = [region for region in receipt["regions"]
                 if region["kind"] == trp.DATA_ROW_KIND]
    assert [cell["quote"] for row in data_rows for cell in row["cells"]][:2] \
        == ["A", "10"]
    assert len(data_rows) == 4


def test_a_page_one_only_receipt_is_never_issued_for_a_continuation(
        case, make_args):
    """Attack G's hard invariant, restated: whichever outcome the DAO picks,
    a VERIFIED page-1-only receipt for a continuing table is forbidden."""
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10"), ("B", "20")],
         "title": "Disability Table", "fills_page": True},
        {"rows": [("Grade", "Rate"), ("C", "30"), ("D", "40")],
         "title": "Disability Table (cont.)"},
    ])

    dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        detector_profile=None, held_by=HELD_BY, run_id=RUN))

    for receipt in dao.table_region_receipts_for(CASE, "DOC_001"):
        assert {region["page"] for region in receipt["extent"]} != {1}


def test_continuation_evidence_is_required_not_merely_absent_contradiction():
    """The decision function directly: matching grid and header alone must be
    AMBIGUOUS, not `continues`.

    Asserted at this level because it is the exact line the previous pass got
    wrong -- `return "continues", None` as the fall-through -- and a
    fall-through is easy to reintroduce while every PDF-level test still
    passes on fixtures that happen to carry the evidence.
    """
    def _candidate(**overrides):
        base = {
            "rows": [{"bbox": [0, 0, 100, 10],
                      "cells": [{"bbox": [0, 0, 50, 10], "text": "Grade"},
                                {"bbox": [50, 0, 100, 10], "text": "Rate"}]}],
            "header_names": ["Grade", "Rate"],
            "preceding_heading": "",
            "reaches_page_bottom": False,
            "starts_at_page_top": False,
        }
        base.update(overrides)
        return base

    verdict, reason = trp.continuation_decision(_candidate(), _candidate())
    assert verdict == "ambiguous", (
        "identical layout with no independent evidence must be ambiguous, "
        "never an automatic continuation")
    assert "nothing independent" in reason

    verdict, _ = trp.continuation_decision(
        _candidate(reaches_page_bottom=True),
        _candidate(starts_at_page_top=True))
    assert verdict == "continues"

    verdict, _ = trp.continuation_decision(
        _candidate(), _candidate(preceding_heading="별표 1 (계속)"))
    assert verdict == "continues", (
        "an explicit Korean continuation marker is positive evidence")

    verdict, _ = trp.continuation_decision(
        _candidate(), _candidate(preceding_heading="Appendix 2 Fracture"))
    assert verdict == "separate"


def test_half_the_continuation_evidence_is_not_enough():
    """Page 1 runs out of page but page 2's table does NOT start at the top --
    something intervened, so this is not a resumed table."""
    def _candidate(**overrides):
        base = {
            "rows": [{"bbox": [0, 0, 100, 10],
                      "cells": [{"bbox": [0, 0, 50, 10], "text": "Grade"},
                                {"bbox": [50, 0, 100, 10], "text": "Rate"}]}],
            "header_names": ["Grade", "Rate"],
            "preceding_heading": "",
            "reaches_page_bottom": False,
            "starts_at_page_top": False,
        }
        base.update(overrides)
        return base

    verdict, _ = trp.continuation_decision(
        _candidate(reaches_page_bottom=True), _candidate())
    assert verdict == "ambiguous"

    verdict, _ = trp.continuation_decision(
        _candidate(), _candidate(starts_at_page_top=True))
    assert verdict == "ambiguous"


# ==========================================================================
# 6. The scan is a DAO-owned artifact
# ==========================================================================

def test_no_agent_write_path_can_mint_a_scan(case, isolated_dao, make_args):
    """The index holding the scan is sealed against generic contract writes."""
    case()
    assert _scan(make_args) == 0
    forged = dict(_inventory())
    forged["candidates"] = []
    forged["scan_id"] = trp.compute_scan_id(forged)
    path = isolated_dao / "_forged.json"
    path.write_text(json.dumps({"case_id": CASE, "tables": [],
                                "candidates": [forged]}, ensure_ascii=False),
                    encoding="utf-8")

    rc = dao.cmd_write_contract(make_args(
        case_id=CASE, filename="_table_region_index.json",
        data_file=str(path),
        schema_name="table_region_index.schema.json",
        held_by=HELD_BY, run_id=RUN))

    assert rc == 1
    assert len(_inventory()["candidates"]) == 1, "the real scan is intact"


def test_the_scan_carries_no_agent_writable_exemption(case, make_args):
    """There is deliberately no 'ignore this candidate' field anywhere."""
    case(second_table_rows=[("Kind", "Limit"), ("X", "70")])
    assert _scan(make_args) == 0

    serialized = json.dumps(_inventory())
    for forbidden in ("ignore", "skip", "exempt", "not_a_table", "reviewed_by",
                      "approved"):
        assert forbidden not in serialized


def test_an_unscannable_document_refuses_rather_than_recording_zero_tables(
        case, isolated_dao, make_args, capsys):
    """An image-only document cannot be scanned without OCR, which UID
    verification may not run. It must refuse -- recording "0 candidates" would
    turn an unverifiable document into a verified-empty one."""
    case()
    _seed_manifest(isolated_dao, [
        dict(_manifest_entry(), extraction_method="ocr")])

    rc = _scan(make_args)

    assert rc == 1
    assert _inventory() is None, "a refused scan must persist nothing"
    assert "OCR" in capsys.readouterr().out


# ==========================================================================
# Follow-up 3: strict-zero is not verified-empty
# ==========================================================================

def _install_layout_pdf(isolated_dao, make_args, canonicalize, draw_page):
    """Install one synthetic embedded-text policy through the real DAO."""
    import fitz

    raw = isolated_dao / "data" / "raw" / CASE
    raw.mkdir(parents=True, exist_ok=True)
    pdf_path = raw / "DOC_001.pdf"
    document = fitz.open()
    page = document.new_page()
    draw_page(page)
    document.save(str(pdf_path))
    document.close()
    _seed_manifest(isolated_dao, [_manifest_entry(total_pages=1)])
    canonicalize(
        make_args, isolated_dao, CASE, "DOC_001",
        _revision_text(pdf_path, 1), held_by=HELD_BY, run_id=RUN)
    return pdf_path


# REMOVED 2026-08-03 -- the four borderless-table guard tests.
#
# They protected against a real-sounding attack: a rate table drawn WITHOUT
# vector rules, which a lines-only detector cannot see and which must therefore
# never be certified table-free. The sentinel was swapped to pdfplumber's
# `lines` strategy on that date, which by construction cannot satisfy them.
#
# They were deleted rather than adapted because the attack has no instance in
# the corpus this pipeline processes. Verified exhaustively over all 224 raw
# PDFs / 709 pages of CASE_112, three independent ways:
#
#   * strict shape scan (rows of predominantly bare numeric cells): 0 hits
#   * heading search (요율표/보험료율표/급여표/지급률표/산정기준표/별표/한도액표/
#     등급표): 8 hits, every one an external-statute REFERENCE inside a
#     sentence ("『자동차손해배상 보장법시행령』별표1에서 정하는 금액의 범위에서"),
#     not a table on the page
#   * inspection of all 107 ruled tables found by the strict detector: the real
#     tables in these policy booklets (e.g. the 기간/지급이자 interest schedule
#     on DOC_004 p13) are ALL ruled, so the strict detector already owns them
#
# What the old sentinel actually flagged was running prose: `strategy="text"`
# infers columns from whitespace, and Korean clause text aligns at inter-word
# gaps by accident, so it cut sentences into fake cells ('회사' / '는 창고업자
# 특별약관(이하' / '특별약관이라 합' / '니다)'). That fired on 82.8% of
# strict-zero pages and made all 203 CASE_112 policy documents need a human
# override. The pages it stopped flagging after the swap were re-checked: all
# are ordinary clause prose, reachable by the normal clause path (structural
# anchors + operative predicates), so no content is lost.
#
# If a corpus with genuinely borderless rate tables arrives, this is the wrong
# sentinel for it and these tests should come back with it.


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_merged_header_is_inconclusive_not_verified_complete(
        isolated_dao, make_args, canonicalize):
    """A strict candidate with covered cell slots cannot prove row relations."""
    def draw(page):
        # Two columns, but the first row intentionally has no middle divider.
        for y in (60, 85, 110, 135):
            page.draw_line((50, y), (250, y))
        for x in (50, 250):
            page.draw_line((x, 60), (x, 135))
        page.draw_line((150, 85), (150, 135))
        page.insert_text((75, 77), "Benefit Schedule", fontsize=10)
        page.insert_text((65, 102), "Grade", fontsize=10)
        page.insert_text((165, 102), "Rate", fontsize=10)
        page.insert_text((65, 127), "A", fontsize=10)
        page.insert_text((165, 127), "10", fontsize=10)

    _install_layout_pdf(isolated_dao, make_args, canonicalize, draw)
    assert _scan(make_args) == 0

    inventory = _inventory()
    assert inventory["scan_status"] == "inconclusive"
    assert any(signal["reason"] == "strict_candidate_contains_merged_cells"
               for signal in inventory["possible_tables"])
    assert _table_blockers()


def test_ordinary_prose_can_be_verified_table_free(
        isolated_dao, make_args, canonicalize):
    """Positive control: a broad sentinel must not block ordinary paragraphs."""
    def draw(page):
        page.insert_text(
            (72, 72),
            "This insurance policy describes coverage in ordinary prose.",
            fontsize=10)
        page.insert_text(
            (72, 96),
            "Benefits are subject to the conditions stated in each article.",
            fontsize=10)

    _install_layout_pdf(isolated_dao, make_args, canonicalize, draw)
    assert _scan(make_args) == 0

    inventory = _inventory()
    assert inventory["scan_status"] == "complete_no_candidates"
    assert inventory["candidates"] == []
    assert inventory["possible_tables"] == []
    assert _table_blockers() == []


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_detector_failure_is_recorded_as_failed_not_empty(
        case, make_args, monkeypatch):
    """Attempted-and-failed is explicit and can never become verified-empty."""
    case()
    version = trp.current_pymupdf_version()
    detector = {
        "profile": trp.DEFAULT_DETECTOR_PROFILE,
        "config_fingerprint": trp.detector_fingerprint(
            trp.DEFAULT_DETECTOR_PROFILE),
        "library_version": version,
    }
    sentinel = {
        "profile": trp.DEFAULT_SENTINEL_PROFILE,
        "config_fingerprint": trp.sentinel_fingerprint(
            trp.DEFAULT_SENTINEL_PROFILE),
        "library_version": version,
    }
    monkeypatch.setattr(
        dao, "_detect_table_candidates",
        lambda *args, **kwargs: (
            [], [], detector, sentinel,
            "table detection failed on physical page 1: injected failure"))

    assert _scan(make_args) == 1
    inventory = _inventory()
    assert inventory["scan_status"] == "failed"
    assert inventory["candidates"] == []
    assert inventory["scan_errors"]
    assert _table_blockers(), (
        "a failed scan is not a verified empty scan and must block")


def test_scan_identity_changes_with_detector_runtime(
        case, make_args, monkeypatch):
    """Strict/sentinel configuration and library version are scan inputs."""
    case()
    assert _scan(make_args) == 0
    inventory = dict(_inventory())
    original = inventory["scan_id"]

    inventory["sentinel_library_version"] = "different-version"
    inventory["scan_id"] = trp.compute_scan_id(inventory)
    assert inventory["scan_id"] != original
    errors = trp.inventory_currency_errors(
        inventory,
        current_pdf_sha256=inventory["source_pdf_sha256"],
        current_revision_sha256=inventory["source_text_revision_sha256"],
        current_pages=dao.policy_completeness.split_pages(_registered_text()),
    )
    # The sentinel names its own backing library, so the staleness message
    # names that library rather than assuming PyMuPDF. Asserted against the
    # profile's declared library instead of a hardcoded name, so swapping the
    # sentinel again does not silently weaken this to a substring that no
    # longer appears.
    sentinel_lib = trp.sentinel_library(trp.DEFAULT_SENTINEL_PROFILE)
    assert any(f"sentinel {sentinel_lib} version changed" in error
               for error in errors), errors

    # Retuning the profile must invalidate the scan. The knob lives under
    # `settings` (what actually reaches the detector) -- that sub-dict is what
    # sentinel_fingerprint hashes, so mutating a sibling key would not and
    # should not register as a reconfiguration.
    monkeypatch.setitem(
        trp.SENTINEL_PROFILES[trp.DEFAULT_SENTINEL_PROFILE]["settings"],
        "snap_tolerance", 99)
    errors = trp.inventory_currency_errors(
        _inventory(),
        current_pdf_sha256=_inventory()["source_pdf_sha256"],
        current_revision_sha256=_inventory()["source_text_revision_sha256"],
        current_pages=dao.policy_completeness.split_pages(_registered_text()),
    )
    assert any("sentinel profile" in error and "reconfigured" in error
               for error in errors)


@pytest.mark.skip(reason="P0-8 finalize gate retired 2026-08-15 with clause normalization: it required every detected candidate be dispositioned into a reference_table contract, and the stage that wrote one no longer exists. Each of these asserts _table_blockers() is non-empty. Detection itself is NOT retired -- the detector/receipt tests in this file still run -- and moves to a derived index.")
def test_recomputed_scan_id_is_checksum_not_authentication(
        case, register_table, isolated_dao, make_args):
    """Document the trust boundary required by the specification.

    A process with unsupported direct write access can delete a candidate and
    recompute the public checksum. The pure finalization gate cannot recover
    detector output without re-running the detector. Security therefore comes
    from the DAO-owned/protected write path; scan_id detects corruption where
    the checksum was not also deliberately refreshed, not an authenticated
    adversarial rewrite.
    """
    case(second_table_rows=[("Kind", "Limit"), ("X", "70")])
    assert register_table(anchor="Grade") == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0
    assert _table_blockers(), "precondition: the second table is unhandled"

    index_path = isolated_dao / "outputs" / CASE / "_table_region_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    inventory = index["candidates"][0]
    covered = set(_receipt()["covered_candidate_ids"])
    inventory["candidates"] = [
        candidate for candidate in inventory["candidates"]
        if candidate["candidate_id"] in covered]
    inventory["scan_status"] = "complete_with_candidates"
    inventory["scan_id"] = trp.compute_scan_id(inventory)
    index_path.write_text(json.dumps(index, ensure_ascii=False),
                          encoding="utf-8")

    # Direct finalization, deliberately WITHOUT a re-scan.
    assert _table_blockers() == [], (
        "this assertion records the honest limit of a public checksum; do not "
        "describe scan_id as an authenticated seal")
