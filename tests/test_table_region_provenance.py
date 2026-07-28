"""P0-8: a table's source_regions are DAO-derived, not self-declared.

Before this, `check_reference_table_structure` enforced downwards from
`table.source_regions`: rows inside regions, cells inside rows, and every
non-whitespace character of the region covered by a row or a header span. Every
one of those checks reads the region the EXTRACTING AGENT submitted. So the
attack is not to break a check, it is to declare a smaller table:

    source:    장해분류 지급률 / A 10 / B 20 / C 30
    submitted: source_regions = header..B only, rows = A, B

`C 30` sits outside every declared region, so reverse coverage never looks at
it. The extraction is complete *with respect to what it claimed the table was*,
which is the only question the pre-P0-8 code asked.

Section 1 runs exactly that attack against the real DAO write path. It is the
test that matters most in this file, because it is the one the pre-P0-8 code
passed.

Every test here runs against a REAL PDF with real ruled table lines, detected
by pymupdf's own `find_tables()`, and calls the real DAO commands. Stubbing the
detector would test the bookkeeping while skipping the only thing P0-8 adds:
that the DAO establishes the table's extent by opening the registered file
itself.

Fixture note: a few tests seed a corrupt or legacy `_table_region_index.json`
by direct file write. That is deliberate and deliberately NOT a production
path -- the index is sealed against every caller-facing DAO write, so these
states cannot be reached by any command. Writing one directly is how a test can
ask "if this state existed anyway, is it caught?"
"""
import json
from pathlib import Path

import pytest

import dao
import dao_transaction
import table_region_provenance as trp


CASE = "CASE_030"
RUN = "RUN_20260728_001"
HELD_BY = "policy-pipeline"


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


# ==========================================================================
# PDF fixtures -- real ruled tables, detected by pymupdf itself
# ==========================================================================

def _draw_table(page, *, top, rows, x0=60, xmid=190, x1=300, row_height=25):
    """Draw a ruled table and return its printed row texts.

    Ruling lines matter: `find_tables()` in its default "lines" strategy needs
    real vector separators, which is also what makes the detector honest here.
    A table drawn with whitespace alone is the ambiguous case, and P0-8 refuses
    it rather than guessing (see the whitespace-only test).
    """
    # A cell given as a tuple/list of strings is drawn as several lines inside
    # one cell -- the multi-line cell case, where a limiting clause on the
    # second line is exactly the content that must not be droppable.
    heights = [
        row_height * max(len(_lines(left)), len(_lines(right)))
        for left, right in rows
    ]
    ys = [top]
    for height in heights:
        ys.append(ys[-1] + height)
    for y in ys:
        page.draw_line((x0, y), (x1, y))
    for x in (x0, xmid, x1):
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for index, (left, right) in enumerate(rows):
        for column, value in ((x0 + 12, left), (xmid + 10, right)):
            for line_index, line in enumerate(_lines(value)):
                page.insert_text((column, ys[index] + 18 + line_index * 18),
                                 line, fontsize=10)
    return ys[-1]


def _lines(value):
    return list(value) if isinstance(value, (list, tuple)) else [value]


@pytest.fixture
def table_pdf():
    """A one-page policy PDF carrying one ruled 3-data-row table."""
    import fitz

    def _page(doc, spec, default_title):
        page = doc.new_page()
        top = 100
        if spec.get("title") is not None:
            page.insert_text((72, 72), spec.get("title", default_title),
                             fontsize=12)
        # Prose drawn ABOVE the table, deliberately allowed to repeat the
        # table's own strings -- the wrong-occurrence attack.
        for offset, line in enumerate(spec.get("leading_prose") or []):
            page.insert_text((72, 78 + offset * 18), line, fontsize=10)
            top = 78 + offset * 18 + 40
        bottom = _draw_table(page, top=top, rows=list(spec["rows"]))
        if spec.get("second_table_rows"):
            bottom = _draw_table(page, top=bottom + 40,
                                 rows=list(spec["second_table_rows"]))
        if spec.get("trailing_body"):
            page.insert_text((72, bottom + 30), spec["trailing_body"],
                             fontsize=10)
        return page

    def _build(path, *, rows=(("Grade", "Rate"), ("A", "10"), ("B", "20"),
                              ("C", "30")),
               title="Disability Table", trailing_body=None,
               second_table_rows=None, leading_prose=None, pages=None):
        doc = fitz.open()
        specs = pages if pages is not None else [{
            "rows": rows, "title": title, "trailing_body": trailing_body,
            "second_table_rows": second_table_rows,
            "leading_prose": leading_prose,
        }]
        for spec in specs:
            _page(doc, spec, title)
        doc.save(str(path))
        doc.close()
        return path

    return _build


# ==========================================================================
# Case construction -- real DAO registration throughout
# ==========================================================================

def _manifest_entry(doc_id="DOC_001", total_pages=1):
    return {
        "document_id": doc_id,
        "file_name": f"{doc_id}.pdf",
        "document_role": "physical",
        "file_path": f"data/raw/{CASE}/{doc_id}.pdf",
        "file_format": "pdf",
        "file_size_bytes": 1000,
        "ocr_status": "completed",
        "extraction_method": "embedded_text",
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
        "source_total_pages": total_pages,
    }


