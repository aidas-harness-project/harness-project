"""P0-6: a segment's page map is bound to the parent PDF, not self-declared.

Before this, `segment_lineage.validate_segment_lineage` checked a segment's
page_map against its `segment_page_ranges`, its `<<<PAGE page=N>>>` markers and
its `derived_text_sha256` -- four values the extracting agent produced. Nothing
compared any of them to the parent PDF. So the attack was not to break a check;
it was to make every check agree:

    truth:     logical 12 is on parent physical page 19
    submitted: logical 12 -> physical 20, markers/ranges/digests all rewritten
               to match, `physical_page_sha256` copied from page 20

Section 3 below runs exactly that. It is the test that matters most in this
file, because it is the one the pre-P0-6 code passed.

Every test here runs against a REAL PDF rendered by pymupdf, with printed page
numbers genuinely drawn on the pages, and calls the real DAO commands. Stubbing
the parent read would test the arithmetic while skipping the only thing P0-6
adds: that the DAO opens the registered file itself.

Fixture note: several tests seed `outputs/`/`_segment_derivation_index.json`
by direct file write to construct a corrupt or legacy state. That is
deliberate, and it is deliberately NOT a production path -- `page_map` and the
receipt index are sealed against every caller-facing DAO write, so these states
cannot be reached by any command. Writing them directly is how a test can ask
"if this state existed anyway, is it caught?"
"""
import hashlib
import json
import unicodedata
from pathlib import Path

import pytest

import dao
import dao_transaction
import segment_derivation as sd
import source_provenance


CASE = "CASE_030"
RUN = "RUN_20260728_001"
OFFSET = 7          # CASE_030's real front-matter offset
TOTAL_LOGICAL = 30  # -> 37 physical pages


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


# --------------------------------------------------------------------------
# Case construction
# --------------------------------------------------------------------------

def _physical_entry(doc_id="DOC_001", total_pages=TOTAL_LOGICAL + OFFSET):
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


def _segment_entry(doc_id, parent, logical_pages, ranges,
                   page_map=None, derivation_method="embedded_text_segment"):
    return {
        "document_id": doc_id,
        "file_name": doc_id,
        "document_role": "segment",
        "source_document_id": parent,
        "derivation_method": derivation_method,
        "derived_text_path": f"data/processed/{CASE}/{doc_id}/redacted_text.md",
        "derived_text_sha256": "0" * 64,   # replaced by _seed_segment_text
        "segment_page_ranges": [dict(r) for r in ranges],
        # A schema-valid placeholder so the entry can exist before the DAO
        # issues the real map. It carries no evidence fields, which is exactly
        # the legacy shape P0-6 must refuse at finalization.
        "page_map": page_map if page_map is not None else [
            {"logical_page": lp, "source_physical_page": lp + OFFSET}
            for lp in logical_pages
        ],
        "file_path": None, "file_format": None, "file_size_bytes": None,
        "ocr_status": "not_applicable",
        "extraction_method": "embedded_text",
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
    }


