"""Cross-document RT/RR references, verified through the real DAO write path.

A clause in one policy document routinely points at a 별표 table living in
another (`reference_table_refs`), and a parent-coverage contract accounts for a
page by naming the document that owns the table on it. Both are cross-document
UID claims, and both were checked only for STRING agreement: if the clause file
and the table file spelled `RT-…` the same way, the link resolved.

That is a consistency check. Consistency is exactly what an attacker controls
when they author both files -- the whole point of canonical_v1 is that a UID is
recomputed from immutable source bytes, and a UID minted in DOC_B has to be
recomputed IN DOC_B, against DOC_B's own registered revision.

`test_canonical_uid_hierarchy.py` covers the same helpers with a monkeypatched
pool, which proves the wiring and nothing about the recomputation. This module
therefore runs two genuinely canonical documents through `cmd_write_contract`
and asserts on the artifacts on disk: a refused write must leave no file and no
run-state behind.

The expected RT/RR here are additionally pinned to a hand-computed SHA
(`test_manually_specified_rt_and_rr_match_the_documented_serialization`), so a
fixture and the validator agreeing is not by itself what makes this pass.
"""
import hashlib
import json
import unicodedata

import pytest

import dao
import policy_uid
import _cross_contract


# DOC_001 holds the clause; DOC_002 owns the 별표 table it points at. Distinct
# raw bytes, so the two documents get different source_pdf_sha256 and a UID
# cannot silently transfer between them.
DOC1_TEXT = (
    "<<<PAGE page=1>>>\n"
    "제3조(보험금의 지급) 회사는 별표1의 지급률에 따라 보험금을 지급합니다.\n"
)
DOC2_TEXT = (
    "<<<PAGE page=1>>>\n"
    "[별표1] 장해분류표\n"
    "구분 지급률\n"
    "눈의 장해 50%\n"
)
DOC1_RAW = b"%PDF-1.7 policy body, immutable"
DOC2_RAW = b"%PDF-1.7 appendix tables, immutable, a different file"

RUN = "RUN_20260728_010"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


@pytest.fixture(autouse=True)
def _no_extractors(monkeypatch):
    """Cross-document verification must read registered bytes, never re-OCR."""
    import subprocess

    def _boom(*args, **kwargs):
        raise AssertionError(
            f"cross-document UID verification shelled out: {args!r}")

    for name in ("run", "check_output", "Popen", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, _boom)


def _manifest_document(doc_id):
    return {
        "document_id": doc_id,
        "file_name": f"{doc_id}.pdf",
        "file_path": f"data/raw/CASE_030/{doc_id}.pdf",
        "file_format": "pdf",
        "file_size_bytes": 100,
        "ocr_status": "completed",
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
        "extraction_method": "embedded_text",
    }


@pytest.fixture
def two_documents(isolated_dao, make_args, canonicalize):
    """DOC_001 canonical, DOC_002 canonical, both through the real flow."""
    out = isolated_dao / "outputs" / "CASE_030"
    _write_json(out / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [_manifest_document("DOC_001"),
                      _manifest_document("DOC_002")],
    })
    raw = isolated_dao / "data" / "raw" / "CASE_030"
    raw.mkdir(parents=True)
    (raw / "DOC_001.pdf").write_bytes(DOC1_RAW)
    (raw / "DOC_002.pdf").write_bytes(DOC2_RAW)

    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_001", DOC1_TEXT,
                 run_id=RUN)
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_002", DOC2_TEXT,
                 run_id=RUN)
    manifest = dao.read_contract_data("CASE_030", "document_manifest.json")
    return {d["document_id"]: d["source_pdf_sha256"]
            for d in manifest["documents"]}


