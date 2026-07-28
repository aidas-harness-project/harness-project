"""Part 2: segment lineage + manifest/classification consistency.

Two layers:
  1. Pure segment_lineage.py checks (non-contiguous ranges, self-parent, cycle,
     marker/page mismatch, unregistered source, classification mismatch).
  2. DAO integration -- the manifest write/patch refuses bad lineage, a
     normalized_policy_clause write refuses an unregistered source, and
     document_processing finalize refuses a classification/manifest mismatch.
"""
import json
import hashlib

import pytest

import dao
import segment_lineage as sl


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

# DOC_004 is the real CASE_030 shape: non-contiguous pages 57-58 and 118-119.
DOC004_MARKERS = "<<<PAGE page=57>>>\na\n<<<PAGE page=58>>>\nb\n<<<PAGE page=118>>>\nc\n<<<PAGE page=119>>>\nd\n"


def _physical(did="DOC_001"):
    return {
        "document_id": did, "file_name": f"{did}.pdf", "document_role": "physical",
        "file_path": f"data/raw/CASE_030/{did}.pdf", "file_format": "pdf",
        "file_size_bytes": 100, "ocr_status": "completed",
        "source_total_pages": 249,
        "downstream_disposition": "automated_text_pipeline",
    }


def _segment(did="DOC_004", parent="DOC_001",
             ranges=({"start": 57, "end": 58}, {"start": 118, "end": 119}),
             logical=(57, 58, 118, 119)):
    text = DOC004_MARKERS
    return {
        "document_id": did, "file_name": did, "document_role": "segment",
        "source_document_id": parent, "derivation_method": "embedded_text_segment",
        "derived_text_path": f"data/processed/CASE_030/{did}/redacted_text.md",
        "derived_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "segment_page_ranges": [dict(r) for r in ranges],
        "page_map": [
            {"logical_page": p, "source_physical_page": p}
            for p in logical
        ],
        "file_path": None, "file_format": None, "file_size_bytes": None,
        "ocr_status": "not_applicable", "downstream_disposition": "automated_text_pipeline",
    }


def _manifest(*docs):
    return {"case_id": "CASE_030", "documents": list(docs)}


def _reader(mapping):
    return lambda did: mapping.get(did)


# --------------------------------------------------------------------------
# Pure lineage checks
# --------------------------------------------------------------------------

def test_noncontiguous_segment_ok():
    man = _manifest(_physical(), _segment())
    errors = sl.validate_segment_lineage(man, _reader({"DOC_004": DOC004_MARKERS}))
    assert errors == []


def test_single_wide_range_for_noncontiguous_is_rejected():
    # A single 57-119 range instead of the real {57-58, 118-119}: its flattened
    # pages won't match the page_map/markers, and it's caught.
    seg = _segment(ranges=({"start": 57, "end": 119},), logical=(57, 58, 118, 119))
    man = _manifest(_physical(), seg)
    errors = sl.validate_segment_lineage(man, _reader({"DOC_004": DOC004_MARKERS}))
    assert any("do not match" in e for e in errors)


def test_missing_parent_rejected():
    man = _manifest(_segment(parent="DOC_099"))  # no DOC_099 in manifest
    errors = sl.validate_segment_lineage(man, _reader({"DOC_004": DOC004_MARKERS}))
    assert any("not a document in this manifest" in e for e in errors)


def test_self_parent_rejected():
    seg = _segment(did="DOC_004", parent="DOC_004")
    man = _manifest(_physical(), seg)
    errors = sl.validate_segment_lineage(man, _reader({"DOC_004": DOC004_MARKERS}))
    assert any("itself" in e for e in errors)


def test_cycle_rejected():
    a = _segment(did="DOC_004", parent="DOC_005")
    b = _segment(did="DOC_005", parent="DOC_004")
    man = _manifest(a, b)
    errors = sl.validate_segment_lineage(man, _reader({"DOC_004": DOC004_MARKERS, "DOC_005": DOC004_MARKERS}))
    assert any("cycle" in e for e in errors)


def test_marker_page_mismatch_rejected():
    # Declared pages 57/58/118/119 but the processed text markers say 57/58 only.
    man = _manifest(_physical(), _segment())
    short = "<<<PAGE page=57>>>\na\n<<<PAGE page=58>>>\nb\n"
    errors = sl.validate_segment_lineage(man, _reader({"DOC_004": short}))
    assert any("do not match the segment's redacted-text markers" in e for e in errors)


def test_changed_segment_digest_rejected():
    man = _manifest(_physical(), _segment())
    changed = DOC004_MARKERS + "changed\n"
    errors = sl.validate_segment_lineage(
        man, _reader({"DOC_004": changed}))
    assert any("derived_text_sha256" in e for e in errors)