def _seed_manifest(isolated_dao, documents):
    out = isolated_dao / "outputs" / CASE
    out.mkdir(parents=True, exist_ok=True)
    (out / "document_manifest.json").write_text(
        json.dumps({"case_id": CASE, "documents": documents},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return out / "document_manifest.json"


def _segment_text(logical_pages, body_for=None):
    body_for = body_for or (
        lambda lp: f"Clause {lp} body\n{lp} / {TOTAL_LOGICAL}\n")
    # Mirrors extract_embedded_segment.py exactly: complete page texts are
    # joined with one assembly newline, which is not part of either page.
    return "\n".join(
        f"<<<PAGE page={lp}>>>\n{body_for(lp)}" for lp in logical_pages) + "\n"


def _seed_segment_text(isolated_dao, make_args, doc_id, text):
    """Register the segment's source text through the real DAO revision path,
    and sync `derived_text_sha256` so lineage is clean."""
    path = isolated_dao / f"_seg_{doc_id}.md"
    path.write_text(text, encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id=CASE, doc_id=doc_id, text_file=str(path),
        held_by="document-pipeline", run_id=RUN)) == 0
    _patch_entry(doc_id, {
        "derived_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})


def _patch_entry(doc_id, fields):
    """Direct manifest edit -- fixture plumbing for fields P0-6 does not seal.

    Not a production path (production uses `dao.py patch-manifest-document`);
    used here only to keep unrelated fixture fields in sync without dragging
    every test through the full pipeline.
    """
    path = dao.case_dir(CASE) / "document_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entry = next(d for d in manifest["documents"] if d["document_id"] == doc_id)
    entry.update(fields)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8")


@pytest.fixture
def case(isolated_dao, segment_pdf, make_args):
    """CASE_030's real shape: a policy PDF with `OFFSET` pages of unnumbered
    front matter, so logical N is printed on physical N+OFFSET, and one segment
    (DOC_004) carved from logical pages 12-13."""
    raw = isolated_dao / "data" / "raw" / CASE
    raw.mkdir(parents=True)
    segment_pdf(raw / "DOC_001.pdf",
                total_logical=TOTAL_LOGICAL, offset=OFFSET)

    logical = [12, 13]
    _seed_manifest(isolated_dao, [
        _physical_entry(),
        _segment_entry("DOC_004", "DOC_001", logical,
                       [{"start": 12, "end": 13}]),
    ])
    _seed_segment_text(isolated_dao, make_args, "DOC_004",
                       _segment_text(logical))
    return isolated_dao


def _receipt(doc_id="DOC_004"):
    return dao.segment_derivation_receipt_for(CASE, doc_id)


def _page_map(doc_id="DOC_004"):
    manifest = dao.read_contract_data(CASE, "document_manifest.json")
    entry = next(d for d in manifest["documents"]
                 if d["document_id"] == doc_id)
    return entry.get("page_map")


# ==========================================================================
# 1. The honest mapping is derived, recorded, and bound
# ==========================================================================

def test_correct_mapping_is_derived_from_the_parent_pdf(
        case, make_args, register_derivation):
    """logical 12 -> physical 19, established by reading page 19 and finding
    '12 / 30' printed on it -- not by trusting the offset."""
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0

    receipt = _receipt()
    assert receipt["scheme"] == sd.RECEIPT_SCHEME
    assert receipt["parent_source_document_id"] == "DOC_001"
    assert [p["source_physical_page"] for p in receipt["pages"]] == [19, 20]
    assert [p["logical_page"] for p in receipt["pages"]] == [12, 13]
    # The evidence is the marker the DAO actually read off the page.
    evidence = receipt["pages"][0]["logical_page_evidence"]
    assert evidence["quote"] == "12 / 30"
    assert evidence["region"] == "footer"
    assert evidence["evidence_profile"] == "header_footer_blocks_v1"
    # Bound to the parent bytes and to the segment's registered revision.
    assert receipt["parent_source_pdf_sha256"] == \
        dao.registered_source_pdf_sha256(CASE, "DOC_001")
    assert receipt["segment_source_text_revision_sha256"] == \
        dao.revision_entry_for(CASE, "DOC_004")["current_revision_sha256"]

    # The manifest carries the receipt's projection, nothing else.
    assert _page_map() == sd.manifest_page_map_from_receipt(receipt)
    assert all(e["physical_page_sha256"] and e["logical_page_evidence"]
               for e in _page_map())


def test_finalization_passes_once_the_mapping_is_registered(
        case, make_args, register_derivation, isolated_dao):
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    _seed_classification(isolated_dao, "DOC_001")

    rc = dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline"))

    assert rc == 0, "a fully derived mapping must not block finalization"


def test_correct_markers_with_fabricated_segment_bodies_are_refused(
        case, isolated_dao, make_args, register_derivation, capsys):
    """A revision hash proves persistence, not parent-page derivation."""
    fabricated = _segment_text(
        [12, 13],
        body_for=lambda lp: (
            f"Fabricated coverage obligation for {lp}\n"
            f"{lp} / {TOTAL_LOGICAL}\n"))
    _seed_segment_text(
        isolated_dao, make_args, "DOC_004", fabricated)

    rc = register_derivation(
        make_args, CASE, "DOC_004", "12,13", OFFSET)

    assert rc == 1
    assert "does not exactly equal parent physical page" in \
        capsys.readouterr().out
    assert _receipt() is None


def test_one_character_segment_body_change_is_refused(
        case, isolated_dao, make_args, register_derivation, capsys):
    changed = _segment_text(
        [12, 13],
        body_for=lambda lp: (
            f"Clause {lp} bodX\n{lp} / {TOTAL_LOGICAL}\n"
            if lp == 12 else
            f"Clause {lp} body\n{lp} / {TOTAL_LOGICAL}\n"))
    _seed_segment_text(isolated_dao, make_args, "DOC_004", changed)

    assert register_derivation(
        make_args, CASE, "DOC_004", "12,13", OFFSET) == 1
    assert "does not exactly equal" in capsys.readouterr().out
    assert _receipt() is None


def _seed_classification(isolated_dao, doc_id, doc_type="insurance_policy"):
    (isolated_dao / "outputs" / CASE / f"classification_result_{doc_id}.json"
     ).write_text(json.dumps({
         "case_id": CASE, "component": "document-pipeline",
         "status": "success", "document_id": doc_id,
         "predicted_document_type": doc_type, "confidence": 0.99,
         "review_required": False,
         "evidence_references": [{"quote": "약관"}],
     }, ensure_ascii=False), encoding="utf-8")


# ==========================================================================
# 2. The offset+1 attack
# ==========================================================================

def test_offset_off_by_one_is_rejected(case, make_args, register_derivation,
                                        capsys):
    """The canonical case: claim logical 12 sits on physical 20 instead of 19.

    Physical 20 prints '13 / 30', so the page itself contradicts the claim. The
    refusal comes from reading the page, not from any consistency rule.
    """
    rc = register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET + 1)

    assert rc == 1
    out = capsys.readouterr().out
    assert "could not be verified" in out
    assert "no printed page number in a verified header/footer region" in out
    assert _receipt() is None, "a refused registration must issue no receipt"


def test_a_refused_registration_writes_absolutely_nothing(
        case, make_args, register_derivation, isolated_dao):
    manifest_path = isolated_dao / "outputs" / CASE / "document_manifest.json"
    before_manifest = manifest_path.read_bytes()

    assert register_derivation(make_args, CASE, "DOC_004", "12,13", 99) == 1

    assert manifest_path.read_bytes() == before_manifest
    assert not dao.segment_derivation_index_path(CASE).exists()


# ==========================================================================
# 3. The mutually-consistent forgery -- the attack that used to pass
# ==========================================================================

def test_mutually_consistent_false_mapping_is_rejected(
        case, make_args, register_derivation, isolated_dao, capsys):
    """Every self-declared value rewritten to agree on the WRONG physical page.

    manifest page_map, segment PAGE markers, segment_page_ranges,
    derived_text_sha256, the submitted physical_page_sha256 and the submitted
    logical_page_evidence are ALL made consistent with 'logical 12 lives on
    physical 20'. `validate_segment_lineage` sees no contradiction, because
    there is none among those values -- they were all built from the same lie.

    The only disagreement in the world is with the parent PDF, and that is the
    comparison P0-6 added.
    """
    # Build the segment text from page 20's real content, labelled as page 12.
    import fitz
    pdf = isolated_dao / "data" / "raw" / CASE / "DOC_001.pdf"
    doc = fitz.open(pdf)
    page20 = doc[19].get_text()
    page21 = doc[20].get_text()
    doc.close()

    forged_text = (f"<<<PAGE page=12>>>\n{page20}"
                   f"<<<PAGE page=13>>>\n{page21}")
    _seed_segment_text(isolated_dao, make_args, "DOC_004", forged_text)

    # A page_map that is internally perfect and externally false: the digests
    # really are of the pages named, and the evidence quotes really appear on
    # them. Only the logical->physical assignment is wrong.
    forged_map = [
        {"logical_page": 12, "source_physical_page": 20,
         "physical_page_sha256": hashlib.sha256(
             page20.encode("utf-8")).hexdigest(),
         "logical_page_evidence": "13 / 30"},
        {"logical_page": 13, "source_physical_page": 21,
         "physical_page_sha256": hashlib.sha256(
             page21.encode("utf-8")).hexdigest(),
         "logical_page_evidence": "14 / 30"},
    ]
    _patch_entry("DOC_004", {"page_map": forged_map})

    # Lineage alone still sees nothing wrong -- that is the point.
    manifest = dao.read_contract_data(CASE, "document_manifest.json")
    assert dao._validate_manifest_lineage(CASE, manifest) == [], \
        "the forgery is self-consistent; lineage cannot be what catches it"

    # Route 1: try to register the forged mapping. The DAO reads the parent.
    map_file = isolated_dao / "forged_map.json"
    map_file.write_text(json.dumps(forged_map), encoding="utf-8")
    rc = register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET + 1,
                             page_map_file=str(map_file))
    assert rc == 1
    assert "could not be verified" in capsys.readouterr().out

    # Route 2: leave the forged map sitting in the manifest and try to
    # finalize. No receipt exists, so the map is an unverified declaration.
    _seed_classification(isolated_dao, "DOC_001")
    rc = dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "not bound to verified parent provenance" in out
    assert "no segment_page_map_v2 derivation receipt exists" in out


def test_submitted_map_disagreeing_with_the_pdf_is_reported_not_ignored(
        case, make_args, register_derivation, isolated_dao, capsys):
    """A submitted mapping is cross-checked, and a conflict refuses the whole
    registration -- it is never silently replaced by the derived value."""
    map_file = isolated_dao / "map.json"
    map_file.write_text(json.dumps([
        {"logical_page": 12, "source_physical_page": 20},
        {"logical_page": 13, "source_physical_page": 21},
    ]), encoding="utf-8")

    rc = register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET,
                             page_map_file=str(map_file))

    assert rc == 1
    out = capsys.readouterr().out
    assert "submitted source_physical_page 20" in out
    assert "physical page 19" in out
    assert _receipt() is None


