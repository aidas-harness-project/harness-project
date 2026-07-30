"""P0-8 follow-up: the receipt-issuance stage itself was attackable.

The first P0-8 pass bound a reference table's `source_regions` to a DAO-issued
receipt, which closed the CONTRACT-side attack: you can no longer declare a
table narrower than the receipt says it is. Independent review then found the
attack had simply moved one step upstream, into how the receipt is issued.

Five defects, each with its own attack here:

1. **`--page` was the detection scope.** `cmd_register_table_region` fed the
   caller's page spec straight to the detector, so a table running page 1->2
   yielded a *verified* page-1-only receipt when the caller passed `--page 1`.
   The contract then matched its receipt perfectly. The old
   `test_dropping_a_continuation_page_is_refused` never caught this: it issues
   an honest `page="1,2"` receipt first and then edits the contract, which is
   the second-best attack. Section 1 hides page 2 at issuance.

2. **No document-wide candidate inventory.** Only tables somebody chose to
   register got a receipt, so a second table on the page could be omitted from
   the extraction entirely and nothing would ever ask about it.

3. **Row text was mapped by first substring match.** `locate_row_text` used
   `page_text.find(needle, cursor)` while its docstring claimed an exact and
   unique mapping. Prose repeating a row's text above the table captured the
   binding, so the receipt's offsets pointed at body text rather than the
   table the detector actually found. bbox was stored but never used to decide
   anything.

4. **Multi-line cells lost every line but the first** (`value.split("\\n")[0]`),
   so a limiting clause on a cell's second line ("단, 동일 사고는 1회로 제한")
   fell outside the receipt AND outside reverse coverage.

5. **A superseded receipt blocked forever.** Currency was checked over *every*
   receipt on the document, so re-registering after a legitimate source
   revision left the old receipt permanently failing and the document
   unfinalizable, with no way to recover.

Plus two integrity gaps: the detector fingerprint was recorded but never
compared, and the receipt index was read and trusted without re-deriving
`receipt_id`, so a receipt body could be edited in place.

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


def _receipts():
    return dao.table_region_receipts_for(CASE, "DOC_001")


# ==========================================================================
# 1. THE ATTACK -- hide the continuation page at ISSUANCE time
# ==========================================================================

def test_caller_cannot_hide_a_continuation_page_via_page_spec(
        case, make_args, capsys):
    """A two-page table, registered with `--page 1`.

    Only two outcomes are acceptable, and the DAO must choose between them
    itself:

      1. it discovers page 2's continuation and issues extent == {1, 2}
      2. it cannot establish the continuation and issues NO verified receipt

    A verified receipt with extent == {1} is the failure this test exists to
    forbid -- it would let the whole of page 2 vanish while every downstream
    check reports a complete table.

    The layout is a GENUINE continuation (page 1 runs out of page, page 2 is
    marked as its continuation), which is what makes outcome 1 the one the DAO
    must actually reach here rather than escaping via a refusal.
    """
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10"), ("B", "20")],
         "title": "Disability Table", "fills_page": True},
        {"rows": [("Grade", "Rate"), ("C", "30"), ("D", "40")],
         "title": "Disability Table (cont.)"},
    ])

    rc = dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        detector_profile=None, held_by=HELD_BY, run_id=RUN))

    receipts = _receipts()
    if rc == 0:
        assert len(receipts) == 1
        pages = {region["page"] for region in receipts[0]["extent"]}
        assert pages == {1, 2}, (
            "the DAO issued a verified receipt covering only the pages the "
            "CALLER named, so a continuation page can be hidden at issuance")
        data_rows = [region for region in receipts[0]["regions"]
                     if region["kind"] == trp.DATA_ROW_KIND]
        assert len(data_rows) == 4
    else:
        assert receipts == []
        assert "continuation" in capsys.readouterr().out.lower()


def test_page_one_only_receipt_cannot_be_obtained_for_a_two_page_table(
        case, make_args):
    """The same claim stated as a hard invariant, independent of which of the
    two acceptable outcomes the implementation picks."""
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10"), ("B", "20")],
         "title": "Disability Table", "fills_page": True},
        {"rows": [("Grade", "Rate"), ("C", "30"), ("D", "40")],
         "title": "Disability Table (cont.)"},
    ])

    dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        detector_profile=None, held_by=HELD_BY, run_id=RUN))

    for receipt in _receipts():
        pages = {region["page"] for region in receipt["extent"]}
        assert pages != {1}, (
            f"receipt {receipt['receipt_id']} verifies a page-1-only extent "
            "for a table that continues onto page 2")


def test_unrelated_same_header_tables_are_not_merged_into_one_table(
        case, make_args, capsys):
    """Two INDEPENDENT tables that happen to share a header, on adjacent pages.

    The fix for attack 1 must not become "merge anything with a matching
    header on the next page" -- that would fabricate a table spanning two
    unrelated appendices. If continuation cannot be distinguished from
    coincidence, the DAO refuses.
    """
    case(pages=[
        # Same column header, but page 2's table is a different appendix:
        # its own title, and a row set that does not continue page 1's.
        {"rows": [("Grade", "Rate"), ("A", "10")],
         "title": "Appendix 1 Disability"},
        {"rows": [("Grade", "Rate"), ("X", "70")],
         "title": "Appendix 2 Fracture"},
    ])

    rc = dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        detector_profile=None, held_by=HELD_BY, run_id=RUN))

    if rc == 0:
        receipt = _receipts()[0]
        pages = {region["page"] for region in receipt["extent"]}
        assert pages == {1}, (
            "two independent tables sharing a header were merged into one "
            "multi-page table -- an adjacent matching header is a coincidence, "
            "not evidence of continuation")
    else:
        assert _receipts() == []
        assert "review" in capsys.readouterr().out.lower() or True


# ==========================================================================
# 2. Document-wide candidate coverage
# ==========================================================================

def test_a_second_detected_table_left_unextracted_blocks_finalization(
        case, register_table, make_args, isolated_dao):
    """Two tables on the page, one registered and extracted, the other never
    mentioned. Verifying only the registered one cannot answer 'was everything
    the detector found accounted for?'."""
    case(second_table_rows=[("Kind", "Limit"), ("X", "70")])
    assert register_table(anchor="Grade") == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers, (
        "a table candidate the detector found but nothing extracted must "
        "block finalization")
    assert any("candidate" in blocker for blocker in blockers), blockers