def _seed_manifest(isolated_dao, documents):
    out = isolated_dao / "outputs" / CASE
    out.mkdir(parents=True, exist_ok=True)
    (out / "document_manifest.json").write_text(
        json.dumps({"case_id": CASE, "documents": documents},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def _page_text(pdf_path, physical_page=1):
    """The exact text pymupdf reads off the page.

    The registered revision is built from this, so the processed text and the
    PDF layout describe the same bytes -- which is the ordinary case P0-8 must
    support, not a convenience.
    """
    import fitz
    doc = fitz.open(pdf_path)
    try:
        return doc[physical_page - 1].get_text()
    finally:
        doc.close()


def _revision_text(pdf_path, page_count=1):
    return "".join(
        f"<<<PAGE page={n}>>>\n{_page_text(pdf_path, n)}"
        for n in range(1, page_count + 1))


@pytest.fixture
def case(isolated_dao, table_pdf, make_args, canonicalize):
    """A canonical_v1 policy document whose registered text is the real PDF's
    own extracted text."""
    def _build(**pdf_kwargs):
        raw = isolated_dao / "data" / "raw" / CASE
        raw.mkdir(parents=True, exist_ok=True)
        pdf = table_pdf(raw / "DOC_001.pdf", **pdf_kwargs)
        page_count = pdf_kwargs.get("pages")
        page_count = len(page_count) if page_count else 1
        _seed_manifest(isolated_dao, [_manifest_entry(total_pages=page_count)])
        canonicalize(make_args, isolated_dao, CASE, "DOC_001",
                     _revision_text(pdf, page_count), held_by=HELD_BY,
                     run_id=RUN)
        return pdf
    return _build


@pytest.fixture
def register_table(make_args):
    """Run the real `register-table-region` command."""
    def _register(*, doc_id="DOC_001", page=1, anchor="Grade",
                  anchor_page=None, detector_profile=None, held_by=HELD_BY,
                  run_id=RUN, case_id=CASE):
        return dao.cmd_register_table_region(make_args(
            case_id=case_id, doc_id=doc_id, page=page,
            anchor=anchor, anchor_page=anchor_page,
            detector_profile=detector_profile,
            held_by=held_by, run_id=run_id))
    return _register


def _receipt(doc_id="DOC_001", index=0):
    receipts = dao.table_region_receipts_for(CASE, doc_id)
    return receipts[index] if len(receipts) > index else None


def _span_from(text, page, needle, occurrence=0):
    body = dao.policy_completeness.split_pages(text)[page]
    start = -1
    for _ in range(occurrence + 1):
        start = body.index(needle, start + 1)
    return {"page": page, "start_char": start,
            "end_char": start + len(needle), "quote": needle}


def _registered_text(doc_id="DOC_001"):
    return dao._registered_revision_text(CASE, doc_id)


# --------------------------------------------------------------------------
# Contract construction. Built from the RECEIPT so an honest contract is easy
# to express and an attack is a deliberate, visible edit of one field.
# --------------------------------------------------------------------------

def _contract_from_receipt(receipt, *, rows=None, regions=None,
                           header_spans=None, table_region_receipt_id=True,
                           review_required=False, columns=None):
    """Build a reference_table contract matching a receipt.

    UIDs are placeholders here: these tests exercise the P0-8 region gate,
    which runs before UID recomputation and reports independently of it. Tests
    that need genuinely recomputed UIDs assert on the P0-8 error text, not on
    UID success.
    """
    data_rows = [region for region in receipt["regions"]
                 if region["kind"] == "data_row"]
    non_data = [region for region in receipt["regions"]
                if region["kind"] != "data_row"]

    def _row(index, region):
        span = {"page": region["page"], "start_char": region["start_char"],
                "end_char": region["end_char"], "quote": region["quote"]}
        cells = []
        for cell_index, cell in enumerate(region["cells"]):
            cells.append({
                "cell_uid": f"RC-{index}{cell_index}" + "0" * 14,
                "column_key": ["code", "rate"][cell_index],
                "value": cell["text"],
                "source_spans": [{
                    "page": cell["page"], "start_char": cell["start_char"],
                    "end_char": cell["end_char"], "quote": cell["quote"],
                }],
                "evidence_references": [{
                    "document_id": receipt["document_id"],
                    "page": cell["page"], "quote": cell["quote"],
                }],
                "review_required": False,
            })
        return {"row_uid": f"RR-{index}" + "0" * 15, "source_span": span,
                "cells": cells}

    table = {
        "table_uid": "RT-" + "1" * 16,
        "table_id": "T-1",
        "title": "Disability Table",
        "columns": columns or [
            {"column_key": "code", "label": "Grade"},
            {"column_key": "rate", "label": "Rate"},
        ],
        "rows": rows if rows is not None else [
            _row(index, region) for index, region in enumerate(data_rows)],
        "source_regions": regions if regions is not None else [
            {"page": extent["page"], "start_char": extent["start_char"],
             "end_char": extent["end_char"], "quote": extent["quote"]}
            for extent in receipt["extent"]],
        "header_spans": header_spans if header_spans is not None else [
            {"span": {"page": region["page"],
                      "start_char": region["start_char"],
                      "end_char": region["end_char"],
                      "quote": region["quote"]},
             "kind": region["kind"]}
            for region in non_data],
        # The table's own evidence must quote its title (Part 11's cell/title
        # grounding), independently of P0-8.
        "evidence_references": [{
            "document_id": receipt["document_id"], "page": 1,
            "quote": "Disability Table",
        }],
        "review_required": review_required,
    }
    if table_region_receipt_id:
        table["table_region_receipt_id"] = (
            receipt["receipt_id"] if table_region_receipt_id is True
            else table_region_receipt_id)
    return {
        "case_id": CASE,
        "run_id": RUN,
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": receipt["document_id"],
        "source_text_revision": {"documents": [{
            "document_id": receipt["document_id"],
            "revision_sha256": dao.revision_entry_for(
                CASE, receipt["document_id"])["current_revision_sha256"],
        }]},
        "tables": [table],
    }


def _canonicalize_uids(data, doc_id="DOC_001"):
    """Fill in genuinely recomputed RT/RR/RC values.

    P0-8 and the UID layer are independent gates, and a test asserting on P0-8
    should not be passing or failing on UID bookkeeping. Recomputing them the
    way the DAO does lets an HONEST contract actually persist, which is what
    makes the finalization and staleness tests meaningful -- a contract that
    never landed cannot demonstrate that finalization re-checks it.
    """
    import policy_uid_resolver as resolver
    context, errors = dao._uid_source_context(CASE, doc_id)
    assert not errors, errors
    for table in data.get("tables") or []:
        region_records = resolver.resolve_spans(
            context, table["source_regions"], "regions")
        table["table_uid"] = resolver.compute_derived_uid(
            "table", context, region_records)
        for row in table.get("rows") or []:
            row_records = resolver.resolve_spans(
                context, row.get("source_spans") or [row["source_span"]],
                "row")
            row["row_uid"] = resolver.compute_derived_uid(
                "row", context, row_records, parent_uid=table["table_uid"])
            for cell in row.get("cells") or []:
                if not cell.get("source_spans"):
                    continue
                cell_records = resolver.resolve_spans(
                    context, cell["source_spans"], "cell")
                cell["cell_uid"] = resolver.compute_derived_uid(
                    "cell", context, cell_records,
                    parent_uid=row["row_uid"],
                    column_key=cell.get("column_key"))
    return data


def _write_contract(isolated_dao, make_args, data, doc_id="DOC_001"):
    """Push a contract through the REAL write path.

    Every P0-8 assertion goes through this rather than calling a checker
    directly, because "the checker returns an error" and "the DAO refuses the
    write" are different claims and only the second one is the gate.
    """
    path = isolated_dao / "_contract.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return dao.cmd_write_contract(make_args(
        case_id=CASE, filename=f"reference_table_{doc_id}.json",
        data_file=str(path), schema_name="reference_table.schema.json",
        held_by=HELD_BY, run_id=RUN))


