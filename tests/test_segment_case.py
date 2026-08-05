"""Pure-function tests for Stage 1 segmentation (build step 2).

No I/O and no provider calls -- rendering, compositing, and the split path arrive
in later build steps with their own tests.
"""
import json
from pathlib import Path

import pytest

import segment_case as sc


# ------------------------------------------------------------- geometry --

@pytest.mark.parametrize("cols,rows", [(2, 4), (3, 4), (4, 4), (3, 5), (1, 1)])
@pytest.mark.parametrize("crop", [0.25, 0.33, 0.5])
def test_geometry_always_lands_within_both_vision_caps(cols, rows, crop):
    """The whole point of computing geometry is arriving under the caps already.

    Exceeding either one means the API silently resamples the sheet, so we would
    pay to render detail the model never sees and lose control of the downscale.
    """
    geo = sc.compute_sheet_geometry(cols=cols, rows=rows, crop_ratio=crop)
    assert max(geo["sheet_w"], geo["sheet_h"]) <= sc.LONG_EDGE_CAP
    assert geo["sheet_w"] * geo["sheet_h"] <= sc.TOTAL_PIXEL_CAP
    assert geo["cell_w"] >= 1 and geo["cell_h"] >= 1


def test_geometry_zoom_lands_on_cell_width_without_an_intermediate_render():
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    rendered_width = sc.DEFAULT_PAGE_WIDTH_PT * geo["zoom"]
    assert abs(rendered_width - geo["cell_w"]) < 1.0


def test_geometry_rejects_impossible_requests():
    with pytest.raises(sc.SegmentationError):
        sc.compute_sheet_geometry(cols=0, rows=4)
    with pytest.raises(sc.SegmentationError):
        sc.compute_sheet_geometry(crop_ratio=0.9)
    with pytest.raises(sc.SegmentationError):
        # Separators would consume the entire sheet.
        sc.compute_sheet_geometry(cols=200, rows=200, separator_px=8)


def test_fewer_pages_per_sheet_buys_bigger_cells():
    """Records the tradeoff the grid-size decision turns on."""
    small = sc.compute_sheet_geometry(cols=2, rows=4)
    large = sc.compute_sheet_geometry(cols=4, rows=4)
    assert small["cell_w"] > large["cell_w"]
    assert small["pages_per_sheet"] < large["pages_per_sheet"]


# ---------------------------------------------------------- plan_sheets --

def test_plan_sheets_leaves_the_last_sheet_short_rather_than_padding():
    sheets = sc.plan_sheets(110, 12)
    assert len(sheets) == 10
    assert sheets[0] == list(range(1, 13))
    assert sheets[-1] == [109, 110]
    # Padding by repeating pages would manufacture phantom boundaries.
    flat = [p for sheet in sheets for p in sheet]
    assert flat == list(range(1, 111))


@pytest.mark.parametrize("count,per,expected", [(12, 12, 1), (13, 12, 2), (1, 12, 1), (24, 12, 2)])
def test_plan_sheets_off_by_one_boundaries(count, per, expected):
    assert len(sc.plan_sheets(count, per)) == expected


def test_plan_sheets_rejects_nonsense():
    with pytest.raises(sc.SegmentationError):
        sc.plan_sheets(0, 12)
    with pytest.raises(sc.SegmentationError):
        sc.plan_sheets(10, 0)


# -------------------------------------------------------------- parsing --

SHEET = list(range(1, 13))


def _response(**payload):
    return json.dumps(payload)


def test_parses_a_clean_response():
    raw = _response(
        boundaries=[{"page": 1, "type_guess": "diagnosis_certificate",
                     "type_label": "후유장해진단서",
                     "confidence": 0.8, "evidence": "form title"}],
        continuations=[2, 3],
        needs_full_page=[4],
    )
    out = sc.parse_segmentation_response(raw, SHEET)
    assert out["ok"] is True
    assert out["boundaries"][0]["page"] == 1
    assert out["boundaries"][0]["type_guess"] == "diagnosis_certificate"
    # The free-text label carries what the enum cannot: 후유장해진단서 and a
    # routine outpatient 진단서 collapse to the same enum value.
    assert out["boundaries"][0]["type_label"] == "후유장해진단서"
    assert out["continuations"] == [2, 3]
    assert out["needs_full_page"] == [4]
    assert out["warning"] is None


@pytest.mark.parametrize("wrapper", [
    'Here is the analysis:\n{payload}',
    '{payload}\n\nLet me know if you need more detail.',
    '```json\n{payload}\n```',
])
def test_tolerates_prose_around_the_json(wrapper):
    """Models wrap JSON in prose often enough that strict json.loads would
    discard otherwise-usable output -- and its vision call is already paid for."""
    payload = _response(boundaries=[{"page": 5}], continuations=[6])
    out = sc.parse_segmentation_response(wrapper.format(payload=payload), SHEET)
    assert out["ok"] is True
    assert out["boundaries"][0]["page"] == 5


def test_unparseable_response_never_invents_a_boundary():
    """The critical fail-safe: there is no safe default segmentation, so a
    failure must propose NOTHING rather than guess."""
    out = sc.parse_segmentation_response("the model refused to answer", SHEET)
    assert out["ok"] is False
    assert out["boundaries"] == []
    assert out["continuations"] == []
    assert out["warning"]


def test_parser_reports_failure_instead_of_raising():
    """One sheet failing must not abort the sheets around it."""
    for bad in ["", "not json", "{", '{"boundaries": "not-a-list"}', "[1,2,3]"]:
        out = sc.parse_segmentation_response(bad, SHEET)
        assert out["ok"] is False, bad
        assert out["boundaries"] == []


def test_page_number_outside_the_sheet_fails_the_response():
    """A page not on the sheet means the model lost track of which image it was
    looking at, which taints the whole response rather than one entry."""
    out = sc.parse_segmentation_response(_response(boundaries=[{"page": 99}]), SHEET)
    assert out["ok"] is False
    assert "99" in out["warning"]

    out = sc.parse_segmentation_response(
        _response(boundaries=[{"page": 1}], continuations=[99]), SHEET)
    assert out["ok"] is False


def test_rejects_malformed_entries():
    for payload in [
        _response(boundaries=[{"page": "one"}]),
        _response(boundaries=["p1"]),
        _response(boundaries=[{"page": 1, "confidence": 5}]),
        _response(boundaries=[{"page": 1}, {"page": 1}]),
        _response(boundaries=[{"page": 1}], continuations=[2.5]),
    ]:
        assert sc.parse_segmentation_response(payload, SHEET)["ok"] is False, payload


def test_dedupes_and_sorts_page_lists():
    out = sc.parse_segmentation_response(
        _response(boundaries=[{"page": 1}], needs_full_page=[7, 3, 7, 3]), SHEET)
    assert out["needs_full_page"] == [3, 7]


def test_document_type_enum_matches_the_schema():
    """The module keeps a literal copy to stay I/O-free; this catches drift."""
    from _validation import load_registry

    schemas, _ = load_registry()
    schema_enum = set(
        schemas["common_component_output.schema.json"]["$defs"]["document_type"]["enum"]
    )
    assert set(sc.DOCUMENT_TYPES) == schema_enum


def test_type_guess_outside_the_enum_is_dropped_but_the_wording_survives():
    """The enum has 8 buckets for a corpus with many more real form types, so a
    model naming a genuine type outside it is expected. Passing the unknown value
    through would break the manifest write; dropping it silently would lose what
    the model saw. Hence: null the enum field, keep the words in type_label.
    """
    out = sc.parse_segmentation_response(
        _response(boundaries=[{"page": 1, "type_guess": "claim_form"}]), SHEET)
    assert out["ok"] is True
    assert out["boundaries"][0]["type_guess"] is None
    assert out["boundaries"][0]["type_label"] == "claim_form"


def test_an_explicit_type_label_is_not_overwritten_by_the_fallback():
    out = sc.parse_segmentation_response(
        _response(boundaries=[{"page": 1, "type_guess": "claim_form", "type_label": "청구서"}]),
        SHEET)
    assert out["boundaries"][0]["type_label"] == "청구서"


def test_a_valid_enum_guess_is_kept():
    out = sc.parse_segmentation_response(
        _response(boundaries=[{"page": 1, "type_guess": "medical_record"}]), SHEET)
    assert out["boundaries"][0]["type_guess"] == "medical_record"


def test_case_d_contradiction_is_recorded_but_still_parses():
    """A page in both lists is contradictory; the parser flags it and lets merge
    apply the boundary-wins rule."""
    out = sc.parse_segmentation_response(
        _response(boundaries=[{"page": 5}], continuations=[5, 6]), SHEET)
    assert out["ok"] is True
    assert "5" in out["warning"]


# ---------------------------------------------------------------- merge --

def _sheet(boundaries=(), continuations=(), needs_full_page=(), ok=True, warning=None):
    return {
        "ok": ok,
        "boundaries": [b if isinstance(b, dict) else {"page": b} for b in boundaries],
        "continuations": list(continuations),
        "needs_full_page": list(needs_full_page),
        "warning": warning,
    }


def test_document_spanning_a_sheet_break_stays_one_segment():
    """The highest-value test here: sheet edges must carry no meaning.

    A document running p1-14 crosses the 12-page sheet boundary. Cutting at p12
    would silently split one document into two.
    """
    pages = sc.plan_sheets(24, 12)
    merged = sc.merge_sheet_proposals(
        [
            _sheet(boundaries=[1], continuations=range(2, 13)),
            _sheet(boundaries=[15], continuations=[13, 14] + list(range(16, 25))),
        ],
        page_count=24,
        sheet_pages=pages,
    )
    spans = [(s["page_start"], s["page_end"]) for s in merged["segments"]]
    assert spans == [(1, 14), (15, 24)]
    assert merged["unassigned_pages"] == []


def test_continuation_omitted_at_a_sheet_edge_is_absorbed_not_unassigned():
    """CASE_026's exact bug: a 문서 begins on sheet 1 (p1) and continues onto
    sheet 2, but the model forgot to list the sheet-2 first page (p13) in
    continuations -- it named p14.. but skipped p13. Without absorption, merge
    tore p13 (and the run after it) out as unassigned. p13 is strictly between
    boundaries p1 and p15, so it belongs to p1's document."""
    pages = sc.plan_sheets(24, 12)
    merged = sc.merge_sheet_proposals(
        [
            _sheet(boundaries=[1], continuations=range(2, 13)),
            # p13 omitted from continuations -- the model's sheet-edge slip.
            _sheet(boundaries=[15], continuations=[14] + list(range(16, 25))),
        ],
        page_count=24,
        sheet_pages=pages,
    )
    spans = [(s["page_start"], s["page_end"]) for s in merged["segments"]]
    assert spans == [(1, 14), (15, 24)]     # p13 absorbed into p1's document
    assert merged["unassigned_pages"] == []
    assert any("absorbed" in w for w in merged["warnings"])  # surfaced, not silent


def test_a_gap_after_the_last_boundary_is_still_unassigned():
    """Absorption must not reach past the final boundary: a page after the last
    document's start that no sheet named is a genuine model drop, not an interior
    continuation -- there is no enclosing document to absorb it into. This is the
    line that keeps case B intact."""
    pages = sc.plan_sheets(24, 12)
    merged = sc.merge_sheet_proposals(
        [
            _sheet(boundaries=[1], continuations=range(2, 13)),
            # last boundary is p15; p20 named by nothing, past the last boundary.
            _sheet(boundaries=[15], continuations=[13, 14, 16, 17, 18, 19, 21, 22, 23, 24]),
        ],
        page_count=24,
        sheet_pages=pages,
    )
    assert 20 in merged["unassigned_pages"]
    assert any("human review" in w for w in merged["warnings"])


def test_case_a_missing_first_boundary_is_treated_as_one():
    """Page 1 of a bundle necessarily begins some document; the model's silence
    does not change that."""
    merged = sc.merge_sheet_proposals(
        [_sheet(boundaries=[13], continuations=list(range(1, 13)) + list(range(14, 21)))],
        page_count=20,
        sheet_pages=[list(range(1, 21))],
    )
    spans = [(s["page_start"], s["page_end"]) for s in merged["segments"]]
    assert spans == [(1, 12), (13, 20)]
    assert any("page 1" in w for w in merged["warnings"])


def test_case_b_unmentioned_page_is_left_unassigned():
    """Absorbing a page the model never mentioned would let a human approve a
    document without knowing it holds an unreviewed page."""
    merged = sc.merge_sheet_proposals(
        [_sheet(boundaries=[1], continuations=[2, 3, 4, 5, 6, 8, 9, 10, 11, 12])],
        page_count=12,
        sheet_pages=[SHEET],
    )
    assert 7 in merged["unassigned_pages"]
    assert merged["segments"][0]["page_end"] == 6
    assert any("human review" in w for w in merged["warnings"])


def test_case_c_needs_full_page_flags_the_segment_without_splitting_it():
    """A page we could not judge is not evidence of a boundary, so it must not
    create one."""
    merged = sc.merge_sheet_proposals(
        [_sheet(boundaries=[1], continuations=[2, 3, 4, 5], needs_full_page=[3])],
        page_count=5,
        sheet_pages=[[1, 2, 3, 4, 5]],
    )
    assert len(merged["segments"]) == 1
    assert merged["segments"][0]["page_start"] == 1
    assert merged["segments"][0]["page_end"] == 5
    assert merged["segments"][0]["needs_full_page"] is True
    assert merged["needs_full_page"] == [3]