# ==========================================================================
# 4. The different-PDF attack
# ==========================================================================

def test_page_evidence_from_a_different_pdf_is_rejected(
        case, make_args, register_derivation, segment_pdf, isolated_dao,
        capsys):
    """A second PDF with the SAME text and the SAME printed numbers.

    Its pages would satisfy any check that only asks "does a page saying this
    exist?". They cannot satisfy a receipt, because the receipt is bound to the
    registered parent's digest, and the parent is resolved from the manifest --
    the caller never names a file.
    """
    other = isolated_dao / "data" / "raw" / CASE / "DOC_099_lookalike.pdf"
    segment_pdf(other, total_logical=TOTAL_LOGICAL, offset=OFFSET)
    other_digest = hashlib.sha256(other.read_bytes()).hexdigest()
    real_digest = dao.registered_source_pdf_sha256(CASE, "DOC_001")
    assert other_digest != real_digest, "the two files must really differ"

    # Claiming the lookalike's digest is refused outright.
    rc = register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET,
                             expect_parent_sha256=other_digest)
    assert rc == 1
    assert "does not match the registered raw source" in capsys.readouterr().out
    assert _receipt() is None

    # And a receipt genuinely issued against the real parent does not become
    # valid for a different file: swap the raw bytes and it goes stale.
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    (isolated_dao / "data" / "raw" / CASE / "DOC_001.pdf").write_bytes(
        other.read_bytes())
    manifest = dao.read_contract_data(CASE, "document_manifest.json")
    blockers = dao._segment_derivation_blockers(CASE, manifest)
    assert any("parent raw source changed" in b for b in blockers), blockers


def test_a_caller_cannot_choose_the_parent(case, make_args,
                                           register_derivation, capsys):
    """The parent comes from the manifest's registered lineage. A
    --parent-document-id that disagrees is refused, not honoured."""
    rc = register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET,
                             parent_document_id="DOC_002")

    assert rc == 1
    assert "disagrees with the manifest's source_document_id" in \
        capsys.readouterr().out


# ==========================================================================
# 5-7. Forged page hash, forged evidence, wrong logical number
# ==========================================================================

def test_forged_page_text_hash_is_rejected(case, make_args,
                                            register_derivation, isolated_dao,
                                            capsys):
    """Right logical page, right physical page, fabricated digest."""
    map_file = isolated_dao / "map.json"
    map_file.write_text(json.dumps([
        {"logical_page": 12, "source_physical_page": 19,
         "physical_page_sha256": "a" * 64},
    ]), encoding="utf-8")

    rc = register_derivation(make_args, CASE, "DOC_004", "12", OFFSET,
                             page_map_file=str(map_file))

    assert rc == 1
    out = capsys.readouterr().out
    assert "submitted physical_page_sha256" in out
    assert "does not match the digest of the text the DAO extracted" in out


def test_forged_logical_page_evidence_is_rejected(
        case, make_args, register_derivation, isolated_dao, capsys):
    """A quote that is not on that physical page at all."""
    map_file = isolated_dao / "map.json"
    map_file.write_text(json.dumps([
        {"logical_page": 12, "source_physical_page": 19,
         "logical_page_evidence": "12 / 999 (제12조 특별약관)"},
    ]), encoding="utf-8")

    rc = register_derivation(make_args, CASE, "DOC_004", "12", OFFSET,
                             page_map_file=str(map_file))

    assert rc == 1
    assert "is not the printed marker the DAO read" in capsys.readouterr().out


def test_evidence_present_but_for_the_wrong_logical_number_is_rejected(
        case, make_args, register_derivation, capsys):
    """Physical page 19 really does print a page marker -- '12 / 30'. Asking
    for logical 13 there is refused even though a marker exists, because the
    marker must name the requested logical page."""
    # offset OFFSET-1 puts logical 13 on physical 19, which prints '12 / 30'.
    rc = register_derivation(make_args, CASE, "DOC_004", "13", OFFSET - 1)

    assert rc == 1
    out = capsys.readouterr().out
    assert "no printed page number in a verified header/footer region" in out


# ==========================================================================
# 8. No printed number: block, never infer from a consistent offset
# ==========================================================================

def test_a_page_without_a_printed_number_blocks_rather_than_inferring(
        isolated_dao, segment_pdf, make_args, register_derivation, capsys):
    """Pages 12 and 14 are printed; page 13 is not.

    The offset is confirmed on both neighbours, which is exactly the argument
    that must NOT carry page 13. No receipt is issued for any page: a partially
    verified mapping is not a mapping.
    """
    raw = isolated_dao / "data" / "raw" / CASE
    raw.mkdir(parents=True)
    segment_pdf(raw / "DOC_001.pdf", total_logical=TOTAL_LOGICAL,
                offset=OFFSET, unnumbered=(13,))
    logical = [12, 13, 14]
    _seed_manifest(isolated_dao, [
        _physical_entry(),
        _segment_entry("DOC_004", "DOC_001", logical,
                       [{"start": 12, "end": 14}]),
    ])
    _seed_segment_text(isolated_dao, make_args, "DOC_004",
                       _segment_text(logical))

    rc = register_derivation(make_args, CASE, "DOC_004", "12-14", OFFSET)

    assert rc == 1
    out = capsys.readouterr().out
    assert "no printed page number in a verified header/footer region" in out
    assert "an offset that is merely consistent elsewhere does not prove" in out
    assert _receipt() is None, \
        "the confirmed pages must not be recorded either -- a partial mapping " \
        "is not a mapping"