# ==========================================================================
# 1. THE ATTACK -- a table declared narrower than it is
# ==========================================================================

def test_narrow_region_attack_is_refused_by_the_real_write_path(
        case, register_table, isolated_dao, make_args, capsys):
    """Source A/B/C; submit source_regions and rows covering only A/B.

    Pre-P0-8 this passed every gate: each declared row sat inside the declared
    region, each cell inside its row, and every non-whitespace character of the
    declared region was covered. `C 30` was never examined, because nothing
    established that the region was the whole table.
    """
    case()
    assert register_table() == 0
    receipt = _receipt()

    text = _registered_text()
    # The honest contract, then narrowed: drop the C row and shrink the extent
    # so it stops just after B -- exactly the attack shape.
    honest = _contract_from_receipt(receipt)
    table = honest["tables"][0]
    table["rows"] = table["rows"][:2]
    narrow_end = table["rows"][1]["source_span"]["end_char"]
    region = dict(table["source_regions"][0])
    body = dao.policy_completeness.split_pages(text)[1]
    region["end_char"] = narrow_end
    region["quote"] = body[region["start_char"]:narrow_end]
    table["source_regions"] = [region]

    rc = _write_contract(isolated_dao, make_args, honest)

    assert rc == 1, "the narrow-region attack must be refused by the DAO"
    out = capsys.readouterr().out
    assert "table_region_v1" in out
    # The refusal must name the concealed row, not merely say "mismatch".
    assert "C" in out and "30" in out
    assert not (isolated_dao / "outputs" / CASE /
                "reference_table_DOC_001.json").exists(), \
        "a refused write must persist nothing"


def test_the_same_narrow_contract_passed_the_pre_p08_checks(
        case, register_table):
    """Reproduce the bypass against the pre-P0-8 checker, so the fix is
    demonstrated to close a real hole rather than to add a redundant one.

    `check_reference_table_structure` is the Part 11E reverse-coverage check,
    unchanged by P0-8. Fed the narrowed contract it still reports NOTHING --
    which is the whole point: coverage within a declared region cannot see a
    row the region excludes.
    """
    import _cross_contract
    case()
    assert register_table() == 0
    receipt = _receipt()
    text = _registered_text()
    pages = dao.policy_completeness.split_pages(text)

    honest = _contract_from_receipt(receipt)
    table = honest["tables"][0]
    table["rows"] = table["rows"][:2]
    narrow_end = table["rows"][1]["source_span"]["end_char"]
    region = dict(table["source_regions"][0])
    region["end_char"] = narrow_end
    region["quote"] = pages[1][region["start_char"]:narrow_end]
    table["source_regions"] = [region]

    structural = _cross_contract.check_reference_table_structure(honest, pages)

    assert structural == [], (
        "pre-P0-8 reverse coverage is satisfied by the narrowed contract -- "
        "if this ever fails, the attack premise changed")
    # And the same contract is refused once the receipt is consulted.
    assert trp.contract_binding_errors(
        honest, [receipt], pages, "tables[0]") != []


def test_missing_last_row_is_named_as_a_missing_data_row(
        case, register_table, isolated_dao, make_args, capsys):
    """Full extent declared, but the C row simply omitted (attack B)."""
    case()
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    honest["tables"][0]["rows"] = honest["tables"][0]["rows"][:2]

    rc = _write_contract(isolated_dao, make_args, honest)

    assert rc == 1
    out = capsys.readouterr().out
    assert "no extracted row" in out or "missing" in out.lower()
    assert "30" in out


def test_missing_middle_row_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    """A/C extracted, B dropped (attack C). The region is honest here, so
    Part 11E's reverse coverage also fires -- both gates should hold."""
    case()
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    rows = honest["tables"][0]["rows"]
    honest["tables"][0]["rows"] = [rows[0], rows[2]]

    rc = _write_contract(isolated_dao, make_args, honest)

    assert rc == 1
    out = capsys.readouterr().out
    assert "20" in out


