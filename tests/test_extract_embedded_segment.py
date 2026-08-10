"""Part 8 + Part 11G: deterministic embedded-text segment extraction.

Part 8 covered page-spec parsing, the embedded read, fail-loud on an empty text
layer, and marker assembly. Part 11G added the part that makes the mapping
verifiable rather than asserted: the source PDF is resolved from the manifest
(not supplied), its digest and page count are checked against the registered
parent, and the `--page-offset` is treated as a CANDIDATE that each page's own
printed number must confirm.

Everything runs against stubbed readers -- no real PDF, no real dao.py
subprocess.
"""
import hashlib

import pytest

import extract_embedded_segment as ees


# --------------------------------------------------------------------------
# Page-spec parsing (Part 8, unchanged)
# --------------------------------------------------------------------------

def test_parse_pages_range():
    assert ees._parse_pages("120-123") == [120, 121, 122, 123]


def test_parse_pages_mixed_and_dedup():
    assert ees._parse_pages("57,58,118-119,58") == [57, 58, 118, 119]


# --------------------------------------------------------------------------
# Printed logical-page confirmation
# --------------------------------------------------------------------------

def test_printed_page_marker_forms_are_recognised():
    assert ees.find_printed_logical_page("본문...\n12 / 240\n", 12) == "12 / 240"
    assert ees.find_printed_logical_page("- 12 -", 12) == "- 12 -"
    assert ees.find_printed_logical_page("page 12", 12).lower() == "page 12"


def test_bare_number_is_not_accepted_as_a_page_marker():
    """A bare number is indistinguishable from an amount or an article number,
    so it must not confirm a page mapping."""
    assert ees.find_printed_logical_page("보험금 12만원을 지급합니다", 12) is None


def test_wrong_printed_number_does_not_confirm():
    assert ees.find_printed_logical_page("13 / 240", 12) is None


# --------------------------------------------------------------------------
# Parent resolution: the source is registered, never supplied
# --------------------------------------------------------------------------

def _manifest(**overrides):
    entry = {
        "document_id": "DOC_001",
        "file_name": "policy.pdf",
        "file_path": "data/raw/CASE_030/DOC_001.pdf",
        "file_format": "pdf",
        "file_size_bytes": 100,
        "ocr_status": "completed",
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
        "source_total_pages": 20,
    }
    entry.update(overrides)
    return {"case_id": "CASE_030", "documents": [entry]}


def test_unregistered_parent_is_refused():
    with pytest.raises(ees.ExtractionBlocked, match="not registered"):
        ees.resolve_parent(_manifest(), "DOC_099")


def test_segment_cannot_be_used_as_a_parent():
    manifest = _manifest(document_role="segment")
    with pytest.raises(ees.ExtractionBlocked, match="is a segment"):
        ees.resolve_parent(manifest, "DOC_001")


def test_parent_without_a_raw_file_path_is_refused():
    """A different PDF path cannot be smuggled in: only data/raw/ counts."""
    manifest = _manifest(file_path="C:/somewhere/else/other.pdf")
    with pytest.raises(ees.ExtractionBlocked, match="no raw file_path"):
        ees.resolve_parent(manifest, "DOC_001")


def test_missing_manifest_blocks_extraction():
    with pytest.raises(ees.ExtractionBlocked, match="no document_manifest"):
        ees.resolve_parent(None, "DOC_001")


# --------------------------------------------------------------------------
# Parent digest / page-count verification
# --------------------------------------------------------------------------

def test_changed_parent_pdf_digest_is_detected(tmp_path):
    pdf = tmp_path / "DOC_001.pdf"
    pdf.write_bytes(b"the real source")
    entry = {"source_pdf_sha256": "0" * 64, "source_total_pages": 20}
    with pytest.raises(ees.ExtractionBlocked, match="digest changed"):
        ees.verify_parent_source(entry, pdf, 20)


def test_matching_parent_pdf_digest_passes(tmp_path):
    pdf = tmp_path / "DOC_001.pdf"
    pdf.write_bytes(b"the real source")
    real = ees.file_sha256(pdf)
    entry = {"source_pdf_sha256": real, "source_total_pages": 20}
    assert ees.verify_parent_source(entry, pdf, 20) == real


def test_page_count_disagreement_is_detected(tmp_path):
    pdf = tmp_path / "DOC_001.pdf"
    pdf.write_bytes(b"x")
    entry = {"source_total_pages": 240}
    with pytest.raises(ees.ExtractionBlocked, match="source_total_pages"):
        ees.verify_parent_source(entry, pdf, 20)


def test_missing_source_file_is_blocked(tmp_path):
    with pytest.raises(ees.ExtractionBlocked, match="does not exist"):
        ees.verify_parent_source({}, tmp_path / "nope.pdf", 20)