@pytest.fixture
def legacy_owner(isolated_dao, make_args, canonicalize):
    """DOC_001 canonical, DOC_002 registered but never activated (legacy)."""
    out = isolated_dao / "outputs" / "CASE_030"
    _write_json(out / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [_manifest_document("DOC_001"),
                      _manifest_document("DOC_002")],
    })
    raw = isolated_dao / "data" / "raw" / "CASE_030"
    raw.mkdir(parents=True)
    (raw / "DOC_001.pdf").write_bytes(DOC1_RAW)
    (raw / "DOC_002.pdf").write_bytes(DOC2_RAW)

    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_001", DOC1_TEXT,
                 run_id=RUN)
    text_file = isolated_dao / "doc2.md"
    text_file.write_text(DOC2_TEXT, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_002", text_file=str(text_file),
        held_by="document-pipeline", run_id=RUN)) == 0
    assert dao.cmd_record_source_digest(make_args(
        case_id="CASE_030", doc_id="DOC_002", held_by="document-pipeline",
        run_id=RUN, expect=None)) == 0
    assert dao.uid_scheme_for("CASE_030", "DOC_002") == "legacy"
    manifest = dao.read_contract_data("CASE_030", "document_manifest.json")
    return {d["document_id"]: d.get("source_pdf_sha256")
            for d in manifest["documents"]}


# --- span / UID helpers ----------------------------------------------------
# These call the production helper, as the hierarchy module does, and the
# independent oracle is the hand-computed SHA test at the bottom of this file.

def _page(doc_id, page):
    entry = dao.revision_entry_for("CASE_030", doc_id)
    text = dao.revision_file_path(
        "CASE_030", doc_id,
        entry["current_revision_sha256"]).read_text(encoding="utf-8")
    return _cross_contract.split_pages(text)[page]


def _span(doc_id, page, needle):
    body = _page(doc_id, page)
    start = body.index(needle)
    return {"page": page, "start_char": start,
            "end_char": start + len(needle), "quote": needle}


def _ps(pdf, doc_id, span):
    return policy_uid.compute_uid(
        "span", source_pdf_sha256=pdf, physical_page=span["page"],
        span_text=span["quote"],
        ordinal=policy_uid.ordinal_of_span_at(
            _page(doc_id, span["page"]), span["quote"], span["start_char"]))


def _derived(kind, pdf, doc_id, spans, parent=None, column_key=None):
    records = sorted(
        ((_ps(pdf, doc_id, s), s) for s in spans),
        key=lambda pair: (pair[1]["page"], pair[1]["start_char"],
                          pair[1]["end_char"], pair[0]))
    identity = "\x1f".join(uid for uid, _ in records)
    return policy_uid.compute_uid(
        kind, source_pdf_sha256=pdf,
        physical_page=records[0][1]["page"], span_text=identity, ordinal=1,
        parent_uid=parent, column_key=column_key)


def _binding(*doc_ids):
    return {"documents": [
        {"document_id": doc_id,
         "revision_sha256": dao.revision_entry_for(
             "CASE_030", doc_id)["current_revision_sha256"]}
        for doc_id in sorted(doc_ids)]}


# --- contract builders -----------------------------------------------------

TABLE_REGION = "구분 지급률\n눈의 장해 50%"
ROW_TEXT = "눈의 장해 50%"
CLAUSE_TEXT = "제3조(보험금의 지급) 회사는 별표1의 지급률에 따라 보험금을 지급합니다."


def build_table(pdf2, *, table_uid=None, row_uid=None):
    """DOC_002's 별표 table, canonically derived unless overridden."""
    region = _span("DOC_002", 1, TABLE_REGION)
    row_span = _span("DOC_002", 1, ROW_TEXT)
    label = _span("DOC_002", 1, "눈의 장해")
    rate = _span("DOC_002", 1, "50%")
    rt = table_uid or _derived("table", pdf2, "DOC_002", [region])
    rr = row_uid or _derived("row", pdf2, "DOC_002", [row_span], parent=rt)
    return {
        "case_id": "CASE_030",
        "run_id": RUN,
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_002",
        "source_text_revision": _binding("DOC_002"),
        "tables": [{
            "table_uid": rt,
            "table_id": "T-1",
            "title": "장해분류표",
            "columns": [{"column_key": "classification", "label": "구분"},
                        {"column_key": "rate", "label": "지급률"}],
            "source_regions": [region],
            "header_spans": [
                {"span": _span("DOC_002", 1, "구분 지급률"),
                 "kind": "column_header"}],
            "rows": [{
                "row_uid": rr,
                "source_span": row_span,
                "cells": [
                    {"cell_uid": _derived(
                        "cell", pdf2, "DOC_002", [label], parent=rr,
                        column_key="classification"),
                     "source_spans": [label],
                     "column_key": "classification",
                     "value": "눈의 장해",
                     "evidence_references": [{
                         "document_id": "DOC_002", "page": 1,
                         "quote": ROW_TEXT}],
                     "review_required": False},
                    {"cell_uid": _derived(
                        "cell", pdf2, "DOC_002", [rate], parent=rr,
                        column_key="rate"),
                     "source_spans": [rate],
                     "column_key": "rate",
                     "value": "50%",
                     "evidence_references": [{
                         "document_id": "DOC_002", "page": 1,
                         "quote": ROW_TEXT}],
                     "review_required": False},
                ],
            }],
            "evidence_references": [{
                "document_id": "DOC_002", "page": 1, "quote": "장해분류표"}],
            "review_required": False,
        }],
    }


