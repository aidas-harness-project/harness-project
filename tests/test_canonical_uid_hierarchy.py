"""P0-2: every canonical_v1 UID kind is recomputed from source provenance.

`test_canonical_uid_gate.py` covers the canonical_v1 SWITCH and the PS layer.
This covers the six kinds that switch left unverified -- PB, PC, CI, RT, RR, RC
-- which were enforced by their format regex alone. Reusing one fabricated
string consistently across every artifact that referenced it passed every gate,
because cross-contract agreement is a consistency check, not a provenance one.

What is deliberately NOT done here: build the expected UIDs by calling the same
production helper the implementation calls. Fixture and implementation sharing
`compute_uid()` means a wrong derivation agrees with itself and both pass. Every
kind therefore also has a FROZEN test vector in
`test_canonical_uid_vectors.py`, computed independently.
"""
import json

import pytest

import dao
import policy_audit
import policy_completeness
import policy_uid
import policy_uid_resolver
import _cross_contract


# Page 1 holds the clause text. The same condition phrase appears TWICE, and
# the table has two rows whose value cells hold identical text -- both are the
# repeated-content cases a quote-only identity cannot tell apart.
PAGE_1 = (
    "제3조(보험금의 지급) 회사는 보험금을 지급합니다.\n"
    "가. 사고일부터 180일 이내에 사망한 경우\n"
    "나. 사고일부터 180일 이내에 사망한 경우\n"
)
PAGE_2 = (
    "제4조(별표) 장해지급률표\n"
    "구분 지급률\n"
    "A 10\n"
    "B 10\n"
)
TEXT = f"<<<PAGE page=1>>>\n{PAGE_1}<<<PAGE page=2>>>\n{PAGE_2}"
RAW = b"%PDF-1.7 immutable policy source for the hierarchy tests"

FAKE = {
    "boundary": "PB-1111111111111111",
    "clause": "PC-1111111111111111",
    "condition": "CI-1111111111111111",
    "table": "RT-1111111111111111",
    "row": "RR-1111111111111111",
    "cell": "RC-1111111111111111",
}


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


@pytest.fixture(autouse=True)
def _no_extractors(monkeypatch):
    """Requirement 10: UID verification must never shell out.

    Every subprocess entry point is replaced with an exploding stub, so any
    OCR/PDF/CLI call made during a UID check fails the test that made it rather
    than quietly costing a real model call.
    """
    import subprocess

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError(
            "UID verification ran an external process -- it must read only the "
            f"registered source-text revision: {args!r}")

    for name in ("run", "check_output", "Popen", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, _boom)
    return _boom


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def canonical(isolated_dao, make_args):
    """A CASE_030/DOC_005 that is genuinely canonical_v1, with TEXT registered.

    Returns the source_pdf_sha256 the DAO derived by hashing the raw file
    itself -- the identity input a caller is never allowed to supply.
    """
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    _write_json(out / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [{
            "document_id": "DOC_005",
            "file_name": "DOC_005.pdf",
            "file_path": "data/raw/CASE_030/DOC_005.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "ocr_status": "completed",
            "document_type": "insurance_policy",
            "downstream_disposition": "automated_text_pipeline",
            "extraction_method": "embedded_text",
        }],
    })
    raw = isolated_dao / "data" / "raw" / "CASE_030"
    raw.mkdir(parents=True)
    (raw / "DOC_005.pdf").write_bytes(RAW)

    text_file = isolated_dao / "text.md"
    text_file.write_text(TEXT, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_001")) == 0
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="document-pipeline",
        run_id="RUN_20260728_001", expect=None)) == 0
    assert dao.cmd_enable_canonical_uids(make_args(
        case_id="CASE_030", doc_id="DOC_005", held_by="policy-pipeline",
        run_id="RUN_20260728_001")) == 0
    manifest = dao.read_contract_data("CASE_030", "document_manifest.json")
    return manifest["documents"][0]["source_pdf_sha256"]


def _revision():
    return dao.revision_entry_for(
        "CASE_030", "DOC_005")["current_revision_sha256"]


def _binding():
    return {"documents": [{"document_id": "DOC_005",
                           "revision_sha256": _revision()}]}


def _span(page, text, occurrence=1, page_text=None):
    """A span object for the Nth occurrence of `text` on `page`.

    Resolved against the currently registered revision unless `page_text` is
    given -- the revision-tampering tests pass a superseded page deliberately,
    to build a span that WAS valid and no longer is.
    """
    page_text = _page_text(page) if page_text is None else page_text
    start = -1
    for _ in range(occurrence):
        start = page_text.find(text, start + 1)
        assert start >= 0, f"{text!r} occurrence {occurrence} not on page {page}"
    return {"page": page, "start_char": start,
            "end_char": start + len(text), "quote": text}


# --- expected-UID helpers ---------------------------------------------------
# These DO use the production helper: their job is to express "what the DAO
# should compute", and the independent oracle lives in the frozen-vector module.

def _page_text(page):
    """The CURRENT registered revision's page text.

    Read back through the DAO rather than from the module constants, so a test
    that registers a different revision still computes its expected UIDs
    against the bytes actually in force.
    """
    entry = dao.revision_entry_for("CASE_030", "DOC_005")
    text = dao.revision_file_path(
        "CASE_030", "DOC_005",
        entry["current_revision_sha256"]).read_text(encoding="utf-8")
    return _cross_contract.split_pages(text)[page]