def test_a_body_page_reference_cannot_impersonate_a_footer(
        isolated_dao, segment_pdf, make_args, register_derivation, capsys):
    """The requested number exists only in body text; the footer disagrees."""
    raw = isolated_dao / "data" / "raw" / CASE
    raw.mkdir(parents=True)
    segment_pdf(
        raw / "DOC_001.pdf", total_logical=TOTAL_LOGICAL, offset=OFFSET,
        body_for=lambda lp: (
            "See page 12 for details" if lp == 13 else
            f"Clause {lp} body"))
    _seed_manifest(isolated_dao, [
        _physical_entry(),
        _segment_entry(
            "DOC_004", "DOC_001", [12], [{"start": 12, "end": 12}]),
    ])
    _seed_segment_text(
        isolated_dao, make_args, "DOC_004",
        _segment_text(
            [12],
            body_for=lambda lp: "See page 12 for details\n13 / 30\n"))

    # Offset +1 points logical 12 at physical page 20. Its body mentions page
    # 12, but its real footer identifies logical page 13.
    assert register_derivation(
        make_args, CASE, "DOC_004", "12", OFFSET + 1) == 1
    assert "verified header/footer region" in capsys.readouterr().out
    assert _receipt() is None


def test_positioned_page_evidence_rejects_conflicting_header_and_footer():
    record = {
        "text": "12 / 30\nbody\n13 / 30\n",
        "width": 595.0,
        "height": 842.0,
        "blocks": [
            {"bbox": [72, 20, 120, 35], "text": "12 / 30"},
            {"bbox": [72, 700, 120, 715], "text": "13 / 30"},
        ],
    }
    assert dao._find_printed_logical_page(record, 12) is None


def test_there_is_no_manual_confirmation_override(case):
    """No writable field can stand in for a printed page number.

    A `logical_page_confirmed: true` / `verified_by: "human"` escape hatch is
    the gate's off-switch, so the schema must have nowhere to put one and the
    code must offer no flag.
    """
    schema = json.loads(
        (dao.ROOT / "schemas" / "segment_derivation_index.schema.json")
        .read_text(encoding="utf-8"))
    page = schema["$defs"]["verified_page"]
    assert page["additionalProperties"] is False
    assert set(page["properties"]) == {
        "logical_page", "source_physical_page",
        "source_page_text_sha256", "segment_page_text_sha256",
        "logical_page_evidence",
    }
    receipt = schema["$defs"]["derivation_receipt"]
    assert receipt["additionalProperties"] is False
    assert not any(
        key in receipt["properties"]
        for key in ("verified_by", "reviewed", "logical_page_confirmed",
                    "override", "confirmed"))


# ==========================================================================
# 9. Generic write paths cannot forge or edit a page map
# ==========================================================================

def test_write_contract_cannot_introduce_a_page_map(case, isolated_dao,
                                                     make_args, capsys):
    manifest_path = isolated_dao / "outputs" / CASE / "document_manifest.json"
    state_before = _run_state_bytes(isolated_dao)
    before = manifest_path.read_bytes()

    manifest = json.loads(before.decode("utf-8"))
    for entry in manifest["documents"]:
        if entry["document_id"] == "DOC_004":
            entry["page_map"] = [
                {"logical_page": 12, "source_physical_page": 20,
                 "physical_page_sha256": "b" * 64,
                 "logical_page_evidence": "12 / 30"},
                {"logical_page": 13, "source_physical_page": 21,
                 "physical_page_sha256": "c" * 64,
                 "logical_page_evidence": "13 / 30"},
            ]
    data_file = isolated_dao / "m.json"
    data_file.write_text(json.dumps(manifest, ensure_ascii=False),
                         encoding="utf-8")

    rc = dao.cmd_write_contract(make_args(
        case_id=CASE, filename="document_manifest.json",
        data_file=str(data_file),
        schema_name="document_manifest.schema.json",
        held_by="policy-pipeline", run_id=RUN))

    assert rc == 1
    assert "page_map is DAO-owned" in capsys.readouterr().out
    assert manifest_path.read_bytes() == before, \
        "the manifest must be byte-identical after a refused write"
    assert _run_state_bytes(isolated_dao) == state_before


def test_patch_manifest_document_cannot_change_a_page_map(
        case, isolated_dao, make_args, register_derivation):
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    manifest_path = isolated_dao / "outputs" / CASE / "document_manifest.json"
    before = manifest_path.read_bytes()
    state_before = _run_state_bytes(isolated_dao)
    receipt_before = dao.segment_derivation_index_path(CASE).read_bytes()

    ok, message = dao.patch_manifest_document(
        CASE, "DOC_004",
        {"page_map": [{"logical_page": 12, "source_physical_page": 20,
                       "physical_page_sha256": "d" * 64,
                       "logical_page_evidence": "12 / 30"}]},
        "policy-pipeline", RUN)

    assert not ok
    assert "page_map is DAO-owned" in message
    assert manifest_path.read_bytes() == before
    assert _run_state_bytes(isolated_dao) == state_before
    assert dao.segment_derivation_index_path(CASE).read_bytes() == receipt_before


def test_patch_manifest_document_cannot_delete_a_page_map(
        case, isolated_dao, make_args, register_derivation):
    """Deletion is refused as firmly as modification: a caller able to strip
    the field could supply its own on the next write."""
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    before = (isolated_dao / "outputs" / CASE / "document_manifest.json").read_bytes()

    ok, message = dao.patch_manifest_document(
        CASE, "DOC_004", {"page_map": None}, "policy-pipeline", RUN)

    assert not ok
    assert "page_map is DAO-owned" in message
    assert (isolated_dao / "outputs" / CASE
            / "document_manifest.json").read_bytes() == before


def test_page_map_is_in_the_sealed_field_set():
    assert "page_map" in source_provenance.PROTECTED_MANIFEST_FIELDS


def test_the_receipt_index_is_not_a_write_contract_target(case, isolated_dao,
                                                           make_args):
    """`_segment_derivation_index.json` has no agent-facing write path.

    write-contract's own path-safety and schema plumbing would let a caller
    name any filename, so the check that matters is that a receipt written
    that way does not become authoritative: the manifest projection would not
    match anything, and finalization refuses.
    """
    forged = {
        "case_id": CASE,
        "segments": [{
            "scheme": sd.RECEIPT_SCHEME,
            "document_id": "DOC_004",
            "parent_source_document_id": "DOC_001",
            "parent_source_pdf_sha256": "e" * 64,
            "parent_source_total_pages": TOTAL_LOGICAL + OFFSET,
            "segment_source_text_revision_sha256": "f" * 64,
            "derivation_method": "embedded_text_segment",
            "page_offset_candidate": 8,
            "extractor": {"tool": "forged", "library": None,
                          "library_version": None, "settings": None},
            "pages": [{"logical_page": 12, "source_physical_page": 20,
                       "source_page_text_sha256": "a" * 64,
                       "segment_page_text_sha256": "a" * 64,
                       "logical_page_evidence": {
                           "quote": "12 / 30",
                           "bbox": [72, 690, 120, 710],
                           "region": "footer",
                           "page_width": 595,
                           "page_height": 842,
                           "evidence_profile":
                               "header_footer_blocks_v1"}}],
            "issued_at": "2026-07-28T00:00:00+09:00",
            "issued_by": "attacker",
        }],
    }
    dao.segment_derivation_index_path(CASE).write_text(
        json.dumps(forged, ensure_ascii=False), encoding="utf-8")

    manifest = dao.read_contract_data(CASE, "document_manifest.json")
    blockers = dao._segment_derivation_blockers(CASE, manifest)

    assert blockers, "a forged receipt must not silently validate"
    # It fails on the parent digest it names, which no real file hashes to.
    assert any("parent raw source changed" in b
               or "does not match the" in b for b in blockers), blockers