def build_inventory(pdf1):
    span = _span("DOC_001", 1, CLAUSE_TEXT)
    pb = _derived("boundary", pdf1, "DOC_001", [span])
    return {
        "case_id": "CASE_030",
        "run_id": RUN,
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_001",
        "source_text_revision": _binding("DOC_001"),
        "boundaries": [{
            "boundary_uid": pb,
            "boundary_level": "article",
            "disposition": "excluded_with_reason",
            "label": "제3조",
            "normalized_mappings": [],
            "reason": "held for the cross-document UID layer under test",
            "review_required": False,
        }],
        "page_spans": [{
            "span_uid": _ps(pdf1, "DOC_001", span),
            "page": 1,
            "start_char": span["start_char"],
            "end_char": span["end_char"],
            "quote": span["quote"],
            "disposition": "boundary",
            "boundary_uid": pb,
            "exclusion_reason": None,
        }],
    }


def build_clauses(pdf1, *, table_ref, binding_docs=("DOC_001", "DOC_002")):
    """A DOC_001 clause whose `reference_table_refs` point into DOC_002."""
    span = _span("DOC_001", 1, CLAUSE_TEXT)
    pb = _derived("boundary", pdf1, "DOC_001", [span])
    pc = _derived("clause", pdf1, "DOC_001", [span], parent=pb)
    # A `coverage` clause must carry at least one condition. Its span sits
    # inside the clause's own span, which is what the tightened CI containment
    # rule requires -- see test_canonical_uid_hierarchy.py.
    cond_span = _span("DOC_001", 1, "별표1의 지급률에 따라 보험금을 지급합니다.")
    condition = {
        "condition_uid": _derived(
            "condition", pdf1, "DOC_001", [cond_span], parent=pc),
        "source_span_uids": [cond_span],
        "text": cond_span["quote"],
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": cond_span["quote"]}],
        "support_level": "direct",
        "support_rationale": "verbatim from the cited page",
        "review_required": False,
    }
    return {
        "case_id": "CASE_030",
        "run_id": RUN,
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_001",
        "source_text_revision": _binding(*binding_docs),
        "clauses": [{
            "clause_uid": pc,
            "clause_id": "C-1",
            "source_boundary_uids": [pb],
            "source_span_uids": [span],
            "clause_kind": "coverage",
            "coverage_type": "장해보험금",
            "payout_conditions": [condition],
            "exclusions": [],
            "reduction_conditions": [],
            "definitions": [],
            "obligations": [],
            "claim_requirements": [],
            "termination_conditions": [],
            "dispute_resolution_conditions": [],
            "coverage_start_conditions": [],
            "reference_table_refs": [table_ref],
            "confidence": 0.9,
            "evidence_references": [{
                "document_id": "DOC_001", "page": 1, "quote": span["quote"]}],
            "review_required": False,
        }],
    }


# --- write-path drivers ----------------------------------------------------

def _write(isolated_dao, make_args, filename, schema_name, data,
           stage="policy_clause_processing"):
    data_file = isolated_dao / f"_w_{filename}"
    _write_json(data_file, data)
    return dao.cmd_write_contract(make_args(
        case_id="CASE_030", filename=filename, data_file=str(data_file),
        schema_name=schema_name, held_by="policy-pipeline", run_id=RUN,
        stage=stage))


def _write_tables(isolated_dao, make_args, data):
    return _write(isolated_dao, make_args, "reference_table_DOC_002.json",
                  _cross_contract.REFERENCE_TABLE_SCHEMA, data)


def _write_clauses(isolated_dao, make_args, data):
    return _write(
        isolated_dao, make_args, "normalized_policy_clause_DOC_001.json",
        _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA, data)