def test_case_d_boundary_wins_over_continuation():
    """Error costs are asymmetric: over-splitting is a human merge away, while
    over-merging only surfaces after downstream stages ran on wrong boundaries."""
    merged = sc.merge_sheet_proposals(
        [_sheet(boundaries=[1, 5], continuations=[2, 3, 4, 5, 6],
                warning="pages [5] were listed as both")],
        page_count=6,
        sheet_pages=[[1, 2, 3, 4, 5, 6]],
    )
    spans = [(s["page_start"], s["page_end"]) for s in merged["segments"]]
    assert spans == [(1, 4), (5, 6)]
    assert any("both" in w for w in merged["warnings"])


def test_a_failed_sheet_only_costs_its_own_pages():
    """Its neighbours' vision calls are already paid for and their answers are
    still good."""
    pages = sc.plan_sheets(24, 12)
    merged = sc.merge_sheet_proposals(
        [
            _sheet(boundaries=[1], continuations=range(2, 13)),
            _sheet(ok=False, warning="unparseable"),
        ],
        page_count=24,
        sheet_pages=pages,
    )
    assert merged["segments"][0]["page_start"] == 1
    assert set(range(13, 25)).issubset(set(merged["unassigned_pages"]))
    assert any("unparseable" in w for w in merged["warnings"])


def test_partial_run_does_not_fabricate_a_segment_at_page_one():
    """Found by feeding a real model response through: sheets covering p65-80 of
    an 80-page document produced a phantom SEG(1,1), because the 'page 1 starts a
    document' rule fired for a page no sheet had looked at."""
    merged = sc.merge_sheet_proposals(
        [_sheet(boundaries=[74], continuations=list(range(65, 74)))],
        page_count=80,
        sheet_pages=[list(range(65, 81))],
    )
    assert all(s["page_start"] != 1 for s in merged["segments"])
    assert 1 in merged["unassigned_pages"]


def test_page_one_rule_still_fires_when_page_one_was_examined():
    merged = sc.merge_sheet_proposals(
        [_sheet(boundaries=[5], continuations=[1, 2, 3, 4, 6])],
        page_count=6,
        sheet_pages=[[1, 2, 3, 4, 5, 6]],
    )
    assert merged["segments"][0]["page_start"] == 1


def test_merge_carries_boundary_metadata_into_the_segment():
    merged = sc.merge_sheet_proposals(
        [_sheet(boundaries=[{"page": 1, "type_guess": "receipt", "type_label": "영수증",
                             "confidence": 0.6, "evidence": "총액 stamp"}],
                continuations=[2, 3])],
        page_count=3,
        sheet_pages=[[1, 2, 3]],
    )
    seg = merged["segments"][0]
    assert seg["provisional_document_type"] == "receipt"
    assert seg["provisional_type_label"] == "영수증"
    assert seg["confidence"] == 0.6
    assert seg["boundary_evidence"] == "총액 stamp"
    assert seg["review_status"] == "pending"
    assert seg["assigned_document_id"] is None


def test_merge_output_passes_its_own_validator():
    pages = sc.plan_sheets(24, 12)
    merged = sc.merge_sheet_proposals(
        [
            _sheet(boundaries=[1, 7], continuations=[2, 3, 4, 5, 6, 8, 9, 10, 11, 12]),
            _sheet(boundaries=[15], continuations=[13, 14] + list(range(16, 25))),
        ],
        page_count=24,
        sheet_pages=pages,
    )
    assert sc.validate_segments(merged["segments"], 24) == []


# ----------------------------------------------------------- validation --

def test_validate_accepts_contiguous_segments():
    segments = [{"page_start": 1, "page_end": 5}, {"page_start": 6, "page_end": 10}]
    assert sc.validate_segments(segments, 10) == []


def test_validate_rejects_overlaps():
    errors = sc.validate_segments(
        [{"page_start": 1, "page_end": 6}, {"page_start": 5, "page_end": 10}], 10)
    assert any("overlap" in e for e in errors)


def test_validate_rejects_reversed_and_out_of_range():
    assert any("precedes" in e for e in sc.validate_segments([{"page_start": 8, "page_end": 3}], 10))
    assert any("exceeds" in e for e in sc.validate_segments([{"page_start": 1, "page_end": 99}], 10))
    assert any("below" in e for e in sc.validate_segments([{"page_start": 0, "page_end": 3}], 10))


def test_validate_allows_gaps_because_they_are_legitimate_review_state():
    """Gaps are recorded in unassigned_pages and blocked at split time, not here."""
    assert sc.validate_segments(
        [{"page_start": 1, "page_end": 3}, {"page_start": 7, "page_end": 10}], 10) == []


def test_validate_rejects_non_integer_pages():
    assert sc.validate_segments([{"page_start": "1", "page_end": 3}], 10)
    assert sc.validate_segments([{"page_start": 1, "page_end": None}], 10)


def test_validate_reports_every_problem_at_once():
    errors = sc.validate_segments(
        [{"page_start": 5, "page_end": 2}, {"page_start": 1, "page_end": 99}], 10)
    assert len(errors) >= 2


# ------------------------------------------------------------- manifest --

def _segments():
    return [
        {"page_start": 1, "page_end": 5, "provisional_document_type": "diagnosis_certificate"},
        {"page_start": 6, "page_end": 12, "provisional_document_type": "medical_record"},
    ]


def test_manifest_entries_number_from_start_index():
    """The bundle entry survives as a superseded record, so its id stays taken."""
    entries = sc.build_manifest_entries(
        _segments(), case_id="CASE_001", source_file_name="bundle.pdf",
        proposal_path="outputs/CASE_001/segmentation_proposal_DOC_001.json",
        start_index=2,
    )
    assert [e["document_id"] for e in entries] == ["DOC_002", "DOC_003"]


def test_manifest_entries_record_provenance_and_leave_document_type_null():
    """checkpoint 1 owns document_type and must classify against real OCR'd text,
    not a cropped thumbnail -- which is the entire reason
    provisional_document_type is a separate field."""
    entries = sc.build_manifest_entries(
        _segments(), case_id="CASE_001", source_file_name="bundle.pdf",
        proposal_path="outputs/CASE_001/segmentation_proposal_DOC_001.json",
        start_index=2,
    )
    first = entries[0]
    assert first["document_role"] == "physical"
    assert first["source_file_name"] == "bundle.pdf"
    assert first["source_page_start"] == 1
    assert first["source_page_end"] == 5
    assert "source_document_id" not in first
    assert "page_map" not in first
    assert first["provisional_document_type"] == "diagnosis_certificate"
    assert first["document_type"] is None
    assert first["classification_confidence"] is None
    assert first["ocr_status"] == "pending"
    assert first["pages"] is None
    assert first["segmentation_status"] == "completed"


def test_manifest_file_paths_use_forward_slashes_on_every_host():
    entries = sc.build_manifest_entries(
        _segments(), case_id="CASE_001", source_file_name="bundle.pdf",
        proposal_path="p.json", start_index=1,
    )
    for entry in entries:
        assert entry["file_path"] == f"data/raw/CASE_001/{entry['file_name']}"
        assert "\\" not in entry["file_path"]


def test_split_children_are_not_processed_text_segments():
    """A split child owns a real raw PDF and must use the physical UID path.

    ``source_file_name`` and ``source_page_*`` preserve the intake audit trail
    back to the superseded bundle.  They do not turn the child into the other
    manifest shape called ``segment``, which has no raw file and requires a
    DAO-issued page-map receipt over already-processed parent text.
    """
    entries = sc.build_manifest_entries(
        _segments(), case_id="CASE_001", source_file_name="bundle.pdf",
        proposal_path="p.json", start_index=2,
    )
    for entry in entries:
        assert entry["document_role"] == "physical"
        assert entry["file_path"].startswith("data/raw/CASE_001/")
        assert entry["file_format"] == "pdf"
        assert entry["source_file_name"] == "bundle.pdf"
        assert "source_document_id" not in entry
        assert "derivation_method" not in entry
        assert "derived_text_path" not in entry
        assert "page_map" not in entry


# ----------------------------------------------------------- cropping --


def test_crop_top_keeps_the_requested_fraction():
    from PIL import Image

    cropped = sc.crop_top(Image.new("RGB", (300, 900), "white"), 0.33)
    assert cropped.size == (300, 297)


def test_crop_top_never_exceeds_the_page():
    from PIL import Image

    cropped = sc.crop_top(Image.new("RGB", (300, 10), "white"), 0.6)
    assert cropped.size[1] <= 10


# ---------------------------------------------------- sheet composition --

def _geometry():
    return sc.compute_sheet_geometry(cols=2, rows=2, separator_px=4)


def _pages(page_numbers, geometry):
    from PIL import Image

    height = int(geometry["cell_h"] / geometry["crop_ratio"])
    return {n: Image.new("RGB", (geometry["cell_w"], height), "white")
            for n in page_numbers}


def test_composed_sheet_matches_the_computed_geometry():
    geo = _geometry()
    sheet, _ = sc.compose_contact_sheet(_pages([1, 2, 3, 4], geo), [1, 2, 3, 4], geo)
    assert sheet.size == (geo["sheet_w"], geo["sheet_h"])


def test_a_short_final_sheet_keeps_full_canvas_size():
    """Shrinking it would change geometry between sheets and break the model's
    spatial expectation; repeating pages would manufacture phantom boundaries."""
    geo = _geometry()
    sheet, _ = sc.compose_contact_sheet(_pages([109, 110], geo), [109, 110], geo)
    assert sheet.size == (geo["sheet_w"], geo["sheet_h"])


def test_unused_cells_are_blank_and_unlabelled():
    geo = _geometry()
    sheet, _ = sc.compose_contact_sheet(_pages([1, 2], geo), [1, 2], geo)
    sep = geo["separator_px"]
    # Bottom-right cell is unused: its interior must be white, not red-boxed.
    x = sep + (geo["cell_w"] + sep) + geo["cell_w"] // 2
    y = sep + (geo["cell_h"] + sep) + geo["cell_h"] // 2
    assert sheet.getpixel((x, y)) == sc.SHEET_BACKGROUND


def test_every_cell_is_fully_boxed_including_at_the_sheet_edge():
    """A cell bounded on only two sides is where 'is this the same document
    continuing?' ambiguity comes from."""
    geo = _geometry()
    sheet, _ = sc.compose_contact_sheet(_pages([1, 2, 3, 4], geo), [1, 2, 3, 4], geo)
    mid_x, mid_y = geo["sheet_w"] // 2, geo["sheet_h"] // 2
    assert sheet.getpixel((0, mid_y)) == sc.SEPARATOR_COLOR       # left edge
    assert sheet.getpixel((geo["sheet_w"] - 1, mid_y)) == sc.SEPARATOR_COLOR
    assert sheet.getpixel((mid_x, 0)) == sc.SEPARATOR_COLOR       # top edge
    assert sheet.getpixel((mid_x, geo["sheet_h"] - 1)) == sc.SEPARATOR_COLOR


def test_composition_flags_blank_pages():
    geo = _geometry()
    sheet, flags = sc.compose_contact_sheet(_pages([1, 2, 3, 4], geo), [1, 2, 3, 4], geo)
    # Every synthetic page here is blank white.
    assert flags["blank_pages"] == [1, 2, 3, 4]


def test_sheet_variants_cover_both_quarter_turns_and_the_original():
    """Roughly half this corpus is scanned sideways and which way is not
    detectable, so every sheet is produced in all three orientations and whoever
    reads them picks the legible one. Rendering is cheap; the model call is not,
    and only one variant per sheet is ever sent."""
    names = [name for name, _ in sc.SHEET_VARIANTS]
    angles = [angle for _, angle in sc.SHEET_VARIANTS]
    assert names == ["as_scanned", "cw", "ccw"]
    assert angles == [0, -90, 90]


def test_rotated_render_lands_on_cell_width_not_cell_height(tmp_path):
    """A quarter turn swaps the axes, so a naive render would come out sized to
    the wrong dimension and get squashed on paste."""
    import fitz

    pdf = tmp_path / "two.pdf"
    doc = fitz.open()
    for _ in range(2):
        doc.new_page(width=595, height=841)
    doc.save(pdf)
    doc.close()

    geo = sc.compute_sheet_geometry()
    upright = sc.render_page_images(pdf, [1], zoom=geo["zoom"])[1]
    turned = sc.render_page_images(pdf, [1], zoom=geo["zoom"], rotate=-90)[1]
    assert abs(upright.size[0] - geo["cell_w"]) <= 2
    assert abs(turned.size[0] - geo["cell_w"]) <= 2


def test_build_sheet_set_writes_one_file_per_sheet_per_variant(tmp_path):
    import fitz

    pdf = tmp_path / "bundle.pdf"
    doc = fitz.open()
    for _ in range(20):  # 20 pages at 16/sheet -> 2 sheets
        doc.new_page(width=595, height=841)
    doc.save(pdf)
    doc.close()

    result = sc.build_sheet_set(pdf, tmp_path / "sheets")
    assert set(result["sheets"]) == {"as_scanned", "cw", "ccw"}
    assert all(len(paths) == 2 for paths in result["sheets"].values())
    assert len(list((tmp_path / "sheets").glob("*.png"))) == 6
    # The variant has to be in the filename or a reviewer cannot tell them apart.
    for variant, paths in result["sheets"].items():
        assert all(variant in p.name for p in paths)


def test_geometry_fingerprint_changes_with_the_parameters():
    """Without this, changing --crop-ratio silently reuses stale sheets and the
    operator compares two runs that actually saw identical images."""
    base = sc.compute_sheet_geometry(cols=3, rows=4, crop_ratio=0.33)
    other = sc.compute_sheet_geometry(cols=3, rows=4, crop_ratio=0.4)
    assert sc.geometry_fingerprint(base, page_count=110) != sc.geometry_fingerprint(other, page_count=110)
    assert sc.geometry_fingerprint(base, page_count=110) != sc.geometry_fingerprint(base, page_count=77)
    assert sc.geometry_fingerprint(base, page_count=110) == sc.geometry_fingerprint(base, page_count=110)