def test_missing_source_physical_page_rejected():
    seg = _segment()
    del seg["page_map"][0]["source_physical_page"]
    man = _manifest(_physical(), seg)
    errors = sl.validate_segment_lineage(
        man, _reader({"DOC_004": DOC004_MARKERS}))
    assert any("source_physical_page" in e for e in errors)


def test_parent_requires_verified_source_total_pages():
    parent = _physical()
    del parent["source_total_pages"]
    man = _manifest(parent, _segment())
    errors = sl.validate_segment_lineage(
        man, _reader({"DOC_004": DOC004_MARKERS}))
    assert any("no verified source_total_pages" in e for e in errors), errors


def test_segment_physical_page_cannot_exceed_parent_total():
    parent = _physical()
    parent["source_total_pages"] = 100
    man = _manifest(parent, _segment())
    errors = sl.validate_segment_lineage(
        man, _reader({"DOC_004": DOC004_MARKERS}))
    assert any("exceeds parent" in e for e in errors), errors


def test_missing_derived_text_rejected():
    man = _manifest(_physical(), _segment())
    errors = sl.validate_segment_lineage(man, _reader({}))  # no text for DOC_004
    assert any("no processed/derived text" in e for e in errors)


def test_parent_of_segment_may_not_be_a_segment():
    parent_seg = _segment(did="DOC_005", parent="DOC_001")
    child_seg = _segment(did="DOC_004", parent="DOC_005")
    man = _manifest(_physical(), parent_seg, child_seg)
    errors = sl.validate_segment_lineage(
        man, _reader({"DOC_004": DOC004_MARKERS, "DOC_005": DOC004_MARKERS}))
    assert any("parent" in e and "segment" in e for e in errors)


# --------------------------------------------------------------------------
# registration + classification consistency
# --------------------------------------------------------------------------

def test_is_registered_automated_source():
    man = _manifest(_physical("DOC_001"), _segment())
    assert sl.is_registered_automated_source(man, "DOC_001")
    assert sl.is_registered_automated_source(man, "DOC_004")
    assert not sl.is_registered_automated_source(man, "DOC_099")  # absent


def test_unregistered_source_not_automated():
    doc = _physical("DOC_010")
    doc["downstream_disposition"] = "expert_review_only"
    man = _manifest(doc)
    assert not sl.is_registered_automated_source(man, "DOC_010")


def test_classification_mismatch_detected():
    doc = _physical("DOC_002")
    doc["document_type"] = "insurance_policy"
    man = _manifest(doc)
    cls = {"DOC_002": {"predicted_document_type": "insurance_certificate"}}
    errors = sl.check_classification_manifest_consistency(man, _reader(cls))
    assert any("disagrees" in e for e in errors)


def test_classification_null_manifest_type_flagged():
    doc = _physical("DOC_002")
    doc["document_type"] = None
    man = _manifest(doc)
    cls = {"DOC_002": {"predicted_document_type": "insurance_certificate"}}
    errors = sl.check_classification_manifest_consistency(man, _reader(cls))
    assert any("never synced" in e for e in errors)


def test_classification_match_clean():
    doc = _physical("DOC_002")
    doc["document_type"] = "insurance_certificate"
    man = _manifest(doc)
    cls = {"DOC_002": {"predicted_document_type": "insurance_certificate"}}
    assert sl.check_classification_manifest_consistency(man, _reader(cls)) == []


def test_missing_physical_classification_blocks_complete_check():
    doc = _physical("DOC_002")
    doc["document_type"] = "insurance_certificate"
    man = _manifest(doc)
    errors = sl.check_classification_manifest_consistency(
        man, _reader({}), require_complete=True)
    assert any("no classification_result" in e for e in errors)


def test_segment_type_must_match_parent():
    parent = _physical()
    parent["document_type"] = "insurance_policy"
    seg = _segment()
    seg["document_type"] = "application_form"
    man = _manifest(parent, seg)
    errors = sl.check_classification_manifest_consistency(
        man, _reader({}), require_complete=True)
    assert any("does not match parent" in e for e in errors)


# --------------------------------------------------------------------------
# DAO integration
# --------------------------------------------------------------------------

def _seed_processed(isolated_dao, doc_id, text):
    d = isolated_dao / "data" / "processed" / "CASE_030" / doc_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "redacted_text.md").write_text(text, encoding="utf-8")


def _write_manifest(isolated_dao, make_args, manifest):
    data_file = isolated_dao / "man.json"
    data_file.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    args = make_args(case_id="CASE_030", filename="document_manifest.json",
                     data_file=str(data_file), schema_name="document_manifest.schema.json",
                     held_by="document-pipeline", run_id="RUN_20260723_001")
    return dao.cmd_write_contract(args)