def _clause_file(isolated_dao):
    return (isolated_dao / "outputs" / "CASE_030" /
            "normalized_policy_clause_DOC_001.json")


def _assert_nothing_persisted(isolated_dao, before_state):
    """A refused write leaves neither the target nor the run-state behind."""
    assert not _clause_file(isolated_dao).exists()
    state = isolated_dao / "outputs" / "CASE_030" / "_run_state.json"
    after = state.read_text(encoding="utf-8") if state.exists() else None
    assert after == before_state


def _run_state(isolated_dao):
    state = isolated_dao / "outputs" / "CASE_030" / "_run_state.json"
    return state.read_text(encoding="utf-8") if state.exists() else None


# =========================================================================
# A. The referenced document is legacy
# =========================================================================

def test_a_clause_may_not_cite_a_table_in_a_legacy_document(
        legacy_owner, isolated_dao, make_args, capsys):
    """DOC_001 is canonical, DOC_002 is not -- the reference must be refused.

    Canonical status is per document. A verified document citing an unverified
    one inherits nothing: DOC_002's `RT-…` was never recomputed from anything,
    so treating DOC_001's own canonical status as covering it would let the
    weakest document in a case set the guarantee for all of them.
    """
    pdf1 = legacy_owner["DOC_001"]
    before = _run_state(isolated_dao)
    _write_json(dao.case_dir("CASE_030") /
                "policy_boundary_inventory_DOC_001.json",
                build_inventory(pdf1))

    data = build_clauses(pdf1, table_ref={
        "document_id": "DOC_002",
        "table_uid": "RT-" + "a" * 16,
        "row_uids": ["RR-" + "a" * 16],
    })
    assert _write_clauses(isolated_dao, make_args, data) == 1
    out = capsys.readouterr().out
    assert "DOC_002" in out
    assert "canonical_v1" in out
    _assert_nothing_persisted(isolated_dao, before)


def test_the_legacy_refusal_names_the_document_and_not_only_the_uid(
        legacy_owner, isolated_dao, make_args, capsys):
    """An operator has to know WHICH document to migrate."""
    pdf1 = legacy_owner["DOC_001"]
    _write_json(dao.case_dir("CASE_030") /
                "policy_boundary_inventory_DOC_001.json",
                build_inventory(pdf1))
    data = build_clauses(pdf1, table_ref={
        "document_id": "DOC_002", "table_uid": "RT-" + "a" * 16})
    assert _write_clauses(isolated_dao, make_args, data) == 1
    out = capsys.readouterr().out
    assert "DOC_002" in out


# =========================================================================
# B. Both documents canonical, but the RT is fabricated in both files
# =========================================================================

def test_a_fabricated_rt_agreed_by_both_documents_is_still_refused(
        two_documents, isolated_dao, make_args, capsys):
    """The consistency-is-not-provenance case, end to end.

    The reference table contract and the clause contract use the SAME made-up
    `RT-…`, so every string-matching link check resolves cleanly. What no
    amount of agreement can produce is a recomputation from DOC_002's
    registered bytes, which is what the DAO does instead.
    """
    pdf1, pdf2 = two_documents["DOC_001"], two_documents["DOC_002"]
    fake_rt = "RT-" + "b" * 16
    fake_rr = "RR-" + "b" * 16

    # The table file itself is refused first -- prove that, so the later
    # refusal cannot be mistaken for a missing-file error.
    assert _write_tables(isolated_dao, make_args,
                         build_table(pdf2, table_uid=fake_rt,
                                     row_uid=fake_rr)) == 1
    capsys.readouterr()

    # Persist the fabricated table past the write gate, the way a compromised
    # or pre-canonical artifact would already be sitting on disk, so the
    # clause write is tested against the strongest version of the attack.
    _write_json(dao.case_dir("CASE_030") / "reference_table_DOC_002.json",
                build_table(pdf2, table_uid=fake_rt, row_uid=fake_rr))
    _write_json(dao.case_dir("CASE_030") /
                "policy_boundary_inventory_DOC_001.json",
                build_inventory(pdf1))
    before = _run_state(isolated_dao)

    data = build_clauses(pdf1, table_ref={
        "document_id": "DOC_002", "table_uid": fake_rt,
        "row_uids": [fake_rr]})
    assert _write_clauses(isolated_dao, make_args, data) == 1
    out = capsys.readouterr().out
    assert fake_rt in out or "reference_table_DOC_002" in out
    _assert_nothing_persisted(isolated_dao, before)