def test_sheets_dir_is_stable_and_not_pid_tagged():
    """Sheets are read by a human after the process exits, and a resumed run must
    find the previous run's renders rather than redoing 110 pages."""
    assert sc.sheets_dir("CASE_001", "DOC_001") == sc.sheets_dir("CASE_001", "DOC_001")
    assert sc.sheets_dir("CASE_001", "DOC_001").parent == sc.SCRATCH_ROOT


# ---------------------------------------------------------------- prompt --

def test_prompt_states_the_actual_grid_shape():
    geo = sc.compute_sheet_geometry(cols=4, rows=4)
    prompt = sc.build_segment_prompt(list(range(1, 17)), geo)
    assert "4x4" in prompt
    assert "16 cells" in prompt


def test_prompt_mentions_blank_cells_only_on_a_short_sheet():
    """Saying it on a full sheet would invite the model to hunt for absent cells."""
    geo = sc.compute_sheet_geometry(cols=4, rows=4)
    assert "blank" not in sc.build_segment_prompt(list(range(1, 17)), geo)
    short = sc.build_segment_prompt([109, 110], geo)
    assert "first 2 cells" in short


def test_prompt_tells_the_model_to_read_rotated_cells_as_is():
    """46% of the real bundle is rotated and we deliberately do not straighten
    it, so the prompt has to carry that instruction."""
    geo = sc.compute_sheet_geometry()
    prompt = sc.build_segment_prompt([1, 2, 3], geo)
    assert "rotated a quarter turn" in prompt
    assert "whatever orientation" in prompt


def test_prompt_carries_no_self_legitimizing_language():
    """A prior version added 'this is a sanctioned step / do not refuse' framing
    and the child model read it as prompt injection and refused. A genuine layout
    question does not argue for itself."""
    prompt = sc.build_segment_prompt([1], sc.compute_sheet_geometry()).lower()
    for phrase in ["sanctioned", "do not refuse", "you are allowed", "authorized",
                   "guardrail", "permitted"]:
        assert phrase not in prompt


def test_type_guess_enum_is_enforced_by_the_schema_not_the_prompt():
    """The prompt used to spell out every DOCUMENT_TYPES value. That listing cost
    ~190 characters and pushed the rendered prompt over the length at which the
    claude-cli child abandons the --json-schema envelope and answers in prose
    (measured: 980 chars OK, 1064 chars prose 3/3). The constraint did not need to
    live in the prompt: SEGMENT_OUTPUT_SCHEMA pins type_guess to the same enum, and
    parse_segmentation_response drops an out-of-enum guess while keeping type_label
    (see test_type_guess_outside_the_enum_is_dropped_but_the_wording_survives).
    So the enum is still enforced -- twice -- just not by spending prompt budget."""
    schema_enum = (
        sc.SEGMENT_OUTPUT_SCHEMA["properties"]["boundaries"]["items"]
        ["properties"]["type_guess"]["enum"]
    )
    assert set(schema_enum) == set(sc.DOCUMENT_TYPES) | {None}
    prompt = sc.build_segment_prompt([1], sc.compute_sheet_geometry())
    assert len(prompt) < 1000, f"prompt grew to {len(prompt)} chars; see the 1064-char prose threshold"


def test_manifest_entries_validate_against_the_real_schema():
    """The provenance and v0.6 segmentation-gate fields survive validation."""
    from _validation import load_registry, validate_instance

    schemas, registry = load_registry()
    entries = sc.build_manifest_entries(
        _segments(), case_id="CASE_001", source_file_name="bundle.pdf",
        proposal_path="outputs/CASE_001/segmentation_proposal_DOC_001.json",
        start_index=2,
    )
    manifest = {"case_id": "CASE_001", "documents": entries}
    assert validate_instance(manifest, "document_manifest.schema.json", schemas, registry) == []


# ----------------------------------------------------- provider path --

class _SequencedProvider:
    """A FixtureProvider-shaped stub returning a canned response per sheet, and
    counting transcribe_image calls so a resume test can assert none were made.

    Sheets are called in order, so the Nth call gets responses[N]. A short list
    repeats its last entry -- convenient for "every sheet says the same thing".
    """
    provider_name = "fixture"

    def __init__(self, responses, *, model_name="fixture-model"):
        self.model_name = model_name
        self._responses = list(responses)
        self.calls = 0

    def transcribe_image(self, image_path, prompt, prompt_version):
        from llm_providers import ProviderResult
        idx = min(self.calls, len(self._responses) - 1)
        text = self._responses[idx]
        self.calls += 1
        return ProviderResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            prompt_version=prompt_version,
            text=text,
        )


class _StructuredSequencedProvider:
    provider_name = "local-vlm"
    model_name = "local-test-model"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.schemas = []

    def analyze_image_structured(
        self, image_path, prompt, prompt_version, output_schema
    ):
        from llm_providers import ProviderResult
        idx = min(self.calls, len(self._responses) - 1)
        value = self._responses[idx]
        self.calls += 1
        self.schemas.append(output_schema)
        if isinstance(value, dict):
            return ProviderResult(
                provider_name=self.provider_name,
                model_name=self.model_name,
                prompt_version=prompt_version,
                text=json.dumps(value),
                structured_output=value,
            )
        return ProviderResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            prompt_version=prompt_version,
            text=value,
        )


def _bundle_pdf(tmp_path, pages):
    import fitz
    pdf = tmp_path / "bundle.pdf"
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=595, height=841)
    doc.save(pdf)
    doc.close()
    return pdf


def _sheet_files(tmp_path, count):
    from PIL import Image
    paths = []
    for i in range(count):
        p = tmp_path / f"sheet_{i}.png"
        Image.new("RGB", (10, 10), (255, 255, 255)).save(p)
        paths.append(p)
    return paths


def test_propose_merges_sheets_and_records_an_honest_method(tmp_path):
    """One 12-cell sheet, a boundary at p1 and p6; the method block reflects the
    real provider and geometry, and ocr_performed is const-false."""
    geo = sc.compute_sheet_geometry(cols=3, rows=4)  # 12 per sheet
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    provider = _SequencedProvider([_response(
        boundaries=[
            {"page": 1, "type_guess": "other", "type_label": "표지",
             "confidence": 0.9, "evidence": "cover"},
            {"page": 6, "type_guess": "receipt", "type_label": "영수증",
             "confidence": 0.7, "evidence": "new form"},
        ],
        continuations=[2, 3, 4, 5, 7, 8, 9, 10, 11, 12],
        needs_full_page=[],
    )])

    out = sc.propose_boundaries(
        pdf, case_id="CASE_900", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
    )

    assert [(s["page_start"], s["page_end"]) for s in out["segments"]] == [(1, 5), (6, 12)]
    assert out["method"]["ocr_performed"] is False
    assert out["method"]["mode"] == "vision_proposal"
    assert out["method"]["provider_name"] == "fixture"
    assert out["method"]["model_name"] == "fixture-model"
    assert out["method"]["grid_cols"] == 3 and out["method"]["grid_rows"] == 4
    assert provider.calls == 1


def test_propose_uses_provider_neutral_structured_image_contract(tmp_path):
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    provider = _StructuredSequencedProvider([{
        "boundaries": [{"page": 1}],
        "continuations": list(range(2, 13)),
        "needs_full_page": [],
    }])

    out = sc.propose_boundaries(
        pdf, case_id="CASE_940", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
    )

    assert provider.calls == 1
    assert provider.schemas == [sc.SEGMENT_OUTPUT_SCHEMA]
    assert out["unassigned_pages"] == []


def test_propose_corrects_unstructured_sheet_exactly_once(tmp_path):
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    valid = {
        "boundaries": [{"page": 1}],
        "continuations": list(range(2, 13)),
        "needs_full_page": [],
    }
    provider = _StructuredSequencedProvider(["analysis prose", valid])

    out = sc.propose_boundaries(
        pdf, case_id="CASE_941", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
    )

    assert provider.calls == 2
    assert out["per_sheet"][0]["ok"] is True
    assert out["unassigned_pages"] == []


def test_non_native_provider_cannot_omit_required_structured_fields(tmp_path):
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    incomplete = {"boundaries": [{"page": 1}]}
    complete = {
        "boundaries": [{"page": 1}],
        "continuations": list(range(2, 13)),
        "needs_full_page": [],
    }
    provider = _StructuredSequencedProvider([incomplete, complete])

    out = sc.propose_boundaries(
        pdf, case_id="CASE_945", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
    )

    assert provider.calls == 2
    assert out["per_sheet"][0]["ok"] is True
    assert out["unassigned_pages"] == []


def test_propose_halts_sheet_after_one_failed_correction(tmp_path):
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    provider = _StructuredSequencedProvider(["analysis prose", "still prose"])

    out = sc.propose_boundaries(
        pdf, case_id="CASE_942", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
    )

    assert provider.calls == 2
    assert out["per_sheet"][0]["ok"] is False
    assert set(out["unassigned_pages"]) == set(range(1, 13))
    assert "exactly one" in out["per_sheet"][0]["warning"]


def test_propose_output_assembles_into_a_schema_valid_proposal(tmp_path):
    """The whole point of the method/segment shapes is that they validate."""
    from _validation import load_registry, validate_instance

    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    provider = _SequencedProvider([_response(
        boundaries=[{"page": 1, "type_guess": "other", "type_label": "표지",
                     "confidence": 0.9, "evidence": "cover"}],
        continuations=list(range(2, 13)),
        needs_full_page=[],
    )])
    out = sc.propose_boundaries(
        pdf, case_id="CASE_900", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
    )
    doc = sc.build_proposal_document(
        out, case_id="CASE_900", source_document_id="DOC_001",
        source_file_name="bundle.pdf",
        source_file_path="data/raw/CASE_900/DOC_001.pdf",
        page_count=12, created_at="2026-07-21T00:00:00Z",
    )
    schemas, registry = load_registry()
    assert validate_instance(doc, "segmentation_proposal.schema.json", schemas, registry) == []
    assert doc["review_status"] == "pending"
    assert all(s["review_status"] == "pending" for s in doc["segments"])


def test_a_second_run_reuses_the_cache_and_calls_the_provider_zero_times(tmp_path):
    """An interrupted propose must not re-pay for sheets it already called --
    the exact loss ocr_extract's resume cache came out of."""
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    resp = [_response(
        boundaries=[{"page": 1, "type_guess": "other", "type_label": "x",
                     "confidence": 0.9, "evidence": "cover"}],
        continuations=list(range(2, 13)), needs_full_page=[],
    )]

    first = _SequencedProvider(resp)
    sc.propose_boundaries(pdf, case_id="CASE_901", doc_id="DOC_001",
                          provider=first, geometry=geo, sheet_paths=sheets, resume=True)
    assert first.calls == 1

    second = _SequencedProvider(resp)
    out = sc.propose_boundaries(pdf, case_id="CASE_901", doc_id="DOC_001",
                                provider=second, geometry=geo, sheet_paths=sheets, resume=True)
    assert second.calls == 0  # served entirely from cache
    assert [(s["page_start"], s["page_end"]) for s in out["segments"]] == [(1, 12)]

    # Clean up this test's stable (non-tmp) resume dir.
    import shutil
    shutil.rmtree(sc._resume_dir("CASE_901", "DOC_001"), ignore_errors=True)


def test_a_geometry_change_invalidates_the_cache(tmp_path):
    """Reusing a sheet rendered under a different crop/grid would compare a run
    against images it never saw -- the near-invisible bug the fingerprint guards."""
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    resp = [_response(
        boundaries=[{"page": 1, "type_guess": "other", "type_label": "x",
                     "confidence": 0.9, "evidence": "c"}],
        continuations=list(range(2, 13)), needs_full_page=[],
    )]

    geo_a = sc.compute_sheet_geometry(cols=3, rows=4, crop_ratio=0.33)
    p1 = _SequencedProvider(resp)
    sc.propose_boundaries(pdf, case_id="CASE_902", doc_id="DOC_001",
                          provider=p1, geometry=geo_a, sheet_paths=sheets, resume=True)

    geo_b = sc.compute_sheet_geometry(cols=3, rows=4, crop_ratio=0.4)
    p2 = _SequencedProvider(resp)
    sc.propose_boundaries(pdf, case_id="CASE_902", doc_id="DOC_001",
                          provider=p2, geometry=geo_b, sheet_paths=sheets, resume=True)
    assert p2.calls == 1  # different geometry -> cache miss -> real call

    import shutil
    shutil.rmtree(sc._resume_dir("CASE_902", "DOC_001"), ignore_errors=True)


def test_failed_cache_is_diagnostic_only_and_is_recalled_next_run(tmp_path):
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    case_id = "CASE_943"
    doc_id = "DOC_001"

    failed = _StructuredSequencedProvider(["prose", "more prose"])
    first = sc.propose_boundaries(
        pdf, case_id=case_id, doc_id=doc_id, provider=failed,
        geometry=geo, sheet_paths=sheets, resume=True,
    )
    assert first["per_sheet"][0]["ok"] is False
    assert failed.calls == 2

    valid = _StructuredSequencedProvider([{
        "boundaries": [{"page": 1}],
        "continuations": list(range(2, 13)),
        "needs_full_page": [],
    }])
    second = sc.propose_boundaries(
        pdf, case_id=case_id, doc_id=doc_id, provider=valid,
        geometry=geo, sheet_paths=sheets, resume=True,
    )

    assert valid.calls == 1
    assert second["per_sheet"][0]["ok"] is True

    import shutil
    shutil.rmtree(sc._resume_dir(case_id, doc_id), ignore_errors=True)