# ==========================================================================
# 2. Row/header classification is the receipt's, not the contract's
# ==========================================================================

def test_data_row_relabelled_as_a_note_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    """Attack E: move the B row out of `rows` and declare its span a `note`
    header span. Reverse coverage is satisfied -- the characters ARE covered --
    but the receipt classified that band as a data_row."""
    case()
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    table = honest["tables"][0]
    victim = table["rows"][1]
    table["rows"] = [table["rows"][0], table["rows"][2]]
    table["header_spans"].append(
        {"span": victim["source_span"], "kind": "note"})

    rc = _write_contract(isolated_dao, make_args, honest)

    assert rc == 1
    out = capsys.readouterr().out
    assert "data_row" in out
    assert "note" in out


def test_data_row_relabelled_as_separator_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    case()
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    table = honest["tables"][0]
    victim = table["rows"][2]
    table["rows"] = table["rows"][:2]
    table["header_spans"].append(
        {"span": victim["source_span"], "kind": "separator"})

    assert _write_contract(isolated_dao, make_args, honest) == 1
    assert "data_row" in capsys.readouterr().out


def test_header_may_not_be_extracted_as_a_data_row(
        case, register_table, isolated_dao, make_args, capsys):
    """The reverse direction: promoting the column header into `rows` would
    inflate the table with a row the source does not have."""
    case()
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    table = honest["tables"][0]
    header_span = table["header_spans"][0]["span"]
    table["header_spans"] = []
    table["rows"].insert(0, {
        "row_uid": "RR-" + "9" * 16,
        "source_span": header_span,
        "cells": [{
            "cell_uid": "RC-" + "9" * 16,
            "column_key": "code",
            "value": header_span["quote"].strip().split("\n")[0],
            "evidence_references": [{
                "document_id": "DOC_001", "page": 1,
                "quote": header_span["quote"].strip().split("\n")[0]}],
            "review_required": False,
        }],
    })

    assert _write_contract(isolated_dao, make_args, honest) == 1
    out = capsys.readouterr().out
    assert "column_header" in out or "not a data_row" in out


def test_duplicate_extraction_of_one_data_row_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    """Receipt data_rows map to extracted rows as a bijection: one row claimed
    twice is as wrong as one row missing."""
    case()
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    table = honest["tables"][0]
    table["rows"].append(json.loads(json.dumps(table["rows"][1])))

    assert _write_contract(isolated_dao, make_args, honest) == 1
    out = capsys.readouterr().out
    assert "more than once" in out or "duplicate" in out.lower()


def test_an_extra_row_outside_the_receipt_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    """Attack: invent a row from body text the detector never classified."""
    case(trailing_body="Ordinary policy sentence follows the table.")
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    text = _registered_text()
    invented = _span_from(text, 1, "Ordinary policy sentence")
    honest["tables"][0]["rows"].append({
        "row_uid": "RR-" + "8" * 16,
        "source_span": invented,
        "cells": [{
            "cell_uid": "RC-" + "8" * 16, "column_key": "code",
            "value": "Ordinary", "review_required": False,
            "evidence_references": [
                {"document_id": "DOC_001", "page": 1, "quote": "Ordinary"}],
        }],
    })

    assert _write_contract(isolated_dao, make_args, honest) == 1
    out = capsys.readouterr().out
    assert "no data_row" in out or "not a data_row" in out


# ==========================================================================
# 3. Over-wide regions
# ==========================================================================

def test_over_wide_region_swallowing_body_text_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    """Attack G: extend source_regions past the table into ordinary policy
    prose. The receipt's extent is the table's, so a wider claim disagrees."""
    case(trailing_body="Ordinary policy sentence follows the table.")
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    text = _registered_text()
    body = dao.policy_completeness.split_pages(text)[1]
    region = dict(honest["tables"][0]["source_regions"][0])
    tail = body.index("Ordinary policy sentence") + len("Ordinary policy sentence")
    region["end_char"] = tail
    region["quote"] = body[region["start_char"]:tail]
    honest["tables"][0]["source_regions"] = [region]

    assert _write_contract(isolated_dao, make_args, honest) == 1
    out = capsys.readouterr().out
    assert "extent" in out.lower()


def test_body_text_cannot_be_excused_as_a_note(
        case, register_table, isolated_dao, make_args, capsys):
    """The obvious follow-up to the over-wide attack: cover the swallowed prose
    with a `note` header span so reverse coverage is satisfied. The receipt
    never classified that band at all, so it is still refused."""
    case(trailing_body="Ordinary policy sentence follows the table.")
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt())
    text = _registered_text()
    body = dao.policy_completeness.split_pages(text)[1]
    region = dict(honest["tables"][0]["source_regions"][0])
    start = body.index("Ordinary policy sentence")
    tail = start + len("Ordinary policy sentence")
    region["end_char"] = tail
    region["quote"] = body[region["start_char"]:tail]
    honest["tables"][0]["source_regions"] = [region]
    honest["tables"][0]["header_spans"].append({
        "span": {"page": 1, "start_char": start, "end_char": tail,
                 "quote": body[start:tail]},
        "kind": "note",
    })

    assert _write_contract(isolated_dao, make_args, honest) == 1
    assert "extent" in capsys.readouterr().out.lower()


# ==========================================================================
# 4. Multi-page tables and continuation pages
# ==========================================================================