def _ps(pdf, span):
    return policy_uid.compute_uid(
        "span", source_pdf_sha256=pdf, physical_page=span["page"],
        span_text=span["quote"],
        ordinal=policy_uid.ordinal_of_span_at(
            _page_text(span["page"]), span["quote"], span["start_char"]))


def _derived(kind, pdf, spans, parent=None, column_key=None):
    """Mirror of the resolver's canonical ordering, written out longhand."""
    records = sorted(
        ((_ps(pdf, s), s) for s in spans),
        key=lambda pair: (pair[1]["page"], pair[1]["start_char"],
                          pair[1]["end_char"], pair[0]))
    identity = "\x1f".join(uid for uid, _ in records)
    return policy_uid.compute_uid(
        kind, source_pdf_sha256=pdf,
        physical_page=records[0][1]["page"], span_text=identity, ordinal=1,
        parent_uid=parent, column_key=column_key)


# --- contract builders ------------------------------------------------------

CLAUSE_SPAN = "제3조(보험금의 지급) 회사는 보험금을 지급합니다."
CONDITION_TEXT = "사고일부터 180일 이내에 사망한 경우"


def build_inventory(pdf, *, boundary_uid=None, span_uid=None):
    span = _span(1, CLAUSE_SPAN)
    ps = span_uid or _ps(pdf, span)
    pb = boundary_uid or _derived("boundary", pdf, [span])
    return {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_005",
        "source_text_revision": _binding(),
        "boundaries": [{
            "boundary_uid": pb,
            "boundary_level": "article",
            # `excluded_with_reason` keeps this inventory schema-valid without
            # a clause contract present, so the write-path tests below fail on
            # the UID layer under test rather than on a normalized_mappings
            # cross-reference that is a different part's concern.
            "disposition": "excluded_with_reason",
            "label": "제3조",
            "normalized_mappings": [],
            "reason": "held for the UID layer under test",
            "review_required": False,
        }],
        "page_spans": [{
            "span_uid": ps,
            "page": 1,
            "start_char": span["start_char"],
            "end_char": span["end_char"],
            "quote": span["quote"],
            "disposition": "boundary",
            "boundary_uid": pb,
            "exclusion_reason": None,
        }],
    }


def build_clauses(pdf, *, clause_uid=None, condition_uids=None,
                  parent_boundary=None, clause_spans=None,
                  condition_spans=None):
    """One clause with TWO conditions whose normalized text is identical.

    Requirement 4: only the exact span (and therefore the occurrence ordinal)
    tells them apart -- their `text` is the same string.
    """
    boundary_span = _span(1, CLAUSE_SPAN)
    pb = parent_boundary or _derived("boundary", pdf, [boundary_span])
    spans = clause_spans if clause_spans is not None else [boundary_span]
    pc = clause_uid or _derived("clause", pdf, spans, parent=pb)

    if condition_spans is None:
        condition_spans = [[_span(1, CONDITION_TEXT, occurrence=1)],
                           [_span(1, CONDITION_TEXT, occurrence=2)]]
    uids = condition_uids or [
        _derived("condition", pdf, group, parent=pc)
        for group in condition_spans
    ]
    conditions = [
        {
            "condition_uid": uid,
            "source_span_uids": group,
            "text": CONDITION_TEXT,
            "evidence_references": [{
                "document_id": "DOC_005", "page": 1,
                "quote": CONDITION_TEXT}],
            "support_level": "direct",
            "support_rationale": "verbatim from the cited page",
            "review_required": False,
        }
        for uid, group in zip(uids, condition_spans)
    ]
    return {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_005",
        "source_text_revision": _binding(),
        "clauses": [{
            "clause_uid": pc,
            "clause_id": "C-1",
            "source_boundary_uids": [pb],
            "source_span_uids": spans,
            "clause_kind": "coverage",
            "coverage_type": "사망보험금",
            "payout_conditions": conditions,
            "exclusions": [],
            "reduction_conditions": [],
            "definitions": [],
            "obligations": [],
            "claim_requirements": [],
            "termination_conditions": [],
            "dispute_resolution_conditions": [],
            "coverage_start_conditions": [],
            "reference_table_refs": [],
            "confidence": 0.9,
            "evidence_references": [{
                "document_id": "DOC_005", "page": 1, "quote": CLAUSE_SPAN}],
            "review_required": False,
        }],
    }