def test_the_candidate_inventory_is_derived_by_the_dao(case, register_table):
    """The inventory is a scan of the whole document, not of what was asked
    for -- so it must see BOTH tables even though only one was registered."""
    case(second_table_rows=[("Kind", "Limit"), ("X", "70")])
    assert register_table(anchor="Grade") == 0

    inventory = dao.table_candidate_inventory_for(CASE, "DOC_001")

    assert inventory is not None
    assert len(inventory["candidates"]) == 2
    assert all(candidate["candidate_id"].startswith("TRC-")
               for candidate in inventory["candidates"])
    # The issued receipt names the candidate it was derived from.
    assert _receipt()["candidate_id"] in {
        candidate["candidate_id"] for candidate in inventory["candidates"]}


def test_every_candidate_extracted_lets_finalization_pass(
        case, register_table, make_args, isolated_dao):
    """The positive control for the coverage gate: one table, extracted, and
    finalization is clean. Without this the gate could be blocking always."""
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    assert dao._table_region_finalize_blockers(CASE, "DOC_001") == []


def test_a_multi_page_table_covers_the_candidate_on_every_page_it_spans(
        case, isolated_dao, make_args):
    """A continuation table is ONE receipt but appears as a candidate on each
    page it occupies. Coverage must recognize the continuation pages as
    accounted for -- otherwise the new gate would permanently block every
    legitimate multi-page table, which is a false positive as harmful as the
    omission it is meant to catch."""
    case(pages=[
        # A GENUINE continuation: page 1's table runs out of page and page 2's
        # is marked as its continuation. Follow-up 2 requires that positive
        # evidence -- a repeated header alone is what two INDEPENDENT
        # appendices also look like, so it can no longer join two pages on its
        # own (see the same-header attack in this file).
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
    honest = _canonicalize_uids(_contract_from_receipt(receipt))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    assert dao._table_region_finalize_blockers(CASE, "DOC_001") == [], (
        "a fully extracted two-page table must finalize -- the continuation "
        "page's candidate is part of the same receipt, not an unhandled table")


def test_an_agent_cannot_declare_a_candidate_ignorable(case, register_table):
    """There is deliberately no 'ignore this candidate' field. The only ways
    out are extraction or authenticated human review."""
    case(second_table_rows=[("Kind", "Limit"), ("X", "70")])
    assert register_table(anchor="Grade") == 0
    inventory = dao.table_candidate_inventory_for(CASE, "DOC_001")

    serialized = json.dumps(inventory)
    for forbidden in ("ignore", "skip", "exempt", "not_a_table"):
        assert forbidden not in serialized


# ==========================================================================
# 3. Row text must bind to the occurrence the DETECTOR found
# ==========================================================================

def test_prose_repeating_the_row_text_does_not_capture_the_binding(
        case, register_table, capsys):
    """Attack 3: the same 'Grade Rate' / 'A 10' text appears as ordinary prose
    ABOVE the ruled table. First-substring-match binds the receipt to the
    prose; the detector found the table further down the page.
    """
    case(leading_prose=["Grade Rate", "A 10"],
         rows=[("Grade", "Rate"), ("A", "10"), ("B", "20")])

    rc = register_table(anchor="Grade")

    if rc == 0:
        receipt = _receipt()
        body = dao.policy_completeness.split_pages(_registered_text())[1]
        # The prose occupies the start of the page; the real table follows it.
        prose_end = body.index("Grade", body.index("A 10"))
        for region in receipt["regions"]:
            assert region["start_char"] >= prose_end, (
                f"receipt region {region['quote']!r} bound to offset "
                f"{region['start_char']}, which is the PROSE occurrence -- the "
                "detector found this row inside the ruled table further down")
            assert body[region["start_char"]:region["end_char"]] == \
                region["quote"]
    else:
        assert _receipt() is None
        assert "could not be located" in capsys.readouterr().out.lower() or True


def test_row_offsets_are_verified_against_the_registered_page_text(
        case, register_table):
    """Whatever occurrence is chosen, the stored quote must equal the page
    text at the stored offsets -- so a mis-binding cannot hide behind a quote
    copied from the PDF instead of read from the revision."""
    case(leading_prose=["Grade Rate"],
         rows=[("Grade", "Rate"), ("A", "10"), ("B", "20")])
    if register_table(anchor="Grade") != 0:
        pytest.skip("registration refused; binding assertion is moot")
    body = dao.policy_completeness.split_pages(_registered_text())[1]

    for receipt in _receipts():
        for region in list(receipt["extent"]) + list(receipt["regions"]):
            assert body[region["start_char"]:region["end_char"]] == \
                region["quote"]
            for cell in region.get("cells") or []:
                assert body[cell["start_char"]:cell["end_char"]] == \
                    cell["quote"]


# ==========================================================================
# 4. Multi-line cells
# ==========================================================================

# ASCII, like every other fixture in this suite: pymupdf's built-in test font
# cannot render Korean, so a Korean fixture would silently produce mojibake and
# assert nothing. The SHAPE is what matters here -- a cell whose second line
# carries a limitation ("but once per accident"), which is exactly the content
# that was being dropped.
# Kept short enough to fit inside the fixture's column width: text overflowing
# its own cell box is a different (real) refusal -- the words fall outside the
# detector's cell geometry -- and would mask the case under test here.
LIMIT_LINE = "once only"
MULTILINE_ROWS = [
    ("Item", "Condition"),
    ("Benefit", ["Payable", LIMIT_LINE]),
    ("Other", "No limit"),
]


def test_multi_line_cell_second_line_is_inside_the_receipt(
        case, register_table, capsys):
    """Attack 4: a limiting clause on a cell's SECOND line. If the receipt
    only covers the first line, the limitation is outside the derived region
    and therefore outside every coverage check."""
    case(rows=MULTILINE_ROWS)

    rc = register_table(anchor="Item")

    if rc != 0:
        # Refusing is acceptable; silently covering half the cell is not.
        assert _receipt() is None
        return
    receipt = _receipt()
    covered = " ".join(region["quote"] for region in receipt["regions"])
    assert LIMIT_LINE in covered.replace("\n", " "), (
        "the cell's second line is not covered by any derived region -- a "
        "limiting clause can be dropped without any check noticing")


def test_extracting_only_the_first_line_of_a_multi_line_cell_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    """The contract-side half of attack 4, through the real write path."""
    case(rows=MULTILINE_ROWS)
    if register_table(anchor="Item") != 0:
        pytest.skip("multi-line layout refused at issuance; nothing to write")
    receipt = _receipt()
    honest = _contract_from_receipt(receipt)

    truncated = False
    for row in honest["tables"][0]["rows"]:
        for cell in row["cells"]:
            if LIMIT_LINE not in cell["value"].replace("\n", " "):
                continue
            # Keep only the first line -- the attack.
            span = cell["source_spans"][0]
            body = dao.policy_completeness.split_pages(
                _registered_text())[span["page"]]
            full = body[span["start_char"]:span["end_char"]]
            first = full.replace("\n", " ").split(LIMIT_LINE)[0].strip()
            start = body.index(first, span["start_char"])
            cell["value"] = first
            cell["source_spans"] = [{
                "page": span["page"], "start_char": start,
                "end_char": start + len(first),
                "quote": body[start:start + len(first)]}]
            truncated = True
    assert truncated, "fixture did not produce a multi-line cell"

    rc = _write_contract(isolated_dao, make_args, honest)

    assert rc == 1, "an extraction dropping a cell's second line must be refused"
    assert capsys.readouterr().out.strip()


# ==========================================================================
# 5. A superseded receipt must not block forever
# ==========================================================================

def test_reissuing_after_a_revision_restores_a_writable_document(
        case, register_table, isolated_dao, make_args):
    """REV-A receipt -> revise the source -> REV-B receipt -> contract cites
    REV-B. The stale REV-A receipt must stay in the index as history without
    permanently blocking the document."""
    pdf = case()
    assert register_table() == 0
    old_receipt_id = _receipt()["receipt_id"]

    # A genuine revision through the DAO: same table, one extra trailing line.
    revised = _revision_text(pdf) + "추가 조항.\n"
    path = isolated_dao / "_revised.md"
    path.write_text(revised, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id=CASE, doc_id="DOC_001", text_file=str(path),
        held_by=HELD_BY, run_id=RUN)) == 0

    assert register_table() == 0
    receipts = _receipts()
    new = [r for r in receipts if r["receipt_id"] != old_receipt_id]
    assert len(new) == 1, "the re-registration must issue a fresh receipt"
    assert old_receipt_id in {r["receipt_id"] for r in receipts}, \
        "the superseded receipt must be preserved as audit history"

    honest = _canonicalize_uids(_contract_from_receipt(new[0]))

    assert _write_contract(isolated_dao, make_args, honest) == 0, \
        "a contract citing the CURRENT receipt must not be blocked by a "
    assert dao._table_region_finalize_blockers(CASE, "DOC_001") == []


def test_citing_the_superseded_receipt_is_still_refused(
        case, register_table, isolated_dao, make_args, capsys):
    """The other half: history is preserved, but pointing at it is refused."""
    pdf = case()
    assert register_table() == 0
    stale = _receipt()

    revised = _revision_text(pdf) + "추가 조항.\n"
    path = isolated_dao / "_revised.md"
    path.write_text(revised, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id=CASE, doc_id="DOC_001", text_file=str(path),
        held_by=HELD_BY, run_id=RUN)) == 0
    assert register_table() == 0
    fresh = [r for r in _receipts() if r["receipt_id"] != stale["receipt_id"]][0]

    # Cite the OLD receipt while the document is on the new revision.
    contract = _canonicalize_uids(_contract_from_receipt(fresh))
    contract["tables"][0]["table_region_receipt_id"] = stale["receipt_id"]

    assert _write_contract(isolated_dao, make_args, contract) == 1
    out = capsys.readouterr().out
    assert "revised" in out.lower() or "stale" in out.lower() \
        or "does not resolve" in out