@pytest.fixture
def two_page_case(case):
    def _build():
        return case(pages=[
            {"rows": [("Grade", "Rate"), ("A", "10"), ("B", "20")],
             "title": "Disability Table"},
            # Same title on the continuation page, as a real Korean policy
            # appendix repeats it -- which is also what makes page 2's header
            # band a `repeated_header` rather than a fresh table.
            {"rows": [("Grade", "Rate"), ("C", "30"), ("D", "40")],
             "title": "Disability Table"},
        ])
    return _build


def test_multi_page_table_registers_both_pages(
        two_page_case, make_args):
    """A continuation page is part of the same table extent, established by
    registering the anchor on each page it occupies."""
    two_page_case()
    assert dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        anchor_page=None, detector_profile=None,
        held_by=HELD_BY, run_id=RUN)) == 0

    receipt = _receipt()
    assert {extent["page"] for extent in receipt["extent"]} == {1, 2}
    data_rows = [r for r in receipt["regions"] if r["kind"] == "data_row"]
    assert {r["page"] for r in data_rows} == {1, 2}
    assert len(data_rows) == 4
    # The repeated header on page 2 is classified, not treated as a data row.
    repeated = [r for r in receipt["regions"]
                if r["kind"] == "repeated_header"]
    assert len(repeated) == 1 and repeated[0]["page"] == 2


def test_dropping_a_continuation_page_is_refused(
        two_page_case, register_table, isolated_dao, make_args, capsys):
    """Attack D: declare only page 1's region and drop page 2's rows.

    Within page 1 the extraction is complete, which is exactly why pre-P0-8
    coverage could not see the loss.
    """
    two_page_case()
    assert dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        anchor_page=None, detector_profile=None,
        held_by=HELD_BY, run_id=RUN)) == 0
    receipt = _receipt()

    honest = _contract_from_receipt(receipt)
    table = honest["tables"][0]
    table["rows"] = [row for row in table["rows"]
                     if row["source_span"]["page"] == 1]
    table["source_regions"] = [region for region in table["source_regions"]
                               if region["page"] == 1]
    table["header_spans"] = [span for span in table["header_spans"]
                             if span["span"]["page"] == 1]

    assert _write_contract(isolated_dao, make_args, honest) == 1
    out = capsys.readouterr().out
    assert "page 2" in out or "extent" in out.lower()


def test_full_multi_page_table_passes_the_region_gate(
        two_page_case, register_table, isolated_dao, make_args):
    """Attack K's honest counterpart: all four rows, both regions, the
    repeated header declared. The P0-8 gate must report nothing."""
    two_page_case()
    assert dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        anchor_page=None, detector_profile=None,
        held_by=HELD_BY, run_id=RUN)) == 0
    receipt = _receipt()
    honest = _contract_from_receipt(receipt)
    pages = dao.policy_completeness.split_pages(_registered_text())

    assert trp.contract_binding_errors(
        honest, [receipt], pages, "tables[0]") == []


# ==========================================================================
# 5. The honest single-page case
# ==========================================================================

def test_honest_contract_passes_the_region_gate(
        case, register_table, isolated_dao, make_args):
    """Attack J: a complete, correct extraction must not be blocked."""
    case()
    assert register_table() == 0
    receipt = _receipt()
    honest = _contract_from_receipt(receipt)
    pages = dao.policy_completeness.split_pages(_registered_text())

    assert trp.contract_binding_errors(
        honest, [receipt], pages, "tables[0]") == []


def test_honest_contract_is_actually_written_by_the_dao(
        case, register_table, isolated_dao, make_args):
    """Attack J end to end: the complete extraction must PASS the real write
    path, not merely fail to produce a P0-8 error.

    Without this, every refusal test above would be satisfied by a gate that
    refuses everything.
    """
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))

    assert _write_contract(isolated_dao, make_args, honest) == 0
    written = dao.read_contract_data(CASE, "reference_table_DOC_001.json")
    assert written["tables"][0]["table_region_receipt_id"] == \
        _receipt()["receipt_id"]
    assert len(written["tables"][0]["rows"]) == 3


def test_multi_page_honest_contract_is_written_by_the_dao(
        two_page_case, isolated_dao, make_args):
    """Attack K end to end, through the real write path."""
    two_page_case()
    assert dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page="1", anchor="Grade",
        anchor_page=None, detector_profile=None,
        held_by=HELD_BY, run_id=RUN)) == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))

    assert _write_contract(isolated_dao, make_args, honest) == 0
    written = dao.read_contract_data(CASE, "reference_table_DOC_001.json")
    assert len(written["tables"][0]["rows"]) == 4
    assert {region["page"]
            for region in written["tables"][0]["source_regions"]} == {1, 2}


def test_receipt_records_real_detector_provenance(case, register_table):
    case()
    assert register_table() == 0
    receipt = _receipt()

    assert receipt["scheme"] == trp.RECEIPT_SCHEME
    assert receipt["document_id"] == "DOC_001"
    assert receipt["source_pdf_sha256"] == \
        dao.registered_source_pdf_sha256(CASE, "DOC_001")
    assert receipt["source_text_revision_sha256"] == \
        dao.revision_entry_for(CASE, "DOC_001")["current_revision_sha256"]
    assert receipt["detector"]["library"] == "pymupdf"
    assert receipt["detector"]["profile"] == trp.DEFAULT_DETECTOR_PROFILE
    assert receipt["detector"]["library_version"]
    # Every data row carries geometry AND exact processed-text offsets.
    for region in receipt["regions"]:
        assert len(region["bbox"]) == 4
        assert region["page_text_sha256"]
        assert region["quote"]
    assert receipt["receipt_id"].startswith("TRR-")


