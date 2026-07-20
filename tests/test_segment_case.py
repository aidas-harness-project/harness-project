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