def _run_state_bytes(isolated_dao):
    path = isolated_dao / "outputs" / CASE / "_run_state.json"
    return path.read_bytes() if path.exists() else None


# ==========================================================================
# 10-11. Staleness: the parent PDF moved, or the segment text moved
# ==========================================================================

def test_changing_the_parent_pdf_invalidates_the_receipt_and_blocks_finalize(
        case, isolated_dao, make_args, register_derivation, segment_pdf,
        capsys):
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    _seed_classification(isolated_dao, "DOC_001")
    assert dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline")) == 0

    # The immutable raw source changes underneath the receipt.
    segment_pdf(isolated_dao / "data" / "raw" / CASE / "DOC_001.pdf",
                total_logical=TOTAL_LOGICAL + 1, offset=OFFSET)

    rc = dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline"))

    assert rc == 1
    assert "parent raw source changed" in capsys.readouterr().out


def test_revising_the_segment_text_invalidates_the_receipt(
        case, isolated_dao, make_args, register_derivation, capsys):
    """A receipt states that THESE parent pages produced THESE segment bytes.
    Rewriting the segment makes it a true statement about bytes nobody has."""
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0

    revised = isolated_dao / "revised.md"
    revised.write_text(_segment_text([12, 13]) + "추가된 문단\n", encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id=CASE, doc_id="DOC_004", text_file=str(revised),
        held_by="document-pipeline", run_id=RUN)) == 0

    manifest = dao.read_contract_data(CASE, "document_manifest.json")
    blockers = dao._segment_derivation_blockers(CASE, manifest)

    assert any("source text was revised after this receipt was issued" in b
               for b in blockers), blockers


def test_policy_clause_finalization_also_re_verifies(
        case, isolated_dao, make_args, register_derivation, segment_pdf,
        capsys):
    """document_processing may have passed before the parent moved, so the
    policy stage re-asks rather than inheriting that answer."""
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    segment_pdf(isolated_dao / "data" / "raw" / CASE / "DOC_001.pdf",
                total_logical=TOTAL_LOGICAL + 2, offset=OFFSET)

    rc = dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="policy_clause_processing",
        held_by="policy-pipeline"))

    assert rc == 1
    out = capsys.readouterr().out
    assert ("not bound to verified parent provenance" in out
            or "unmet dependencies" in out)


def test_transitive_downstream_finalization_rechecks_live_receipts(
        case, isolated_dao, make_args, register_derivation, segment_pdf,
        monkeypatch, capsys):
    """A historical policy pass cannot hide a now-stale parent receipt."""
    assert register_derivation(
        make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    segment_pdf(
        isolated_dao / "data" / "raw" / CASE / "DOC_001.pdf",
        total_logical=TOTAL_LOGICAL + 3, offset=OFFSET)

    # Isolate the live provenance gate from graph setup. The test asks whether
    # a downstream stage with otherwise-satisfied prerequisites still rechecks
    # the receipt.
    monkeypatch.setattr(
        dao.stage_dependencies, "check_dependencies",
        lambda *args, **kwargs: [])

    rc = dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="screening_report",
        held_by="screening-report"))

    assert rc == 1
    out = capsys.readouterr().out
    assert "segment derivation provenance is no longer current" in out
    assert "parent raw source changed" in out


# ==========================================================================
# 12. Re-registering identical bytes is a no-op
# ==========================================================================

def test_identical_re_registration_is_a_noop_and_does_not_cascade(
        case, isolated_dao, make_args, register_derivation, capsys):
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    _seed_classification(isolated_dao, "DOC_001")
    assert dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline")) == 0

    receipt_before = dao.segment_derivation_index_path(CASE).read_bytes()
    manifest_before = (isolated_dao / "outputs" / CASE
                       / "document_manifest.json").read_bytes()
    revision_before = dao.revision_entry_for(CASE, "DOC_004")

    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0

    assert "no-op" in capsys.readouterr().out
    assert dao.segment_derivation_index_path(CASE).read_bytes() == receipt_before
    assert (isolated_dao / "outputs" / CASE
            / "document_manifest.json").read_bytes() == manifest_before
    assert dao.revision_entry_for(CASE, "DOC_004") == revision_before
    assert next(s["status"] for s in dao.load_run_state(CASE)["stages"]
                if s["stage_name"] == "document_processing") == "passed", \
        "an unchanged re-registration must not invalidate downstream work"


# ==========================================================================
# 13-14. Multi-range segments and the page-swap attack
# ==========================================================================

def test_multi_range_segment_binds_every_page_independently(
        isolated_dao, segment_pdf, make_args, register_derivation):
    """CASE_030's real DOC_004 shape: {57,58} and {118,119}, non-contiguous.

    Each logical page carries its own proof; no page rides on another's.
    """
    raw = isolated_dao / "data" / "raw" / CASE
    raw.mkdir(parents=True)
    segment_pdf(raw / "DOC_001.pdf", total_logical=130, offset=OFFSET)
    logical = [57, 58, 118, 119]
    _seed_manifest(isolated_dao, [
        _physical_entry(total_pages=137),
        _segment_entry("DOC_004", "DOC_001", logical,
                       [{"start": 57, "end": 58}, {"start": 118, "end": 119}]),
    ])
    _seed_segment_text(
        isolated_dao, make_args, "DOC_004",
        "\n".join(
            f"<<<PAGE page={lp}>>>\nClause {lp} body\n{lp} / 130\n"
            for lp in logical) + "\n")

    assert register_derivation(
        make_args, CASE, "DOC_004", "57,58,118,119", OFFSET) == 0

    receipt = _receipt()
    assert [p["source_physical_page"] for p in receipt["pages"]] == \
        [64, 65, 125, 126]
    assert [p["logical_page_evidence"]["quote"]
            for p in receipt["pages"]] == \
        ["57 / 130", "58 / 130", "118 / 130", "119 / 130"]
    # Every page's digest is distinct -- no page's proof was reused.
    digests = [p["source_page_text_sha256"] for p in receipt["pages"]]
    assert len(set(digests)) == 4


