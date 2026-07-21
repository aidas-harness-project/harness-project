"""Pure-function tests for Stage 1 segmentation (build step 2).

No I/O and no provider calls -- rendering, compositing, and the split path arrive
in later build steps with their own tests.
"""
import json

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
    assert first["source_file_name"] == "bundle.pdf"
    assert first["source_page_start"] == 1
    assert first["source_page_end"] == 5
    assert first["provisional_document_type"] == "diagnosis_certificate"
    assert first["document_type"] is None
    assert first["classification_confidence"] is None
    assert first["ocr_status"] == "pending"
    assert first["pages"] is None


def test_manifest_file_paths_use_forward_slashes_on_every_host():
    entries = sc.build_manifest_entries(
        _segments(), case_id="CASE_001", source_file_name="bundle.pdf",
        proposal_path="p.json", start_index=1,
    )
    for entry in entries:
        assert entry["file_path"] == f"data/raw/CASE_001/{entry['file_name']}"
        assert "\\" not in entry["file_path"]


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


def test_prompt_offers_the_real_enum_values():
    prompt = sc.build_segment_prompt([1], sc.compute_sheet_geometry())
    for value in sc.DOCUMENT_TYPES:
        assert value in prompt


def test_manifest_entries_validate_against_the_real_schema():
    """The point of the v0.5 provenance fields is that they survive validation."""
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


def test_refine_survives_a_provider_failure_on_one_page(tmp_path):
    """A single page's provider exception must not kill the run -- it fails safe
    to continuation, and the rest of the segment is still examined."""
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
                                  text=_response(starts_new_document=False, confidence=0.8))
        # no cache so the failure is actually hit
    provider = _FlakyProvider()
    out = sc.refine_long_segments(segments, pdf_path=pdf, provider=provider,
                                  threshold=4, scratch_dir=None)
    assert len(out["pages_examined"]) == 5  # all interior pages still examined
    assert out["new_boundaries"] == []      # failed page fell safe to continuation


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
                text = _response(starts_new_document=(page == 7), confidence=0.8)
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