def test_dao_manifest_write_rejects_bad_lineage(isolated_dao, make_args):
    _seed_processed(isolated_dao, "DOC_004", DOC004_MARKERS)
    # A single wide range: markers won't match.
    seg = _segment(ranges=({"start": 57, "end": 119},), logical=(57, 58, 118, 119))
    rc = _write_manifest(isolated_dao, make_args, _manifest(_physical(), seg))
    assert rc == 1
    assert not (isolated_dao / "outputs" / "CASE_030" / "document_manifest.json").exists()


def test_dao_manifest_write_cannot_introduce_a_page_map(isolated_dao, make_args):
    """P0-6: `page_map` is DAO-owned, so even a perfectly valid one is refused
    when it arrives through write-contract.

    This used to be `test_dao_manifest_write_accepts_valid_segment`, asserting
    that a hand-written page_map with correct lineage was accepted. That
    acceptance WAS the vulnerability: every value in the map came from the
    caller, and the checks it passed only compared those values to each other.
    The mapping now has exactly one writer,
    `dao.py register-segment-derivation`, which derives it from the parent PDF.
    """
    _seed_processed(isolated_dao, "DOC_004", DOC004_MARKERS)
    rc = _write_manifest(isolated_dao, make_args, _manifest(_physical(), _segment()))
    assert rc == 1
    assert not (isolated_dao / "outputs" / "CASE_030" / "document_manifest.json").exists()


def test_dao_manifest_write_accepts_a_dao_issued_page_map(
        isolated_dao, make_args, segment_pdf, register_derivation):
    """The lineage path still works end to end -- but the page_map has to have
    been issued by the DAO from the real parent PDF first."""
    raw = isolated_dao / "data" / "raw" / "CASE_030"
    raw.mkdir(parents=True)
    segment_pdf(raw / "DOC_001.pdf", total_logical=8, offset=2)

    parent = _physical()
    parent["source_total_pages"] = 10
    # Register the parent and the segment with NO page_map: the DAO issues it.
    seg_text = (
        "<<<PAGE page=1>>>\nClause 1 body\n1 / 8\n\n"
        "<<<PAGE page=2>>>\nClause 2 body\n2 / 8\n\n")
    _seed_processed(isolated_dao, "DOC_004", seg_text)
    seg = _segment(ranges=({"start": 1, "end": 2},), logical=(1, 2))
    seg["derived_text_sha256"] = hashlib.sha256(
        seg_text.encode("utf-8")).hexdigest()
    seg.pop("page_map")
    bootstrap = _manifest(parent, seg)
    # A segment entry needs a page_map to be schema-valid, so the manifest is
    # seeded directly here -- a fixture-only shortcut standing in for the
    # intake/extraction step, NOT a production write path (which is exactly why
    # write-contract refuses it above).
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True, exist_ok=True)
    seg["page_map"] = [{"logical_page": 1, "source_physical_page": 3},
                       {"logical_page": 2, "source_physical_page": 4}]
    (out / "document_manifest.json").write_text(
        json.dumps(bootstrap, ensure_ascii=False), encoding="utf-8")

    text_file = isolated_dao / "seg.md"
    text_file.write_text(seg_text, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_004", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260723_001")) == 0

    assert register_derivation(
        make_args, "CASE_030", "DOC_004", "1,2", 2,
        run_id="RUN_20260723_001") == 0

    manifest = dao.read_contract_data("CASE_030", "document_manifest.json")
    entry = next(d for d in manifest["documents"] if d["document_id"] == "DOC_004")
    assert [e["source_physical_page"] for e in entry["page_map"]] == [3, 4]
    assert all(e["logical_page_evidence"] for e in entry["page_map"])