def test_swapping_two_pages_is_rejected_even_with_shared_vocabulary(
        isolated_dao, segment_pdf, make_args, capsys):
    """Two pages whose words are identical, differing only in printed number.

    A content-similarity check would find nothing wrong with swapping them. The
    printed marker is what distinguishes the pages, so the swap is caught.
    """
    raw = isolated_dao / "data" / "raw" / CASE
    raw.mkdir(parents=True)
    shared = "coverage paid not paid"
    segment_pdf(raw / "DOC_001.pdf", total_logical=TOTAL_LOGICAL,
                offset=OFFSET, body_for=lambda lp: shared)
    _seed_manifest(isolated_dao, [
        _physical_entry(),
        _segment_entry("DOC_004", "DOC_001", [12, 13],
                       [{"start": 12, "end": 13}]),
    ])
    _seed_segment_text(
        isolated_dao, make_args, "DOC_004",
        _segment_text(
            [12, 13],
            body_for=lambda lp: (
                f"{shared}\n{lp} / {TOTAL_LOGICAL}\n")))

    import fitz
    doc = fitz.open(raw / "DOC_001.pdf")
    p19, p20 = doc[18].get_text(), doc[19].get_text()
    doc.close()

    # Swapped: logical 12 claimed on physical 20, logical 13 on physical 19.
    map_file = isolated_dao / "swap.json"
    map_file.write_text(json.dumps([
        {"logical_page": 12, "source_physical_page": 20,
         "physical_page_sha256": hashlib.sha256(p20.encode()).hexdigest()},
        {"logical_page": 13, "source_physical_page": 19,
         "physical_page_sha256": hashlib.sha256(p19.encode()).hexdigest()},
    ]), encoding="utf-8")

    rc = dao.cmd_register_segment_derivation(make_args(
        case_id=CASE, doc_id="DOC_004", pages="12,13", page_offset=OFFSET,
        page_map_file=str(map_file), held_by="document-pipeline", run_id=RUN))

    assert rc == 1
    out = capsys.readouterr().out
    assert "submitted source_physical_page 20" in out
    assert _receipt() is None


# ==========================================================================
# 15. Finalization re-verifies against a corrupted state
# ==========================================================================

def test_finalize_blocked_when_the_manifest_projection_is_tampered_with(
        case, isolated_dao, make_args, register_derivation, capsys):
    """The receipt is intact; the manifest's copy of it is edited on disk.

    Reachable only by writing past the DAO (page_map is sealed), which is why
    the fixture does it directly. Finalization must still catch it: the
    manifest is a projection, and a projection that disagrees with its source
    is not a page map anyone verified.
    """
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    _seed_classification(isolated_dao, "DOC_001")

    # Shift BOTH pages by one, so the edit stays lineage-clean (still unique,
    # still increasing, still within the parent) and the projection check is
    # the only thing that can catch it.
    path = isolated_dao / "outputs" / CASE / "document_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entry = next(d for d in manifest["documents"]
                 if d["document_id"] == "DOC_004")
    for page in entry["page_map"]:
        page["source_physical_page"] += 1
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    assert dao._validate_manifest_lineage(
        CASE, json.loads(path.read_text(encoding="utf-8"))) == [], \
        "the tampered map must be lineage-clean, or lineage catches it instead"

    rc = dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline"))

    assert rc == 1
    out = capsys.readouterr().out
    assert "does not match the segment_page_map_v2 receipt's projection" in out


def test_finalize_blocked_when_the_receipt_is_deleted(
        case, isolated_dao, make_args, register_derivation, capsys):
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    _seed_classification(isolated_dao, "DOC_001")
    dao.segment_derivation_index_path(CASE).unlink()

    rc = dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline"))

    assert rc == 1
    assert "no segment_page_map_v2 derivation receipt exists" in \
        capsys.readouterr().out


def test_a_legacy_evidence_free_page_map_blocks_finalization(
        case, isolated_dao, make_args, capsys):
    """CASE_030's real state: page_map entries with no digest and no printed
    evidence, written before P0-6 existed.

    Missing proof is a BLOCKER, never "the field is absent so there is nothing
    to check" -- that reasoning is what let these stand.
    """
    _seed_classification(isolated_dao, "DOC_001")
    entry = next(d for d in dao.read_contract_data(
        CASE, "document_manifest.json")["documents"]
        if d["document_id"] == "DOC_004")
    assert not any("physical_page_sha256" in e for e in entry["page_map"]), \
        "this fixture must start in the evidence-free legacy shape"

    rc = dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline"))

    assert rc == 1
    assert "no segment_page_map_v2 derivation receipt exists" in \
        capsys.readouterr().out


def test_legacy_segments_stay_readable(case, make_args, capsys):
    """Refusing to FINALIZE a legacy segment is not refusing to read it --
    the migration path depends on legacy artifacts remaining accessible."""
    assert dao.cmd_read_contract(make_args(
        case_id=CASE, filename="document_manifest.json")) == 0
    assert "DOC_004" in capsys.readouterr().out


def test_a_re_parented_segment_loses_its_receipt(
        case, isolated_dao, make_args, register_derivation):
    """A receipt is for one parent. Pointing the segment at a different
    document does not carry the verification across."""
    assert register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET) == 0
    _patch_entry("DOC_004", {"source_document_id": "DOC_002"})

    manifest = dao.read_contract_data(CASE, "document_manifest.json")
    blockers = dao._segment_derivation_blockers(CASE, manifest)

    assert any("the manifest now names 'DOC_002'" in b for b in blockers), \
        blockers


# ==========================================================================
# 16. Fault injection: no partial writes at any failure point
# ==========================================================================

def _snapshot(isolated_dao):
    """Every file P0-6 can touch, byte for byte."""
    out = isolated_dao / "outputs" / CASE
    state = {}
    for name in ("document_manifest.json", "_run_state.json",
                 "_segment_derivation_index.json", "_revision_index.json",
                 "_transaction_journal.json"):
        path = out / name
        state[name] = path.read_bytes() if path.exists() else None
    return state


@pytest.mark.parametrize("failure,setup", [
    ("parent digest mismatch",
     lambda d, m: (d / "data" / "raw" / CASE / "DOC_001.pdf").write_bytes(
         b"%PDF-1.7 not the registered file")),
    ("page evidence unverifiable",
     lambda d, m: None),  # driven by a bad offset below
])
def test_no_partial_write_on_a_failed_registration(
        case, isolated_dao, make_args, register_derivation, failure, setup):
    """Whatever fails, the case is left exactly as it was."""
    # Record the parent digest first so a changed file is a MISMATCH rather
    # than an unrecorded one.
    assert dao.cmd_record_source_digest(make_args(
        case_id=CASE, doc_id="DOC_001", held_by="document-pipeline",
        run_id=RUN, expect=None)) == 0
    before = _snapshot(isolated_dao)

    setup(isolated_dao, make_args)
    offset = OFFSET if failure == "parent digest mismatch" else 42
    rc = register_derivation(make_args, CASE, "DOC_004", "12,13", offset)

    assert rc == 1
    assert _snapshot(isolated_dao) == before, \
        f"a failure at '{failure}' must leave every file untouched"