def test_region_receipts_do_not_enter_any_uid(case, register_table,
                                               isolated_dao, make_args):
    """The receipt is a provenance selector, never identity.

    P0-8 must not move a single canonical_v1 UID: RT/RR/RC are computed from
    source spans, and a receipt that changed them would silently invalidate
    every previously-issued identifier. Asserted directly rather than left to
    the frozen vectors, since those do not exercise this path at all.
    """
    import policy_uid_resolver as resolver
    case()
    assert register_table() == 0
    honest = _canonicalize_uids(_contract_from_receipt(_receipt()))
    before = [t["table_uid"] for t in honest["tables"]]
    before_rows = [r["row_uid"] for r in honest["tables"][0]["rows"]]
    before_cells = [c["cell_uid"] for r in honest["tables"][0]["rows"]
                    for c in r["cells"]]

    # Same spans, no receipt reference at all -> identical UIDs.
    stripped = json.loads(json.dumps(honest))
    stripped["tables"][0].pop("table_region_receipt_id")
    context, errors = dao._uid_source_context(CASE, "DOC_001")
    assert not errors
    region_records = resolver.resolve_spans(
        context, stripped["tables"][0]["source_regions"], "regions")

    assert resolver.compute_derived_uid(
        "table", context, region_records) == before[0]
    # And the resolver reports no UID error either way -- the receipt is not
    # one of its inputs.
    assert resolver.check_reference_tables(context, stripped) == []
    assert [r["row_uid"] for r in stripped["tables"][0]["rows"]] == before_rows
    assert [c["cell_uid"] for r in stripped["tables"][0]["rows"]
            for c in r["cells"]] == before_cells


def test_receipt_id_is_not_derived_from_the_table_uid(case, register_table):
    """The receipt must not be issued against a caller's table_uid: RT is
    derived FROM source_regions, so trusting it to select a receipt would make
    the region's authority circular."""
    case()
    assert register_table() == 0
    receipt = _receipt()

    assert "table_uid" not in json.dumps(receipt)
    recomputed = trp.compute_receipt_id(receipt)
    assert recomputed == receipt["receipt_id"]


# ==========================================================================
# 6. Fabricated / mismatched receipt references (attack F)
# ==========================================================================

def test_contract_without_a_receipt_reference_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    case()
    assert register_table() == 0
    honest = _contract_from_receipt(_receipt(), table_region_receipt_id=False)

    assert _write_contract(isolated_dao, make_args, honest) == 1
    out = capsys.readouterr().out
    assert "table_region_receipt_id" in out


def test_fabricated_receipt_id_is_refused(
        case, register_table, isolated_dao, make_args, capsys):
    case()
    assert register_table() == 0
    honest = _contract_from_receipt(
        _receipt(), table_region_receipt_id="TRR-" + "a" * 64)

    assert _write_contract(isolated_dao, make_args, honest) == 1
    assert "does not resolve" in capsys.readouterr().out


def test_receipt_from_another_document_is_refused(
        case, register_table, isolated_dao, make_args, table_pdf,
        canonicalize, capsys):
    """A real, correctly-issued receipt -- for a different document.

    The reference is well-formed and resolves to a genuine receipt, so only the
    document binding can refuse it.
    """
    case()
    assert register_table() == 0
    own_receipt = _receipt()

    # A second, genuinely registered document with its own real receipt.
    other_pdf = table_pdf(
        isolated_dao / "data" / "raw" / CASE / "DOC_002.pdf",
        rows=(("Grade", "Rate"), ("X", "70")))
    manifest = dao.read_contract_data(CASE, "document_manifest.json")
    manifest["documents"].append(_manifest_entry("DOC_002"))
    (dao.case_dir(CASE) / "document_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    canonicalize(make_args, isolated_dao, CASE, "DOC_002",
                 _revision_text(other_pdf), held_by=HELD_BY, run_id=RUN)
    assert register_table(doc_id="DOC_002") == 0
    foreign = dao.table_region_receipts_for(CASE, "DOC_002")[0]
    assert foreign["receipt_id"] != own_receipt["receipt_id"]

    honest = _contract_from_receipt(
        own_receipt, table_region_receipt_id=foreign["receipt_id"])

    assert _write_contract(isolated_dao, make_args, honest) == 1
    out = capsys.readouterr().out
    assert "DOC_002" in out or "does not resolve" in out


def test_receipt_index_cannot_be_written_through_write_contract(
        case, register_table, isolated_dao, make_args, capsys):
    """The index is DAO-owned: a generic write must not be able to mint one."""
    case()
    forged = {
        "case_id": CASE,
        "tables": [{
            "receipt_id": "TRR-" + "b" * 64,
            "scheme": trp.RECEIPT_SCHEME,
            "document_id": "DOC_001",
        }],
    }
    path = isolated_dao / "_forged_index.json"
    path.write_text(json.dumps(forged), encoding="utf-8")

    rc = dao.cmd_write_contract(make_args(
        case_id=CASE, filename="_table_region_index.json",
        data_file=str(path), schema_name="table_region_index.schema.json",
        held_by="policy-pipeline", run_id=RUN))

    assert rc == 1, "write-contract must not be able to write the receipt index"
    assert "DAO-owned" in capsys.readouterr().out


# ==========================================================================
# 7. Fail-closed: ambiguous and unsupported layouts
# ==========================================================================

def test_two_identical_tables_on_one_page_refuse_a_receipt(
        case, register_table, capsys):
    """Attack H: the selector matches two candidates. Choosing the first would
    be a coin flip recorded as provenance."""
    case(second_table_rows=[("Grade", "Rate"), ("D", "40")])

    rc = register_table(anchor="Grade")

    assert rc == 1
    out = capsys.readouterr().out
    assert "more than one" in out or "ambiguous" in out.lower()
    assert _receipt() is None


def test_selector_matching_no_candidate_refuses(case, register_table, capsys):
    case()
    rc = register_table(anchor="NoSuchHeaderAnywhere")

    assert rc == 1
    assert _receipt() is None
    out = capsys.readouterr().out
    assert "no detected table" in out.lower() or "matches" in out.lower()