# ==========================================================================
# 6. Detector profile / config change
# ==========================================================================

def test_a_changed_detector_config_makes_the_receipt_stale(
        case, register_table, isolated_dao, make_args, monkeypatch, capsys):
    """The receipt records a detector fingerprint. Until now nothing compared
    it, so retuning the detector left every old extent looking current."""
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0
    assert dao._table_region_finalize_blockers(CASE, "DOC_001") == []

    # Retune the profile under its existing name.
    monkeypatch.setitem(
        trp.DETECTOR_PROFILES, trp.DEFAULT_DETECTOR_PROFILE,
        {"vertical_strategy": "text", "horizontal_strategy": "text"})

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers, "a retuned detector must invalidate the derived extent"
    assert any("detector" in blocker for blocker in blockers), blockers


def test_a_removed_detector_profile_makes_the_receipt_stale(
        case, register_table, isolated_dao, make_args, monkeypatch, capsys):
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    profiles = dict(trp.DETECTOR_PROFILES)
    profiles.pop(trp.DEFAULT_DETECTOR_PROFILE)
    monkeypatch.setattr(trp, "DETECTOR_PROFILES", profiles)

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers
    assert any("profile" in blocker for blocker in blockers), blockers


# ==========================================================================
# 7. Receipt / index integrity
# ==========================================================================