def test_normalized_output_for_unregistered_doc_refused(isolated_dao, make_args):
    """A normalized_policy_clause file citing a DOC absent from the manifest is
    refused -- the exact CASE_030 hole (DOC_004/005/006 cited, never registered)."""
    # Manifest registers only DOC_001; the clause file is for DOC_004.
    _write_manifest(isolated_dao, make_args, _manifest(_physical("DOC_001")))
    _seed_processed(isolated_dao, "DOC_004", "<<<PAGE page=57>>>\n회사는 보험금을 지급합니다\n")
    contract = {
        "case_id": "CASE_030", "run_id": "RUN_20260723_001",
        "component": "policy-pipeline", "status": "success",
        "clauses": [{
            "clause_uid": "PC-1111111111111111",
            "clause_id": "C-1",
            "source_boundary_uids": ["PB-1111111111111111"],
            "clause_kind": "other", "coverage_type": "x",
            "payout_conditions": [], "exclusions": [], "reduction_conditions": [],
            "definitions": [], "obligations": [], "claim_requirements": [],
            "termination_conditions": [],
            "dispute_resolution_conditions": [],
            "coverage_start_conditions": [],
            "reference_table_refs": [],
            "confidence": 0.9, "review_required": True,
            "evidence_references": [{"document_id": "DOC_004", "page": 57, "quote": "회사는 보험금을 지급합니다"}],
        }],
    }
    data_file = isolated_dao / "c.json"
    data_file.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
    args = make_args(case_id="CASE_030", filename="normalized_policy_clause_DOC_004.json",
                     data_file=str(data_file), schema_name="normalized_policy_clause.schema.json",
                     held_by="policy-pipeline", run_id="RUN_20260723_001", stage="policy_clause_processing")
    rc = dao.cmd_write_contract(args)
    assert rc == 1, "a clause file for an unregistered DOC must be refused"
    assert not (isolated_dao / "outputs" / "CASE_030" / "normalized_policy_clause_DOC_004.json").exists()


def test_document_processing_finalize_refused_on_classification_mismatch(isolated_dao, make_args, run_id):
    # Manifest says DOC_002 is insurance_policy; classification says certificate.
    doc = _physical("DOC_002")
    doc["document_type"] = "insurance_policy"
    _write_manifest(isolated_dao, make_args, _manifest(doc))
    cls = {
        "case_id": "CASE_030", "component": "document-pipeline", "status": "success",
        "document_id": "DOC_002", "predicted_document_type": "insurance_certificate",
        "confidence": 0.99, "review_required": False,
        "evidence_references": [{"quote": "증권"}],
    }
    (isolated_dao / "outputs" / "CASE_030" / "classification_result_DOC_002.json").write_text(
        json.dumps(cls, ensure_ascii=False), encoding="utf-8")

    rc = dao.cmd_snapshot_backup(make_args(case_id="CASE_030", run_id=run_id, stage="document_processing"))

    assert rc == 1, "document_processing must not finalize while manifest/classification disagree"
    state = dao.load_run_state("CASE_030")
    passed = [s for s in state["stages"] if s["status"] == "passed"]
    assert passed == []


def test_document_processing_finalize_ok_when_consistent(isolated_dao, make_args, run_id):
    doc = _physical("DOC_002")
    doc["document_type"] = "insurance_certificate"
    _write_manifest(isolated_dao, make_args, _manifest(doc))
    cls = {
        "case_id": "CASE_030", "component": "document-pipeline", "status": "success",
        "document_id": "DOC_002", "predicted_document_type": "insurance_certificate",
        "confidence": 0.99, "review_required": False,
        "evidence_references": [{"quote": "증권"}],
    }
    (isolated_dao / "outputs" / "CASE_030" / "classification_result_DOC_002.json").write_text(
        json.dumps(cls, ensure_ascii=False), encoding="utf-8")

    rc = dao.cmd_snapshot_backup(make_args(case_id="CASE_030", run_id=run_id, stage="document_processing"))

    assert rc == 0
    state = dao.load_run_state("CASE_030")
    assert next(s for s in state["stages"] if s["stage_name"] == "document_processing")["status"] == "passed"


def test_partial_manifest_not_written_on_lineage_failure(isolated_dao, make_args):
    """A lineage failure during a manifest patch must not leave a half-updated
    manifest -- the write is refused whole."""
    _seed_processed(isolated_dao, "DOC_004", DOC004_MARKERS)
    good = _manifest(_physical(), _segment())
    # Seeded directly rather than through write-contract: `page_map` is
    # DAO-owned since P0-6, so no caller-facing write path can create this
    # starting state. A fixture shortcut to reach the state under test, not a
    # production path.
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True, exist_ok=True)
    (out / "document_manifest.json").write_text(
        json.dumps(good, ensure_ascii=False), encoding="utf-8")
    before = (isolated_dao / "outputs" / "CASE_030" / "document_manifest.json").read_bytes()

    # Patch the segment to a self-parent -> lineage failure.
    fields_file = isolated_dao / "f.json"
    fields_file.write_text(json.dumps({"source_document_id": "DOC_004"}), encoding="utf-8")
    ok, msg = dao.patch_manifest_document("CASE_030", "DOC_004", {"source_document_id": "DOC_004"},
                                          "tester", "RUN_20260723_001")
    assert not ok
    after = (isolated_dao / "outputs" / "CASE_030" / "document_manifest.json").read_bytes()
    assert before == after, "a lineage-failing patch must not modify the manifest"