def test_provider_or_model_change_invalidates_success_cache(tmp_path):
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    case_id = "CASE_944"
    doc_id = "DOC_001"
    response = _response(
        boundaries=[{"page": 1}],
        continuations=list(range(2, 13)),
        needs_full_page=[],
    )

    first = _SequencedProvider([response], model_name="model-a")
    sc.propose_boundaries(
        pdf, case_id=case_id, doc_id=doc_id, provider=first,
        geometry=geo, sheet_paths=sheets, resume=True,
    )

    second = _SequencedProvider([response], model_name="model-b")
    sc.propose_boundaries(
        pdf, case_id=case_id, doc_id=doc_id, provider=second,
        geometry=geo, sheet_paths=sheets, resume=True,
    )

    assert second.calls == 1

    import shutil
    shutil.rmtree(sc._resume_dir(case_id, doc_id), ignore_errors=True)


def test_fallback_saturation_flags_without_triggering(tmp_path):
    """More needs_full_page pages than the cap allows: the fallback is skipped
    and the pages stay flagged, per the plan's tuning-signal policy."""
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    # 5 of 12 pages need a full-page look; cap at 0.25*12 = 3.
    provider = _SequencedProvider([_response(
        boundaries=[{"page": 1, "type_guess": "other", "type_label": "x",
                     "confidence": 0.9, "evidence": "c"}],
        continuations=[7, 8, 9, 10, 11, 12],
        needs_full_page=[2, 3, 4, 5, 6],
    )])
    out = sc.propose_boundaries(
        pdf, case_id="CASE_903", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
    )
    fb = out["method"]["full_page_fallback"]
    assert fb["saturated"] is True
    assert fb["triggered"] is False
    assert fb["cap"] == 3
    assert set(fb["pages"]) == {2, 3, 4, 5, 6}
    assert any("saturated" in w for w in out["warnings"])


def test_crop_ambiguous_pages_are_actually_rechecked_full_page(tmp_path):
    """Under the cap, needs_full_page is an executed second pass, not merely
    metadata. A titled full page becomes a boundary; a titleless one becomes a
    continuation and both uncertainty flags clear."""
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    sheet_text = _response(
        boundaries=[{"page": 1, "type_guess": "other", "type_label": "x",
                     "confidence": 0.9, "evidence": "c"}],
        continuations=[2, 3, 4, 6, 7, 9, 10, 11, 12],
        needs_full_page=[5, 8],
    )

    class _TargetedFallbackProvider:
        provider_name = "fixture"
        model_name = "fixture-model"
        def __init__(self):
            self.calls = 0
        def transcribe_image(self, image_path, prompt, prompt_version):
            from llm_providers import ProviderResult
            import re
            self.calls += 1
            name = str(image_path)
            if "fullpage" not in name:
                text = sheet_text
            else:
                page = int(re.search(r"p(\d+)", name).group(1))
                text = _response(
                    starts_new_document=(page == 5),
                    type_label="진단서" if page == 5 else None,
                    confidence=0.9,
                    evidence="own title" if page == 5 else "no title",
                )
            return ProviderResult(
                provider_name=self.provider_name,
                model_name=self.model_name,
                prompt_version=prompt_version,
                text=text,
            )

    provider = _TargetedFallbackProvider()
    out = sc.propose_boundaries(
        pdf, case_id="CASE_911", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
        refine_scratch_dir=tmp_path / "fp",
    )

    assert provider.calls == 3  # one sheet + two targeted full-page calls
    assert [(s["page_start"], s["page_end"]) for s in out["segments"]] == [
        (1, 4), (5, 12),
    ]
    assert out["needs_full_page"] == []
    fallback = out["method"]["full_page_fallback"]
    assert fallback["triggered"] is True
    assert fallback["resolved_pages"] == [5, 8]
    assert fallback["unresolved_pages"] == []
    assert fallback["new_boundaries"] == [5]


def test_failed_targeted_fallback_stays_flagged_for_human_review(tmp_path):
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    sheet_text = _response(
        boundaries=[{"page": 1, "type_guess": "other", "type_label": "x",
                     "confidence": 0.9, "evidence": "c"}],
        continuations=[2, 3, 4, 6, 7, 8, 9, 10, 11, 12],
        needs_full_page=[5],
    )

    class _FailingFallbackProvider:
        provider_name = "fixture"
        model_name = "fixture-model"
        def transcribe_image(self, image_path, prompt, prompt_version):
            from llm_providers import ProviderResult
            if "fullpage" in str(image_path):
                raise RuntimeError("vision unavailable")
            return ProviderResult(
                provider_name=self.provider_name,
                model_name=self.model_name,
                prompt_version=prompt_version,
                text=sheet_text,
            )

    out = sc.propose_boundaries(
        pdf, case_id="CASE_912", doc_id="DOC_001",
        provider=_FailingFallbackProvider(), geometry=geo,
        sheet_paths=sheets, resume=False, refine_scratch_dir=tmp_path / "fp",
    )

    assert out["needs_full_page"] == [5]
    assert out["segments"][0]["needs_full_page"] is True
    fallback = out["method"]["full_page_fallback"]
    assert fallback["triggered"] is True
    assert fallback["resolved_pages"] == []
    assert fallback["unresolved_pages"] == [5]
    assert any("remain flagged for human review" in w for w in out["warnings"])


def test_a_parse_failed_sheet_leaves_its_pages_unassigned(tmp_path):
    """One sheet's garbage response must not discard the other sheet, and must
    never invent a boundary -- its pages fall through to unassigned."""
    geo = sc.compute_sheet_geometry(cols=3, rows=4)  # 12 per sheet
    pdf = _bundle_pdf(tmp_path, 24)  # 2 sheets
    sheets = _sheet_files(tmp_path, 2)
    good = _response(
        boundaries=[{"page": 1, "type_guess": "other", "type_label": "x",
                     "confidence": 0.9, "evidence": "c"}],
        continuations=list(range(2, 13)), needs_full_page=[],
    )
    provider = _SequencedProvider([good, "not json at all"])
    out = sc.propose_boundaries(
        pdf, case_id="CASE_904", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
    )
    # Sheet 0 (p1-12) segments cleanly; sheet 1 (p13-24) failed -> unassigned.
    assert [(s["page_start"], s["page_end"]) for s in out["segments"]] == [(1, 12)]
    assert set(out["unassigned_pages"]) == set(range(13, 25))


def test_propose_rejects_a_sheet_path_count_mismatch(tmp_path):
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 24)  # plans 2 sheets
    provider = _SequencedProvider(["{}"])
    with pytest.raises(sc.SegmentationError):
        sc.propose_boundaries(
            pdf, case_id="CASE_905", doc_id="DOC_001", provider=provider,
            geometry=geo, sheet_paths=_sheet_files(tmp_path, 1), resume=False,
        )


# ------------------------------------------------------- approve/split --

def _proposal(segments, *, page_count=12, review_status="pending", unassigned=None):
    return {
        "case_id": "CASE_900", "source_document_id": "DOC_001",
        "source_file_name": "bundle.pdf",
        "source_file_path": "data/raw/CASE_900/DOC_001.pdf",
        "source_page_count": page_count,
        "created_at": "2026-07-21T00:00:00Z", "updated_at": "2026-07-21T00:00:00Z",
        "review_status": review_status,
        "reviewed_by": None, "reviewed_at": None, "rejection_reason": None,
        "method": {"ocr_performed": False, "method_version": "v", "mode": "vision_proposal",
                   "crop_ratio": 0.33, "grid_cols": 3, "grid_rows": 4},
        "segments": segments,
        "unassigned_pages": unassigned or [],
        "warnings": [],
    }


def _seg(index, start, end, status="pending"):
    return {"segment_index": index, "page_start": start, "page_end": end,
            "review_status": status, "provisional_document_type": None,
            "provisional_type_label": None, "confidence": None,
            "boundary_evidence": None, "needs_full_page": False,
            "orientation_suspect": False, "assigned_document_id": None}


def test_case_level_approval_advances_the_gate_and_sweeps_pending_segments():
    prop = _proposal([_seg(0, 1, 5), _seg(1, 6, 12)])
    out = sc.apply_approval(prop, reviewer="Rekhet", now="2026-07-21T01:00:00Z")
    assert out["review_status"] == "approved"
    assert out["reviewed_by"] == "Rekhet"
    assert all(s["review_status"] == "approved" for s in out["segments"])
    # Pure: the input is untouched.
    assert prop["review_status"] == "pending"


def test_bulk_approval_does_not_un_reject_a_segment():
    prop = _proposal([_seg(0, 1, 5), _seg(1, 6, 12, status="rejected")])
    out = sc.apply_approval(prop, reviewer="R", now="t")
    assert out["segments"][0]["review_status"] == "approved"
    assert out["segments"][1]["review_status"] == "rejected"  # preserved


def test_editing_a_range_marks_it_edited_so_the_correction_is_recorded():
    prop = _proposal([_seg(0, 1, 5), _seg(1, 6, 12)])
    out = sc.apply_approval(prop, reviewer="R", now="t", segment_index=1, edit=(7, 12))
    assert out["segments"][1]["page_start"] == 7
    assert out["segments"][1]["review_status"] == "edited"


def test_approving_a_single_segment_leaves_the_case_gate_alone():
    prop = _proposal([_seg(0, 1, 5), _seg(1, 6, 12)])
    out = sc.apply_approval(prop, reviewer="R", now="t", segment_index=0)
    assert out["segments"][0]["review_status"] == "approved"
    assert out["review_status"] == "pending"  # case gate not advanced by a per-segment approve


def test_approval_rejects_an_out_of_range_segment_index():
    prop = _proposal([_seg(0, 1, 12)])
    with pytest.raises(sc.SegmentationError):
        sc.apply_approval(prop, reviewer="R", now="t", segment_index=5)


def test_split_readiness_requires_case_approval():
    prop = _proposal([_seg(0, 1, 12, status="approved")], review_status="pending")
    errors = sc.split_readiness_errors(prop)
    assert any("case-level" in e for e in errors)


def test_split_readiness_blocks_on_unassigned_pages():
    prop = _proposal([_seg(0, 1, 11, status="approved")],
                     review_status="approved", unassigned=[12])
    errors = sc.split_readiness_errors(prop)
    assert any("unassigned" in e for e in errors)


def test_split_readiness_blocks_on_a_pending_or_rejected_segment():
    prop = _proposal([_seg(0, 1, 5, status="approved"), _seg(1, 6, 12, status="pending")],
                     review_status="approved")
    assert any("not approved/edited" in e for e in sc.split_readiness_errors(prop))
    prop["segments"][1]["review_status"] = "rejected"
    assert any("was rejected" in e for e in sc.split_readiness_errors(prop))


def test_split_readiness_runs_validate_segments():
    """An edit that introduced an overlap must be caught before splitting."""
    prop = _proposal([_seg(0, 1, 7, status="approved"), _seg(1, 5, 12, status="edited")],
                     review_status="approved")
    assert any("overlap" in e for e in sc.split_readiness_errors(prop))


def test_split_readiness_passes_a_clean_approved_proposal():
    prop = _proposal([_seg(0, 1, 5, status="approved"), _seg(1, 6, 12, status="edited")],
                     review_status="approved")
    assert sc.split_readiness_errors(prop) == []


def test_next_document_index_continues_past_the_highest_existing_id():
    manifest = {"documents": [{"document_id": "DOC_001"}, {"document_id": "DOC_004"},
                              {"document_id": "GT_002"}]}
    assert sc._next_document_index(manifest) == 5


class _FakeDao:
    """Captures a replace_manifest_documents call instead of touching disk."""
    def __init__(self, *, ok=True, message="PASS"):
        self.ok = ok
        self.message = message
        self.calls = []

    def replace_manifest_documents(self, case_id, bundle_id, bundle_fields,
                                   new_documents, held_by, run_id, **kw):
        self.calls.append({"case_id": case_id, "bundle_id": bundle_id,
                           "bundle_fields": bundle_fields, "new_documents": new_documents})
        return self.ok, self.message


def _manifest_with_bundle(bundle_id="DOC_001"):
    return {"case_id": "CASE_900", "documents": [{
        "document_id": bundle_id, "file_name": f"{bundle_id}.pdf",
        "file_path": f"data/raw/CASE_900/{bundle_id}.pdf", "file_format": "pdf",
        "file_size_bytes": 1234, "ocr_status": "pending",
        "source_file_name": "bundle.pdf",
    }]}


def test_split_writes_one_pdf_per_segment_with_correct_page_counts(tmp_path):
    pdf = _bundle_pdf(tmp_path, 12)
    prop = _proposal([_seg(0, 1, 5, status="approved"), _seg(1, 6, 12, status="approved")],
                     review_status="approved")
    dao = _FakeDao()
    # Point ROOT's data/raw at tmp so the test never writes into the real tree.
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        out = sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_1", dao=dao,
        )
    finally:
        sc.ROOT = orig_root

    assert out["status"] == "split"
    assert out["new_document_ids"] == ["DOC_002", "DOC_003"]
    import fitz
    p2 = tmp_path / "data" / "raw" / "CASE_900" / "DOC_002.pdf"
    p3 = tmp_path / "data" / "raw" / "CASE_900" / "DOC_003.pdf"
    with fitz.open(p2) as d:
        assert d.page_count == 5
    with fitz.open(p3) as d:
        assert d.page_count == 7