def test_a_canonically_derived_cross_document_reference_is_accepted(
        two_documents, isolated_dao, make_args):
    """The refusals above must not have made legitimate references unwritable.

    Same shape as every attack in this module, with the one difference that
    the RT/RR really are DOC_002's -- so this is what separates "the gate
    works" from "the gate refuses everything".
    """
    pdf1, pdf2 = two_documents["DOC_001"], two_documents["DOC_002"]
    table = build_table(pdf2)
    assert _write_tables(isolated_dao, make_args, table) == 0

    _write_json(dao.case_dir("CASE_030") /
                "policy_boundary_inventory_DOC_001.json",
                build_inventory(pdf1))
    real = table["tables"][0]
    data = build_clauses(pdf1, table_ref={
        "document_id": "DOC_002",
        "table_uid": real["table_uid"],
        "row_uids": [real["rows"][0]["row_uid"]]})
    assert _write_clauses(isolated_dao, make_args, data) == 0
    assert _clause_file(isolated_dao).exists()


# =========================================================================
# C. The RT is genuine but the RR belongs to a different table
# =========================================================================

def test_a_row_uid_from_another_table_is_refused(
        two_documents, isolated_dao, make_args, capsys):
    """RR is parented on RT, so a real row of the wrong table is still wrong.

    The row UID recomputes perfectly -- in its own table. Membership, not
    derivability, is what fails, and only recomputing DOC_002 can tell the
    difference.
    """
    pdf1, pdf2 = two_documents["DOC_001"], two_documents["DOC_002"]
    table = build_table(pdf2)
    assert _write_tables(isolated_dao, make_args, table) == 0

    # A row genuinely derived from DOC_002 bytes, under a DIFFERENT parent RT.
    other_rt = _derived(
        "table", pdf2, "DOC_002", [_span("DOC_002", 1, "구분 지급률")])
    foreign_row = _derived(
        "row", pdf2, "DOC_002", [_span("DOC_002", 1, ROW_TEXT)],
        parent=other_rt)
    assert foreign_row != table["tables"][0]["rows"][0]["row_uid"]

    _write_json(dao.case_dir("CASE_030") /
                "policy_boundary_inventory_DOC_001.json",
                build_inventory(pdf1))
    before = _run_state(isolated_dao)
    data = build_clauses(pdf1, table_ref={
        "document_id": "DOC_002",
        "table_uid": table["tables"][0]["table_uid"],
        "row_uids": [foreign_row]})
    assert _write_clauses(isolated_dao, make_args, data) == 1
    out = capsys.readouterr().out
    assert foreign_row in out or "row_uid" in out
    _assert_nothing_persisted(isolated_dao, before)


# =========================================================================
# D. Parent coverage: the RT must be recomputed in its DECLARED owner
# =========================================================================

def _coverage(table_uid, owner):
    page = {
        "logical_page": 1,
        "physical_page": 1,
        "disposition": "reference_table",
        "owner_document_id": None,
        "table_uid": table_uid,
        "reason": None,
        "evidence_references": [],
    }
    if owner is not None:
        page["reference_table_document_id"] = owner
    return {"pages": [page]}


def test_parent_coverage_recomputes_the_table_in_its_owner_document(
        two_documents, isolated_dao, make_args):
    pdf2 = two_documents["DOC_002"]
    table = build_table(pdf2)
    assert _write_tables(isolated_dao, make_args, table) == 0
    rt = table["tables"][0]["table_uid"]
    assert dao._canonical_parent_table_reference_errors(
        "CASE_030", _coverage(rt, "DOC_002")) == []


def test_parent_coverage_rejects_a_table_attributed_to_the_wrong_owner(
        two_documents, isolated_dao, make_args):
    """A REAL table, re-labelled as another document's.

    Nothing about the UID is fabricated -- it is DOC_002's genuine RT. The lie
    is `reference_table_document_id`, and it is only detectable by recomputing
    in the document actually named, which is why the owner field cannot be
    taken as a hint.
    """
    pdf2 = two_documents["DOC_002"]
    table = build_table(pdf2)
    assert _write_tables(isolated_dao, make_args, table) == 0
    rt = table["tables"][0]["table_uid"]

    errors = dao._canonical_parent_table_reference_errors(
        "CASE_030", _coverage(rt, "DOC_001"))
    assert errors
    assert any("DOC_001" in e for e in errors), errors