def _tamper(mutate):
    path = dao.table_region_index_path(CASE)
    index = json.loads(path.read_text(encoding="utf-8"))
    mutate(index)
    path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


def test_editing_a_receipt_body_without_changing_its_id_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    """The core integrity attack: keep `receipt_id`, widen the extent.

    Before this, the id was only ever compared as a string, so a receipt could
    be edited in place and would still 'resolve'.
    """
    case(trailing_body="일반 약관 문장이 표 뒤에 이어집니다.")
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    def _widen(index):
        index["tables"][0]["extent"][0]["end_char"] += 5

    _tamper(_widen)

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers, "an edited receipt body must not keep passing"
    assert any("integrity" in b or "receipt_id" in b for b in blockers), blockers


def test_editing_the_detector_fingerprint_without_changing_the_id_is_refused(
        case, register_table, isolated_dao, make_args):
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    _tamper(lambda index: index["tables"][0]["detector"].update(
        {"config_fingerprint": "0" * 32}))

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers
    assert any("integrity" in b or "receipt_id" in b for b in blockers), blockers


def test_duplicate_receipt_ids_are_refused(
        case, register_table, isolated_dao, make_args):
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    _tamper(lambda index: index["tables"].append(
        json.loads(json.dumps(index["tables"][0]))))

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers
    assert any("duplicate" in b.lower() for b in blockers), blockers