def test_split_preserves_the_text_layer_of_every_page(tmp_path):
    """A split child must extract the same text its source page does.

    Regression: split_bundle used insert_pdf(), which rebuilds page resources
    into a fresh document and dropped the glyphs of pages whose text is drawn
    in a subset TrueType font with a mislabelled encoding -- the Korean cover
    pages of CASE_905's two policy bundles ("영업배상책임보험\n보통약관").  A
    cover extracting 0 chars reads as a genuine scan to
    ocr_extract.pdf_embedded_page_texts(), silently routing the segment to
    vision OCR.  Nothing failed loudly; the only symptom was cost.
    """
    import fitz

    # NOTE on fixture fidelity: the production trigger is a page drawing text
    # in a subset TrueType font whose /BaseFont name is UTF-8 bytes reinterpreted
    # as Latin-1 ("ABCDEE+ë\x8f\x8bì\x9b\x80") and whose encoding is mislabelled
    # WinAnsi.  A synthetic PDF built with PyMuPDF does not reproduce that
    # corruption -- both insert_pdf and select keep its text -- so this test
    # asserts the invariant (child text == source text) rather than proving the
    # old implementation fails.  The measured evidence for the fix is in the
    # real bundles: across CASE_905's 323 policy pages, insert_pdf silently
    # dropped the text layer on 3 cover pages and select on 0.
    pdf = tmp_path / "textful.pdf"
    with fitz.open() as doc:
        for i in range(4):
            page = doc.new_page()
            page.insert_text((72, 100), f"PAGE {i + 1} CONTENT", fontsize=14)
        doc.save(pdf)

    with fitz.open(pdf) as doc:
        source_text = [doc[i].get_text().strip() for i in range(4)]
    assert all(source_text), "fixture must have text on every page"

    prop = _proposal([_seg(0, 1, 1, status="approved"),
                      _seg(1, 2, 4, status="approved")],
                     review_status="approved")
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        out = sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_1",
            dao=_FakeDao(),
        )
    finally:
        sc.ROOT = orig_root

    assert out["status"] == "split"
    raw = tmp_path / "data" / "raw" / "CASE_900"
    with fitz.open(raw / "DOC_002.pdf") as d:
        assert d[0].get_text().strip() == source_text[0]
    with fitz.open(raw / "DOC_003.pdf") as d:
        assert [d[i].get_text().strip() for i in range(3)] == source_text[1:]


def test_split_returns_proposal_linked_to_created_document_ids(tmp_path):
    pdf = _bundle_pdf(tmp_path, 12)
    prop = _proposal([
        _seg(0, 1, 5, status="approved"),
        _seg(1, 6, 12, status="approved"),
    ], review_status="approved")
    dao = _FakeDao()
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        out = sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_1", dao=dao,
        )
    finally:
        sc.ROOT = orig_root

    assert [
        segment["assigned_document_id"]
        for segment in out["updated_proposal"]["segments"]
    ] == ["DOC_002", "DOC_003"]
    assert all(segment["assigned_document_id"] is None for segment in prop["segments"])


def test_split_marks_the_bundle_superseded_and_records_provenance(tmp_path):
    pdf = _bundle_pdf(tmp_path, 12)
    prop = _proposal([_seg(0, 1, 12, status="approved")], review_status="approved")
    dao = _FakeDao()
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_1", dao=dao,
        )
    finally:
        sc.ROOT = orig_root

    call = dao.calls[0]
    assert call["bundle_fields"]["downstream_disposition"] == "superseded_bundle"
    assert call["bundle_fields"]["ocr_status"] == "not_applicable"
    assert call["bundle_fields"]["segmentation_proposal_path"].endswith("DOC_001.json")
    entry = call["new_documents"][0]
    assert entry["source_file_name"] == "bundle.pdf"
    assert entry["source_page_start"] == 1 and entry["source_page_end"] == 12
    assert entry["document_type"] is None  # checkpoint 1 owns it, not segmentation


def test_split_result_manifest_validates_against_the_real_schema(tmp_path):
    """The superseded bundle + new entries must together satisfy the schema's
    conditional for a superseded_bundle disposition."""
    from _validation import load_registry, validate_instance

    pdf = _bundle_pdf(tmp_path, 12)
    prop = _proposal([_seg(0, 1, 6, status="approved"), _seg(1, 7, 12, status="approved")],
                     review_status="approved")
    dao = _FakeDao()
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_1", dao=dao,
        )
    finally:
        sc.ROOT = orig_root

    # Reconstruct what replace_manifest_documents would have written.
    manifest = _manifest_with_bundle()
    manifest["documents"][0].update(dao.calls[0]["bundle_fields"])
    manifest["documents"].extend(dao.calls[0]["new_documents"])
    schemas, registry = load_registry()
    assert validate_instance(manifest, "document_manifest.schema.json", schemas, registry) == []


def test_split_refuses_a_not_ready_proposal(tmp_path):
    pdf = _bundle_pdf(tmp_path, 12)
    prop = _proposal([_seg(0, 1, 12)], review_status="pending")  # nothing approved
    dao = _FakeDao()
    out = sc.split_bundle(
        prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
        proposal_path="p.json", manifest=_manifest_with_bundle(),
        held_by="R", run_id="RUN_1", dao=dao,
    )
    assert out["status"] == "not_ready"
    assert dao.calls == []  # never reached the manifest write


def test_split_is_idempotent_when_entries_already_exist(tmp_path):
    pdf = _bundle_pdf(tmp_path, 12)
    prop = _proposal([_seg(0, 1, 12, status="approved")], review_status="approved")
    manifest = _manifest_with_bundle()
    # Simulate a prior split: a per-document entry already carries this range.
    manifest["documents"].append({
        "document_id": "DOC_002", "file_name": "DOC_002.pdf",
        "file_path": "data/raw/CASE_900/DOC_002.pdf", "file_format": "pdf",
        "file_size_bytes": 10, "ocr_status": "pending",
        "source_file_name": "bundle.pdf", "source_page_start": 1, "source_page_end": 12,
    })
    dao = _FakeDao()
    out = sc.split_bundle(
        prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
        proposal_path="p.json", manifest=manifest, held_by="R", run_id="RUN_1", dao=dao,
    )
    assert out["status"] == "already_split"
    assert dao.calls == []


def test_idempotent_split_returns_proposal_linked_to_existing_document_ids(tmp_path):
    pdf = _bundle_pdf(tmp_path, 12)
    prop = _proposal([_seg(0, 1, 12, status="approved")], review_status="approved")
    manifest = _manifest_with_bundle()
    manifest["documents"].append({
        "document_id": "DOC_002", "file_name": "DOC_002.pdf",
        "file_path": "data/raw/CASE_900/DOC_002.pdf", "file_format": "pdf",
        "file_size_bytes": 10, "ocr_status": "pending",
        "source_file_name": "bundle.pdf", "source_page_start": 1, "source_page_end": 12,
    })

    out = sc.split_bundle(
        prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
        proposal_path="p.json", manifest=manifest, held_by="R", run_id="RUN_1",
        dao=_FakeDao(),
    )

    assert out["status"] == "already_split"
    assert out["updated_proposal"]["segments"][0]["assigned_document_id"] == "DOC_002"


def test_split_reports_orphans_when_the_manifest_write_fails(tmp_path):
    pdf = _bundle_pdf(tmp_path, 12)
    prop = _proposal([_seg(0, 1, 12, status="approved")], review_status="approved")
    dao = _FakeDao(ok=False, message="LOCKED: held_by=other")
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        out = sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="p.json", manifest=_manifest_with_bundle(),
            held_by="R", run_id="RUN_1", dao=dao,
        )
    finally:
        sc.ROOT = orig_root
    assert out["status"] == "manifest_write_failed"
    assert len(out["orphan_pdfs"]) == 1  # the child PDF exists but nothing trusts it


# ------------------------------------------------------------- CLI --

def test_grid_parser_accepts_colsxrows():
    assert sc._parse_grid("4x4") == (4, 4)
    assert sc._parse_grid("3X4") == (3, 4)  # case-insensitive


def test_grid_parser_rejects_garbage():
    for bad in ["4", "4x", "axb", "4x4x4", ""]:
        with pytest.raises(sc.SegmentationError):
            sc._parse_grid(bad)


def test_proposal_filename_round_trips_through_schema_name_for():
    """The whole reason no DAO carve-out was needed: the per-bundle filename must
    resolve back to the proposal schema via the standard suffix stripping."""
    from _validation import schema_name_for
    from pathlib import Path
    name = sc.proposal_filename("DOC_007")
    assert name == "segmentation_proposal_DOC_007.json"
    assert schema_name_for(Path(name)) == "segmentation_proposal.schema.json"


# ---------------------------------------------- full-page refinement --

def test_parse_full_page_reads_a_clean_verdict():
    out = sc.parse_full_page_response(_response(
        starts_new_document=True, type_label="진료비 세부내역서",
        confidence=0.9, evidence="new title block"))
    assert out["ok"] is True
    assert out["starts_new_document"] is True
    assert out["type_label"] == "진료비 세부내역서"
    assert out["confidence"] == 0.9


def test_parse_full_page_fails_safe_to_continuation():
    """An unreadable verdict must default to 'continuation' -- fail-safe means
    never inventing a split, the same rule the sheet parser follows."""
    out = sc.parse_full_page_response("not json at all")
    assert out["ok"] is False
    assert out["starts_new_document"] is False


def test_parse_full_page_rejects_a_non_boolean_verdict():
    out = sc.parse_full_page_response(_response(starts_new_document="yes"))
    assert out["ok"] is False
    assert out["starts_new_document"] is False


def test_full_page_prompt_encodes_the_owner_set_title_rule():
    assert "OWN title block" in sc.FULL_PAGE_PROMPT
    assert "EACH titled page" in sc.FULL_PAGE_PROMPT
    assert "continuation ONLY when this page has NO title block" in sc.FULL_PAGE_PROMPT
    assert sc.FULL_PAGE_PROMPT_VERSION == "segment_full_page_v0.3"
    assert sc.FULL_PAGE_OUTPUT_SCHEMA["required"] == [
        "starts_new_document", "type_label", "confidence", "evidence",
    ]
    assert len(sc.FULL_PAGE_PROMPT) < 1000


class _PageVerdictProvider:
    """Returns a canned full-page verdict keyed by the page number in the image
    filename (fullpage_pNNN.png), so a test can script which pages split."""
    provider_name = "fixture"

    def __init__(self, new_pages):
        self.model_name = "fixture-model"
        self.new_pages = set(new_pages)
        self.calls = 0

    def transcribe_image(self, image_path, prompt, prompt_version):
        from llm_providers import ProviderResult
        import re
        self.calls += 1
        m = re.search(r"p(\d+)", str(image_path))
        page = int(m.group(1)) if m else -1
        text = _response(
            starts_new_document=(page in self.new_pages),
            type_label="doc" if page in self.new_pages else None,
            confidence=0.8, evidence="e")
        return ProviderResult(provider_name="fixture", model_name="fixture-model",
                              prompt_version=prompt_version, text=text)


class _StructuredPageVerdictProvider:
    provider_name = "fixture-structured"
    model_name = "fixture-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.schemas = []

    def analyze_image_structured(
        self, image_path, prompt, prompt_version, output_schema
    ):
        from llm_providers import ProviderResult
        self.calls += 1
        self.schemas.append(output_schema)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            return ProviderResult(
                provider_name=self.provider_name,
                model_name=self.model_name,
                prompt_version=prompt_version,
                text="",
                structured_output=response,
            )
        return ProviderResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            prompt_version=prompt_version,
            text=response,
        )


def test_full_page_uses_provider_neutral_structured_contract(tmp_path):
    pdf = _bundle_pdf(tmp_path, 2)
    payload = {
        "starts_new_document": True,
        "type_label": "진단서",
        "confidence": 0.9,
        "evidence": "own title",
    }
    provider = _StructuredPageVerdictProvider([payload])

    out = sc._inspect_full_pages(
        [2], pdf_path=pdf, provider=provider, scratch_dir=tmp_path / "fp"
    )

    assert provider.calls == 1
    assert provider.schemas == [sc.FULL_PAGE_OUTPUT_SCHEMA]
    assert out["verdicts"][2]["ok"] is True
    assert out["verdicts"][2]["starts_new_document"] is True
    assert out["failure_reasons"] == {}


def test_full_page_contract_failure_corrects_once_then_stays_unresolved(tmp_path):
    pdf = _bundle_pdf(tmp_path, 2)
    provider = _StructuredPageVerdictProvider([
        "analysis prose",
        "still prose",
    ])
    messages = []

    out = sc._inspect_full_pages(
        [2], pdf_path=pdf, provider=provider, scratch_dir=tmp_path / "fp",
        progress=messages.append,
    )

    assert provider.calls == 2
    assert out["calls"] == 2
    assert out["verdicts"][2]["ok"] is False
    assert 2 in out["failure_reasons"]
    assert "exactly one structured-output correction" in out["failure_reasons"][2]
    assert any("p2: ERROR" in message for message in messages)
    assert not any("p2: cont" in message for message in messages)


def test_refine_splits_a_long_segment_at_recovered_boundaries(tmp_path):
    pdf = _bundle_pdf(tmp_path, 30)
    segments = [
        {"segment_index": 0, "page_start": 1, "page_end": 4},   # short: untouched
        {"segment_index": 1, "page_start": 5, "page_end": 20},  # long: re-examine
    ]
    # Model now says p10 and p15 start new documents.
    provider = _PageVerdictProvider(new_pages={10, 15})
    out = sc.refine_long_segments(
        segments, pdf_path=pdf, provider=provider, threshold=5,
        scratch_dir=tmp_path / "fp")
    ranges = [(s["page_start"], s["page_end"]) for s in out["segments"]]
    assert (1, 4) in ranges              # short segment passed through
    assert (5, 9) in ranges              # long segment split at 10 and 15
    assert (10, 14) in ranges
    assert (15, 20) in ranges
    assert out["new_boundaries"] == [10, 15]
    # 15 interior pages (6..20) examined; the short segment never called.
    assert provider.calls == 15