def build_tables(pdf, *, table_uid=None, row_uids=None, cell_uids=None,
                 region_spans=None, row_specs=None):
    """A 2-row table whose value cells BOTH read '10'.

    Requirement 4/7: the two `10` cells are distinguishable only by their exact
    spans, and because RC is parented on RR, swapping row contents changes the
    cell identifiers rather than merely failing a containment check.
    """
    if region_spans is None:
        region_spans = [_span(2, "A 10\nB 10")]
    rt = table_uid or _derived("table", pdf, region_spans)

    if row_specs is None:
        row_specs = [
            (_span(2, "A 10"), [("label", _span(2, "A")),
                                ("rate", _span(2, "10", occurrence=1))]),
            (_span(2, "B 10"), [("label", _span(2, "B")),
                                ("rate", _span(2, "10", occurrence=2))]),
        ]
    rows = []
    for row_index, (row_span, cells) in enumerate(row_specs):
        rr = (row_uids or [None] * len(row_specs))[row_index] \
            or _derived("row", pdf, [row_span], parent=rt)
        built_cells = []
        for cell_index, (column_key, cell_span) in enumerate(cells):
            rc = None
            if cell_uids:
                rc = cell_uids[row_index][cell_index]
            rc = rc or _derived("cell", pdf, [cell_span], parent=rr,
                                column_key=column_key)
            built_cells.append({
                "cell_uid": rc,
                "source_spans": [cell_span],
                "column_key": column_key,
                "value": cell_span["quote"],
                "evidence_references": [{
                    "document_id": "DOC_005", "page": 2,
                    "quote": row_span["quote"]}],
                "review_required": False,
            })
        rows.append({
            "row_uid": rr,
            "source_span": row_span,
            "cells": built_cells,
        })
    return {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_005",
        "source_text_revision": _binding(),
        "tables": [{
            "table_uid": rt,
            "table_id": "T-1",
            "title": "장해지급률표",
            "columns": [{"column_key": "label", "label": "구분"},
                        {"column_key": "rate", "label": "지급률"}],
            "rows": rows,
            "source_regions": region_spans,
            "header_spans": [],
            "evidence_references": [{
                "document_id": "DOC_005", "page": 2, "quote": "장해지급률표"}],
            "review_required": False,
        }],
    }


# --- direct checker entry points -------------------------------------------

def _check(filename, schema_name, data):
    return dao._canonical_uid_errors("CASE_030", filename, schema_name, data)


def _check_inventory(data):
    return _check("policy_boundary_inventory_DOC_005.json",
                  policy_completeness.INVENTORY_SCHEMA, data)


def _check_clauses(data):
    return _check("normalized_policy_clause_DOC_005.json",
                  _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA, data)


def _check_tables(data):
    return _check("reference_table_DOC_005.json",
                  _cross_contract.REFERENCE_TABLE_SCHEMA, data)


def _persist(name, data):
    _write_json(dao.case_dir("CASE_030") / name, data)


# =========================================================================
# 1. Fabricated UIDs are refused, even when used consistently everywhere
# =========================================================================

def test_the_six_fabricated_uids_are_each_refused(canonical):
    """The headline defect: PB/PC/CI/RT/RR/RC of sixteen hex digits of
    anything, written CONSISTENTLY into every artifact that references them.

    Every cross-contract check in the repo passes on this input -- the strings
    match wherever they appear. Only recomputation from source rejects it.
    """
    pdf = canonical
    inventory = build_inventory(pdf, boundary_uid=FAKE["boundary"])
    clauses = build_clauses(
        pdf,
        clause_uid=FAKE["clause"],
        condition_uids=[FAKE["condition"], FAKE["condition"]],
        parent_boundary=FAKE["boundary"])
    tables = build_tables(
        pdf, table_uid=FAKE["table"],
        row_uids=[FAKE["row"], FAKE["row"]],
        cell_uids=[[FAKE["cell"], FAKE["cell"]],
                   [FAKE["cell"], FAKE["cell"]]])
    _persist("policy_boundary_inventory_DOC_005.json", inventory)

    assert any("boundary UID" in e for e in _check_inventory(inventory))
    clause_errors = _check_clauses(clauses)
    assert clause_errors, "a fabricated PC must not pass"
    table_errors = _check_tables(tables)
    assert any("table UID" in e for e in table_errors), table_errors


@pytest.mark.parametrize("kind", ["clause", "condition"])
def test_a_fabricated_clause_layer_uid_is_named_in_the_error(canonical, kind):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    if kind == "clause":
        data = build_clauses(pdf, clause_uid=FAKE["clause"])
    else:
        good = _derived("condition", pdf,
                        [_span(1, CONDITION_TEXT, occurrence=1)],
                        parent=_derived(
                            "clause", pdf, [_span(1, CLAUSE_SPAN)],
                            parent=_derived("boundary", pdf,
                                            [_span(1, CLAUSE_SPAN)])))
        data = build_clauses(pdf, condition_uids=[good, FAKE["condition"]])
    errors = _check_clauses(data)
    assert any(f"{kind} UID" in e for e in errors), errors


@pytest.mark.parametrize("kind", ["row", "cell"])
def test_a_fabricated_table_layer_uid_is_named_in_the_error(canonical, kind):
    pdf = canonical
    if kind == "row":
        data = build_tables(pdf, row_uids=[FAKE["row"], None])
    else:
        data = build_tables(pdf, cell_uids=[[FAKE["cell"], None], [None, None]])
    errors = _check_tables(data)
    assert any(f"{kind} UID" in e for e in errors), errors


# =========================================================================
# 2. The full hierarchies recompute cleanly
# =========================================================================

def test_ps_pb_pc_ci_recompute_end_to_end(canonical):
    pdf = canonical
    inventory = build_inventory(pdf)
    assert _check_inventory(inventory) == []
    _persist("policy_boundary_inventory_DOC_005.json", inventory)
    assert _check_clauses(build_clauses(pdf)) == []