def test_parent_coverage_rejects_a_table_with_no_declared_owner(
        two_documents, isolated_dao, make_args):
    """No owner means no document to recompute in -- refuse, do not guess.

    Searching every document for a matching RT would make the check pass on
    whichever file happened to contain the string, which is the consistency
    trap again in a new place.
    """
    pdf2 = two_documents["DOC_002"]
    table = build_table(pdf2)
    assert _write_tables(isolated_dao, make_args, table) == 0
    rt = table["tables"][0]["table_uid"]

    errors = dao._canonical_parent_table_reference_errors(
        "CASE_030", _coverage(rt, None))
    assert errors
    assert any("reference_table_document_id" in e for e in errors), errors


def test_parent_coverage_rejects_a_table_owned_by_a_legacy_document(
        legacy_owner, isolated_dao, make_args):
    errors = dao._canonical_parent_table_reference_errors(
        "CASE_030", _coverage("RT-" + "c" * 16, "DOC_002"))
    assert errors
    assert any("canonical_v1" in e for e in errors), errors


# =========================================================================
# E. Source-revision binding spans every referenced document
# =========================================================================

def test_a_cross_document_clause_must_bind_both_documents_revisions(
        two_documents, isolated_dao, make_args, capsys):
    """Binding DOC_001 alone is not enough once DOC_002's bytes matter.

    The clause's UIDs are computed against DOC_001's revision AND DOC_002's --
    the RT it cites is only meaningful relative to the appendix text in force.
    A binding that omits DOC_002 leaves half the derivation unpinned.
    """
    pdf1, pdf2 = two_documents["DOC_001"], two_documents["DOC_002"]
    table = build_table(pdf2)
    assert _write_tables(isolated_dao, make_args, table) == 0
    _write_json(dao.case_dir("CASE_030") /
                "policy_boundary_inventory_DOC_001.json",
                build_inventory(pdf1))
    before = _run_state(isolated_dao)

    real = table["tables"][0]
    data = build_clauses(
        pdf1,
        table_ref={"document_id": "DOC_002",
                   "table_uid": real["table_uid"],
                   "row_uids": [real["rows"][0]["row_uid"]]},
        binding_docs=("DOC_001",))
    assert _write_clauses(isolated_dao, make_args, data) == 1
    assert "DOC_002" in capsys.readouterr().out
    _assert_nothing_persisted(isolated_dao, before)


def test_the_binding_lists_every_referenced_document(two_documents):
    """Stated against the helper the write path uses to build the requirement,
    so the rule holds even where the write path is not the caller."""
    pdf1 = two_documents["DOC_001"]
    data = build_clauses(pdf1, table_ref={
        "document_id": "DOC_002", "table_uid": "RT-" + "a" * 16})
    referenced = dao._referenced_policy_documents(
        "CASE_030", "normalized_policy_clause_DOC_001.json", data)
    assert referenced == ["DOC_001", "DOC_002"]


def test_a_new_doc2_revision_makes_the_existing_clause_binding_stale(
        two_documents, isolated_dao, make_args, capsys):
    """Revising the appendix invalidates clauses that cite it.

    This is the property the binding exists for: after DOC_002's text changes,
    a clause still claiming the old appendix revision is asserting a link to
    bytes that are no longer in force, so the write must be refused rather
    than quietly re-pointed at the new ones.
    """
    pdf1, pdf2 = two_documents["DOC_001"], two_documents["DOC_002"]
    table = build_table(pdf2)
    assert _write_tables(isolated_dao, make_args, table) == 0
    _write_json(dao.case_dir("CASE_030") /
                "policy_boundary_inventory_DOC_001.json",
                build_inventory(pdf1))

    real = table["tables"][0]
    ref = {"document_id": "DOC_002",
           "table_uid": real["table_uid"],
           "row_uids": [real["rows"][0]["row_uid"]]}
    stale = build_clauses(pdf1, table_ref=ref)

    # Register a corrected DOC_002 through the DAO, so the pointer and the
    # bytes it names stay consistent.
    corrected = isolated_dao / "doc2_v2.md"
    corrected.write_text(DOC2_TEXT.replace("50%", "60%"), encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_002", text_file=str(corrected),
        held_by="document-pipeline", run_id=RUN)) == 0
    before = _run_state(isolated_dao)

    assert _write_clauses(isolated_dao, make_args, stale) == 1
    out = capsys.readouterr().out
    assert "DOC_002" in out
    _assert_nothing_persisted(isolated_dao, before)