def test_page_without_a_detectable_table_refuses(
        register_table, isolated_dao, make_args, canonicalize, capsys):
    """Attack I's shape: no usable layout table. This must refuse, never be
    read as 'the page has no table, so nothing needs checking'."""
    import fitz
    raw = isolated_dao / "data" / "raw" / CASE
    raw.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page()
    # Whitespace-aligned columns, no ruling lines: visually a table, but not a
    # layout structure the detector can establish. Extent is exactly what is
    # unknowable here, so a receipt must not be issued.
    page.insert_text((72, 100), "Grade    Rate", fontsize=10)
    page.insert_text((72, 125), "A        10", fontsize=10)
    page.insert_text((72, 150), "B        20", fontsize=10)
    pdf = raw / "DOC_001.pdf"
    doc.save(str(pdf))
    doc.close()
    _seed_manifest(isolated_dao, [_manifest_entry()])
    canonicalize(make_args, isolated_dao, CASE, "DOC_001",
                 _revision_text(pdf), held_by=HELD_BY, run_id=RUN)

    rc = register_table(anchor="Grade")

    assert rc == 1, "an undetectable table must not yield a receipt"
    assert _receipt() is None
    out = capsys.readouterr().out
    assert "no detected table" in out.lower() or "review" in out.lower()


def test_ocr_document_cannot_obtain_a_receipt(
        case, register_table, isolated_dao, make_args, capsys):
    """Attack I: an image-only document has no deterministic text layer, and
    verification may not run OCR. It must fail closed."""
    case()
    dao.patch_manifest_document(
        CASE, "DOC_001", {"extraction_method": "ocr"},
        held_by=HELD_BY, run_id=RUN)

    rc = register_table()

    assert rc == 1
    out = capsys.readouterr().out
    assert "ocr" in out.lower()
    assert _receipt() is None


def test_layout_text_not_matching_processed_text_refuses(
        case, register_table, isolated_dao, make_args, canonicalize, capsys):
    """If the registered revision is not the PDF's own text, the layout rows
    cannot be mapped to exact offsets. Refuse rather than approximate."""
    case()
    replacement = (
        "<<<PAGE page=1>>>\n"
        "Completely different processed text that shares no table rows.\n")
    path = isolated_dao / "_replacement.md"
    path.write_text(replacement, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id=CASE, doc_id="DOC_001", text_file=str(path),
        held_by=HELD_BY, run_id=RUN)) == 0

    rc = register_table()

    assert rc == 1
    out = capsys.readouterr().out
    assert "could not be located" in out or "exact" in out.lower()
    assert _receipt() is None


# ==========================================================================
# 8. Staleness (attack L)
# ==========================================================================

def test_revision_change_makes_the_receipt_stale(
        case, register_table, isolated_dao, make_args, capsys):
    """Register honestly, then revise the source text through the DAO. The old
    receipt describes a state that no longer exists."""
    case()
    assert register_table() == 0
    receipt = _receipt()
    honest = _canonicalize_uids(_contract_from_receipt(receipt))
    pdf = isolated_dao / "data" / "raw" / CASE / "DOC_001.pdf"

    revised = _revision_text(pdf) + "Appended clause.\n"
    path = isolated_dao / "_revised.md"
    path.write_text(revised, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id=CASE, doc_id="DOC_001", text_file=str(path),
        held_by=HELD_BY, run_id=RUN)) == 0

    honest["source_text_revision"]["documents"][0]["revision_sha256"] = \
        dao.revision_entry_for(CASE, "DOC_001")["current_revision_sha256"]
    rc = _write_contract(isolated_dao, make_args, honest)

    assert rc == 1
    out = capsys.readouterr().out
    assert "stale" in out.lower() or "revised" in out.lower()


def test_stale_receipt_blocks_policy_finalization(
        case, register_table, isolated_dao, make_args, capsys):
    """A receipt that was current when the contract was written must not stay
    good forever: finalization re-checks it against the world as it is now."""
    case()
    assert register_table() == 0
    receipt = _receipt()
    honest = _canonicalize_uids(_contract_from_receipt(receipt))
    assert _write_contract(isolated_dao, make_args, honest) == 0, \
        "the honest contract must land, or staleness has nothing to invalidate"
    assert dao._table_region_finalize_blockers(CASE, "DOC_001") == []

    # Make the receipt genuinely stale through the real revision path: revise
    # the document's source text after the contract was written. Editing the
    # receipt's recorded digest in place would instead trip the P0-8-follow-up
    # INTEGRITY check (the body no longer hashes to its id), which is a
    # different -- and separately tested -- refusal.
    revised = _registered_text() + "추가 조항.\n"
    path = isolated_dao / "_stale_revision.md"
    path.write_text(revised, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id=CASE, doc_id="DOC_001", text_file=str(path),
        held_by=HELD_BY, run_id=RUN)) == 0

    blockers = dao._table_region_finalize_blockers(CASE, "DOC_001")

    assert blockers, "a stale receipt must block finalization"
    assert any("revised" in blocker for blocker in blockers), blockers


def test_reregistering_identical_bytes_is_idempotent(
        case, register_table, capsys):
    """Same PDF, same revision, same detector output -- a no-op that must not
    mint a second receipt or cascade downstream."""
    case()
    assert register_table() == 0
    first = _receipt()

    assert register_table() == 0
    out = capsys.readouterr().out

    assert "no-op" in out or "unchanged" in out
    assert len(dao.table_region_receipts_for(CASE, "DOC_001")) == 1
    assert _receipt()["receipt_id"] == first["receipt_id"]