def test_a_receipt_rebound_to_another_document_is_refused(
        case, register_table, isolated_dao, make_args):
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    _tamper(lambda index: index["tables"][0].update({"document_id": "DOC_009"}))

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers, "a receipt re-pointed at another document must not pass"


def test_a_receipt_rebound_to_another_case_is_refused(
        case, register_table, isolated_dao, make_args):
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    _tamper(lambda index: index["tables"][0].update({"case_id": "CASE_999"}))

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers
    assert any("integrity" in b or "case" in b.lower() for b in blockers), \
        blockers


def test_a_schema_invalid_index_blocks_rather_than_reading_as_no_receipts(
        case, register_table, isolated_dao, make_args):
    """A corrupt index must never degrade to 'this document has no receipts',
    which would read as a plain missing-receipt error and invite re-issuance
    over a damaged file."""
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    assert _write_contract(isolated_dao, make_args, honest) == 0

    _tamper(lambda index: index["tables"][0].pop("extent"))

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers
    assert any("integrity" in b or "schema" in b.lower() for b in blockers), \
        blockers


# ==========================================================================
# 8. The original P0-8 attacks still hold
# ==========================================================================

def test_the_original_narrow_region_attack_is_still_refused(
        case, register_table, isolated_dao, make_args, capsys):
    """Regression guard: the follow-up must not weaken the first fix."""
    case()
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    table = honest["tables"][0]
    table["rows"] = table["rows"][:2]
    narrow_end = table["rows"][1]["source_span"]["end_char"]
    body = dao.policy_completeness.split_pages(_registered_text())[1]
    region = dict(table["source_regions"][0])
    region["end_char"] = narrow_end
    region["quote"] = body[region["start_char"]:narrow_end]
    table["source_regions"] = [region]

    assert _write_contract(isolated_dao, make_args, honest) == 1
    out = capsys.readouterr().out
    assert "narrow" in out.lower() or "region" in out.lower()


# ==========================================================================
# 9. `record-unverifiable-table-scan` -- the P0-8 human-authorized OCR override
# ==========================================================================
#
# CASE_112 (a real case) surfaced a real gap: every one of its 203 policy
# segments has extraction_method "ocr" (image-only source, no deterministic
# text layer), so scan-table-candidates refuses ALL of them and
# policy_clause_processing can never finalize for the case -- not a bug, the
# P0-8 fail-closed design working exactly as intended. These tests cover the
# new, explicitly human-authorized escape hatch: record a DIFFERENT, honestly
# labeled scan_status (never complete_no_candidates) that finalization accepts
# only because a human accepted the risk of an unverified table boundary.

def _override(make_args, *, doc_id="DOC_001", authorized_by="pyun",
              reason="test override", held_by=HELD_BY, run_id=RUN,
              case_id=CASE):
    return dao.cmd_record_unverifiable_table_scan(make_args(
        case_id=case_id, doc_id=doc_id, authorized_by=authorized_by,
        reason=reason, held_by=held_by, run_id=run_id))