def test_ps_rt_rr_rc_recompute_end_to_end(canonical):
    assert _check_tables(build_tables(canonical)) == []


# =========================================================================
# 3. Missing provenance is fail-closed, never a skipped check
# =========================================================================

def test_a_contract_with_no_page_spans_no_longer_passes_by_being_empty(
        canonical):
    """The literal defect: `if not spans: return []`.

    A canonical clause contract carries no `page_spans` at all, so the old
    checker returned clean for every one of them.
    """
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    data = build_clauses(pdf, clause_uid=FAKE["clause"])
    assert "page_spans" not in data
    assert _check_clauses(data), "an empty page_spans must not mean 'verified'"


def test_a_clause_without_source_spans_is_refused(canonical):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    data = build_clauses(pdf)
    del data["clauses"][0]["source_span_uids"]
    errors = _check_clauses(data)
    assert any("no source spans" in e for e in errors), errors


def test_a_condition_without_source_spans_is_refused(canonical):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    data = build_clauses(pdf)
    del data["clauses"][0]["payout_conditions"][0]["source_span_uids"]
    errors = _check_clauses(data)
    assert any("no source spans" in e for e in errors), errors


def test_a_clause_without_a_parent_boundary_is_refused(canonical):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    data = build_clauses(pdf)
    data["clauses"][0]["source_boundary_uids"] = []
    errors = _check_clauses(data)
    assert any("no source_boundary_uids" in e for e in errors), errors


def test_a_clause_whose_document_has_no_inventory_is_refused(canonical):
    """A clause cannot mint its own parent: with no inventory there is no
    boundary layer to derive the PC's parent from."""
    errors = _check_clauses(build_clauses(canonical))
    assert any("no policy_boundary_inventory" in e for e in errors), errors


def test_a_cell_without_source_spans_is_refused(canonical):
    data = build_tables(canonical)
    del data["tables"][0]["rows"][0]["cells"][0]["source_spans"]
    errors = _check_tables(data)
    assert any("no source spans" in e for e in errors), errors


def test_a_table_without_source_regions_is_refused(canonical):
    data = build_tables(canonical)
    del data["tables"][0]["source_regions"]
    errors = _check_tables(data)
    assert any("no source spans" in e for e in errors), errors


def test_a_boundary_owning_no_span_is_refused(canonical):
    """A PB is its spans; a boundary nothing points at cannot be derived."""
    pdf = canonical
    data = build_inventory(pdf)
    data["page_spans"][0]["disposition"] = "excluded"
    data["page_spans"][0]["boundary_uid"] = None
    data["page_spans"][0]["exclusion_reason"] = "page furniture"
    errors = _check_inventory(data)
    assert any("owns no page span" in e for e in errors), errors


def test_an_unmapped_policy_layer_schema_is_refused_not_skipped(canonical):
    """Fail-closed by construction: a UID-bearing schema nobody taught this
    checker about must refuse, not pass. Adding one silently is how P0-2
    happened."""
    errors = dao._canonical_uid_errors(
        "CASE_030", "some_new_policy_contract_DOC_005.json",
        "some_new_policy_contract.schema.json", {"anything": True})
    assert any("no UID recomputation rule" in e for e in errors), errors


# =========================================================================
# 4. Repeated content: exact span and occurrence discriminate
# =========================================================================

def test_two_identical_conditions_get_different_uids(canonical):
    """Same normalized text, same page, two occurrences -- different CI.

    A quote-based identity would collapse these into one; the whole point of
    the occurrence ordinal is that it does not.
    """
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    data = build_clauses(pdf)
    first, second = data["clauses"][0]["payout_conditions"]
    assert first["text"] == second["text"]
    assert first["condition_uid"] != second["condition_uid"]
    assert _check_clauses(data) == []


def test_swapping_two_identical_conditions_uids_is_refused(canonical):
    """Identical text, so the swap is invisible to every text-based check --
    but each UID encodes WHICH occurrence, so the swap is caught."""
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    data = build_clauses(pdf)
    conditions = data["clauses"][0]["payout_conditions"]
    conditions[0]["condition_uid"], conditions[1]["condition_uid"] = (
        conditions[1]["condition_uid"], conditions[0]["condition_uid"])
    assert _check_clauses(data)


def test_two_identical_cell_values_get_different_uids(canonical):
    data = build_tables(canonical)
    rows = data["tables"][0]["rows"]
    first = rows[0]["cells"][1]
    second = rows[1]["cells"][1]
    assert first["value"] == second["value"] == "10"
    assert first["cell_uid"] != second["cell_uid"]
    assert _check_tables(data) == []