# --------------------------------------------------------------------------
# The offset is a candidate, confirmed page by page
# --------------------------------------------------------------------------

def _pages_with_markers(offset=7, logicals=(1, 2)):
    """Physical pages whose printed number matches logical = physical - offset."""
    return {
        lp + offset: f"본문 텍스트\n{lp} / 240\n" for lp in logicals
    }


def test_correct_offset_is_confirmed_and_bound():
    pages = _pages_with_markers(offset=7, logicals=(1, 2))
    page_map = ees.build_page_map(pages, [1, 2], 7)
    assert [e["logical_page"] for e in page_map] == [1, 2]
    assert [e["source_physical_page"] for e in page_map] == [8, 9]
    assert page_map[0]["logical_page_evidence"] == "1 / 240"
    # Bound to the real page content.
    assert page_map[0]["physical_page_sha256"] == \
        hashlib.sha256(pages[8].encode("utf-8")).hexdigest()


def test_wrong_offset_is_rejected():
    """Pages printed 1 and 2 sit at physical 8 and 9; claiming offset 5 lands
    on pages whose printed numbers disagree."""
    pages = _pages_with_markers(offset=7, logicals=(1, 2, 3, 4))
    with pytest.raises(ees.ExtractionBlocked, match="could not be verified"):
        ees.build_page_map(pages, [1, 2], 5)


def test_page_missing_from_the_pdf_is_rejected():
    with pytest.raises(ees.ExtractionBlocked, match="no such page"):
        ees.build_page_map({}, [1], 7)


def test_unreadable_printed_number_blocks_rather_than_guessing():
    """Requirement: if the printed logical number cannot be confirmed, the run
    blocks for review -- it never writes a page on a guessed mapping."""
    pages = {8: "표지 이미지 캡션만 있는 페이지\n"}
    with pytest.raises(ees.ExtractionBlocked,
                       match="printed page number could not be confirmed"):
        ees.build_page_map(pages, [1], 7)


def test_partial_confirmation_blocks_the_whole_extraction():
    pages = {8: "본문\n1 / 240\n", 9: "번호 없는 페이지\n"}
    with pytest.raises(ees.ExtractionBlocked) as excinfo:
        ees.build_page_map(pages, [1, 2], 7)
    assert "logical 2" in str(excinfo.value)


# --------------------------------------------------------------------------
# End to end, with every external dependency stubbed
# --------------------------------------------------------------------------

def test_extract_segment_binds_source_and_assembles_markers(tmp_path):
    pdf = tmp_path / "DOC_001.pdf"
    pdf.write_bytes(b"real source bytes")
    real_sha = ees.file_sha256(pdf)

    pages = {8: "PAGE ONE\n1 / 240\n", 9: "PAGE TWO\n2 / 240\n"}
    calls = []

    result = ees.extract_segment(
        "CASE_030", "DOC_009", "DOC_001", [1, 2], 7,
        held_by="tester", run_id="RUN_1",
        manifest_reader=lambda case_id: _manifest(source_pdf_sha256=real_sha),
        page_reader=lambda path, physicals: pages,
        page_counter=lambda path: 20,
        source_verifier=lambda entry, path, count: real_sha,
        dao_call=lambda *args: calls.append(args) or "",
    )

    kinds = [c[0] for c in calls]
    assert kinds.count("write-page-text") == 2
    assert kinds.count("write-redacted-text") == 1

    expected = ("<<<PAGE page=1>>>\nPAGE ONE\n1 / 240\n\n"
                "<<<PAGE page=2>>>\nPAGE TWO\n2 / 240\n\n")
    assert result["derived_text_sha256"] == \
        hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert result["source_pdf_sha256"] == real_sha
    assert result["page_offset_confirmed"] == 7
    assert result["extraction_method"] == "embedded_text"
    assert result["cross_validation_mode"] == "deferred_poc"
    # The page map is bound, not merely declared.
    assert all("physical_page_sha256" in e and "logical_page_evidence" in e
               for e in result["page_map"])


def test_extract_segment_writes_nothing_when_mapping_is_unverified(tmp_path):
    """Fail closed: a bad offset must not produce any DAO write."""
    pdf = tmp_path / "DOC_001.pdf"
    pdf.write_bytes(b"x")
    pages = {8: "PAGE ONE\n1 / 240\n", 9: "PAGE TWO\n2 / 240\n"}
    calls = []

    with pytest.raises(ees.ExtractionBlocked):
        ees.extract_segment(
            "CASE_030", "DOC_009", "DOC_001", [1, 2], 99,
            held_by="tester", run_id="RUN_1",
            manifest_reader=lambda case_id: _manifest(),
            page_reader=lambda path, physicals: pages,
            page_counter=lambda path: 20,
            source_verifier=lambda entry, path, count: "a" * 64,
            dao_call=lambda *args: calls.append(args) or "",
        )
    assert calls == []
