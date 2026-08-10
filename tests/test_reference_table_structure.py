"""Part 11E: reference-table row structure, column assignment, row coverage.

Cell-level grounding proves a VALUE exists on the page; it never proves the ROW
does. With source rows 'A 10' and 'B 20', an extraction of 'A 20' / 'B 10' has
every individual value present, so every pre-11E check passed. These tests cover
the row/table source structure that closes that: row.source_span,
table.source_regions, and table.header_spans.
"""
from _cross_contract import (
    check_reference_table,
    split_pages,
    unresolved_reference_table_reviews,
)


TWO_ROW_TEXT = (
    "<<<PAGE page=1>>>\n"
    "장해분류 지급률\n"
    "A 10\n"
    "B 20\n"
)

MULTIPAGE_TEXT = (
    "<<<PAGE page=1>>>\n"
    "장해분류 지급률\n"
    "A 10\n"
    "<<<PAGE page=2>>>\n"
    "장해분류 지급률\n"
    "B 20\n"
)


def _span(page, needle, text):
    """An exact source span, offsets computed from the real page body so a
    fixture can never drift from the text it describes."""
    body = split_pages(text)[page]
    start = body.index(needle)
    return {
        "page": page,
        "start_char": start,
        "end_char": start + len(needle),
        "quote": needle,
    }


def _cell(uid, key, value, quote, page=1):
    return {
        "cell_uid": uid,
        "column_key": key,
        "value": value,
        "evidence_references": [
            {"document_id": "DOC_001", "page": page, "quote": quote},
        ],
        "review_required": False,
    }


def _contract(rate_first="10", rate_second="20", text=TWO_ROW_TEXT):
    """A two-row table. rate_first/rate_second let a test transpose the values
    between rows while leaving both present on the page."""
    return {
        "case_id": "CASE_030",
        "run_id": "RUN_20260724_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_001",
        "tables": [{
            "table_uid": "RT-1111111111111111",
            "table_id": "T-1",
            "title": "장해분류 지급률",
            "columns": [
                {"column_key": "code", "label": "장해분류"},
                {"column_key": "rate", "label": "지급률"},
            ],
            "source_regions": [
                _span(1, "장해분류 지급률\nA 10\nB 20", text)],
            "header_spans": [{
                "span": _span(1, "장해분류 지급률", text),
                "kind": "column_header",
            }],
            "rows": [
                {
                    "row_uid": "RR-1111111111111111",
                    "source_span": _span(1, "A 10", text),
                    "cells": [
                        _cell("RC-1111111111111111", "code", "A", "A 10"),
                        _cell("RC-2222222222222222", "rate", rate_first,
                              "A 10" if rate_first == "10" else "B 20"),
                    ],
                },
                {
                    "row_uid": "RR-2222222222222222",
                    "source_span": _span(1, "B 20", text),
                    "cells": [
                        _cell("RC-3333333333333333", "code", "B", "B 20"),
                        _cell("RC-4444444444444444", "rate", rate_second,
                              "B 20" if rate_second == "20" else "A 10"),
                    ],
                },
            ],
            "evidence_references": [
                {"document_id": "DOC_001", "page": 1,
                 "quote": "장해분류 지급률"},
            ],
            "review_required": False,
        }],
    }


def _errors(contract, text=TWO_ROW_TEXT):
    return check_reference_table(
        contract, "reference_table_DOC_001.json", text)


def test_correct_two_row_table_passes():
    assert _errors(_contract()) == []


def test_swapped_row_values_are_rejected():
    """The headline 11E case: source 'A 10 / B 20' extracted as 'A 20 / B 10'.
    Both values exist on the page, so only row-span binding can catch it."""
    errors = _errors(_contract(rate_first="20", rate_second="10"))
    assert any("is not present in this row" in e for e in errors), errors


def test_omitted_source_row_is_detected():
    """Dropping row B leaves its source text uncovered inside the region."""
    contract = _contract()
    contract["tables"][0]["rows"].pop()
    errors = _errors(contract)
    assert any("appears to be missing from the extraction" in e
               for e in errors), errors


def test_wrong_column_assignment_is_rejected():
    """Values present in the row, but bound to the wrong columns."""
    contract = _contract()
    cells = contract["tables"][0]["rows"][0]["cells"]
    cells[0]["value"] = "10"   # the code column gets the rate
    cells[1]["value"] = "A"    # the rate column gets the code
    errors = _errors(contract)
    assert any("not in declared column order" in e for e in errors), errors


def test_row_span_outside_declared_region_is_rejected():
    contract = _contract()
    contract["tables"][0]["source_regions"] = [
        _span(1, "장해분류 지급률\nA 10", TWO_ROW_TEXT)]
    errors = _errors(contract)
    assert any("outside every declared source_region" in e
               for e in errors), errors


def test_row_span_quote_must_match_the_real_page_text():
    contract = _contract()
    contract["tables"][0]["rows"][0]["source_span"]["quote"] = "A 99"
    errors = _errors(contract)
    assert any("does not equal the exact page" in e for e in errors), errors


def _multipage_contract(declare_repeated_header=True):
    contract = _contract(text=TWO_ROW_TEXT)
    table = contract["tables"][0]
    table["source_regions"] = [
        _span(1, "장해분류 지급률\nA 10", MULTIPAGE_TEXT),
        _span(2, "장해분류 지급률\nB 20", MULTIPAGE_TEXT),
    ]
    table["header_spans"] = [{
        "span": _span(1, "장해분류 지급률", MULTIPAGE_TEXT),
        "kind": "column_header",
    }]
    if declare_repeated_header:
        table["header_spans"].append({
            "span": _span(2, "장해분류 지급률", MULTIPAGE_TEXT),
            "kind": "repeated_header",
        })
    table["rows"][0]["source_span"] = _span(1, "A 10", MULTIPAGE_TEXT)
    table["rows"][1]["source_span"] = _span(2, "B 20", MULTIPAGE_TEXT)
    for cell in table["rows"][1]["cells"]:
        cell["evidence_references"][0]["page"] = 2
    return contract


def test_multipage_table_with_repeated_header_passes():
    """A continuation page's repeated header is a header span, not a lost row."""
    errors = _errors(_multipage_contract(), MULTIPAGE_TEXT)
    assert errors == [], errors


def test_undeclared_repeated_header_reads_as_a_missing_row():
    """Fail closed: an unaccounted continuation-page line is reported rather
    than assumed to be a header."""
    errors = _errors(
        _multipage_contract(declare_repeated_header=False), MULTIPAGE_TEXT)
    assert any("appears to be missing from the extraction" in e
               for e in errors), errors


def test_layout_ambiguity_review_flag_blocks_finalization():
    """An ambiguous layout is declared for review, never silently resolved by
    rearranging values."""
    contract = _contract()
    contract["tables"][0]["review_required"] = True
    blockers = unresolved_reference_table_reviews(contract)
    assert any("review_required=true" in b for b in blockers), blockers