def test_an_offset_shift_that_preserves_page_text_and_occurrence_keeps_uids(
        canonical, isolated_dao, make_args):
    """The canonical_v1 stability guarantee, exercised for real.

    Prepending text to page 1 moves every later offset on it. The clause bytes,
    its physical page, and its occurrence ordinal are unchanged, so the UIDs
    must be unchanged too -- offsets locate a span, they never identify it.
    """
    pdf = canonical
    before = build_clauses(pdf)
    before_inventory = build_inventory(pdf)
    before_uids = [before["clauses"][0]["clause_uid"]] + [
        c["condition_uid"] for c in before["clauses"][0]["payout_conditions"]]

    shifted_page_1 = "제1조(목적) 이 약관은 다음과 같습니다.\n" + PAGE_1
    text_file = isolated_dao / "shifted.md"
    text_file.write_text(
        f"<<<PAGE page=1>>>\n{shifted_page_1}<<<PAGE page=2>>>\n{PAGE_2}",
        encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_002")) == 0

    offset = len("제1조(목적) 이 약관은 다음과 같습니다.\n")
    # Rebuilt from the pre-shift page and then translated: the spans must name
    # the same bytes at their new offsets, which is exactly what a source edit
    # elsewhere on the page does to a real contract.
    after = json.loads(json.dumps(before))
    for clause in after["clauses"]:
        for span in clause["source_span_uids"]:
            span["start_char"] += offset
            span["end_char"] += offset
        for condition in clause["payout_conditions"]:
            for span in condition["source_span_uids"]:
                span["start_char"] += offset
                span["end_char"] += offset
    after_uids = [after["clauses"][0]["clause_uid"]] + [
        c["condition_uid"] for c in after["clauses"][0]["payout_conditions"]]
    assert after_uids == before_uids

    inventory = before_inventory
    inventory["source_text_revision"] = _binding()
    inventory["page_spans"][0]["start_char"] += offset
    inventory["page_spans"][0]["end_char"] += offset
    assert _check_inventory(inventory) == []
    _persist("policy_boundary_inventory_DOC_005.json", inventory)
    after["source_text_revision"] = _binding()
    assert _check_clauses(after) == []


def test_a_submitted_occurrence_ordinal_that_disagrees_is_refused(canonical):
    """The ordinal is DERIVED, never believed -- a caller claiming occurrence 1
    for the second occurrence is refused rather than taken at its word."""
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    data = build_clauses(pdf)
    second = data["clauses"][0]["payout_conditions"][1]
    second["source_span_uids"][0]["occurrence_ordinal"] = 1
    errors = _check_clauses(data)
    assert any("occurrence_ordinal" in e for e in errors), errors


# =========================================================================
# 5. Multi-span / multi-page, order-independent
# =========================================================================

def _multi_span_clause(pdf):
    """A clause whose source is two spans, one on each page."""
    return [_span(1, CLAUSE_SPAN), _span(2, "제4조(별표) 장해지급률표")]


def test_a_multi_span_boundary_recomputes(canonical):
    """A PB built from two page spans, not one."""
    pdf = canonical
    data = build_inventory(pdf)
    second = _span(1, "가. 사고일부터 180일 이내에 사망한 경우")
    spans = [_span(1, CLAUSE_SPAN), second]
    pb = _derived("boundary", pdf, spans)
    data["boundaries"][0]["boundary_uid"] = pb
    data["page_spans"][0]["boundary_uid"] = pb
    data["page_spans"].append({
        "span_uid": _ps(pdf, second),
        "page": 1,
        "start_char": second["start_char"],
        "end_char": second["end_char"],
        "quote": second["quote"],
        "disposition": "boundary",
        "boundary_uid": pb,
        "exclusion_reason": None,
    })
    assert _check_inventory(data) == []


def test_a_multi_page_clause_recomputes(canonical):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    spans = _multi_span_clause(pdf)
    assert {s["page"] for s in spans} == {1, 2}
    assert _check_clauses(build_clauses(pdf, clause_spans=spans)) == []


def test_a_multi_region_table_and_multi_span_row_recompute(canonical):
    """A table spanning both pages, with a row made of two ranges."""
    pdf = canonical
    regions = [_span(1, "가. 사고일부터 180일 이내에 사망한 경우"),
               _span(2, "A 10\nB 10")]
    rt = _derived("table", pdf, regions)
    row_a_spans = [_span(2, "A 10")]
    row_b_spans = [_span(2, "B 10"), _span(1, "나. 사고일부터 180일 이내에 사망한 경우")]
    data = build_tables(pdf, region_spans=regions)
    table = data["tables"][0]
    table["table_uid"] = rt
    for row, spans in zip(table["rows"], (row_a_spans, row_b_spans)):
        rr = _derived("row", pdf, spans, parent=rt)
        row["row_uid"] = rr
        row["source_span"] = spans[0]
        row["source_spans"] = spans
        for cell in row["cells"]:
            cell["cell_uid"] = _derived(
                "cell", pdf, cell["source_spans"], parent=rr,
                column_key=cell["column_key"])
    assert _check_tables(data) == []


def test_reversing_the_submitted_span_order_does_not_change_any_uid(canonical):
    """Canonical order is SOURCE order. Extraction order is a forbidden input,
    so listing the same spans backwards must be the same element."""
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    spans = _multi_span_clause(pdf)
    forward = build_clauses(pdf, clause_spans=spans)
    reversed_ = build_clauses(pdf, clause_spans=list(reversed(spans)))
    assert (forward["clauses"][0]["clause_uid"]
            == reversed_["clauses"][0]["clause_uid"])
    assert _check_clauses(reversed_) == []


def test_a_duplicate_span_in_one_identity_is_refused(canonical):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    span = _span(1, CLAUSE_SPAN)
    data = build_clauses(pdf, clause_spans=[span, dict(span)])
    errors = _check_clauses(data)
    assert any("listed twice" in e or "overlap" in e for e in errors), errors


def test_overlapping_spans_in_one_identity_are_refused(canonical):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    whole = _span(1, CLAUSE_SPAN)
    part = _span(1, "회사는 보험금을 지급합니다.")
    data = build_clauses(pdf, clause_spans=[whole, part])
    errors = _check_clauses(data)
    assert any("overlap" in e for e in errors), errors


# =========================================================================
# 6. Parent / revision tampering
# =========================================================================

def test_repointing_a_clause_at_a_different_parent_boundary_is_refused(
        canonical):
    """Only the parent changes; the clause's own spans are untouched. The PC
    must still move, because the parent is an identity input."""
    pdf = canonical
    inventory = build_inventory(pdf)
    _persist("policy_boundary_inventory_DOC_005.json", inventory)
    other = _derived("boundary", pdf, [_span(1, CONDITION_TEXT)])
    data = build_clauses(pdf)
    data["clauses"][0]["source_boundary_uids"] = [other]
    errors = _check_clauses(data)
    assert any("do not resolve to a canonical boundary" in e
               for e in errors), errors


def test_repointing_a_cell_at_a_different_parent_row_is_refused(canonical):
    """RC carries its parent RR, so a cell claiming the other row's identity is
    refused even though its own span is a real one."""
    pdf = canonical
    data = build_tables(pdf)
    rows = data["tables"][0]["rows"]
    other_row_uid = rows[1]["row_uid"]
    cell = rows[0]["cells"][1]
    cell["cell_uid"] = _derived(
        "cell", pdf, cell["source_spans"], parent=other_row_uid,
        column_key=cell["column_key"])
    assert _check_tables(data)


def test_a_span_from_a_different_source_pdf_is_refused(canonical):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    foreign_pb = _derived("boundary", "f" * 64, [_span(1, CLAUSE_SPAN)])
    data = build_clauses(pdf, parent_boundary=foreign_pb)
    assert _check_clauses(data)


def test_a_uid_from_the_previous_revision_is_refused(canonical, isolated_dao,
                                                     make_args):
    """A revision that genuinely CHANGES the span text must move its UIDs --
    otherwise a stale identifier survives a source correction."""
    pdf = canonical
    # Built against the pre-edit revision, so these UIDs were genuinely
    # canonical when computed -- the test is that they stop being so.
    stale = build_clauses(pdf)
    stale_pc = stale["clauses"][0]["clause_uid"]
    stale_inventory = build_inventory(pdf)

    edited_page_1 = PAGE_1.replace("지급합니다", "지급하지 않습니다")
    text_file = isolated_dao / "edited.md"
    text_file.write_text(
        f"<<<PAGE page=1>>>\n{edited_page_1}<<<PAGE page=2>>>\n{PAGE_2}",
        encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_003")) == 0

    new_quote = "제3조(보험금의 지급) 회사는 보험금을 지급하지 않습니다."
    span = {"page": 1, "start_char": 0, "end_char": len(new_quote),
            "quote": new_quote}
    inventory = stale_inventory
    inventory["source_text_revision"] = _binding()
    inventory["page_spans"][0].update(span)
    inventory["page_spans"][0]["span_uid"] = policy_uid.compute_uid(
        "span", source_pdf_sha256=pdf, physical_page=1, span_text=new_quote,
        ordinal=1)
    new_pb = policy_uid.compute_uid(
        "boundary", source_pdf_sha256=pdf, physical_page=1,
        span_text=inventory["page_spans"][0]["span_uid"], ordinal=1)
    inventory["boundaries"][0]["boundary_uid"] = new_pb
    inventory["page_spans"][0]["boundary_uid"] = new_pb
    assert _check_inventory(inventory) == []
    _persist("policy_boundary_inventory_DOC_005.json", inventory)

    # The stale clause, re-pointed at the new revision's span and parent but
    # keeping its OLD clause_uid -- the exact shape of a contract that was not
    # recomputed after its source changed.
    data = stale
    data["source_text_revision"] = _binding()
    clause = data["clauses"][0]
    clause["clause_uid"] = stale_pc
    clause["source_boundary_uids"] = [new_pb]
    clause["source_span_uids"] = [span]
    errors = _check_clauses(data)
    assert any("clause UID" in e for e in errors), errors


def test_mixing_an_old_revision_span_with_a_current_one_is_refused(
        canonical, isolated_dao, make_args):
    """Half the spans quote text that no longer exists -- the resolver reads
    the CURRENT registered revision, so the stale half cannot be located."""
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    stale_clauses = build_clauses(pdf)  # built against the pre-edit revision
    edited_page_1 = PAGE_1.replace("지급합니다", "지급하지 않습니다")
    text_file = isolated_dao / "mixed.md"
    text_file.write_text(
        f"<<<PAGE page=1>>>\n{edited_page_1}<<<PAGE page=2>>>\n{PAGE_2}",
        encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_004")) == 0
    data = stale_clauses  # spans still quote the OLD page-1 text
    data["source_text_revision"] = _binding()
    errors = _check_clauses(data)
    assert errors, "a span quoting a superseded revision must not resolve"


def test_verification_reads_the_registered_revision_not_the_working_file(
        canonical, isolated_dao):
    """Tampering with redacted_text.md directly must not change what verifies.

    The revision pointer is the authority; the working copy is only a copy.
    """
    pdf = canonical
    working = (dao.processed_dir("CASE_030", "DOC_005") / "redacted_text.md")
    working.write_text("<<<PAGE page=1>>>\n완전히 다른 내용.\n", encoding="utf-8")
    assert _check_inventory(build_inventory(pdf)) == []


# =========================================================================
# 7. Table relationship tampering
# =========================================================================

def test_transposing_values_between_rows_is_refused(canonical, isolated_dao,
                                                    make_args):
    """The 'A 10 / B 20' -> 'A 20 / B 10' attack, expressed in UIDs.

    Every value still exists on the page, so value-level grounding passes. The
    cell spans now point into the other row, which both breaks containment and
    changes the identifiers.
    """
    pdf = canonical
    page_2 = "제4조(별표) 장해지급률표\n구분 지급률\nA 10\nB 20\n"

    def span2(needle, occurrence=1):
        start = -1
        for _ in range(occurrence):
            start = page_2.find(needle, start + 1)
        return {"page": 2, "start_char": start,
                "end_char": start + len(needle), "quote": needle}

    # Register the two-distinct-values page as a real revision, through the
    # DAO -- writing the revision file directly would leave the pointer digest
    # disagreeing with the bytes it names.
    text_file = isolated_dao / "distinct_rows.md"
    text_file.write_text(
        f"<<<PAGE page=1>>>\n{PAGE_1}<<<PAGE page=2>>>\n{page_2}",
        encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_005")) == 0

    regions = [span2("A 10\nB 20")]
    rt = _derived("table", pdf, regions)
    row_a, row_b = span2("A 10"), span2("B 20")
    rr_a = _derived("row", pdf, [row_a], parent=rt)
    rr_b = _derived("row", pdf, [row_b], parent=rt)
    # The transposition: row A claims the "20" that physically belongs to row B.
    stolen = span2("20")
    data = build_tables(
        pdf, region_spans=regions,
        row_specs=[(row_a, [("label", span2("A")), ("rate", span2("10"))]),
                   (row_b, [("label", span2("B")), ("rate", span2("20"))])])
    table = data["tables"][0]
    table["table_uid"] = rt
    table["rows"] = [
        {"row_uid": rr_a, "source_span": row_a, "cells": [
            {"cell_uid": _derived("cell", pdf, [span2("A")], parent=rr_a,
                                  column_key="label"),
             "source_spans": [span2("A")], "column_key": "label",
             "value": "A",
             "evidence_references": [{"document_id": "DOC_005", "page": 2,
                                      "quote": "A 10"}],
             "review_required": False},
            {"cell_uid": _derived("cell", pdf, [stolen], parent=rr_a,
                                  column_key="rate"),
             "source_spans": [stolen], "column_key": "rate", "value": "20",
             "evidence_references": [{"document_id": "DOC_005", "page": 2,
                                      "quote": "B 20"}],
             "review_required": False},
        ]},
        {"row_uid": rr_b, "source_span": row_b, "cells": [
            {"cell_uid": _derived("cell", pdf, [span2("B")], parent=rr_b,
                                  column_key="label"),
             "source_spans": [span2("B")], "column_key": "label",
             "value": "B",
             "evidence_references": [{"document_id": "DOC_005", "page": 2,
                                      "quote": "B 20"}],
             "review_required": False},
            {"cell_uid": _derived("cell", pdf, [span2("10")], parent=rr_b,
                                  column_key="rate"),
             "source_spans": [span2("10")], "column_key": "rate",
             "value": "10",
             "evidence_references": [{"document_id": "DOC_005", "page": 2,
                                      "quote": "A 10"}],
             "review_required": False},
        ]},
    ]
    errors = _check_tables(data)
    assert any("outside its own row" in e for e in errors), errors


def test_a_cell_span_from_another_row_is_refused_even_when_uid_matches(
        canonical):
    """The span is real and the RC recomputes from it correctly -- what is
    wrong is the ROW it claims. Containment is what catches that."""
    pdf = canonical
    data = build_tables(pdf)
    rows = data["tables"][0]["rows"]
    foreign = rows[1]["cells"][0]["source_spans"][0]
    cell = rows[0]["cells"][0]
    cell["source_spans"] = [foreign]
    cell["value"] = foreign["quote"]
    cell["cell_uid"] = _derived("cell", pdf, [foreign],
                                parent=rows[0]["row_uid"],
                                column_key=cell["column_key"])
    errors = _check_tables(data)
    assert any("outside its own row" in e for e in errors), errors


# =========================================================================
# 8. The real write path, across every canonical contract type
# =========================================================================

def _write(isolated_dao, make_args, filename, schema_name, data):
    data_file = isolated_dao / f"write_{filename}"
    _write_json(data_file, data)
    return dao.cmd_write_contract(make_args(
        case_id="CASE_030", filename=filename, data_file=str(data_file),
        schema_name=schema_name, held_by="policy-pipeline",
        run_id="RUN_20260728_001", stage=None))


@pytest.mark.parametrize("contract", ["inventory", "clauses", "tables"])
def test_write_contract_refuses_every_fabricated_uid_kind(
        canonical, isolated_dao, make_args, capsys, contract):
    """Not the checker in isolation -- the actual `write-contract` path, for
    every canonical contract type that mints UIDs."""
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    spec = {
        "inventory": ("policy_boundary_inventory_DOC_005.json",
                      policy_completeness.INVENTORY_SCHEMA,
                      build_inventory(pdf, boundary_uid=FAKE["boundary"])),
        "clauses": ("normalized_policy_clause_DOC_005.json",
                    _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
                    build_clauses(pdf, clause_uid=FAKE["clause"])),
        "tables": ("reference_table_DOC_005.json",
                   _cross_contract.REFERENCE_TABLE_SCHEMA,
                   build_tables(pdf, table_uid=FAKE["table"])),
    }[contract]
    filename, schema_name, data = spec
    assert _write(isolated_dao, make_args, filename, schema_name, data) == 1
    assert "non-canonical UIDs" in capsys.readouterr().out


def test_write_contract_accepts_a_canonically_derived_reference_table(
        canonical, isolated_dao, make_args, capsys):
    """The positive end-to-end case: a table whose whole RT/RR/RC hierarchy
    derives from source is written."""
    assert _write(
        isolated_dao, make_args, "reference_table_DOC_005.json",
        _cross_contract.REFERENCE_TABLE_SCHEMA,
        build_tables(canonical)) == 0
    assert (dao.case_dir("CASE_030") / "reference_table_DOC_005.json").exists()


def test_finalization_recomputes_uids_of_already_written_contracts(canonical):
    """Write-time verification proves the UIDs were canonical once. A later
    source revision can invalidate them, so finalization re-derives."""
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json",
             build_inventory(pdf, boundary_uid=FAKE["boundary"]))
    blockers = dao._canonical_uid_finalize_blockers("CASE_030", "DOC_005")
    assert any("boundary UID" in b for b in blockers), blockers


def test_finalization_is_clean_when_every_uid_derives(canonical):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    _persist("normalized_policy_clause_DOC_005.json", build_clauses(pdf))
    _persist("reference_table_DOC_005.json", build_tables(pdf))
    assert dao._canonical_uid_finalize_blockers("CASE_030", "DOC_005") == []


def test_an_audit_citing_a_fabricated_clause_uid_is_refused(canonical):
    """A referencing contract mints nothing, so it is checked by RESOLUTION --
    against the recomputed originals, never by string match."""
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    _persist("normalized_policy_clause_DOC_005.json", build_clauses(pdf))
    audit = {"findings": [{"clause_uid": FAKE["clause"]}]}
    errors = dao._canonical_uid_errors(
        "CASE_030", "policy_audit_result_DOC_005.json",
        policy_audit.AUDIT_SCHEMA, audit)
    assert any("does not resolve to a canonically recomputed UID" in e
               for e in errors), errors


def test_an_audit_citing_a_real_clause_uid_resolves(canonical):
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    clauses = build_clauses(pdf)
    _persist("normalized_policy_clause_DOC_005.json", clauses)
    audit = {"findings": [
        {"clause_uid": clauses["clauses"][0]["clause_uid"]}]}
    assert dao._canonical_uid_errors(
        "CASE_030", "policy_audit_result_DOC_005.json",
        policy_audit.AUDIT_SCHEMA, audit) == []


def test_an_audit_cannot_launder_a_fake_uid_by_agreeing_with_the_clause_file(
        canonical):
    """The consistency bypass, stated directly: the SAME fabricated PC in both
    the clause contract and the audit citing it. String agreement is total;
    recomputation still refuses."""
    pdf = canonical
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))
    _persist("normalized_policy_clause_DOC_005.json",
             build_clauses(pdf, clause_uid=FAKE["clause"]))
    audit = {"findings": [{"clause_uid": FAKE["clause"]}]}
    assert dao._canonical_uid_errors(
        "CASE_030", "policy_audit_result_DOC_005.json",
        policy_audit.AUDIT_SCHEMA, audit)


# =========================================================================
# 9/10. Legacy isolation, and no side effects
# =========================================================================

def test_legacy_documents_are_still_not_uid_checked(isolated_dao, make_args):
    """Legacy artifacts stay readable and are never rewritten by this work."""
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    _write_json(out / "document_manifest.json",
                {"case_id": "CASE_030", "documents": []})
    assert dao.uid_scheme_for("CASE_030", "DOC_005") == "legacy"
    assert dao._canonical_uid_errors(
        "CASE_030", "normalized_policy_clause_DOC_005.json",
        _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
        {"clauses": [{"clause_uid": FAKE["clause"]}]}) == []


def test_uid_verification_runs_no_external_process(canonical, _no_extractors):
    """Requirement 10, asserted rather than assumed: the autouse fixture makes
    any subprocess call raise, so a clean run over every kind proves zero
    extractor invocations."""
    pdf = canonical
    inventory = build_inventory(pdf)
    _persist("policy_boundary_inventory_DOC_005.json", inventory)
    assert _check_inventory(inventory) == []
    assert _check_clauses(build_clauses(pdf)) == []
    assert _check_tables(build_tables(pdf)) == []