def test_schema_failure_writes_neither_receipt_nor_manifest(
        case, isolated_dao, make_args, monkeypatch):
    """The prospective receipt AND the prospective manifest are validated
    before either lands, so a schema failure cannot write one of the two."""
    before = _snapshot(isolated_dao)
    real = dao._schema_check

    def _fail_on_receipt(data, schema_name):
        if schema_name == sd.INDEX_SCHEMA:
            return ["injected receipt schema failure"]
        return real(data, schema_name)

    monkeypatch.setattr(dao, "_schema_check", _fail_on_receipt)

    rc = dao.cmd_register_segment_derivation(make_args(
        case_id=CASE, doc_id="DOC_004", pages="12,13", page_offset=OFFSET,
        held_by="document-pipeline", run_id=RUN))

    assert rc == 1
    assert _snapshot(isolated_dao) == before


def test_a_failed_cascade_leaves_the_old_mapping_standing(
        case, isolated_dao, make_args, monkeypatch, capsys):
    """The invalidation runs BEFORE the receipt is written, so a cascade that
    cannot be applied aborts with nothing changed -- never a new mapping beside
    a stale `passed`."""
    before = _snapshot(isolated_dao)

    def _boom(*args, **kwargs):
        raise dao.CascadeFailed("injected cascade failure")

    monkeypatch.setattr(dao, "_invalidate_dependents", _boom)

    rc = dao.cmd_register_segment_derivation(make_args(
        case_id=CASE, doc_id="DOC_004", pages="12,13", page_offset=OFFSET,
        held_by="document-pipeline", run_id=RUN))

    assert rc == 1
    assert "page map NOT registered" in capsys.readouterr().out
    assert _snapshot(isolated_dao) == before, \
        "including the journal, which must be cleared on abort"


def test_lock_contention_aborts_the_whole_registration(
        case, isolated_dao, make_args, register_derivation, capsys):
    """The receipt, the projection and the invalidation must land together, so
    a lock held on ANY of the three refuses rather than writing part."""
    before = _snapshot(isolated_dao)
    manifest_target = dao.case_dir(CASE) / "document_manifest.json"
    dao.acquire_lock(manifest_target, "someone-else", "RUN_20260728_002",
                     "holding the manifest")
    try:
        rc = register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET)
    finally:
        dao.release_lock(manifest_target)

    assert rc == 1
    assert "LOCKED" in capsys.readouterr().out
    assert _snapshot(isolated_dao) == before


def test_receipt_is_rolled_back_when_manifest_persistence_fails(
        case, isolated_dao, make_args, monkeypatch, capsys):
    """The second atomic replace fails after the receipt index landed."""
    manifest_path = (
        isolated_dao / "outputs" / CASE / "document_manifest.json")
    manifest_before = manifest_path.read_bytes()
    index_path = dao.segment_derivation_index_path(CASE)
    assert not index_path.exists()
    real_write = dao.atomic_write_json

    def _fail_manifest(path, obj):
        if Path(path).name == "document_manifest.json":
            raise OSError("injected second-write failure")
        return real_write(path, obj)

    monkeypatch.setattr(dao, "atomic_write_json", _fail_manifest)

    rc = dao.cmd_register_segment_derivation(make_args(
        case_id=CASE, doc_id="DOC_004", pages="12,13",
        page_offset=OFFSET, held_by="document-pipeline", run_id=RUN))

    assert rc == 1
    assert "restored to their exact pre-transaction bytes" in \
        capsys.readouterr().out
    assert not index_path.exists()
    assert manifest_path.read_bytes() == manifest_before
    assert not (dao.case_dir(CASE) / "_transaction_journal.json").exists()


def test_receipt_is_rolled_back_when_first_write_raises_after_replace(
        case, isolated_dao, make_args, monkeypatch, capsys):
    """A writer can fail after os.replace; return flags cannot prove no write."""
    manifest_path = (
        isolated_dao / "outputs" / CASE / "document_manifest.json")
    manifest_before = manifest_path.read_bytes()
    index_path = dao.segment_derivation_index_path(CASE)
    real_write = dao.atomic_write_json

    def _write_then_fail(path, obj):
        result = real_write(path, obj)
        if Path(path).name == "_segment_derivation_index.json":
            raise OSError("injected post-replace failure")
        return result

    monkeypatch.setattr(dao, "atomic_write_json", _write_then_fail)

    assert dao.cmd_register_segment_derivation(make_args(
        case_id=CASE, doc_id="DOC_004", pages="12,13",
        page_offset=OFFSET, held_by="document-pipeline", run_id=RUN)) == 1

    assert "restored to their exact pre-transaction bytes" in \
        capsys.readouterr().out
    assert not index_path.exists()
    assert manifest_path.read_bytes() == manifest_before
    assert not (dao.case_dir(CASE) / "_transaction_journal.json").exists()


def test_failed_rollback_leaves_a_blocking_journal(
        case, isolated_dao, make_args, monkeypatch, capsys):
    real_write = dao.atomic_write_json

    def _fail_manifest(path, obj):
        if Path(path).name == "document_manifest.json":
            raise OSError("injected second-write failure")
        return real_write(path, obj)

    monkeypatch.setattr(dao, "atomic_write_json", _fail_manifest)

    def _fail_restore(*args, **kwargs):
        raise OSError("injected rollback failure")

    monkeypatch.setattr(dao, "_restore_file_preimage", _fail_restore)

    assert dao.cmd_register_segment_derivation(make_args(
        case_id=CASE, doc_id="DOC_004", pages="12,13",
        page_offset=OFFSET, held_by="document-pipeline", run_id=RUN)) == 1
    assert "ROLLBACK INCOMPLETE" in capsys.readouterr().out
    assert dao_transaction.pending_journal_errors(dao.case_dir(CASE))

    assert dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline")) == 1
    assert "transaction is incomplete" in capsys.readouterr().out


def test_an_interrupted_transaction_blocks_new_registrations(
        case, isolated_dao, make_args, register_derivation, capsys):
    """A pending journal is not resumed automatically -- the stage reruns."""
    import dao_transaction
    dao_transaction.write_journal(dao.case_dir(CASE), {
        "operation": "register_segment_derivation",
        "case_id": CASE, "document_id": "DOC_004", "status": "invalidating",
    })

    rc = register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET)

    assert rc == 1
    assert "interrupted DAO transaction" in capsys.readouterr().out