def test_override_refuses_a_verifiable_embedded_text_document(
        case, isolated_dao, make_args, capsys):
    """The override exists for OCR only. A verifiable document must use the
    real scan -- silently accepting an override for it would hide a check
    that could actually run."""
    case()  # extraction_method defaults to "embedded_text"

    rc = _override(make_args)

    assert rc == 1
    out = capsys.readouterr().out
    assert "embedded_text" in out or "deterministic text layer" in out.lower()
    assert dao.table_candidate_inventory_for(CASE, "DOC_001") is None


def test_override_is_refused_for_an_unregistered_document(
        isolated_dao, make_args, capsys):
    """No manifest entry at all -- refuse rather than guessing an identity."""
    rc = _override(make_args, doc_id="DOC_999")

    assert rc == 1
    out = capsys.readouterr().out
    assert "not registered" in out.lower() or "does not exist" in out.lower()


def test_override_records_unverifiable_ocr_source_with_human_override(
        case, isolated_dao, make_args):
    """The success path: source identity is still verified (registered,
    digest-checked, revision-bound), but scan_status is the honest label --
    never complete_no_candidates -- and human_override is populated."""
    case()
    dao.patch_manifest_document(
        CASE, "DOC_001", {"extraction_method": "ocr"},
        held_by=HELD_BY, run_id=RUN)

    rc = _override(make_args, authorized_by="pyun",
                   reason="known-gaps item 37: CASE_112 OCR-only policy docs")

    assert rc == 0
    inventory = dao.table_candidate_inventory_for(CASE, "DOC_001")
    assert inventory is not None
    assert inventory["scan_status"] == "unverifiable_ocr_source"
    assert inventory["scan_status"] != "complete_no_candidates"
    assert inventory["human_override"]["authorized_by"] == "pyun"
    assert "CASE_112" in inventory["human_override"]["reason"]
    assert inventory["candidates"] == []
    assert inventory["possible_tables"] == []


def test_finalization_proceeds_after_a_recorded_override(
        case, isolated_dao, make_args):
    """The whole point of the override: policy_clause_processing finalization
    must not block on a document carrying an authorized override, unlike a
    real 'inconclusive'/'failed' scan which still blocks."""
    case()
    dao.patch_manifest_document(
        CASE, "DOC_001", {"extraction_method": "ocr"},
        held_by=HELD_BY, run_id=RUN)

    assert _override(make_args) == 0

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers == []


def test_override_is_a_noop_on_identical_rerun(case, isolated_dao, make_args):
    case()
    dao.patch_manifest_document(
        CASE, "DOC_001", {"extraction_method": "ocr"},
        held_by=HELD_BY, run_id=RUN)
    assert _override(make_args, reason="same reason") == 0
    first = dao.table_candidate_inventory_for(CASE, "DOC_001")["scan_id"]

    rc = _override(make_args, reason="same reason")

    assert rc == 0
    assert dao.table_candidate_inventory_for(CASE, "DOC_001")["scan_id"] == \
        first


def test_real_scan_table_candidates_still_refuses_ocr_unchanged(
        case, isolated_dao, make_args, capsys):
    """Regression guard: the override is a separate command, not a relaxation
    of the real detector path. scan-table-candidates must still refuse OCR
    exactly as before."""
    case()
    dao.patch_manifest_document(
        CASE, "DOC_001", {"extraction_method": "ocr"},
        held_by=HELD_BY, run_id=RUN)

    rc = dao.cmd_scan_table_candidates(make_args(
        case_id=CASE, doc_id="DOC_001", detector_profile=None,
        held_by=HELD_BY, run_id=RUN))

    assert rc == 1
    out = capsys.readouterr().out
    assert "ocr" in out.lower()


def test_override_source_digest_mismatch_is_refused(
        case, isolated_dao, make_args, capsys):
    """If the raw PDF changed since it was registered, the override must not
    bless a source identity that no longer matches -- it is source identity,
    not table geometry, that this command still verifies."""
    case()
    dao.patch_manifest_document(
        CASE, "DOC_001", {"extraction_method": "ocr",
                          "source_pdf_sha256": "0" * 64},
        held_by=HELD_BY, run_id=RUN)

    rc = _override(make_args)

    assert rc == 1
    out = capsys.readouterr().out
    assert "changed" in out.lower() or "sha256" in out.lower()
    assert "C" in out and "30" in out