# ==========================================================================
# 9. Transaction faults (attack M)
# ==========================================================================

def test_lock_contention_aborts_registration(
        case, register_table, isolated_dao, make_args, capsys):
    case()
    target = dao.table_region_index_path(CASE)
    target.parent.mkdir(parents=True, exist_ok=True)
    dao.acquire_lock(target, "other-agent", "RUN_OTHER", "holding")

    rc = register_table()

    assert rc == 1
    assert "LOCKED" in capsys.readouterr().out
    assert _receipt() is None


def test_pending_journal_blocks_registration(
        case, register_table, isolated_dao, capsys):
    case()
    dao_transaction.write_journal(dao.case_dir(CASE), {
        "operation": "register_table_region", "status": "invalidating"})

    rc = register_table()

    assert rc == 1
    assert "BLOCKED" in capsys.readouterr().out
    assert _receipt() is None


def test_schema_invalid_receipt_is_not_persisted(
        case, register_table, isolated_dao, make_args, monkeypatch, capsys):
    """Validation runs before persistence, so a receipt that would not validate
    leaves nothing behind."""
    case()
    monkeypatch.setattr(
        trp, "build_receipt",
        lambda **kwargs: {"scheme": "wrong", "receipt_id": "nope"})

    rc = register_table()

    assert rc == 1
    assert _receipt() is None
    assert not dao.table_region_index_path(CASE).exists()


def test_write_failure_restores_the_previous_index(
        case, register_table, isolated_dao, make_args, monkeypatch, capsys):
    """A caught persistence failure must roll the index back to its exact
    pre-transaction bytes, not leave a half-written one."""
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10")], "title": "T1"},
        {"rows": [("Grade", "Rate"), ("B", "20")], "title": "T2"},
    ])
    assert dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page=1, anchor="Grade",
        anchor_page=None, detector_profile=None,
        held_by=HELD_BY, run_id=RUN)) == 0
    before = dao.table_region_index_path(CASE).read_bytes()

    real_write = dao.atomic_write_json

    def _boom(path, obj):
        if path == dao.table_region_index_path(CASE):
            raise OSError("disk full")
        return real_write(path, obj)

    monkeypatch.setattr(dao, "atomic_write_json", _boom)
    rc = dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page=2, anchor="Grade",
        anchor_page=None, detector_profile=None,
        held_by=HELD_BY, run_id=RUN))

    assert rc == 1
    assert dao.table_region_index_path(CASE).read_bytes() == before
    assert "FAIL" in capsys.readouterr().out


def test_failed_rollback_leaves_a_blocking_pending_journal(
        case, isolated_dao, make_args, monkeypatch, capsys):
    """If the write fails AND the rollback fails, the journal must stay
    pending so nothing treats the partial state as committed."""
    case(pages=[
        {"rows": [("Grade", "Rate"), ("A", "10")], "title": "T1"},
        {"rows": [("Grade", "Rate"), ("B", "20")], "title": "T2"},
    ])
    assert dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page=1, anchor="Grade",
        anchor_page=None, detector_profile=None,
        held_by=HELD_BY, run_id=RUN)) == 0

    monkeypatch.setattr(dao, "atomic_write_json", _raise_disk_full)
    monkeypatch.setattr(dao, "_restore_file_preimage", _raise_restore_failed)

    rc = dao.cmd_register_table_region(make_args(
        case_id=CASE, doc_id="DOC_001", page=2, anchor="Grade",
        anchor_page=None, detector_profile=None,
        held_by=HELD_BY, run_id=RUN))

    assert rc == 1
    assert "ROLLBACK INCOMPLETE" in capsys.readouterr().out
    assert dao_transaction.pending_journal_errors(dao.case_dir(CASE)), \
        "the journal must remain pending and block further work"


def _raise_disk_full(path, obj):
    raise OSError("disk full")


def _raise_restore_failed(path, preimage):
    raise OSError("restore failed")


def test_a_refused_registration_writes_absolutely_nothing(
        case, register_table, isolated_dao):
    """Every refusal path above claims 'NOTHING was written'. This checks it
    as a byte comparison rather than taking the message's word for it."""
    case(second_table_rows=[("Grade", "Rate"), ("D", "40")])
    manifest_path = dao.case_dir(CASE) / "document_manifest.json"
    before_manifest = manifest_path.read_bytes()
    state_path = dao.run_state_path(CASE)
    before_state = state_path.read_bytes() if state_path.exists() else None

    assert register_table(anchor="Grade") == 1  # ambiguous -> refused

    assert not dao.table_region_index_path(CASE).exists()
    assert manifest_path.read_bytes() == before_manifest
    after_state = state_path.read_bytes() if state_path.exists() else None
    assert after_state == before_state


# ==========================================================================
# 10. Legacy readability
# ==========================================================================

def test_legacy_contract_without_receipt_field_still_validates(
        case, register_table, isolated_dao, make_args):
    """Schema-optional, DAO-required: a pre-P0-8 artifact must stay readable
    and migratable, while a NEW canonical_v1 write is refused without it."""
    from _validation import load_registry, validate_instance
    case()
    assert register_table() == 0
    legacy = _contract_from_receipt(_receipt(), table_region_receipt_id=False)
    schemas, registry = load_registry()

    errors = validate_instance(
        legacy, "reference_table.schema.json", schemas, registry)

    assert errors == [], (
        "the receipt reference must be schema-optional so legacy tables stay "
        "readable -- enforcement is the DAO's job, on canonical_v1 writes")
    # The same payload, schema-VALID, is refused by the DAO. "Legacy" must not
    # be usable as a write bypass.
    assert _write_contract(isolated_dao, make_args,
                           _canonicalize_uids(legacy)) == 1