# ==========================================================================
# 17. OCR segments fail closed
# ==========================================================================

def test_an_ocr_segment_cannot_obtain_a_receipt(
        isolated_dao, segment_pdf, make_args, register_derivation, capsys):
    raw = isolated_dao / "data" / "raw" / CASE
    raw.mkdir(parents=True)
    segment_pdf(raw / "DOC_001.pdf", total_logical=TOTAL_LOGICAL, offset=OFFSET)
    _seed_manifest(isolated_dao, [
        _physical_entry(),
        _segment_entry("DOC_004", "DOC_001", [12, 13],
                       [{"start": 12, "end": 13}],
                       derivation_method="ocr_segment"),
    ])
    _seed_segment_text(isolated_dao, make_args, "DOC_004",
                       _segment_text([12, 13]))

    rc = register_derivation(make_args, CASE, "DOC_004", "12,13", OFFSET)

    assert rc == 1
    out = capsys.readouterr().out
    assert "cannot be verified deterministically" in out
    assert "may not re-run OCR" in out
    assert _receipt() is None


def test_an_ocr_segment_blocks_finalization_rather_than_passing(
        isolated_dao, segment_pdf, make_args, capsys):
    """The honest outcome: P0-6 solves the embedded-text case and REFUSES the
    OCR case. It does not quietly let OCR segments through."""
    raw = isolated_dao / "data" / "raw" / CASE
    raw.mkdir(parents=True)
    segment_pdf(raw / "DOC_001.pdf", total_logical=TOTAL_LOGICAL, offset=OFFSET)
    _seed_manifest(isolated_dao, [
        _physical_entry(),
        _segment_entry("DOC_004", "DOC_001", [12, 13],
                       [{"start": 12, "end": 13}],
                       derivation_method="ocr_segment"),
    ])
    _seed_segment_text(isolated_dao, make_args, "DOC_004",
                       _segment_text([12, 13]))
    _seed_classification(isolated_dao, "DOC_001")

    rc = dao.cmd_snapshot_backup(make_args(
        case_id=CASE, run_id=RUN, stage="document_processing",
        held_by="document-pipeline"))

    assert rc == 1
    out = capsys.readouterr().out
    assert "known P0-6 limitation" in out
    assert "not a passing state" in out


# ==========================================================================
# 18. Pure-unit checks on the verification logic
# ==========================================================================

def test_two_logical_pages_cannot_share_one_physical_page():
    pages, errors = sd.verify_candidate_mapping(
        [12, 12], 7, lambda p: "12 / 30", lambda t, n: "12 / 30")
    assert pages == []
    assert any("both resolve to physical page" in e for e in errors)


def test_a_mapping_past_the_end_of_the_parent_is_refused():
    pages, errors = sd.verify_candidate_mapping(
        [12], 7, lambda p: "12 / 30", lambda t, n: "12 / 30",
        parent_total_pages=10)
    assert pages == []
    assert any("exceeds the parent's 10 physical pages" in e for e in errors)


def test_receipt_fingerprint_ignores_timestamps_but_not_the_mapping():
    base = dict(
        segment_document_id="DOC_004", parent_document_id="DOC_001",
        parent_source_pdf_sha256="a" * 64, parent_source_total_pages=37,
        segment_source_text_revision_sha256="b" * 64,
        derivation_method="embedded_text_segment",
        extractor={"tool": "t", "library": None, "library_version": None,
                   "settings": None},
        pages=[{"logical_page": 12, "source_physical_page": 19,
                "source_page_text_sha256": "c" * 64,
                "segment_page_text_sha256": "c" * 64,
                "logical_page_evidence": {
                    "quote": "12 / 30",
                    "bbox": [72, 690, 120, 710],
                    "region": "footer",
                    "page_width": 595,
                    "page_height": 842,
                    "evidence_profile": "header_footer_blocks_v1"}}],
        page_offset_candidate=7, run_id=RUN)
    first = sd.build_receipt(issued_at="2026-07-28T00:00:00+09:00",
                             issued_by="a", **base)
    later = sd.build_receipt(issued_at="2026-07-29T12:00:00+09:00",
                             issued_by="b", **base)
    assert sd.receipt_fingerprint(first) == sd.receipt_fingerprint(later)

    moved = dict(base)
    moved["pages"] = [{**base["pages"][0], "source_physical_page": 20}]
    assert sd.receipt_fingerprint(first) != sd.receipt_fingerprint(
        sd.build_receipt(issued_at="2026-07-28T00:00:00+09:00",
                         issued_by="a", **moved))


def test_an_unknown_receipt_scheme_is_refused_not_assumed_equivalent():
    errors = sd.receipt_currency_errors(
        receipt={"scheme": "segment_page_map_v3_future"},
        parent_entry=None, current_parent_pdf_sha256="a" * 64,
        current_segment_revision_sha256="b" * 64)
    assert any("is not 'segment_page_map_v2'" in e for e in errors)


def test_v1_receipt_is_not_silently_upgraded_to_v2():
    errors = sd.receipt_currency_errors(
        receipt={"scheme": "segment_page_map_v1"},
        parent_entry=None, current_parent_pdf_sha256="a" * 64,
        current_segment_revision_sha256="b" * 64)
    assert any("is not 'segment_page_map_v2'" in error for error in errors)


def test_segment_binding_normalizes_only_newlines_and_unicode():
    source = "보험금 café\n12 / 30\n"
    source_nfd = unicodedata.normalize("NFD", source)
    source_digest = sd.text_sha256(sd.canonical_source_page_text(source))
    page = {
        "logical_page": 12,
        "source_physical_page": 19,
        "source_page_text_sha256": source_digest,
        "logical_page_evidence": {
            "quote": "12 / 30",
            "bbox": [72, 690, 120, 710],
            "region": "footer",
            "page_width": 595,
            "page_height": 842,
            "evidence_profile": "header_footer_blocks_v1",
        },
    }
    crlf_revision = (
        "<<<PAGE page=12>>>\r\n"
        + source_nfd.replace("\n", "\r\n")
        + "\r\n")
    bound, errors = sd.bind_segment_revision(
        [page], crlf_revision, lambda physical: source)
    assert errors == []
    assert bound[0]["segment_page_text_sha256"] == source_digest

    spaced = crlf_revision.replace("caf", "caf ")
    bound, errors = sd.bind_segment_revision(
        [page], spaced, lambda physical: source)
    assert bound == []
    assert any("does not exactly equal" in error for error in errors)