def test_refine_leaves_a_short_segment_alone(tmp_path):
    pdf = _bundle_pdf(tmp_path, 10)
    segments = [{"segment_index": 0, "page_start": 1, "page_end": 3}]
    provider = _PageVerdictProvider(new_pages=set())
    out = sc.refine_long_segments(
        segments, pdf_path=pdf, provider=provider, threshold=4,
        scratch_dir=tmp_path / "fp")
    assert provider.calls == 0  # below threshold -> no calls
    assert [(s["page_start"], s["page_end"]) for s in out["segments"]] == [(1, 3)]


def test_refine_never_invents_a_split_when_the_model_says_continue(tmp_path):
    pdf = _bundle_pdf(tmp_path, 10)
    segments = [{"segment_index": 0, "page_start": 1, "page_end": 8}]
    provider = _PageVerdictProvider(new_pages=set())  # every page: continuation
    out = sc.refine_long_segments(
        segments, pdf_path=pdf, provider=provider, threshold=4,
        scratch_dir=tmp_path / "fp")
    assert out["new_boundaries"] == []
    assert [(s["page_start"], s["page_end"]) for s in out["segments"]] == [(1, 8)]


def test_refine_reuses_the_verdict_cache_on_a_second_run(tmp_path):
    """A re-run (or a threshold change) must not re-pay for pages already
    called -- the per-page verdict cache mirrors the sheet resume cache."""
    pdf = _bundle_pdf(tmp_path, 10)
    segments = [{"segment_index": 0, "page_start": 1, "page_end": 8}]
    scratch = tmp_path / "fp"

    first = _PageVerdictProvider(new_pages={5})
    sc.refine_long_segments(segments, pdf_path=pdf, provider=first,
                            threshold=4, scratch_dir=scratch)
    assert first.calls == 7  # pages 2..8

    second = _PageVerdictProvider(new_pages={5})
    out = sc.refine_long_segments(segments, pdf_path=pdf, provider=second,
                                  threshold=4, scratch_dir=scratch)
    assert second.calls == 0  # served entirely from the verdict cache
    assert out["new_boundaries"] == [5]  # cached verdict still splits at 5


def test_refine_corrects_one_transient_provider_failure(tmp_path):
    """A transient page failure receives the one caller-owned correction and
    does not contaminate the remaining full-page verdicts."""
    pdf = _bundle_pdf(tmp_path, 10)
    segments = [{"segment_index": 0, "page_start": 1, "page_end": 6}]

    class _FlakyProvider:
        provider_name = "fixture"
        model_name = "fixture-model"
        def __init__(self):
            self.calls = 0
        def transcribe_image(self, image_path, prompt, prompt_version):
            from llm_providers import ProviderResult
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("transient provider failure")
            return ProviderResult(provider_name="fixture", model_name="fixture-model",
                                  prompt_version=prompt_version,
                                  text=_response(
                                      starts_new_document=False,
                                      type_label=None,
                                      confidence=0.8,
                                      evidence="no own title",
                                  ))
        # no cache so the failure is actually hit
    provider = _FlakyProvider()
    out = sc.refine_long_segments(segments, pdf_path=pdf, provider=provider,
                                  threshold=4, scratch_dir=None)
    assert len(out["pages_examined"]) == 5  # all interior pages still examined
    assert out["new_boundaries"] == []
    assert out["unresolved_pages"] == []
    assert out["calls"] == 6  # five pages plus one correction


def test_propose_with_refine_reruns_long_segments(tmp_path):
    """End to end through propose_boundaries: --refine splits a long segment and
    marks the fallback triggered in the method record."""
    geo = sc.compute_sheet_geometry(cols=3, rows=4)  # 12 per sheet
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    # The crop pass merges everything into one long p1-12 segment...
    sheet_provider_text = _response(
        boundaries=[{"page": 1, "type_guess": "other", "type_label": "x",
                     "confidence": 0.9, "evidence": "c"}],
        continuations=list(range(2, 13)), needs_full_page=[])

    class _Combined:
        """Sheet calls (png with 'sheet' in name) get the merge response; full-page
        calls (fullpage_pNNN.png) get a per-page verdict."""
        provider_name = "fixture"
        model_name = "fixture-model"
        def __init__(self):
            self.calls = 0
        def transcribe_image(self, image_path, prompt, prompt_version):
            from llm_providers import ProviderResult
            import re
            self.calls += 1
            name = str(image_path)
            if "fullpage" in name:
                page = int(re.search(r"p(\d+)", name).group(1))
                text = _response(
                    starts_new_document=(page == 7),
                    type_label="doc" if page == 7 else None,
                    confidence=0.8,
                    evidence="page verdict",
                )
            else:
                text = sheet_provider_text
            return ProviderResult(provider_name="fixture", model_name="fixture-model",
                                  prompt_version=prompt_version, text=text)

    provider = _Combined()
    out = sc.propose_boundaries(
        pdf, case_id="CASE_910", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=False,
        refine=True, refine_threshold=4, refine_scratch_dir=tmp_path / "fp",
    )
    ranges = [(s["page_start"], s["page_end"]) for s in out["segments"]]
    assert ranges == [(1, 6), (7, 12)]  # long p1-12 split at the recovered p7
    assert out["method"]["full_page_fallback"]["triggered"] is True
    assert out["refinement"]["new_boundaries"] == [7]


# ---------------------------------------------------------------------------
# Text-layer boundary veto (known-gaps.md item 34, CASE_112)
# ---------------------------------------------------------------------------

def _veto_sheet(*boundary_pages, continuations=(), needs_full_page=()):
    return {
        "ok": True,
        "boundaries": [
            {
                "page": p,
                "type_guess": "insurance_policy",
                "type_label": f"doc-{p}",
                "confidence": 0.9,
                "evidence": "title",
            }
            for p in boundary_pages
        ],
        "continuations": list(continuations),
        "needs_full_page": list(needs_full_page),
        "warning": None,
    }


def test_text_layer_veto_absorbs_a_mid_clause_boundary():
    """A page whose own text begins mid-clause is not a document start."""
    per_sheet = [_veto_sheet(1, 2, 3)]
    merged = sc.merge_sheet_proposals(
        per_sheet, 3, sheet_pages=[[1, 2, 3]], text_layer_continuations={2}
    )
    spans = [(s["page_start"], s["page_end"]) for s in merged["segments"]]
    assert spans == [(1, 2), (3, 3)]
    assert merged["unassigned_pages"] == []
    assert sc.validate_segments(merged["segments"], 3) == []


def test_text_layer_veto_is_recorded_as_a_warning():
    merged = sc.merge_sheet_proposals(
        [_veto_sheet(1, 2)], 2, sheet_pages=[[1, 2]], text_layer_continuations={2}
    )
    assert any("dropped" in w for w in merged["warnings"])


def test_text_layer_veto_never_removes_page_1():
    """Case A: a bundle's first page begins something whatever its text looks like."""
    merged = sc.merge_sheet_proposals(
        [_veto_sheet(1, 3)], 4, sheet_pages=[[1, 2, 3, 4]], text_layer_continuations={1}
    )
    assert merged["segments"][0]["page_start"] == 1
    assert sc.validate_segments(merged["segments"], 4) == []


def test_no_text_layer_evidence_leaves_the_proposal_untouched():
    """A scanned bundle (CASE_112 DOC_005) must behave exactly as before."""
    per_sheet = [_veto_sheet(1, 2, 3)]
    base = sc.merge_sheet_proposals(per_sheet, 3, sheet_pages=[[1, 2, 3]])
    with_empty = sc.merge_sheet_proposals(
        per_sheet, 3, sheet_pages=[[1, 2, 3]], text_layer_continuations=set()
    )
    assert base["segments"] == with_empty["segments"]
    assert base["warnings"] == with_empty["warnings"]


def test_veto_only_removes_boundaries_never_adds_them():
    """Evidence about a non-boundary page must not create a segment."""
    merged = sc.merge_sheet_proposals(
        [_veto_sheet(1, 3)], 4, sheet_pages=[[1, 2, 3, 4]], text_layer_continuations={2, 4}
    )
    assert [s["page_start"] for s in merged["segments"]] == [1, 3]


def test_continuation_start_re_matches_real_case_112_page_openings():
    """The exact first lines of the 16 over-split CASE_112 pages."""
    for line in [
        "11. 에너지 및 관리할 수 있는 자연력, 상표권, 특허권 등 무체물에 입힌 손해에",
        "3. 회사는 1회의 보험사고에 대하여 다음과 같이 보상합니다.",
        "나. 부상당한 사람에게 그 부상이 원인이 되어 후유장애가 생긴 경우",
        "그러나 제1조(사고) 제1호 내지 제4호의 재물손해는 보상합니다.",
        "다만, 보호자의 감독하에 있는 경우는 그러하지 아니하다",
    ]:
        assert sc.CONTINUATION_START_RE.match(line), line


def test_continuation_start_re_does_not_match_document_titles():
    """A real document start must never be vetoed."""
    for line in [
        "구내치료비 추가특별약관",
        "영업배상책임보험 보통약관",
        "제1조(보상하는 손해)",
        "시설소유(관리)자 특별약관",
        "대위권포기 특별약관",
    ]:
        assert not sc.CONTINUATION_START_RE.match(line), line