def test_finalization_rechecks_cross_document_references(
        two_documents, isolated_dao, make_args, capsys):
    """A contract that was valid at write time is re-verified at finalization.

    Otherwise a legitimate write followed by a DOC_002 revision would leave a
    stage able to pass on a reference that no longer resolves.
    """
    pdf1, pdf2 = two_documents["DOC_001"], two_documents["DOC_002"]
    table = build_table(pdf2)
    assert _write_tables(isolated_dao, make_args, table) == 0
    _write_json(dao.case_dir("CASE_030") /
                "policy_boundary_inventory_DOC_001.json",
                build_inventory(pdf1))
    real = table["tables"][0]
    data = build_clauses(pdf1, table_ref={
        "document_id": "DOC_002",
        "table_uid": real["table_uid"],
        "row_uids": [real["rows"][0]["row_uid"]]})
    assert _write_clauses(isolated_dao, make_args, data) == 0
    capsys.readouterr()

    # Now corrupt the table file on disk, the way a later bad write would.
    _write_json(dao.case_dir("CASE_030") / "reference_table_DOC_002.json",
                build_table(pdf2, table_uid="RT-" + "d" * 16,
                            row_uid="RR-" + "d" * 16))
    errors = dao._canonical_uid_errors(
        "CASE_030", "normalized_policy_clause_DOC_001.json",
        _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
        dao.read_contract_data(
            "CASE_030", "normalized_policy_clause_DOC_001.json"))
    assert errors, "a broken cross-document link must not survive re-checking"


# =========================================================================
# The independent oracle
# =========================================================================

def test_manually_specified_rt_and_rr_match_the_documented_serialization(
        two_documents):
    """RT and RR recomputed by hand, not by calling the production helper.

    Everything above builds its expectations with `policy_uid.compute_uid`, so
    a wrong derivation would agree with itself. This recomputes the same two
    UIDs straight from the documented rule --

        canonical_v1 \\0 kind \\0 source_pdf_sha256 \\0 physical_page \\0
        NFC(span text) \\0 ordinal \\0 parent_uid \\0 column_key

    -- truncated to 16 hex chars, with a multi-span object's "span text" being
    its child span UIDs joined by \\x1f. If the implementation ever drifts from
    the spec, these two disagree.
    """
    pdf2 = two_documents["DOC_002"]

    def sha(*parts):
        return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()

    def nfc(value):
        return unicodedata.normalize("NFC", value).replace("\r\n", "\n")

    # PS of the table's one source region, then RT over that PS.
    ps_region = "PS-" + sha(
        "canonical_v1", "span", pdf2, "1", nfc(TABLE_REGION), "1", "", "")[:16]
    rt = "RT-" + sha(
        "canonical_v1", "table", pdf2, "1", ps_region, "1", "", "")[:16]

    # PS of the row's span, then RR under that RT.
    ps_row = "PS-" + sha(
        "canonical_v1", "span", pdf2, "1", nfc(ROW_TEXT), "1", "", "")[:16]
    rr = "RR-" + sha(
        "canonical_v1", "row", pdf2, "1", ps_row, "1", rt, "")[:16]

    table = build_table(pdf2)["tables"][0]
    assert table["table_uid"] == rt
    assert table["rows"][0]["row_uid"] == rr

    # And the cell, which is the one kind carrying column_key.
    ps_cell = "PS-" + sha(
        "canonical_v1", "span", pdf2, "1", nfc("50%"), "1", "", "")[:16]
    rc = "RC-" + sha(
        "canonical_v1", "cell", pdf2, "1", ps_cell, "1", rr, "rate")[:16]
    assert table["rows"][0]["cells"][1]["cell_uid"] == rc