def test_continuation_pages_from_text_layer_on_a_scan_returns_empty(tmp_path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "scan.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    doc.save(str(path))
    doc.close()
    assert sc.continuation_pages_from_text_layer(path, 2) == set()


def test_continuation_pages_from_text_layer_detects_a_mid_clause_page(tmp_path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "policy.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Chapter One", fontname="helv", fontsize=11)
    doc.new_page().insert_text((72, 72), "11. continued item", fontname="helv", fontsize=11)
    doc.save(str(path))
    doc.close()
    assert sc.continuation_pages_from_text_layer(path, 2) == {2}


def test_continuation_pages_from_text_layer_on_a_broken_pdf_returns_empty(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 not really a pdf")
    assert sc.continuation_pages_from_text_layer(path, 2) == set()


# ------------------------------------------ text-anchor boundary detection --

# The fixtures below need a font that can round-trip Hangul through a PDF text
# layer; PyMuPDF's built-in CJK aliases insert blanks. Every Windows/most Linux
# boxes have one of these, and the whole group skips rather than silently
# testing empty strings if none does.
_KOREAN_FONTS = (
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/gulim.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _korean_pdf(tmp_path, pages, name="bundle.pdf"):
    """A PDF whose pages carry the given lines as a real embedded text layer."""
    fitz = pytest.importorskip("fitz")
    font = next((f for f in _KOREAN_FONTS if Path(f).exists()), None)
    if font is None:
        pytest.skip("no Hangul-capable font available to build the fixture")
    path = tmp_path / name
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page(width=595, height=841)
        for offset, line in enumerate(lines):
            page.insert_text((72, 72 + offset * 16), line,
                             fontfile=font, fontname="K", fontsize=11)
    doc.save(str(path))
    doc.close()
    # Guard the fixture itself: if the font silently dropped the glyphs, every
    # assertion below would pass vacuously against empty pages.
    with fitz.open(str(path)) as check:
        if pages and pages[0] and not check[0].get_text().strip():
            pytest.skip("font did not embed Hangul; fixture unusable")
    return path


def test_text_anchor_boundaries_cuts_on_title_lines(tmp_path):
    path = _korean_pdf(tmp_path, [
        ["영업배상책임보험", "보통약관"],
        ["11. 계속되는 항목입니다"],
        ["시설소유(관리)자 특별약관", "제1조(사고)"],
        ["그러나 앞 조항에서 정한 손해는 보상합니다"],
        ["구내치료비 추가특별약관", "(시설소유(관리)자 특별약관에 적용)"],
    ])
    assert set(sc.text_anchor_boundaries(path, 5)) == {1, 3, 5}


def test_text_anchor_boundaries_records_the_title_that_marked_each_cut(tmp_path):
    path = _korean_pdf(tmp_path, [
        ["영업배상책임보험"],
        ["도급업자 특별약관", "제1조(사고)"],
    ])
    found = sc.text_anchor_boundaries(path, 2)
    # Page 1 starts a document by position, not by a title, so it carries none.
    assert found[1] is None
    assert found[2] == "도급업자 특별약관"


def test_text_anchor_boundaries_accepts_an_ordinal_suffixed_title(tmp_path):
    # "주위재산 추가특별약관2" and "창고업자 특별약관(Ⅰ)" are real CASE_112 titles;
    # a rule anchored on a bare "약관$" would miss both.
    path = _korean_pdf(tmp_path, [
        ["영업배상책임보험"],
        ["주위재산 추가특별약관2"],
        ["창고업자 특별약관(Ⅰ)"],
    ])
    assert set(sc.text_anchor_boundaries(path, 3)) == {1, 2, 3}


def test_text_anchor_boundaries_does_not_cut_on_an_article_heading(tmp_path):
    # 제N조 is an ARTICLE boundary, not a document boundary. Admitting these was
    # measured on CASE_112 to drop precision 1.00 -> 0.91.
    path = _korean_pdf(tmp_path, [
        ["영업배상책임보험"],
        ["제4조(준용규정)", "이 특별약관에 정하지 않은 사항은 보통약관을 따릅니다"],
        ["제5조(현물보상)", "회사는 현물로 보상하여 드립니다"],
    ])
    assert set(sc.text_anchor_boundaries(path, 3)) == {1}


def test_text_anchor_boundaries_suppresses_a_table_of_contents(tmp_path):
    # A TOC lists the very titles this module keys on. Without suppression the
    # 6-page CASE_112 DOC_004 contents block alone emits ~60 spurious cuts.
    toc = [
        "날짜인식오류 보상제외 특별약관",
        "정보기술 추가특별약관",
        "테러행위 면책 특별약관",
        "시설소유(관리)자 특별약관",
        "구내치료비 추가특별약관",
        "비행 추가특별약관",
    ]
    path = _korean_pdf(tmp_path, [
        ["영업배상책임보험 목차"] + toc,
        toc,
        ["영업배상책임보험", "보통약관", "제1조(목적)", "이 계약은 다음과 같이 보상합니다"],
    ])
    found = set(sc.text_anchor_boundaries(path, 3))
    # p2 is inside the contents block; p3 is the first real page after it.
    assert found == {1, 3}


def test_page_furniture_is_ignored_when_judging_a_contents_page(tmp_path):
    """A running footer must not decide whether a page is contents.

    Measured on CASE_112 DOC_004 p6: the embedded text layer holds the bare
    title line, but the OCR of the same page also picks up the publisher's
    running footer ("당신에게 좋은보험 삼성화재"), which the text layer omits.
    That single extra line broke the short-page rule's `titles == len(lines)`
    equality, flipping p6 from contents to a boundary and shifting the cut to
    p6/p7 -- the only boundary disagreement in 323 pages between the two
    readings. The footer is page furniture on 25 of the 28 short pages in this
    bundle, appearing under body text, article headings and titles alike, so it
    carries no information about what kind of page it sits on.
    """
    footer = "당신에게 좋은보험 삼성화재"
    with_footer = sc._page_content_lines(["치료비 추가특별약관", footer])
    assert with_footer == ["치료비 추가특별약관"]
    # Same verdict with or without the footer -- that is the whole point.
    assert sc._is_toc_page(with_footer) is sc._is_toc_page(["치료비 추가특별약관"])


def test_a_body_page_is_not_contents_however_its_lines_are_wrapped():
    """Rejoined body text must not read as a contents block.

    CASE_112 DOC_004 p31 is a body page opening with a title line and carrying
    two `제N조` headings. The embedded text layer breaks its one long clause into
    many short lines (20 total); OCR rejoins them (9 total). Under a pure ratio
    the OCR reading crossed the 0.6 share and the page became "contents",
    swallowing a real boundary -- the page's content never changed, only the
    reader's line wrapping. Length is what separates a contents entry from a
    sentence that merely starts with one.
    """
    body_sentence = (
        "회사는 보통약관 및 특별약관의 제조건·제규정에 불구하고, 피보험자의 소유여부에 "
        "관계없이 컴퓨터, 자료처리기기, 마이크로칩, 운영체제, 마이크로프로세서, 집적회로 "
        "및 이와 유사한 장치로 인해 발생되는 모든 형태의 직접 또는 간접손해를 보상하지 "
        "않습니다."
    )
    rejoined = ["날짜인식오류 보상제외 특별약관", "제1조", body_sentence, "제2조"]
    assert sc._is_toc_page(rejoined) is False
    # A real contents page of the same line count is still recognised.
    assert sc._is_toc_page([
        "날짜인식오류 보상제외 특별약관",
        "정보기술 추가특별약관",
        "테러행위 면책 특별약관",
        "시설소유(관리)자 특별약관",
        "구내치료비 추가특별약관",
    ]) is True


def test_page_furniture_never_empties_a_page(tmp_path):
    """A page that is ONLY furniture keeps its lines rather than becoming blank.

    text_anchor_boundaries treats an empty page as "not fully born-digital" and
    declines the whole bundle (returns None). Stripping a footer-only page down
    to nothing would silently turn one boilerplate page into a scan verdict for
    a 145-page bundle, so the strip is skipped when it would remove everything.
    """
    footer = "당신에게 좋은보험 삼성화재"
    assert sc._page_content_lines([footer]) == [footer]
    assert sc._page_content_lines([]) == []


def test_text_anchor_boundaries_returns_none_for_a_scan(tmp_path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "scan.pdf"
    doc = fitz.open()
    doc.new_page(width=595, height=841)
    doc.new_page(width=595, height=841)
    doc.save(str(path))
    doc.close()
    # None, not an empty set: the caller must fall back to vision, not treat the
    # bundle as having exactly zero boundaries.
    assert sc.text_anchor_boundaries(path, 2) is None


def test_text_anchor_boundaries_returns_none_when_only_some_pages_have_text(tmp_path):
    fitz = pytest.importorskip("fitz")
    font = next((f for f in _KOREAN_FONTS if Path(f).exists()), None)
    if font is None:
        pytest.skip("no Hangul-capable font available to build the fixture")
    path = tmp_path / "mixed.pdf"
    doc = fitz.open()
    doc.new_page(width=595, height=841).insert_text(
        (72, 72), "시설소유(관리)자 특별약관", fontfile=font, fontname="K", fontsize=11)
    doc.new_page(width=595, height=841)  # scanned insert: no text layer
    doc.save(str(path))
    doc.close()
    # A partially-digital bundle must not get a deterministic verdict that is
    # blind to the pages it cannot read.
    assert sc.text_anchor_boundaries(path, 2) is None


def test_text_anchor_boundaries_on_a_broken_pdf_returns_none(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 not really a pdf")
    assert sc.text_anchor_boundaries(path, 2) is None


def test_segments_from_boundaries_covers_every_page_without_overlap(tmp_path):
    segments = sc.segments_from_boundaries({1: None, 4: "도급업자 특별약관"}, 9)
    assert [(s["page_start"], s["page_end"]) for s in segments] == [(1, 3), (4, 9)]
    assert [s["segment_index"] for s in segments] == [0, 1]
    assert all(s["review_status"] == "pending" for s in segments)
    assert segments[1]["provisional_type_label"] == "도급업자 특별약관"
    # Provisional TYPE stays null even though a label is known: mapping the
    # publisher's words onto the document_type enum is not this stage's call.
    assert all(s["provisional_document_type"] is None for s in segments)


def test_propose_boundaries_takes_the_text_anchor_path_without_a_provider(tmp_path):
    path = _korean_pdf(tmp_path, [
        ["영업배상책임보험"],
        ["11. 계속되는 항목입니다"],
        ["도급업자 특별약관", "제1조(사고)"],
    ])
    # provider=None proves structurally that no model call is made: any attempt
    # to use it would raise.
    result = sc.propose_boundaries(
        path, case_id="CASE_001", doc_id="DOC_001", provider=None
    )
    assert result["method"]["mode"] == "text_anchor"
    assert result["method"]["provider_name"] is None
    assert result["method"]["contact_sheets"] == []
    assert result["per_sheet"] == []
    assert result["unassigned_pages"] == []
    assert [(s["page_start"], s["page_end"]) for s in result["segments"]] == [(1, 2), (3, 3)]


def test_propose_boundaries_can_be_forced_back_onto_the_vision_path(tmp_path):
    path = _korean_pdf(tmp_path, [
        ["영업배상책임보험"],
        ["도급업자 특별약관"],
    ])
    # The diagnostic escape hatch (--no-text-anchor) must actually leave the
    # deterministic path, otherwise a vision-vs-text comparison run is
    # impossible. The vision path degrades gracefully rather than raising when a
    # sheet cannot be produced, so assert on the mode it records, not on a throw.
    result = sc.propose_boundaries(
        path, case_id="CASE_001", doc_id="DOC_001", provider=None,
        prefer_text_anchor=False, resume=False,
    )
    assert result["method"]["mode"] == "vision_proposal"


# --- OCR redistribution: parent bundle -> child documents --------------------
#
# When OCR runs BEFORE segmentation (so that boundaries are derived from real
# page text rather than from a contact-sheet image), the text lands on the
# bundle. Every downstream stage -- redaction, chunking, citation verification
# -- addresses a document by its own id and its own page numbers, so the split
# has to hand each child the pages it now owns. No OCR re-runs: this is a
# redistribution of text already read, which is also why it is strictly cheaper
# than the old order (145 bundle pages read once, not once per child document).


def _ocr_result(doc_id, page_count, *, disagreed=()):
    """A minimal ocr_result payload shaped like run_checkpoint1 writes it."""
    return {
        "document_id": doc_id,
        "extraction_method": "ocr",
        "ocr_status": "completed",
        "pages": [
            {
                "page": str(n),
                "text_path": f"data/processed/CASE_900/{doc_id}/page_{n:03d}.md",
                "mean_confidence": None,
                "uncertain_regions": [],
                "cross_validation": {
                    "agreement": "disagreed" if n in disagreed else "agreed",
                },
            }
            for n in range(1, page_count + 1)
        ],
    }


def test_redistribute_gives_each_child_only_its_own_pages(tmp_path):
    """A child owns exactly the bundle pages its approved range covers."""
    parent = _ocr_result("DOC_003", 5)
    segments = [
        {"page_start": 1, "page_end": 1},
        {"page_start": 2, "page_end": 4},
        {"page_start": 5, "page_end": 5},
    ]
    got = sc.redistribute_ocr_pages(parent, segments, ["DOC_006", "DOC_007", "DOC_008"])
    assert [len(r["pages"]) for r in got] == [1, 3, 1]
    assert [r["document_id"] for r in got] == ["DOC_006", "DOC_007", "DOC_008"]


def test_redistribute_renumbers_pages_from_one_within_each_child(tmp_path):
    """Bundle p2-p4 becomes the child's p1-p3.

    Every downstream citation is (document_id, page); a child that kept the
    bundle's numbering would cite a page it does not have.
    """
    parent = _ocr_result("DOC_003", 5)
    segments = [{"page_start": 1, "page_end": 1},
                {"page_start": 2, "page_end": 4},
                {"page_start": 5, "page_end": 5}]
    child = sc.redistribute_ocr_pages(
        parent, segments, ["DOC_006", "DOC_007", "DOC_008"])[1]
    assert [p["page"] for p in child["pages"]] == ["1", "2", "3"]
    assert [p["text_path"] for p in child["pages"]] == [
        "data/processed/CASE_900/DOC_007/page_001.md",
        "data/processed/CASE_900/DOC_007/page_002.md",
        "data/processed/CASE_900/DOC_007/page_003.md",
    ]


def test_redistribute_carries_each_page_cross_validation_to_its_new_owner(tmp_path):
    """A P8 disagreement follows its page, and lands on the right child.

    The verdict is a property of the physical page, so splitting must neither
    invent agreement nor spread one page's disagreement across siblings -- the
    document-level rollup that blocks downstream use is computed from these.
    """
    parent = _ocr_result("DOC_003", 5, disagreed={4})
    segments = [{"page_start": 1, "page_end": 3}, {"page_start": 4, "page_end": 5}]
    first, second = sc.redistribute_ocr_pages(parent, segments, ["DOC_006", "DOC_007"])
    assert all(p["cross_validation"]["agreement"] == "agreed" for p in first["pages"])
    assert second["pages"][0]["cross_validation"]["agreement"] == "disagreed"
    assert second["pages"][1]["cross_validation"]["agreement"] == "agreed"


def test_redistribute_accounts_for_every_parent_page(tmp_path):
    """No page may be dropped or duplicated across the children.

    split_readiness_errors already refuses a proposal with an unassigned page,
    so a mismatch here means the ranges and the OCR record disagree about how
    long the bundle is -- fail loud rather than silently truncate a document.
    """
    parent = _ocr_result("DOC_003", 5)
    with pytest.raises(sc.SegmentationError):
        sc.redistribute_ocr_pages(parent, [{"page_start": 1, "page_end": 3}], ["DOC_006"])


def test_redistribute_rejects_a_range_past_the_end_of_the_ocr_record(tmp_path):
    parent = _ocr_result("DOC_003", 3)
    with pytest.raises(sc.SegmentationError):
        sc.redistribute_ocr_pages(
            parent, [{"page_start": 1, "page_end": 4}], ["DOC_006"])


# --- split_bundle wiring: redistribute the parent's OCR when it exists --------


def _write_parent_ocr(root, case_id, bundle_id, page_count):
    """Puts a parent bundle's OCR record + page files on disk, as the
    OCR-before-segmentation order produces them."""
    out = root / "outputs" / case_id
    out.mkdir(parents=True, exist_ok=True)
    proc = root / "data" / "processed" / case_id / bundle_id
    proc.mkdir(parents=True, exist_ok=True)
    pages = []
    for n in range(1, page_count + 1):
        (proc / f"page_{n:03d}.md").write_text(f"bundle page {n}", encoding="utf-8")
        pages.append({
            "page": str(n),
            "text_path": f"data/processed/{case_id}/{bundle_id}/page_{n:03d}.md",
            "mean_confidence": None,
            "uncertain_regions": [],
            "cross_validation": {"agreement": "agreed"},
        })
    record = {"case_id": case_id, "document_id": bundle_id,
              "extraction_method": "ocr", "ocr_status": "completed", "pages": pages}
    (out / f"ocr_result_{bundle_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record


def test_split_hands_each_child_its_own_page_text_when_the_parent_was_ocrd(tmp_path):
    """With OCR before segmentation, the split re-files the parent's pages.

    Downstream stages address a document by its own id and its own 1-based
    pages, so a child with no page text of its own would be invisible to
    redaction, chunking and citation verification.
    """
    pdf = _bundle_pdf(tmp_path, 4)
    _write_parent_ocr(tmp_path, "CASE_900", "DOC_001", 4)
    prop = _proposal([_seg(0, 1, 1, status="approved"),
                      _seg(1, 2, 4, status="approved")],
                     review_status="approved")
    dao = _FakeDao()
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        out = sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_1", dao=dao,
        )
    finally:
        sc.ROOT = orig_root

    assert out["status"] == "split"
    assert out["redistributed_ocr"] is True
    proc = tmp_path / "data" / "processed" / "CASE_900"
    # DOC_002 owns bundle p1; DOC_003 owns bundle p2-4, renumbered 1-3.
    assert (proc / "DOC_002" / "page_001.md").read_text(encoding="utf-8") == "bundle page 1"
    assert (proc / "DOC_003" / "page_001.md").read_text(encoding="utf-8") == "bundle page 2"
    assert (proc / "DOC_003" / "page_003.md").read_text(encoding="utf-8") == "bundle page 4"
    assert not (proc / "DOC_003" / "page_004.md").exists()

    child = json.loads((tmp_path / "outputs" / "CASE_900"
                        / "ocr_result_DOC_003.json").read_text(encoding="utf-8"))
    assert child["document_id"] == "DOC_003"
    assert [p["page"] for p in child["pages"]] == ["1", "2", "3"]


def test_split_keeps_the_parent_ocr_record_after_redistributing(tmp_path):
    """The parent's OCR output is the original P8 record and is retained.

    A child's cross_validation is inherited from it, so auditing that
    inheritance later requires the source to still exist. The manifest marks
    the bundle superseded; nothing downstream reads these files, and the
    schema constrains only the manifest entry, not the artifacts on disk.
    """
    pdf = _bundle_pdf(tmp_path, 3)
    _write_parent_ocr(tmp_path, "CASE_900", "DOC_001", 3)
    prop = _proposal([_seg(0, 1, 3, status="approved")], review_status="approved")
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_1",
            dao=_FakeDao(),
        )
    finally:
        sc.ROOT = orig_root
    assert (tmp_path / "outputs" / "CASE_900" / "ocr_result_DOC_001.json").exists()
    assert (tmp_path / "data" / "processed" / "CASE_900" / "DOC_001"
            / "page_001.md").exists()


def test_split_without_a_parent_ocr_record_behaves_exactly_as_before(tmp_path):
    """The old order still works: children get OCR'd individually afterwards.

    Both orders must coexist -- already-processed cases are not reprocessed,
    so a split with no parent OCR on disk must not fail or invent one.
    """
    pdf = _bundle_pdf(tmp_path, 4)
    prop = _proposal([_seg(0, 1, 2, status="approved"),
                      _seg(1, 3, 4, status="approved")],
                     review_status="approved")
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        out = sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_1",
            dao=_FakeDao(),
        )
    finally:
        sc.ROOT = orig_root
    assert out["status"] == "split"
    assert out["redistributed_ocr"] is False
    assert not (tmp_path / "outputs" / "CASE_900" / "ocr_result_DOC_002.json").exists()


# --- boundaries from already-extracted page text -----------------------------
#
# text_anchor_boundaries() opens the PDF and reads its embedded text layer,
# which confines it to born-digital bundles. This corpus is overwhelmingly
# scans (CASE_025 110p and CASE_026 59p carry zero embedded characters), so the
# deterministic path never applied to most of it. Running OCR first produces
# page text for a scan too; boundaries_from_page_texts() is the same rule over
# whatever text a caller already has -- redacted OCR output included, which is
# what keeps segmentation from ever reading pre-redaction PII.


def test_boundaries_from_page_texts_matches_the_pdf_path_on_the_same_text(tmp_path):
    """One rule, two sources. The PDF wrapper must add nothing but extraction."""
    pages = [
        ["영업배상책임보험", "보통약관"],
        ["11. 계속되는 항목입니다"],
        ["시설소유(관리)자 특별약관", "제1조(사고)"],
        ["그러나 앞 조항에서 정한 손해는 보상합니다"],
        ["구내치료비 추가특별약관"],
    ]
    path = _korean_pdf(tmp_path, pages)
    from_pdf = sc.text_anchor_boundaries(path, 5)
    from_text = sc.boundaries_from_page_texts(["\n".join(p) for p in pages])
    assert set(from_text) == set(from_pdf) == {1, 3, 5}
    assert from_text == from_pdf


def test_boundaries_from_page_texts_works_on_redacted_ocr_text():
    """The real input: OCR text that has been through redaction.

    Measured on CASE_112's 217 split children (208 policy + 9 medical), the
    document title line survived redaction in every single case -- redaction
    identifies PII VALUES and substitutes only those spans, and a form's title
    is not PII. So segmentation can read the redacted text and never needs the
    pre-redaction page text that dao.read-page-text guards behind checkpoint
    2's one-shot capability.
    """
    pages = [
        "후유장애 진단서(Mc Bride)\n환자: [REDACTED]\n주민등록번호: [REDACTED]",
        "계속되는 소견 내용입니다",
        "구내치료비 추가특별약관\n제1조(보상하는 손해)",
    ]
    found = sc.boundaries_from_page_texts(pages)
    assert set(found) == {1, 3}
    assert found[3] == "구내치료비 추가특별약관"


def test_boundaries_from_page_texts_declines_when_any_page_is_empty():
    """An empty page means the text is incomplete, not that it has no boundary.

    Same fail-closed contract the PDF path uses for a partially-digital bundle:
    a verdict blind to part of the document is worse than no verdict, because
    the caller can fall back to vision only if it is told nothing was decided.
    """
    assert sc.boundaries_from_page_texts(["구내치료비 추가특별약관", "", "제1조"]) is None
    assert sc.boundaries_from_page_texts([]) is None


def test_boundaries_from_page_texts_ignores_running_footers():
    """Page furniture must not shift a boundary -- the CASE_112 p6/p7 defect."""
    footer = "당신에게 좋은보험 삼성화재"
    pages = [
        f"영업배상책임보험\n보통약관\n{footer}",
        f"치료비 추가특별약관\n{footer}",
        f"제2조(준용규정)\n이 특별약관에 정하지 않은 사항은 보통약관을 따릅니다.\n{footer}",
    ]
    assert set(sc.boundaries_from_page_texts(pages)) == {1, 2}


# --- propose_boundaries prefers already-processed text ------------------------


class _never_called_provider:
    """Any provider call is a failure: the deterministic path must spend zero."""
    provider_name = "must-not-be-called"
    model_name = "must-not-be-called"

    def analyze_image_structured(self, *a, **k):
        raise AssertionError("provider called on the deterministic text path")


def _processed_bundle(root, case_id, doc_id, pages, *, redacted=True):
    """Write a bundle's processed text the way checkpoint 1/2 leaves it."""
    d = root / "data" / "processed" / case_id / doc_id
    d.mkdir(parents=True, exist_ok=True)
    if redacted:
        body = "".join(f"<<<PAGE page={n}>>>\n{t}\n" for n, t in enumerate(pages, 1))
        (d / "redacted_text.md").write_text(body, encoding="utf-8")
    else:
        for n, t in enumerate(pages, 1):
            (d / f"page_{n:03d}.md").write_text(t, encoding="utf-8")
    return d


def test_propose_uses_redacted_processed_text_when_it_exists(tmp_path):
    """A scan with processed text takes the deterministic path, not vision.

    This is what the inverted order buys: text_anchor_boundaries reads the
    PDF's embedded layer, so a scan (zero embedded characters) always fell
    through to a model reading contact-sheet crops. Once OCR has run, the same
    deterministic rule applies to a scan too.
    """
    pdf = _bundle_pdf(tmp_path, 3)  # no text layer -- a "scan"
    _processed_bundle(tmp_path, "CASE_900", "DOC_001", [
        "영업배상책임보험\n보통약관",
        "제1조(목적)\n이 계약은 다음과 같이 보상합니다",
        "구내치료비 추가특별약관",
    ])
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        result = sc.propose_boundaries(
            pdf, case_id="CASE_900", doc_id="DOC_001",
            provider=_never_called_provider(), resume=False,
        )
    finally:
        sc.ROOT = orig_root
    assert result["method"]["mode"] == "text_anchor"
    assert {s["page_start"] for s in result["segments"]} == {1, 3}


def test_propose_falls_back_to_vision_when_no_processed_text_exists(tmp_path):
    """Unchanged behaviour for a bundle that has not been OCR'd yet."""
    pdf = _bundle_pdf(tmp_path, 3)
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        result = sc.propose_boundaries(
            pdf, case_id="CASE_900", doc_id="DOC_001",
            provider=_StructuredSequencedProvider([
                {"boundaries": [], "continuations": [2, 3], "needs_full_page": []}]),
            resume=False,
        )
    finally:
        sc.ROOT = orig_root
    assert result["method"]["mode"] == "vision_proposal"


def test_propose_prefers_redacted_text_over_raw_page_text(tmp_path):
    """Segmentation reads the redacted layer when both exist.

    Raw page_NNN.md still carries claimant PII; dao.read-page-text guards it
    behind checkpoint 2's one-shot capability precisely so no analysis stage
    reads it. Measured on CASE_112's 217 split children, every document title
    survived redaction, so preferring the redacted text costs no accuracy.
    """
    pdf = _bundle_pdf(tmp_path, 3)
    # p2's title differs between the two layers, so which one was read is
    # visible in the recorded label. Page 1 is a boundary by position and
    # carries no title, which is why the assertion targets p2.
    _processed_bundle(tmp_path, "CASE_900", "DOC_001",
                      ["표지", "구내치료비 추가특별약관", "계속되는 본문"], redacted=True)
    _processed_bundle(tmp_path, "CASE_900", "DOC_001",
                      ["표지", "환자명 홍길동 특별약관", "계속되는 본문"], redacted=False)
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        result = sc.propose_boundaries(
            pdf, case_id="CASE_900", doc_id="DOC_001",
            provider=_never_called_provider(), resume=False,
        )
    finally:
        sc.ROOT = orig_root
    assert result["method"]["mode"] == "text_anchor"
    titles = [s.get("provisional_type_label") for s in result["segments"]]
    assert "구내치료비 추가특별약관" in titles


# --- medical form titles ------------------------------------------------------
#
# A 약관 bundle announces each document with a title line ending in
# 보통약관/특별약관/특약. A MEDICAL bundle does the same thing with form names,
# but Korean official forms are typeset differently in three ways that the
# policy rule cannot see. All three were found in the real corpus, each in more
# than one case, so they are the standard typography rather than one publisher's
# quirk:
#
#   "진 단 서"                    -- letter-spaced (CASE_005, CASE_024, CASE_112)
#   "■ 의료법 시행규칙 [별지...]"  -- statutory header ABOVE the title
#                                    (CASE_112, CASE_907)
#   "병록 번호" / "등 록 번 호"     -- field labels above it (CASE_112 x2)
#
# The last two are why the title is not always line 1: DOC_214's 진 단 서 is on
# line 2, DOC_216's 경과기록지 and DOC_217's 수 술 기 록 are on line 5.


def test_letter_spaced_form_titles_are_recognised():
    """Korean official forms space out their titles; the words are unchanged."""
    assert sc.medical_form_title("진 단 서") == "진 단 서"
    assert sc.medical_form_title("입 · 퇴 원 확 인 서") == "입 · 퇴 원 확 인 서"
    assert sc.medical_form_title("수 술 기 록") == "수 술 기 록"


def test_plain_and_parenthesised_form_titles_are_recognised():
    assert sc.medical_form_title("후유장애 진단서(Mc Bride)")
    assert sc.medical_form_title("진료비 내역서(외래)")
    assert sc.medical_form_title("경과기록지")
    assert sc.medical_form_title("REPORT")


def test_body_prose_mentioning_a_form_name_is_not_a_title():
    """Anchored like the policy rule: a sentence ABOUT a form is not one.

    The medical vocabulary is open where the policy one is closed
    (보통약관/특별약관/특약), so without this the wider rule would fire on
    ordinary narrative text.
    """
    assert sc.medical_form_title("위 진단서를 첨부하여 제출하였습니다") is None
    assert sc.medical_form_title("환자는 경과기록지에 기재된 대로 호전되었습니다") is None


def test_field_labels_are_not_form_titles():
    """A form's field labels are not its name -- they are what sits above it."""
    assert sc.medical_form_title("병록 번호") is None
    assert sc.medical_form_title("환자의 성명") is None
    assert sc.medical_form_title("등 록 번 호") is None


def test_medical_boundaries_find_a_title_below_a_statutory_header():
    """DOC_214/CASE_907 DOC_005: the 별지 서식 header precedes the title."""
    pages = [
        "■ 의료법 시행규칙 [별지 제5호의2서식] <개정 2019. 9. 27..>\n"
        "진 단 서\n등록번호\n연 번 호\n환자의 성명",
        "계속되는 진단 내용입니다",
    ]
    found = sc.boundaries_from_page_texts(pages, medical=True)
    assert set(found) == {1}
    assert "진 단 서" in found[1]


def test_medical_boundaries_find_a_title_below_field_labels():
    """DOC_216/DOC_217: four field labels sit above the form name."""
    pages = [
        "표지",
        "등 록 번 호\n성    명\n성별 / 나이\nOS  과\n수 술 기 록\n수 술 일 자 :",
    ]
    found = sc.boundaries_from_page_texts(pages, medical=True)
    assert set(found) == {1, 2}
    assert "수 술 기 록" in found[2]


def test_medical_mode_does_not_scan_past_the_form_header():
    """The widened scan is a header window, not a whole-page search.

    A title found deep in a page is a mention, not a heading -- that distinction
    is the only thing keeping the open medical vocabulary from matching body
    text on every continuation page.
    """
    page = "\n".join(["본문 " + str(n) for n in range(10)] + ["진 단 서"])
    assert sc.boundaries_from_page_texts(["표지", page], medical=True) == {1: None}


def test_policy_mode_is_unchanged_by_the_medical_rule():
    """Policy bundles keep the strict first-line rule that measured 1.0000.

    The medical rule trades precision for the recall its typography needs;
    applying it to 약관 text would spend that precision for nothing.
    """
    pages = ["영업배상책임보험\n보통약관", "등 록 번 호\n성명\n나이\n과\n수 술 기 록"]
    assert set(sc.boundaries_from_page_texts(pages)) == {1}
