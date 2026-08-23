"""Pure-function tests for Stage 1 segmentation (build step 2).

No I/O and no provider calls -- rendering, compositing, and the split path arrive
in later build steps with their own tests.
"""
import hashlib
import json
import sys
from types import SimpleNamespace
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


def _schema_document_types() -> set:
    from _validation import load_registry

    schemas, _ = load_registry()
    return set(
        schemas["common_component_output.schema.json"]["$defs"]["document_type"]["enum"]
    )


def test_document_type_enum_matches_the_schema():
    """The module keeps a literal copy to stay I/O-free; this catches drift."""
    assert set(sc.DOCUMENT_TYPES) == _schema_document_types()


def test_every_document_type_copy_matches_the_schema():
    """All THREE literal copies, not just this module's.

    Added 2026-08-21 with `legal_opinion`/`legal_reference`. Only
    `segment_case.DOCUMENT_TYPES` was drift-tested, while two other copies
    carried the same enum untested:

    * `run_checkpoint1.DOCUMENT_TYPES` is rendered INTO the classifier prompt,
      so a code missing there is a bucket the model cannot choose however
      plainly the page names it -- CASE_053's 법률질의회신서 was classified
      `other` at confidence 0.95 for exactly that reason, and dropped out of
      claim analysis entirely.
    * `medical_document_routing.DOCUMENT_TYPE_LABEL_KO` is what a Korean
      reader sees; a missing key renders no label at all.
    """
    import run_checkpoint1
    import medical_document_routing

    schema_enum = _schema_document_types()
    assert set(run_checkpoint1.DOCUMENT_TYPES) == schema_enum, (
        "run_checkpoint1.DOCUMENT_TYPES drifted from the schema; it is shown "
        "to the classifier, so a missing code is an unchoosable bucket")
    assert set(medical_document_routing.DOCUMENT_TYPE_LABEL_KO) == schema_enum, (
        "DOCUMENT_TYPE_LABEL_KO drifted; a missing key renders no Korean label")
    assert all(
        isinstance(label, str) and label.strip()
        for label in medical_document_routing.DOCUMENT_TYPE_LABEL_KO.values()
    ), "every document type needs a non-empty Korean label"


def test_the_denial_prompt_lists_every_document_type():
    """The FOURTH copy, which this test was named for and did not check.

    `run_denial_response_driver._prompt` renders the type codes into the
    instruction for `requested_documents`, so a code missing there is a
    document the insurer can ask for and the driver cannot name -- the same
    unchoosable-bucket failure as the classifier list, one stage later.

    It was found on 2026-08-22 while adding `power_of_attorney` /
    `accident_statement` / `public_benefit_certificate`: the docstring above
    said "all THREE literal copies" while a fourth sat in a prompt string that
    no test read. Copies are found by grepping the enum, not by counting the
    ones a previous change happened to touch.
    """
    import run_denial_response_driver

    prompt = run_denial_response_driver._prompt([])

    for code in sorted(_schema_document_types()):
        assert code in prompt, (
            f"{code} is in the schema enum but never named in the denial "
            "prompt; the model cannot report a document type it is not shown")


def test_the_classifier_prompt_names_every_type_it_offers():
    """A code in the list but absent from the guidance is a silent bucket.

    The prompt shows the model `DOCUMENT_TYPES` and then explains only some of
    them. That is fine for self-evident codes, but the three non-medical ones a
    liability case turns on are mutually confusable -- an insurer letter that
    quotes a legal opinion, the opinion itself, and an attached standards table
    -- so each must be named in the guidance text, not merely listed.
    """
    import run_checkpoint1

    prompt = run_checkpoint1.CLASSIFY_PROMPT_TEMPLATE
    for code in ("insurer_response", "legal_opinion", "legal_reference"):
        assert f"- {code} (" in prompt, (
            f"{code} is offered to the classifier but never explained; the "
            "three are confusable and need an explicit distinguishing rule")


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

    # CASE_TESTONLY_902, not CASE_902: the resume cache is keyed by case_id in a
    # shared scratch dir, so once a real CASE_902 run existed on disk this test's
    # first propose_boundaries hit ITS cache and made zero provider calls --
    # the assertion below then failed on state no test created. Same defect as
    # commit 6338a15 fixed for CASE_943/944; a test must never name a case_id a
    # real run can claim.
    case_id = "CASE_TESTONLY_902"
    geo_a = sc.compute_sheet_geometry(cols=3, rows=4, crop_ratio=0.33)
    p1 = _SequencedProvider(resp)
    sc.propose_boundaries(pdf, case_id=case_id, doc_id="DOC_001",
                          provider=p1, geometry=geo_a, sheet_paths=sheets, resume=True)

    geo_b = sc.compute_sheet_geometry(cols=3, rows=4, crop_ratio=0.4)
    p2 = _SequencedProvider(resp)
    sc.propose_boundaries(pdf, case_id=case_id, doc_id="DOC_001",
                          provider=p2, geometry=geo_b, sheet_paths=sheets, resume=True)
    assert p2.calls == 1  # different geometry -> cache miss -> real call

    import shutil
    shutil.rmtree(sc._resume_dir(case_id, "DOC_001"), ignore_errors=True)


def test_failed_cache_is_diagnostic_only_and_is_recalled_next_run(tmp_path):
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    pdf = _bundle_pdf(tmp_path, 12)
    sheets = _sheet_files(tmp_path, 1)
    # Deliberately outside the real outputs/ + data/processed/ namespace.
    # These tests pass a tmp_path PDF but a REAL case_id, and
    # propose_boundaries resolves processed text by case_id -- so if a
    # case with this number ever exists on disk, the deterministic
    # text-anchor path wins, the vision provider is never called, and
    # per_sheet comes back empty. That is exactly what happened when
    # CASE_943 was created for a real benchmark run: this test began
    # failing with IndexError on a tree that had not changed.
    case_id = "CASE_TESTONLY_943"
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
    # Deliberately outside the real outputs/ + data/processed/ namespace.
    # These tests pass a tmp_path PDF but a REAL case_id, and
    # propose_boundaries resolves processed text by case_id -- so if a
    # case with this number ever exists on disk, the deterministic
    # text-anchor path wins, the vision provider is never called, and
    # per_sheet comes back empty. That is exactly what happened when
    # CASE_943 was created for a real benchmark run: this test began
    # failing with IndexError on a tree that had not changed.
    case_id = "CASE_TESTONLY_944"
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


def test_split_readiness_blocks_saturated_or_unresolved_full_page_fallback():
    prop = _proposal([_seg(0, 1, 12, status="approved")], review_status="approved")
    prop["needs_full_page"] = [5]
    prop["method"]["full_page_fallback"] = {"saturated": True}
    errors = sc.split_readiness_errors(prop)
    assert any("saturated" in error for error in errors)
    assert any("full-page review" in error for error in errors)


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


def test_segmentation_prerequisites_require_p8_clear_complete_redacted_text(tmp_path):
    bundle = {
        "document_id": "DOC_001",
        "ocr_status": "completed",
        "cross_validation_status": "agreed",
        "redacted_text_path": "data/processed/CASE_900/DOC_001/redacted_text.md",
    }
    text = tmp_path / bundle["redacted_text_path"]
    text.parent.mkdir(parents=True)
    text.write_text(
        "<<<PAGE page=1>>>\nfirst\n<<<PAGE page=2>>>\nsecond\n",
        encoding="utf-8",
    )
    original_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        assert sc.segmentation_prerequisite_errors(bundle, 2) == []
        bundle["cross_validation_status"] = "disagreed_pending_review"
        errors = sc.segmentation_prerequisite_errors(bundle, 2)
    finally:
        sc.ROOT = original_root
    assert any("human resolution" in error for error in errors)


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
    assert [p["page"] for p in child["pages"]] == [1, 2, 3]
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
    assert [p["page"] for p in child["pages"]] == [1, 2, 3]


def test_split_registers_a_source_revision_for_each_child(tmp_path):
    """A child's inherited redacted text must be REGISTERED, not just written.

    `_revision_index.json` is populated only as a side effect of
    `dao.write-redacted-text`, and a split child never takes that path -- its
    text is inherited and written directly. Nothing else registers it: no
    pipeline tool calls `record-source-digest`.

    The cost is real and was measured. `policy_clause_processing` gates on
    canonical_v1 UID verification, which cannot be enabled for a document whose
    source bytes were never registered, so CASE_133 halted with 174
    unregistered-revision blockers, one per policy child, with no in-pipeline
    way to clear them. Keeping the 약관 bundles whole cut that to 2 rather than
    0 -- proof the two defects are independent, since a collapsed bundle is
    still split into a 1:1 child (CASE_135: DOC_003 145p -> DOC_010 145p) that
    inherits its text exactly the same way.
    """
    pdf = _bundle_pdf(tmp_path, 4)
    _write_parent_ocr(tmp_path, "CASE_900", "DOC_001", 4)
    parent_dir = tmp_path / "data" / "processed" / "CASE_900" / "DOC_001"
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "redacted_text.md").write_text(
        "".join(f"<<<PAGE page={n}>>>\nredacted page {n}\n" for n in range(1, 5)),
        encoding="utf-8")

    registered = []

    def fake_register(case_id, doc_id, text, revision_sha, held_by, run_id,
                      supersedes=None, lock_already_held=False):
        registered.append((doc_id, revision_sha, text, run_id))

    import dao as real_dao
    original = real_dao._register_revision
    real_dao._register_revision = fake_register
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
        real_dao._register_revision = original

    assert out["status"] == "split"
    assert [d for d, _, _, _ in registered] == ["DOC_002", "DOC_003"]
    # The hash must be over the child's OWN bytes, not the parent's.
    for doc_id, sha, text, _ in registered:
        assert sha == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert "redacted page 1" in registered[0][2]
    assert "redacted page 2" in registered[1][2]
    assert "redacted page 1" not in registered[1][2]
    # The split's REAL run id must reach the index. The first version of this
    # passed None, which the schema types `string` -- so every child wrote an
    # explicit null and the whole index failed validation. This test could not
    # see it, because the fake above never validates; the fix is asserted here
    # AND exercised against the real writer in the test below.
    assert [r for _, _, _, r in registered] == ["RUN_1", "RUN_1"]


def test_split_survives_a_failed_child_revision_registration(tmp_path):
    """Registration is diagnostic, never fatal to a completed split.

    Registration is what makes a later policy stage possible; it is not what
    makes the split correct. A registration failure must not destroy pages that
    were cut correctly.
    """
    pdf = _bundle_pdf(tmp_path, 4)
    _write_parent_ocr(tmp_path, "CASE_900", "DOC_001", 4)
    parent_dir = tmp_path / "data" / "processed" / "CASE_900" / "DOC_001"
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "redacted_text.md").write_text(
        "".join(f"<<<PAGE page={n}>>>\nredacted page {n}\n" for n in range(1, 5)),
        encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("revision index unavailable")

    import dao as real_dao
    original = real_dao._register_revision
    real_dao._register_revision = boom
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
        real_dao._register_revision = original

    assert out["status"] == "split"
    proc = tmp_path / "data" / "processed" / "CASE_900"
    assert (proc / "DOC_003" / "redacted_text.md").exists()


def test_child_revisions_survive_the_real_schema_validated_writer(tmp_path):
    """Register through the REAL `_register_revision`, schema check included.

    The two tests above replace `_register_revision` with a fake, which is the
    right isolation for asserting WHICH children get registered and over which
    bytes -- but a fake validates nothing, so both passed while the real write
    path was broken for every child. `run_id=None` produced an explicit null in
    a field the schema types `string`, and because the index is validated as a
    WHOLE document, one bad revision invalidated the entire file: on CASE_135
    all 18 children failed at once, and the failure was invisible until the
    function was run against real data.

    So this test deliberately does not mock the writer. It is slower and needs a
    real manifest, and that is the point -- it is the only one here that would
    have failed before the fix.
    """
    import dao as real_dao
    from _validation import load_registry, validate_instance

    pdf = _bundle_pdf(tmp_path, 4)
    _write_parent_ocr(tmp_path, "CASE_900", "DOC_001", 4)
    parent_dir = tmp_path / "data" / "processed" / "CASE_900" / "DOC_001"
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "redacted_text.md").write_text(
        "".join(f"<<<PAGE page={n}>>>\nredacted page {n}\n" for n in range(1, 5)),
        encoding="utf-8")

    outputs = tmp_path / "outputs" / "CASE_900"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "document_manifest.json").write_text(
        json.dumps(_manifest_with_bundle()), encoding="utf-8")

    prop = _proposal([_seg(0, 1, 1, status="approved"),
                      _seg(1, 2, 4, status="approved")],
                     review_status="approved")
    # `dao.OUTPUTS` is derived from `dao.ROOT` at IMPORT time, so redirecting
    # ROOT alone leaves every path helper pointing at the real outputs tree --
    # the write lands outside tmp_path and the assertion below finds nothing.
    orig_root, orig_dao_root = sc.ROOT, real_dao.ROOT
    orig_outputs = real_dao.OUTPUTS
    sc.ROOT = real_dao.ROOT = tmp_path
    real_dao.OUTPUTS = tmp_path / "outputs"
    try:
        out = sc.split_bundle(
            prop, case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_20260812_1",
            dao=_FakeDao(),
        )
    finally:
        sc.ROOT, real_dao.ROOT = orig_root, orig_dao_root
        real_dao.OUTPUTS = orig_outputs

    assert out["status"] == "split"
    index = json.loads((outputs / "_revision_index.json").read_text(encoding="utf-8"))
    by_id = {d["document_id"]: d for d in index["documents"]}
    assert {"DOC_002", "DOC_003"} <= set(by_id)
    for doc_id in ("DOC_002", "DOC_003"):
        revision = by_id[doc_id]["revisions"][0]
        # The null that broke CASE_135: present as a key, wrong as a value.
        assert revision["run_id"] == "RUN_20260812_1"
        assert revision["registered_by"] == "segment_case.split"
    schemas, registry = load_registry()
    assert not validate_instance(
        index, "revision_index.schema.json", schemas, registry)


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
    """No VISION call may happen on the deterministic text path.

    The provider also serves as the LLM tier's judge, which reads page TEXT for
    the pages the title rules cannot settle -- that is expected and cheap, and
    is not what this double is guarding. What must never happen is rendering a
    contact sheet and asking a model to look at it.
    """
    provider_name = "must-not-be-called"
    model_name = "must-not-be-called"

    def analyze_image_structured(self, *a, **k):
        raise AssertionError("vision call made on the deterministic text path")

    def classify_document(self, prompt, prompt_version):
        from llm_providers import ProviderResult
        # Undecided pages default to "continues", which keeps these tests about
        # the deterministic boundaries they are actually asserting.
        return ProviderResult(self.provider_name, self.model_name, prompt_version,
                              '{"starts_new_document": false, "confidence": 0.9, "title": null}')


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
    # A MEDICAL bundle, not a policy one: this test is about which PATH runs
    # (deterministic text_anchor vs vision), and a policy bundle now collapses
    # to a single segment by design, which would hide the boundary result the
    # assertion below is actually checking.
    _processed_bundle(tmp_path, "CASE_900", "DOC_001", [
        "진 단 서\n환자 성명",
        "제1조(목적)\n이 계약은 다음과 같이 보상합니다",
        "입퇴원확인서",
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
    # p2's title differs between the two layers, so which one was read is
    # visible in the recorded label. Page 1 is a boundary by position and
    # carries no title, which is why the assertion targets p2. A MEDICAL form
    # name is used rather than a 약관 title: a policy bundle now collapses to a
    # single segment by design, which would erase the very label this test
    # reads to tell the two layers apart.
    _processed_bundle(tmp_path, "CASE_900", "DOC_001",
                      ["표지", "입퇴원확인서", "계속되는 본문"], redacted=True)
    _processed_bundle(tmp_path, "CASE_900", "DOC_001",
                      ["표지", "환자명 홍길동 진단서", "계속되는 본문"], redacted=False)
    pdf = _bundle_pdf(tmp_path, 3)
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
    assert "입퇴원확인서" in titles


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


def test_form_title_survives_a_stamp_annotation_after_it():
    """CASE_907 DOC_005 p2: a 원본대조필 stamp is printed beside the title.

    The policy rule anchors titles to end-of-line because a 약관 name always
    ends its line. A medical form's title shares its line with whatever the
    clinic stamped there, so anchoring to the line end drops a real boundary.
    """
    assert sc.medical_form_title("후유장애 진단서(Mc Bride)          원본대조필 인")
    assert sc.medical_form_title("진 단 서    사본")


def test_form_title_accepts_the_내역_family():
    """CASE_907 DOC_005 p9/p16: 진료비 세부산정내역(퇴원) / (외래).

    Found by running the rule on a second bundle -- CASE_112 happened to use
    내역서 for the same kind of document, and a vocabulary derived from one
    bundle encoded that bundle's word ending rather than the form family.
    """
    assert sc.medical_form_title("진료비 세부산정내역(퇴원)")
    assert sc.medical_form_title("진료비 세부산정내역(외래)")
    assert sc.medical_form_title("진료비 내역서(외래)")


def test_form_title_ignores_measured_viewer_and_reissue_suffixes():
    """CASE_047 DOC_020--022: layout suffixes are not title content.

    The actual redacted forms put a page counter or an issuance marker after a
    title on the same line.  Keep the conservative 40-character/prose guard;
    only these exact end markers are removed before applying it.
    """
    assert sc.medical_form_title(
        "진료비 세부산정내역                                                    Page 1 / 1"
    ) == "진료비 세부산정내역"
    assert sc.medical_form_title(
        "[√]외래 [   ]입원([   ]퇴원[   ]중간) 진료비 계산서·영수증  [재발행]"
    ) == "[√]외래 [   ]입원([   ]퇴원[   ]중간) 진료비 계산서·영수증"
    assert sc.medical_form_title(
        "[√]외래 [　]입원([　]퇴원[　]중간) 진료비 계산서·영수증  [재발행]"
    ) == "[√]외래 [　]입원([　]퇴원[　]중간) 진료비 계산서·영수증"


def test_a_stamp_suffix_does_not_turn_prose_into_a_title():
    """The relaxed anchor must not open the rule to body sentences."""
    assert sc.medical_form_title("위 진단서를 첨부하여 제출하였습니다") is None
    assert sc.medical_form_title("진단서 발급 사유는 다음과 같이 기재되어 있습니다") is None


def test_consecutive_pages_repeating_one_title_stay_one_document():
    """A multi-page form reprints its title on every page; that is one document.

    CASE_907 DOC_005 p9-15 is a single 7-page 진료비 세부산정내역(퇴원) whose
    title is a running page header -- the human baseline records it as one
    document (DOC_221). Treating each reprint as a document start scored
    precision 0.6250 on that bundle; merging identical consecutive titles
    scores 1.0000.

    This narrows the split-biased rule settled 2026-07-21 (known-gaps item 31),
    which said a repeated title still starts a document. That rule was written
    for the VISION path, where the evidence is a cropped thumbnail and the
    reader cannot tell a reprint from a new form of the same kind. Reading the
    text, "the previous page carried this exact title" is directly observable,
    so the ambiguity the rule was hedging against is not present here.
    """
    pages = [
        "진료비 세부산정내역(퇴원)\n환자등록번호 :\n항목 | 일자 | 금액",
        "진료비 세부산정내역(퇴원)\n항목 | 일자 | 금액\n01. 진찰료",
        "진료비 세부산정내역 (퇴원)\n항목 | 일자 | 금액\n02. 입원료",
        "진료비 세부산정내역(외래)\n항목 | 일자 | 금액",
    ]
    found = sc.boundaries_from_page_texts(pages, medical=True)
    # p2/p3 are reprints of p1's header -- spacing differs, the title does not.
    # p4 is a DIFFERENT form (외래 vs 퇴원) and does start a new document.
    assert set(found) == {1, 4}


def test_a_different_form_of_the_same_family_still_starts_a_document():
    """Merging is on the title itself, not on the form family."""
    pages = [
        "진 단 서\n환자의 성명",
        "후유장애 진단서(Mc Bride)\n병록번호 :",
    ]
    assert set(sc.boundaries_from_page_texts(pages, medical=True)) == {1, 2}


def test_a_title_returning_after_another_document_starts_a_new_one():
    """Merging applies to CONSECUTIVE repeats only.

    The same form recurring later in a bundle is a genuinely separate
    submission, not a continuation of the earlier one.
    """
    pages = [
        "진료비 내역서(외래)\n금액",
        "진 단 서\n환자의 성명",
        "진료비 내역서(외래)\n금액",
    ]
    assert set(sc.boundaries_from_page_texts(pages, medical=True)) == {1, 2, 3}


def test_a_generic_title_does_not_merge_two_documents():
    """CASE_907/CASE_112 DOC_005 p6-p7: two imaging reports both titled REPORT.

    p6 reads an MR HAND, p7 a Right Wrist AP/Lateral -- separate studies, each
    with its own patient block and Conclusion, and the human baseline records
    them as two documents. A generic form-kind word is not a document NAME, so
    a repeat of one is no evidence of a reprint; merging on it turned a real
    boundary into a continuation on both bundles measured.

    Titles that name the form ("진료비 세부산정내역(퇴원)") still merge. The
    distinction is whether the title identifies the document or only its type,
    which is exactly the judgement the LLM tier exists for -- this list is the
    deterministic floor under it, not a substitute.
    """
    pages = ["REPORT\nReading | MR | HAND", "REPORT\nReading | Right Wrist AP"]
    assert set(sc.boundaries_from_page_texts(pages, medical=True)) == {1, 2}


# --- LLM tier: pages the deterministic rule cannot decide ---------------------
#
# The title rule answers most pages and is measurably exact where it applies
# (precision 1.0000 on both medical bundles). It leaves two shapes undecided,
# both observed on real data rather than imagined:
#
#   * a page with NO recognisable form title -- CASE_907 DOC_005 p18,
#     "[자료 13] 위자료 산정기준표", a court compensation table typed `other`
#   * a page repeating a GENERIC form-kind word -- p6/p7 both headed "REPORT"
#     while reading different studies
#
# Only those pages are sent to the model. On the 19-page bundle that is 2 of 19.


class _FakeBoundaryJudge:
    """Records what it was asked and replays scripted verdicts."""

    provider_name = "fake"
    model_name = "fake-model"

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.prompts = []

    def classify_document(self, prompt, prompt_version):
        from llm_providers import ProviderResult
        self.prompts.append(prompt)
        value = self._verdicts[min(len(self.prompts) - 1, len(self._verdicts) - 1)]
        return ProviderResult(self.provider_name, self.model_name, prompt_version,
                              value if isinstance(value, str) else json.dumps(value))


def test_llm_tier_is_asked_only_about_undecided_pages():
    """A page the title rule settled must not cost a model call.

    p2 and p3 both lack a form title, so both are genuinely undecided: an
    untitled page is usually a continuation, but CASE_907 DOC_005 p18 opens a
    court compensation table with no medical form name, so "untitled" cannot be
    read as "continuation" on its own. p1 and p4 are settled by their titles
    and must cost nothing.
    """
    pages = [
        "진 단 서\n환자의 성명",
        "계속되는 진단 내용입니다",
        "[자료 13] 위자료 산정기준표\n※ 서울중앙지방법원 기준",
        "진료비 내역서(외래)\n금액",
    ]
    judge = _FakeBoundaryJudge([
        {"starts_new_document": False, "confidence": 0.9, "title": None},
        {"starts_new_document": True, "confidence": 0.9, "title": "위자료 산정기준표"},
    ])
    found = sc.boundaries_from_page_texts(pages, medical=True, judge=judge)
    assert len(judge.prompts) == 2, "only the untitled pages may be sent"
    assert "위자료 산정기준표" in judge.prompts[1]
    assert set(found) == {1, 3, 4}


def test_llm_tier_can_separate_two_pages_sharing_a_generic_title():
    """CASE_907/CASE_112 p6-p7: both "REPORT", different studies."""
    pages = ["REPORT\nReading | MR | HAND", "REPORT\nReading | Right Wrist AP"]
    judge = _FakeBoundaryJudge([{"starts_new_document": True, "confidence": 0.85,
                                 "title": "REPORT"}])
    assert set(sc.boundaries_from_page_texts(pages, medical=True, judge=judge)) == {1, 2}


def test_llm_tier_may_merge_a_continuation_it_recognises():
    pages = [
        "진료비 세부산정내역(퇴원)\n항목 | 금액",
        "REPORT\n(앞 장에서 이어짐)",
    ]
    judge = _FakeBoundaryJudge([{"starts_new_document": False, "confidence": 0.8,
                                 "title": None}])
    assert set(sc.boundaries_from_page_texts(pages, medical=True, judge=judge)) == {1}


def test_an_unusable_verdict_splits_and_is_flagged_for_review():
    """Fail toward splitting, and say so.

    Over-splitting is undone by a human merging two segments at the approval
    gate; over-merging fuses two documents into one document_type and
    propagates downstream. The page is also reported so the gate knows the
    boundary was not actually decided -- unlike a title match, which is
    evidence a reviewer can check on the page itself.
    """
    pages = ["진 단 서\n환자의 성명", "REPORT\nReading"]
    for bad in ("not json at all", {"confidence": 0.4}):
        judge = _FakeBoundaryJudge([bad])
        found = sc.boundaries_from_page_texts(pages, medical=True, judge=judge)
        assert set(found) == {1, 2}
        assert sc.undecided_pages(pages, medical=True, judge=judge) == [2]


def test_without_a_judge_the_deterministic_result_is_unchanged():
    """The tier is additive: no judge, exactly the behaviour measured before."""
    pages = ["REPORT\nReading | MR", "REPORT\nReading | Wrist"]
    assert set(sc.boundaries_from_page_texts(pages, medical=True)) == {1, 2}


# --- picking a title rule without knowing the document type -------------------
#
# `propose` runs BEFORE classification (document_type is per-document, so it
# cannot be known until after the split), yet the policy and medical title rules
# are different. Rather than guess the type, try both and take the one that
# actually found boundaries: the two vocabularies do not overlap, so the wrong
# rule reports nothing. Measured on real bundles -- CASE_909/DOC_005 (medical):
# policy rule 1 boundary, medical rule 10; CASE_112 DOC_003/DOC_004 (policy):
# policy rule 84 and 89, medical rule 1 each.


def test_a_medical_bundle_is_read_by_the_medical_rule():
    pages = [
        "진 단 서\n환자의 성명",
        "계속되는 진단 내용",
        "진료비 내역서(외래)\n금액",
    ]
    assert set(sc.boundaries_from_page_texts(pages, medical="auto")) == {1, 3}


def test_a_policy_bundle_is_read_by_the_policy_rule():
    """The policy rule finds the 약관 titles -- and the bundle then STAYS WHOLE.

    The rule itself is unchanged and still cuts on `구내치료비 추가특별약관`
    (asserted directly below via `_boundaries_from_page_lines`). What changed is
    the DOCUMENT decision built on top of it: a 약관 bundle is deliberately not
    split, so `boundaries_from_page_texts` collapses it to one segment.
    """
    pages = [
        "영업배상책임보험\n보통약관",
        "제1조(목적)\n이 계약은 다음과 같이 보상합니다",
        "구내치료비 추가특별약관\n제1조",
    ]
    lines = [
        sc._page_content_lines([ln.strip() for ln in t.splitlines() if ln.strip()])
        for t in pages
    ]
    # The rule still sees both 약관 boundaries...
    assert set(sc._boundaries_from_page_lines(lines, page_texts=pages)) == {1, 3}
    # ...and the bundle is nonetheless kept whole.
    assert set(sc.boundaries_from_page_texts(pages, medical="auto")) == {1}


def test_a_policy_bundle_collapses_to_one_segment():
    """A 약관 bundle is never split -- the rule that had no code behind it.

    Korean policy clauses print their owning 약관 in the clause body, so
    downstream identification runs off clause text, not the PDF a clause sits
    in; and `chunk_text` re-splits to page granularity either way. Splitting is
    therefore pure cost. Measured on CASE_133 (RUN_20260812_133): two bundles
    split into 174 children (median 1 page), costing 381 classification calls
    and 344 judge calls, while `page_chunks.json` still held exactly the
    pre-split page count.

    Enforced here rather than at the human approval gate because
    `--auto-approve-segmentation` skips that gate on every automated run.
    """
    pages = [
        "영업배상책임보험\n보통약관",
        "제1조(목적)",
        "구내치료비 추가특별약관",
        "제1조(목적)",
        "물적손해 확장 추가특별약관",
    ]
    assert set(sc.boundaries_from_page_texts(pages, medical="auto")) == {1}


def test_a_policy_bundle_collapses_even_when_a_judge_is_supplied():
    """The regression that a judge=None check could not see.

    The medical pass consults the judge, and a judge that declines a page splits
    it (the deliberate fail-safe direction), so on a policy bundle the medical
    pass returns roughly ONE BOUNDARY PER PAGE. Measured on CASE_134/DOC_003:
    policy 84, medical-with-judge 145. `len(medical) > len(policy)` is then true
    and the medical result is returned -- so a collapse placed after that
    comparison never runs. The first version of this fix passed a judge=None
    check and still split that bundle 85 ways in the real run.

    Also pins the saving: a recognised policy bundle must cost ZERO judge calls,
    since the medical pass is skipped rather than run-and-discarded.
    """
    pages = [
        "영업배상책임보험\n보통약관",
        "제1조(목적)",
        "구내치료비 추가특별약관",
        "제1조(목적)",
        "물적손해 확장 추가특별약관",
    ]

    class DecliningJudge:
        def __init__(self):
            self.calls = 0

        def __call__(self, *args, **kwargs):
            self.calls += 1
            return None

    judge = DecliningJudge()
    assert set(sc.boundaries_from_page_texts(
        pages, medical="auto", judge=judge)) == {1}
    assert judge.calls == 0


def test_a_single_policy_document_is_not_treated_as_a_bundle():
    """One 약관 title on page 1 is a document, not a bundle.

    Collapsing it would change nothing, but the predicate must not claim a
    bundle it did not see -- page 1 carries a null title on the policy path, so
    a naive count of title strings would misread this shape.
    """
    pages = ["영업배상책임보험\n보통약관", "제1조(목적)", "제2조(보상하지 않는 손해)"]
    assert set(sc.boundaries_from_page_texts(pages, medical="auto")) == {1}
    lines = [
        sc._page_content_lines([ln.strip() for ln in t.splitlines() if ln.strip()])
        for t in pages
    ]
    assert sc._is_policy_bundle(sc._boundaries_from_page_lines(
        lines, page_texts=pages)) is False


def test_a_policy_boundary_from_the_contents_branch_still_counts_as_a_bundle():
    """Not every policy boundary is a 약관 title, and unanimity is too strict.

    The policy path also opens a document on the first real page after a
    contents block, whose line 1 is a cover title. On CASE_133/DOC_004 that is
    p7 `영업배상책임보험` -- 1 of 89 boundaries. Requiring every boundary to be a
    약관 title scored that real bundle False and left it split.
    """
    boundaries = {1: None, 7: "영업배상책임보험", 9: "시설소유(관리)자 특별약관"}
    assert sc._is_policy_bundle(boundaries) is True


def test_a_medical_bundle_still_splits():
    """The collapse is policy-only -- a medical bundle genuinely needs splitting.

    `document_type` is per-document and one label cannot describe a 진단서 plus
    a 입퇴원확인서, which is the whole reason medical bundles are split.
    """
    pages = ["진 단 서\n환자 성명", "계속되는 본문", "입퇴원확인서"]
    assert set(sc.boundaries_from_page_texts(pages, medical="auto")) == {1, 3}


def test_auto_does_not_let_the_wrong_rule_weaken_the_right_one():
    """The rules are not unioned -- the losing one contributes nothing.

    Merging both would import the medical rule's 5-line header window into
    policy text, where the strict first-line rule is what measured precision
    1.0000 across 173 boundaries.
    """
    pages = [
        "영업배상책임보험\n보통약관",
        # A policy page whose body happens to mention a medical form name.
        "제3조(서류)\n회사는 진단서를 요구할 수 있습니다\n진 단 서",
        "구내치료비 추가특별약관",
    ]
    lines = [
        sc._page_content_lines([ln.strip() for ln in t.splitlines() if ln.strip()])
        for t in pages
    ]
    # The policy rule wins and cuts on the 약관 title only -- p2's stray
    # `진 단 서` contributes nothing. Asserted on the rule itself, since the
    # bundle is then collapsed to one segment as a policy bundle.
    assert set(sc._boundaries_from_page_lines(lines, page_texts=pages)) == {1, 3}
    assert set(sc.boundaries_from_page_texts(pages, medical="auto")) == {1}


def test_auto_falls_back_to_the_policy_result_when_neither_fires():
    """A bundle neither rule recognises still returns page 1, not None.

    None means "no verdict, fall through to vision"; a single boundary means
    "one document". Those are different answers and must stay so.
    """
    pages = ["표지입니다", "본문이 이어집니다"]
    assert set(sc.boundaries_from_page_texts(pages, medical="auto")) == {1}


# --- title -> document_type, where the title actually determines it -----------
#
# The split already records the publisher's own printed title at precision
# 1.0000. Asking a model to re-read the same page and name a type is a second
# opinion on evidence we already hold exactly -- but only where the title names
# the FORM. Some titles name a genre, not a form, and those must still be
# classified from content.


def test_a_form_naming_title_maps_to_its_type():
    assert sc.document_type_from_title("진 단 서") == "diagnosis_certificate"
    assert sc.document_type_from_title("후유장애 진단서(Mc Bride)") == "diagnosis_certificate"
    assert sc.document_type_from_title("경과기록지") == "medical_record"
    assert sc.document_type_from_title("수 술 기 록") == "medical_record"
    assert sc.document_type_from_title("진료비 내역서(외래)") == "receipt"
    assert sc.document_type_from_title("진료비 세부산정내역(퇴원)") == "receipt"


def test_a_genre_naming_title_is_left_to_the_classifier():
    """"REPORT" says a report exists, not which kind.

    CASE_909's p6/p7 are imaging readings, but the same heading sits on lab and
    pathology reports too. Mapping it would encode one bundle's coincidence as a
    rule; the model reads the page and can tell them apart.
    """
    assert sc.document_type_from_title("REPORT") is None
    assert sc.document_type_from_title("SUMMARY") is None
    assert sc.document_type_from_title("보고서") is None


def test_an_unmapped_title_returns_none_rather_than_guessing():
    """Absence of a mapping is not a type. Unknown falls through to the model."""
    assert sc.document_type_from_title("업무내용") is None
    assert sc.document_type_from_title("중기명세") is None
    assert sc.document_type_from_title(None) is None


# --- non-medical form titles, 2026-08-23 --------------------------------------
#
# The rule was medical-only, which was a scope accident rather than a decision:
# a 법률질의회신서 is headed that because that is the form's name, exactly as a
# 진단서 is. Scored against every model verdict on disk before extending --
# coverage of model-classified documents went 18.4% -> 48.8% at 98.1%
# agreement, and each of the 11 remaining disagreements was read individually.


def test_legal_and_administrative_titles_map_to_their_type():
    assert sc.document_type_from_title("법률질의회신서") == "legal_opinion"
    assert sc.document_type_from_title("법 률 질 의 회 신 서") == "legal_opinion"
    assert sc.document_type_from_title("위자료 산정기준표") == "legal_reference"
    assert sc.document_type_from_title("위 임 장") == "power_of_attorney"
    assert sc.document_type_from_title("사 고 경 위 서") == "accident_statement"
    assert (sc.document_type_from_title("보험급여 지급확인원")
            == "public_benefit_certificate")


def test_policy_and_certificate_titles_are_separated():
    assert sc.document_type_from_title("영업배상책임보험 보통약관") == "insurance_policy"
    assert sc.document_type_from_title("시설소유(관리)자 특별약관") == "insurance_policy"
    assert sc.document_type_from_title("보험증권") == "insurance_certificate"
    assert sc.document_type_from_title("공 제 등 록 증 권") == "insurance_certificate"


def test_co_insurance_panel_is_a_certificate_despite_its_clause_title():
    """The one case where the generic 특별약관 rule was measurably wrong.

    CASE_112's DOC_091/DOC_194 open "공동인수 특별약관" and continue "이
    보험증권은 아래의 회사들을 대리하여 우리회사가 발행하며" -- the co-insurance
    panel printed on the certificate, naming who carries which share. They were
    the only 특별약관 documents the model called `insurance_certificate`, and it
    was right. The table is sorted longest-first, so the specific entry wins.
    """
    assert (sc.document_type_from_title("공동인수 특별약관")
            == "insurance_certificate")
    assert (sc.document_type_from_title("공동비율 특별약관")
            == "insurance_policy")


def test_confirmation_forms_split_by_their_own_stem_not_the_shared_suffix():
    """`확인서` alone names no form -- two unrelated kinds share it.

    It was reachable as a title but mapped to nothing, so all 156 입퇴원확인서
    went to the model. 입퇴원/입원/퇴원 is a record of a hospital stay;
    납입/수납 is a payment receipt. A bare 확인서 still declines.
    """
    assert sc.document_type_from_title("입 · 퇴 원 확 인 서") == "medical_record"
    assert sc.document_type_from_title("입 퇴 원 사 실 확 인 서") == "medical_record"
    assert sc.document_type_from_title("진료비(약제비) 납입 확인서") == "receipt"
    assert sc.document_type_from_title("확인서") is None


def test_a_judgment_cited_inside_an_opinion_is_not_a_reference_document():
    """The disagreement that proved the model right.

    On CASE_046/DOC_007 a `지방법원.*판결` rule said `legal_reference` while the
    model said `legal_opinion`. The model was right: the line was "다. 유사
    사안에 대한 판례 (1) 서울남부지방법원 ... 판결" -- body text under an outline
    marker, an opinion citing precedent. A judgment filed as its own exhibit and
    one quoted inside an opinion are not separable by title alone, so 판결문 is
    deliberately unmapped.
    """
    assert sc.document_type_from_title(
        "다. 유사 사안에 대한 판례 (1) 서울남부지방법원 2022. 1. 13. 선고 2020나69036 판결"
    ) is None


def test_a_form_issued_by_either_an_insurer_or_a_public_scheme_declines():
    """지급결의확인서 does not say who issued it.

    CASE_319/DOC_012 came back `insurer_response` at 0.72 while the same form
    name elsewhere read `public_benefit_certificate`. A rule here would harden a
    coin flip; the model at least reports its uncertainty.
    """
    assert sc.document_type_from_title("교통사고사항 및 지급결의확인서") is None
    assert sc.document_type_from_title("") is None


def test_the_mapping_reads_a_letter_spaced_title():
    """Korean official forms letter-space their titles; that is not a new form."""
    assert sc.document_type_from_title("진 단 서") == sc.document_type_from_title("진단서")
    assert sc.document_type_from_title("수 술 기 록") == sc.document_type_from_title("수술기록")


def test_the_mapping_never_fires_on_prose_mentioning_a_form():
    """A sentence about a 진단서 is not a 진단서."""
    assert sc.document_type_from_title("위 진단서를 첨부하여 제출하였습니다") is None


def test_redistributed_pages_keep_the_schema_s_integer_page_numbers():
    """`page` is an integer in ocr_result.schema.json, and must stay one.

    Caught by the schema test on the real CASE_909 children: redistribution
    wrote str(offset), so every redistributed ocr_result failed validation --
    silently, until something happened to validate them. The parent's own pages
    are integers; re-filing them must not change their type.
    """
    parent = _ocr_result("DOC_003", 4)
    for page in parent["pages"]:
        page["page"] = int(page["page"])
    children = sc.redistribute_ocr_pages(
        parent, [{"page_start": 1, "page_end": 2}, {"page_start": 3, "page_end": 4}],
        ["DOC_006", "DOC_007"])
    for child in children:
        assert [p["page"] for p in child["pages"]] == [1, 2]


def test_a_judge_undecided_boundary_is_flagged_for_the_human_gate():
    """A boundary the LLM tier could not settle must be visible to a reviewer.

    It splits (the fail-safe direction) but there is no evidence on the page a
    reviewer can check, unlike a title match. `needs_full_page` already carries
    exactly that meaning for the vision path -- "this segment's boundary needed
    more than the default look" -- so the same flag is reused rather than adding
    a second field meaning the same thing.
    """
    pages = ["진 단 서\n환자의 성명", "REPORT\nReading"]

    class _Unusable:
        provider_name = "x"
        model_name = "x"

        def classify_document(self, prompt, prompt_version):
            from llm_providers import ProviderResult
            return ProviderResult("x", "x", prompt_version, "not json at all")

    result = sc.propose_from_page_texts(pages, medical="auto", judge=_Unusable())
    assert {s["page_start"] for s in result["segments"]} == {1, 2}
    flagged = {s["page_start"] for s in result["segments"] if s["needs_full_page"]}
    assert flagged == {2}, "only the undecided boundary is flagged"


def test_a_deterministic_boundary_is_not_flagged():
    pages = ["진 단 서\n환자의 성명", "진료비 내역서(외래)\n금액"]
    result = sc.propose_from_page_texts(pages, medical="auto")
    assert not any(s["needs_full_page"] for s in result["segments"])


def test_split_redistributes_the_bundles_redacted_text_too(tmp_path):
    """The bundle is redacted BEFORE the split, so its children inherit that too.

    Without this each child would have to be redacted again -- 12 more model
    calls on CASE_909 for work the bundle already did -- and, worse, would have
    no redacted text for classification to read, leaving the raw page text as
    the only available input.
    """
    pdf = _bundle_pdf(tmp_path, 4)
    _write_parent_ocr(tmp_path, "CASE_900", "DOC_001", 4)
    proc = tmp_path / "data" / "processed" / "CASE_900" / "DOC_001"
    (proc / "redacted_text.md").write_text(
        "".join(f"<<<PAGE page={n}>>>\nredacted page {n}\n" for n in range(1, 5)),
        encoding="utf-8")
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

    assert out["redistributed_redaction"] is True
    base = tmp_path / "data" / "processed" / "CASE_900"
    first = (base / "DOC_002" / "redacted_text.md").read_text(encoding="utf-8")
    second = (base / "DOC_003" / "redacted_text.md").read_text(encoding="utf-8")
    # Page numbers restart at 1 within each child, matching its own pages.
    assert "<<<PAGE page=1>>>" in first and "redacted page 1" in first
    assert "page=2" not in first
    assert second.count("<<<PAGE page=") == 3
    assert "redacted page 2" in second and "redacted page 4" in second
    assert "<<<PAGE page=3>>>" in second


def test_split_without_parent_redaction_reports_it_rather_than_inventing_one(tmp_path):
    """A bundle split before redaction is a valid state, not an error."""
    pdf = _bundle_pdf(tmp_path, 2)
    _write_parent_ocr(tmp_path, "CASE_900", "DOC_001", 2)
    prop = _proposal([_seg(0, 1, 2, status="approved")], review_status="approved")
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
    assert out["redistributed_redaction"] is False
    assert not (tmp_path / "data" / "processed" / "CASE_900" / "DOC_002"
                / "redacted_text.md").exists()


def test_redistributed_redaction_writes_each_childs_contract(tmp_path):
    """A child needs its own redaction_result and manifest path, not just text.

    Downstream stages find a document's redacted text through the manifest's
    `redacted_text_path`; chunking reads that field. A child with the file but
    no contract is invisible to them, which is the same shape of gap as having
    the OCR pages without an ocr_result.
    """
    pdf = _bundle_pdf(tmp_path, 3)
    _write_parent_ocr(tmp_path, "CASE_900", "DOC_001", 3)
    proc = tmp_path / "data" / "processed" / "CASE_900" / "DOC_001"
    (proc / "redacted_text.md").write_text(
        "".join(f"<<<PAGE page={n}>>>\nredacted {n}\n" for n in (1, 2, 3)),
        encoding="utf-8")
    out = tmp_path / "outputs" / "CASE_900"
    out.mkdir(parents=True, exist_ok=True)
    (out / "redaction_result_DOC_001.json").write_text(json.dumps({
        "case_id": "CASE_900", "run_id": "RUN_20260805_002",
        "component": "document-pipeline", "status": "success",
        "created_at": "2026-08-05T00:00:00+09:00",
        "model_info": {"model_name": "llm_span_redaction:x",
                        "prompt_version": "pii_redaction_v0.3"},
        "document_id": "DOC_001", "method": "llm_span_redaction",
        "redacted_text_path": "data/processed/CASE_900/DOC_001/redacted_text.md",
        "items_redacted": 4, "review_required": False,
    }, ensure_ascii=False), encoding="utf-8")

    dao = _FakeDao()
    orig_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        sc.split_bundle(
            prop := _proposal([_seg(0, 1, 1, status="approved"),
                               _seg(1, 2, 3, status="approved")],
                              review_status="approved"),
            case_id="CASE_900", bundle_id="DOC_001", bundle_pdf_path=pdf,
            proposal_path="outputs/CASE_900/segmentation_proposal_DOC_001.json",
            manifest=_manifest_with_bundle(), held_by="R", run_id="RUN_20260805_002",
            dao=dao,
        )
    finally:
        sc.ROOT = orig_root

    from _validation import load_registry as _load_registry, validate_instance as _validate

    child = json.loads(
        (out / "redaction_result_DOC_002.json").read_text(encoding="utf-8"))
    assert child["document_id"] == "DOC_002"
    assert child["redacted_text_path"] == "data/processed/CASE_900/DOC_002/redacted_text.md"
    # The parent's item count describes the parent, not this child -- but the
    # schema REQUIRES a non-negative integer here, so `None` is not an option
    # either. This assertion used to accept None, which is why 18 of 18 split
    # children on CASE_135 shipped schema-invalid contracts with the suite green.
    assert child["items_redacted"] == 0, "child count must be its OWN, and an int"
    assert child["items_redacted"] != 4, "must not inherit the parent's count"
    schemas, registry = _load_registry()
    assert not _validate(child, "redaction_result.schema.json", schemas, registry)
    # And the manifest entry points at it, so chunking can find it.
    written = {d["document_id"]: d for d in dao.calls[0]["new_documents"]}
    assert written["DOC_002"]["redacted_text_path"] == \
        "data/processed/CASE_900/DOC_002/redacted_text.md"


def test_a_real_case_on_disk_diverts_propose_boundaries_from_the_vision_path():
    """Pins the hazard that made a passing test start failing.

    `propose_boundaries` resolves processed text by case_id. A test that hands
    it a tmp_path PDF but a REAL case_id therefore depends on whether that
    case exists on disk: once it does, the deterministic text-anchor path wins,
    the vision provider is never called, and `per_sheet` comes back empty.

    That is what happened. Creating CASE_943 for a benchmark run made
    test_failed_cache_is_diagnostic_only_and_is_recalled_next_run fail with
    IndexError on a tree whose code had not changed -- the same failure
    reproduced on the branch-point commit, which is what proved it
    environmental rather than a regression. The fix was to move that test's
    case_id out of the real namespace; this test states why, executably.

    Rather than grepping for id strings (too blunt -- other tests in this file
    legitimately name real ids on paths that never read processed text), it
    demonstrates the divergence directly.
    """
    geo = sc.compute_sheet_geometry(cols=3, rows=4)
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    pdf = _bundle_pdf(tmp, 12)
    sheets = _sheet_files(tmp, 1)

    provider = _StructuredSequencedProvider(["prose", "more prose"])
    result = sc.propose_boundaries(
        pdf, case_id="CASE_TESTONLY_9999", doc_id="DOC_001", provider=provider,
        geometry=geo, sheet_paths=sheets, resume=True,
    )
    # With no processed text for this id, the vision path runs: the provider is
    # consulted and per_sheet is populated. A test asserting on per_sheet[0]
    # is only meaningful under this condition.
    assert provider.calls == 2
    assert len(result["per_sheet"]) == 1


# --- 영수증/계산서: recognised, merged, and never judged ---------------------

_RECEIPT_PAGE = ("( 외래 ) 진료비 계산서 · 영수증\n보조유형 : 정상급여\n"
                 "환자등록번호 | 환자 성명 | 진료기간\n진 찰 료 | 8,460")


def test_a_run_of_receipts_is_one_document_and_costs_no_judge_call():
    """영수증/계산서 take the ordinary repeated-title merge, and the reason to
    recognise them at all is that the judge is what a missing title costs.

    Measured on CASE_050 (97p, 2026-08-18) before 영수증 was in
    `_MEDICAL_TITLE_RE`: the title returned None, so the deterministic branch
    was skipped and each of the 31 consecutive receipt pages went to the LLM
    tier -- 63 judge calls and a 649.5s Stage 2. The judge stub here is the
    part that matters: `boundaries_from_page_texts` reaches it whenever
    `_title_key` yields None, so a rule that suppresses the KEY (rather than
    the title) does not avoid the call -- it re-routes into it. A later
    re-propose that returned `model_calls: 0` had in fact made 126.
    """
    judged = []

    def judge(*args, **kwargs):
        judged.append(args)
        return {"starts_new_document": True}

    pages = [_RECEIPT_PAGE, _RECEIPT_PAGE, _RECEIPT_PAGE]
    found = sc.boundaries_from_page_texts(pages, medical=True, judge=judge)
    assert set(found) == {1}, "consecutive receipts are one document"
    assert judged == [], "a titled receipt page must never reach the judge"


def test_text_anchor_propose_reports_the_actual_injected_judge_calls(
        monkeypatch, capsys):
    """The CLI result is an execution-cost receipt, not a deterministic label.

    Two untitled pages require two real calls to the injected judge.  This
    exercises `_cmd_propose` through its processed-text path and asserts the
    JSON it emits, so restoring the old literal `model_calls: 0` fails even
    though the proposal boundaries themselves remain unchanged.
    """
    pages = [
        "진 단 서\n환자의 성명",
        "계속되는 진단 내용입니다",
        "[자료 13] 위자료 산정기준표\n서울중앙지방법원 기준",
    ]
    judge = _FakeBoundaryJudge([
        {"starts_new_document": False, "confidence": 0.9, "title": None},
        {"starts_new_document": True, "confidence": 0.9,
         "title": "위자료 산정기준표"},
    ])

    class _Document:
        page_count = len(pages)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(sc, "_manifest_bundle", lambda *args: ({}, {
        "file_path": "ignored.pdf", "source_file_name": "bundle.pdf",
    }))
    monkeypatch.setattr(sc, "_processed_page_texts", lambda *args: pages)
    monkeypatch.setattr(sc, "segmentation_prerequisite_errors", lambda *args: [])
    monkeypatch.setattr(sc, "_LazyJudge", lambda factory: judge)
    monkeypatch.setattr(sc, "_write_proposal", lambda *args: Path("proposal.json"))
    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda _: _Document()))

    args = SimpleNamespace(
        case_id="CASE_TESTONLY_9998", doc_id="DOC_001", grid="3x4",
        crop_ratio=0.33, no_text_anchor=False, held_by="test", run_id="RUN_TEST",
    )
    assert sc._cmd_propose(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(judge.prompts) == 2
    assert result["model_calls"] == 2


def test_judge_calls_survive_when_the_other_auto_pass_wins(monkeypatch):
    """`model_calls` is cost incurred, not calls behind returned boundaries.

    The policy pass wins this constructed non-bundle, but the medical pass has
    already sent two genuinely ambiguous page pairs to an injected judge.  The
    page list must survive that discarded result; clearing it would recreate a
    deceptively cheap proposal receipt.
    """
    pages = [
        "첫 번째 보통약관",
        "제목 없는 계속 페이지",
        "두 번째 특별약관",
    ]
    judge = _FakeBoundaryJudge([
        {"starts_new_document": False, "confidence": 0.9, "title": None},
        {"starts_new_document": False, "confidence": 0.9, "title": None},
    ])
    # This isolates the discarded-pass branch. Real policy bundles return
    # before the medical pass deliberately, so they correctly cost zero calls.
    monkeypatch.setattr(sc, "_is_policy_bundle", lambda boundaries: False)
    judged = []
    found = sc.boundaries_from_page_texts(
        pages, medical="auto", judge=judge, judged=judged)

    assert set(found) == {1, 3}
    assert len(judge.prompts) == 2
    assert judged == [2, 3]


def test_receipt_variants_that_name_a_different_visit_type_still_split():
    """The merge is on the printed title, so 입원 / 입원중간금 / 외래 receipts
    remain separate documents -- these are real CASE_050 page titles."""
    pages = [
        "(입원) 진료비 계산서 · 영수증\n진 찰 료 | 8,460",
        "(입원중간금) 진료비 계산서 · 영수증\n진 찰 료 | 1,200",
        _RECEIPT_PAGE,
        _RECEIPT_PAGE,
    ]
    assert set(sc.boundaries_from_page_texts(pages, medical=True)) == {1, 2, 3}


def test_a_multi_page_medical_form_still_merges():
    """The merge rule itself is untouched -- 진료비 세부산정내역 runs over
    several pages as ONE document, and must keep doing so."""
    header = "진료비 세부산정내역\n항목 | 코드 | 금액"
    pages = [header, header, "계속되는 표 내용입니다"]
    assert set(sc.boundaries_from_page_texts(pages, medical=True)) == {1}


# --- repeated table headers settle a continuation without a judge call -------

class _StubJudge:
    """The judge is a PROVIDER OBJECT, not a callable: `_judge_boundary` calls
    `.classify_document(prompt, version)` on it. A bare-function stub is never
    invoked, so it records nothing and makes every judged page look unjudged --
    which is exactly how an earlier version of these tests passed while the run
    it described was making 126 model calls."""

    def __init__(self):
        self.prompts = []

    def classify_document(self, prompt, prompt_version):
        self.prompts.append(prompt)
        return {"starts_new_document": True}

_TABLE_TITLE_PAGE = ("진료비 세부내역서\n등록번호   환자성명   진료기간\n"
                     "항목 | 일자 | 코드 | 명칭 | 단가 | 수량 | 횟수 | 일수 | 가산후총액 | 금액\n"
                     "01.진찰료 | 2025-10-16 | AA156 | 초진진찰료 | 19,100 | 1 | 1 | 1 | 19,100")
# The same printed header, read differently by OCR on the next page -- tab
# delimiters instead of pipes, and the merged 금액 columns split another way.
_TABLE_CONT_PAGE = ("항목\t일자\t코드\t명칭\t단가\t수량\t횟수\t일수\t가산후총액\t본인부담금\t공단부담금\n"
                    "02.투약료 | 2025-10-16 | AB201 | 아목시실린 | 1,091 | 1 | 1 | 1 | 1,091")


def test_a_reprinted_table_header_continues_without_asking_the_judge():
    """A tabular form prints its column names once and its continuation pages
    repeat only the header, so those pages carry no form title and used to cost
    a judge call each. Measured on CASE_047 DOC_001 (97p): 15 of 34 judged
    pages were exactly this, and settling them here took the run to 19.
    """
    judged = []

    def judge(*args, **kwargs):
        judged.append(args)
        return {"starts_new_document": True}

    pages = [_TABLE_TITLE_PAGE, _TABLE_CONT_PAGE, _TABLE_CONT_PAGE]
    found = sc.boundaries_from_page_texts(pages, medical=True,
                                          judge=_StubJudge(), judged=judged)
    assert set(found) == {1}, "continuation pages must not open a document"
    assert judged == [], "a reprinted table header must not reach the judge"


def test_the_header_match_tolerates_ocr_drift_in_the_trailing_columns():
    """Equality would match nothing: the same printed header reads
    '가산후총액 | 본인부담금 | 공단부담금' on one page and
    '가산총액\t금액 분류부담금\t...' on the next. Only the LEADING run is
    compared -- real CASE_047 pairs agree on 8-9 columns."""
    left = sc._table_header_cells([
        "항목 | 일자 | 코드 | 명칭 | 단가 | 수량 | 횟수 | 일수 | 가산후총액 | 본인부담금"])
    right = sc._table_header_cells([
        "항목\t일자\t코드\t명칭\t단가\t수량\t횟수\t일수\t가산총액\t금액 분류부담금"])
    assert left[:8] == right[:8]
    assert sc._continues_table(left, right)


def test_a_data_row_is_not_mistaken_for_a_table_header():
    """The header is told from the rows below it structurally -- a data row
    starts with a numbered item code, a date or an amount."""
    assert sc._table_header_cells([
        "01.진찰료 | 2025-10-16 | AA156 | 초진진찰료 | 19,100 | 1 | 1 | 1"]) == []


def test_two_unrelated_tables_are_not_fused_by_a_short_header():
    """The leading-run floor is what stops a two-column header from merging
    documents that merely both contain a table."""
    assert not sc._continues_table(["항목", "금액"], ["항목", "금액"])


def test_a_generic_heading_still_reaches_the_judge_despite_a_matching_table():
    """Two studies printed with the same table are still two documents, so the
    table rule must not pre-empt the judge for a page that HAS a heading.

    `judged` is the list the proposal reports as `model_calls`, so asserting on
    it asserts on the number a real run publishes."""
    judged = []
    header = "항목 | 일자 | 코드 | 명칭 | 단가 | 수량 | 횟수 | 일수 | 가산후총액 | 금액"
    pages = [f"REPORT\n{header}\n01.a | 2025-10-16 | X | y | 1 | 1 | 1 | 1 | 1",
             f"REPORT\n{header}\n02.b | 2025-10-17 | X | y | 2 | 1 | 1 | 1 | 2"]
    sc.boundaries_from_page_texts(pages, medical=True, judge=_StubJudge(),
                                  judged=judged)
    assert judged == [2], "a titled page must still be judged"


# --- per-issuance forms: a repeated title is a NEW document ------------------

def test_two_detail_statements_with_the_same_title_stay_separate():
    """진료비 세부내역서 is issued per billing run, so a second one is a second
    document -- unlike a form whose title simply reprints on its own
    continuation pages.

    Measured on CASE_047 (2026-08-18): pages 16, 23 and 30 each open their own
    statement, the repeated-title merge fused all three into one 21-page
    DOC_012, and claim_analysis then failed on it -- "quote is not present on
    page 3 -- that text appears on page(s) 10, 17" -- because the same header
    and item names recur at three offsets inside the fused document. Deleting
    the `_is_per_issuance` check collapses this to {1}.
    """
    titled = "진료비 세부내역서\n등록번호   환자성명   진료기간\n" + \
             "항목 | 일자 | 코드 | 명칭 | 단가 | 수량 | 횟수 | 일수 | 가산후총액 | 금액\n" + \
             "01.진찰료 | 2025-10-16 | AA156 | 초진진찰료 | 19,100 | 1 | 1 | 1 | 19,100"
    assert set(sc.boundaries_from_page_texts([titled, titled], medical=True)) == {1, 2}


def test_a_per_issuance_statement_still_keeps_its_own_continuation_pages():
    """The exception is narrow: it separates two TITLED pages. A continuation
    page carries no title, only the reprinted column header, so it still
    belongs to the statement above it."""
    judged = []
    titled = "진료비 세부내역서\n등록번호   환자성명   진료기간\n" + \
             "항목 | 일자 | 코드 | 명칭 | 단가 | 수량 | 횟수 | 일수 | 가산후총액 | 금액\n" + \
             "01.진찰료 | 2025-10-16 | AA156 | 초진진찰료 | 19,100 | 1 | 1 | 1 | 19,100"
    pages = [titled, _TABLE_CONT_PAGE, _TABLE_CONT_PAGE, titled]
    found = sc.boundaries_from_page_texts(pages, medical=True,
                                          judge=_StubJudge(), judged=judged)
    assert set(found) == {1, 4}, "continuations stay, the second statement splits"
    assert judged == [], "no judge call is needed for either decision"


def test_receipts_are_still_merged_because_that_is_the_operator_decision():
    """A receipt reprints the patient block like any reissued form, so the
    evidence says "new document" -- it is merged anyway, by operator decision,
    via `_MERGED_FORM_TITLES`. Asserting the block IS present keeps this test
    honest about which of the two rules is doing the work."""
    assert sc._reprints_patient_block(
        [line.strip() for line in _RECEIPT_PAGE.splitlines() if line.strip()])
    assert set(sc.boundaries_from_page_texts(
        [_RECEIPT_PAGE, _RECEIPT_PAGE], medical=True)) == {1}


# --- blank-line layout pushes the column row out of the header window -------
# Measured on CASE_488/DOC_005 (2026-08-20), a 19-page 손해사정서 evidence
# appendix. Pages 9-15 are ONE 진료비 세부산정내역, but each page opened its own
# document, splitting a 7-page form into 7 documents.
#
# The form prints blank lines between its header rows:
#
#     0  진료비 세부산정내역(퇴원)
#     1
#     2  요양기관호 :                        의사면허번호 :
#     3
#     4  환자등록번호   환자성명   진료기간   병실   환자구분   비고
#     5
#     6  항목 | 일자 | 코드 | 명칭 | 금액 | 횟수 일수 | 총액 | ...
#
# so the real column row sits at index 6 -- one line past a 6-line window. The
# widest-line tiebreak then settled on the PATIENT BLOCK at index 4, which is
# delimited identically. Page 9's block has 6 cells and page 10's has 7 (an
# 의사면허번호 column moves up), so the leading-run comparison failed and every
# page read as a new document.
#
# This form also reprints the patient block on every page, so
# `_reprints_patient_block` cannot carry the merge on its own -- the table-header
# rule is the only one that can, which is why the window has to reach it.

_BLANK_SPACED_BILLING_PAGE_9 = "\n".join([
    "진료비 세부산정내역(퇴원)",
    "",
    "요양기관호 :                              의사면허번호 :",
    "",
    "환자등록번호    환자성명    진료기간    병실    환자구분    비고",
    "",
    "항목 | 일자 | 코드 | 명칭 | 금액 | 횟수 일수 | 총액 | 금액 | 비급여",
    "",
    "01. 진찰료 | 2023-12-04 | AA156 | 초진료 | 18,520 | 1 | 1 | 18,520 | 0",
])

_BLANK_SPACED_BILLING_PAGE_10 = "\n".join([
    "진료비 세부산정내역(퇴원)",
    "",
    "요양기관기호 :",
    "",
    "환자등록번호 | 환자성명 | 진료기간 | 병실 | 환자구분 | 의사면허번호 | 비고",
    "",
    "항목 | 일자 | 코드 | 명칭 | 금액 | 횟수 | 일수 | 총액 | 금액 | 비고",
    "",
    "04. 약품투약료 | 2023-12-07 | J2000 | 조제료 | 1,812 | 2 | 3,624 | 0",
])


def test_column_row_below_blank_lines_is_found_not_the_patient_block():
    """The window must reach the real column row, and the patient block above
    it must not win the widest-line tiebreak."""
    cells = sc._table_header_cells(_BLANK_SPACED_BILLING_PAGE_9.splitlines())
    assert cells[:4] == ["항목", "일자", "코드", "명칭"], (
        f"picked the wrong row: {cells}")


def test_blank_spaced_billing_continuation_pages_stay_one_document():
    """CASE_488 DOC_005 pages 9-15: one form, not seven documents."""
    left = sc._table_header_cells(_BLANK_SPACED_BILLING_PAGE_9.splitlines())
    right = sc._table_header_cells(_BLANK_SPACED_BILLING_PAGE_10.splitlines())
    assert sc._continues_table(left, right), (
        f"continuation not detected: {left} vs {right}")


def test_patient_block_alone_is_still_not_a_column_header():
    """A page with the block but no table must yield no header, so the widened
    window cannot start matching two unrelated forms on their patient blocks."""
    assert sc._table_header_cells([
        "진 단 서",
        "",
        "환자등록번호    환자성명    진료기간    병실    환자구분    비고",
        "",
    ]) == []


# --- OCR splits or joins a header cell -------------------------------------
# Measured on CASE_488/DOC_005 p9 vs p10 (2026-08-20). The SAME printed header
# reads "... | 금액 | 횟수 일수 | 총액 ..." on one page and
# "... | 금액 | 횟수 | 일수 | 총액 ..." on the next -- two adjacent columns
# joined into one cell by the transcription. Strict positional equality stops
# at the join (5 matched, floor is 6) and the pages split into two documents,
# even though every remaining column agrees. Pages 10-15 of the same form agree
# on 8-9 leading cells and merged correctly, so the join on p9 was the only
# thing separating page 9 from its own continuation.

def test_a_joined_header_cell_still_matches_the_split_reading():
    joined = ["항목", "일자", "코드", "명칭", "금액", "횟수일수", "총액", "금액", "비급여"]
    split = ["항목", "일자", "코드", "명칭", "금액", "횟수", "일수", "총액", "금액"]
    assert sc._continues_table(joined, split)
    assert sc._continues_table(split, joined), "comparison must be symmetric"


def test_joining_does_not_fuse_genuinely_different_headers():
    """The join tolerance consumes cells only while they spell the same run;
    two different tables still score below the floor."""
    billing = ["항목", "일자", "코드", "명칭", "금액", "횟수", "일수", "총액"]
    lab = ["검사명", "결과", "참고치", "단위", "판정", "비고", "채취일", "보고일"]
    assert not sc._continues_table(billing, lab)


# --- a repeated title + patient block, but the table runs on ---------------
# Measured on CASE_488/DOC_005 pages 9-15 (2026-08-20). A 7-page
# 진료비 세부산정내역 reprints BOTH its title and its patient block on every
# page, so the patient-block test -- which separates CASE_907's continuing
# statement from CASE_047's three separately-issued ones -- reads every page as
# a reissue and split one form into 7 documents.
#
# The table header is the evidence that settles it: a form running over resumes
# the SAME columns, while a genuinely re-issued form starts its table again
# under its own header. This is already how titleless continuation pages are
# decided; extending it to titled pages removes the need to keep listing
# individual form names in _MERGED_FORM_TITLES, which is the maintenance trap
# the vocabulary approach walks into every time a new form appears.

_REISSUE_HEADER = ["항목", "일자", "코드", "명칭", "금액", "횟수", "일수", "총액"]


def _billing_page(title="진료비 세부산정내역(퇴원)", columns=None, first_row=None):
    cols = columns if columns is not None else _REISSUE_HEADER
    return "\n".join([
        title,
        "",
        "요양기관기호 :",
        "",
        "환자등록번호  환자성명  진료기간  병실  환자구분  비고",
        "",
        " | ".join(cols),
        "",
        first_row or "01. 진찰료 | 2023-12-04 | AA156 | 초진료 | 18,520 | 1 | 1 | 18,520",
    ])


def test_repeated_title_with_a_continuing_item_run_is_one_document():
    """CASE_488 DOC_005 p9-15: title and patient block both reprint, but the
    numbered item run climbs 01 -> 06 -> 12, so these pages continue."""
    pages = [
        _billing_page(first_row="01. 진찰료 | 2023-12-04 | AA156 | 초진료 | 18,520"),
        _billing_page(first_row="06. 비급여주사료 | 2023-12-05 | 0647801081 | 90,000"),
        _billing_page(first_row="12. 급여80치료재료 | 2023-12-05 | K7202009 | 1,150"),
    ]
    found = sc.boundaries_from_page_texts(pages, medical=True)
    assert set(found) == {1}, f"continuation pages opened documents: {sorted(found)}"


def test_a_restarted_item_run_still_opens_a_new_document():
    """CASE_047 pages 16/23/30: three statements sharing one title, each
    restarting at 01. Merging them broke claim_analysis, so a restart must
    still split even though the title and columns are identical."""
    first = _billing_page(first_row="01. 진찰료 | 2025-10-16 | AA156 | 초진진찰료 | 19,100")
    again = _billing_page(first_row="01. 진찰료 | 2025-11-20 | AA156 | 초진진찰료 | 19,100")
    found = sc.boundaries_from_page_texts([first, again], medical=True)
    assert set(found) == {1, 2}, f"a reissued statement was merged away: {sorted(found)}"


# --- an operator-chosen P8 reduction is not an unresolved disagreement ------
# Measured on CASE_487 and CASE_489 (2026-08-20). Both flags that reduce P8 --
# `--single-reader` and `--on-disagreement assume-reading-a` -- are documented
# orchestrator decisions with their own honest status values, and both leave
# REAL TEXT on disk. The gate accepted only {agreed, disagreed_resolved}, so a
# reduced run could OCR and redact every page and then be refused at
# segmentation with "human resolution is required", which there is nothing to
# resolve: single-reader never compared, so no disagreement exists to settle.
#
# What this gate exists to prevent, per its own docstring, is falling back to
# raw-PDF vision when text is missing or blocked -- "that would turn an
# extraction hard gate into a routing preference". A reduced-P8 document is not
# that case. `disagreed_pending_review` still IS: real pages disagreed, a human
# owes a verdict, and the text is not settled. That distinction is the fix.
#
# Title lines -- the boundary signal -- were byte-identical across the two
# readings on every disagreed page of CASE_488/DOC_005; the differences sat in
# table codes and amounts. So a reduced read costs precision inside a document,
# not the evidence segmentation actually cuts on.

def _p8_bundle(status, tmp_path):
    bundle = {
        "document_id": "DOC_001",
        "ocr_status": "completed",
        "cross_validation_status": status,
        "redacted_text_path": "data/processed/CASE_900/DOC_001/redacted_text.md",
    }
    text = tmp_path / bundle["redacted_text_path"]
    if not text.exists():
        text.parent.mkdir(parents=True, exist_ok=True)
        text.write_text("<<<PAGE page=1>>>\nfirst\n<<<PAGE page=2>>>\nsecond\n",
                        encoding="utf-8")
    return bundle


@pytest.mark.parametrize("status", [
    "single_reader_no_cross_validation",   # --single-reader
    "assume_reading_a_unreviewed",         # --on-disagreement assume-reading-a
])
def test_a_reduced_p8_document_may_be_segmented(status, tmp_path):
    original_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        errors = sc.segmentation_prerequisite_errors(_p8_bundle(status, tmp_path), 2)
    finally:
        sc.ROOT = original_root
    assert errors == [], f"{status} was refused: {errors}"


def test_an_unresolved_disagreement_is_still_refused(tmp_path):
    """The case the gate is actually for: pages disagreed, nobody adjudicated,
    and the text genuinely is not settled."""
    original_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        errors = sc.segmentation_prerequisite_errors(
            _p8_bundle("disagreed_pending_review", tmp_path), 2)
    finally:
        sc.ROOT = original_root
    assert any("human resolution" in error for error in errors)


def test_a_document_with_no_ocr_is_still_refused(tmp_path):
    """Reducing P8 must not become a way past a document that has no text --
    that is the raw-vision fallback the gate exists to block."""
    bundle = _p8_bundle("single_reader_no_cross_validation", tmp_path)
    bundle["ocr_status"] = "pending"
    original_root = sc.ROOT
    sc.ROOT = tmp_path
    try:
        errors = sc.segmentation_prerequisite_errors(bundle, 2)
    finally:
        sc.ROOT = original_root
    assert any("OCR is" in error for error in errors)


# ------------------------------------------- judge tier concurrency --

class _ConcurrencyProbeJudge:
    """Blocks each call until every expected call has arrived.

    A judge tier that dispatches serially can never satisfy the barrier: call 1
    waits for call 2, which is not sent until call 1 returns. So the barrier
    timing out IS the serial-execution assertion -- no sleep-and-compare, which
    would only measure that the machine was fast that afternoon.
    """

    provider_name = "probe"
    model_name = "probe-model"

    def __init__(self, expected):
        import threading
        self._barrier = threading.Barrier(expected, timeout=10)
        self._lock = threading.Lock()
        self.prompts = []
        self.max_inflight = 0
        self._inflight = 0
        self.timed_out = False

    def classify_document(self, prompt, prompt_version):
        from llm_providers import ProviderResult
        import threading
        with self._lock:
            self.prompts.append(prompt)
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            self._barrier.wait()
        except threading.BrokenBarrierError:
            self.timed_out = True
        finally:
            with self._lock:
                self._inflight -= 1
        return ProviderResult(self.provider_name, self.model_name, prompt_version,
                              json.dumps({"starts_new_document": False,
                                          "confidence": 0.9, "title": None}))


def _three_untitled_judged_pages():
    """A medical bundle whose pages 2-4 all reach the judge.

    Each is untitled and shares no table header with its predecessor, so every
    deterministic shortcut declines and the LLM tier gets all three.
    """
    return [
        "진단서\n환자성명 홍길동\n상병명 요추 염좌",
        "이것은 제목이 없는 본문 페이지입니다\n내용이 이어집니다",
        "또 다른 제목 없는 페이지\n다른 내용이 적혀 있습니다",
        "세 번째 제목 없는 페이지\n전혀 다른 서술이 있습니다",
    ]


def test_judge_tier_dispatches_its_calls_concurrently():
    """The CASE_701 finding: 28 boundary judgements ran strictly back-to-back.

    Measured on that run, `segment.judge` self-time summed to exactly its own
    wall span on all three bundles (48.8/48.8, 77.0/77.0, 29.4/29.4) -- zero
    overlap -- for 169s of the stage's 411s. Which pages reach the judge is
    decidable from page text and the contents-page map alone, so the verdicts
    can be fetched together even though `previous_title` stays sequential.
    """
    pages = _three_untitled_judged_pages()
    judged = []
    judge = _ConcurrencyProbeJudge(expected=3)

    found = sc.boundaries_from_page_texts(pages, medical=True, judge=judge,
                                          judged=judged)

    assert judged == [2, 3, 4], "all three untitled pages must reach the judge"
    assert not judge.timed_out, (
        "the judge tier dispatched serially: three independent page-pair "
        "verdicts never overlapped")
    assert judge.max_inflight == 3
    # Every verdict said 'continues', so only page 1 opens a document.
    assert set(found) == {1}


def test_prefetched_judge_verdicts_match_the_sequential_result():
    """Concurrency must not move a boundary.

    Same pages, same scripted verdicts, and the boundary set plus the recorded
    `judged` list must be what the one-at-a-time loop produced.
    """
    pages = _three_untitled_judged_pages()
    verdicts = [
        {"starts_new_document": False, "confidence": 0.9, "title": None},
        {"starts_new_document": True, "confidence": 0.9, "title": "소견서"},
        {"starts_new_document": False, "confidence": 0.9, "title": None},
    ]
    judged = []
    judge = _FakeBoundaryJudge(verdicts)
    found = sc.boundaries_from_page_texts(pages, medical=True, judge=judge,
                                          judged=judged)

    assert judged == [2, 3, 4]
    assert len(judge.prompts) == 3, "one call per judged page, no duplicates"
    assert set(found) == {1, 3}
    assert found[3] == "소견서"


def test_a_judge_failure_still_splits_and_flags_when_prefetched():
    """Fail-toward-splitting survives the concurrent path.

    A provider that raises must still be recorded as a judge failure and leave
    the page in `undecided`, not vanish into a swallowed future.
    """
    class _Exploding:
        provider_name = "boom"
        model_name = "boom-model"

        def classify_document(self, prompt, prompt_version):
            raise RuntimeError("provider is down")

    pages = _three_untitled_judged_pages()
    judge = _Exploding()
    undecided = sc.undecided_pages(pages, medical=True, judge=judge)

    assert undecided == [2, 3, 4], "every failed judgement is flagged undecided"
    assert len(sc.judge_failures(judge)) == 3


class _PureJudge:
    """Verdict is a pure function of the prompt: same page pair, same answer.

    Deliberately stateless. A first attempt at this stub drew from a shared
    `random.Random` the first time it saw each prompt, so a different arrival
    order handed the same page a different verdict -- it reported 27 of 60 seeds
    as mismatched when the code under test was correct and the STUB was the
    order-dependent thing. A differential test whose oracle depends on ordering
    cannot say anything about ordering.
    """

    provider_name = "pure"
    model_name = "pure-model"

    def __init__(self, seed):
        self._seed = seed

    def classify_document(self, prompt, prompt_version):
        from llm_providers import ProviderResult
        digest = hashlib.sha256(f"{self._seed}|{prompt}".encode()).digest()
        new = digest[0] < 102  # ~40% of pages start a document
        return ProviderResult(self.provider_name, self.model_name, prompt_version,
                              json.dumps({"starts_new_document": new,
                                          "confidence": 0.9,
                                          "title": "소견서" if new else None}))


@pytest.mark.parametrize("seed", range(24))
def test_concurrent_judging_gives_the_same_boundaries_as_serial(seed, monkeypatch):
    """Prefetching must be invisible in the result, not merely faster.

    `previous_title` is still threaded one page at a time; only the provider
    calls overlap. Boundaries, the judged-page list, and the undecided list must
    all come back identical to the one-at-a-time path on the same input.
    """
    import random

    rng = random.Random(seed)
    pages = ["진단서\n환자성명 홍길동"]
    for i in range(rng.randint(3, 20)):
        if rng.random() < 0.3:
            pages.append(f"진료비 세부산정내역\n등록번호 {i}\n01.진찰료 | {i}")
        else:
            pages.append(f"제목없는 페이지 {i}\n본문 내용 {rng.random()}")
    lines = [p.split("\n") for p in pages]

    results = {}
    for workers in (1, 8):
        monkeypatch.setattr(sc, "_JUDGE_WORKERS", workers)
        judged, undecided = [], []
        boundaries = sc._boundaries_from_page_lines(
            lines, medical=True, judge=_PureJudge(seed), page_texts=pages,
            undecided=undecided, judged=judged)
        results[workers] = (boundaries, judged, undecided)

    assert results[1] == results[8]


def test_prefetch_asks_about_exactly_the_pages_the_loop_judges():
    """The prefetch page set must EQUAL what the sequential loop asks about.

    Set equality, not sufficiency. Over-fetching is invisible to any boundary
    assertion -- the loop simply ignores entries it never looks up -- so it
    would ship as a correct answer that quietly pays for extra model calls.
    Verified by deleting the table-continuation gate from
    `_pages_needing_judgement`: with the earlier fixture the test still passed,
    because that fixture never produced a continuation page at all.
    """
    import random

    # Table-continuation pages are the reason this is not merely "every untitled
    # page": they carry no form title but repeat the previous page's column
    # header, and the loop skips them WITHOUT asking -- 15 of 34 judged pages on
    # CASE_047 DOC_001. The fixture must contain them for the equality to bite.
    fixtures = []
    for seed in range(12):
        rng = random.Random(seed)
        pages = ["진단서\n환자성명 홍길동"]
        for i in range(rng.randint(3, 16)):
            roll = rng.random()
            if roll < 0.25:
                pages.append(_TABLE_TITLE_PAGE)
            elif roll < 0.50:
                pages.append(_TABLE_CONT_PAGE)
            elif roll < 0.65:
                pages.append(f"진료비 세부산정내역\n등록번호 {i}\n01.진찰료 | {i}")
            else:
                pages.append(f"제목없는 페이지 {i}\n본문 내용 {rng.random()}")
        fixtures.append(pages)

    # The gate is only under test if the corpus actually reaches it.
    reached = 0
    for pages in fixtures:
        lines = [p.split("\n") for p in pages]
        for index in range(1, len(lines)):
            if sc._medical_header_title(lines[index]) is None and sc._continues_table(
                    sc._table_header_cells(lines[index - 1]),
                    sc._table_header_cells(lines[index])):
                reached += 1
    assert reached >= 5, (
        f"fixture never exercises the table-continuation skip ({reached} hits); "
        "the equality assertion below would pass on an over-fetching prefetch")

    for seed, pages in enumerate(fixtures):
        lines = [p.split("\n") for p in pages]
        judged = []
        sc._boundaries_from_page_lines(lines, medical=True, judge=_PureJudge(seed),
                                       page_texts=pages, judged=judged)
        # `judged` holds page numbers, the prefetch holds indexes.
        toc = [sc._is_toc_page(line_set) for line_set in lines]
        predicted = sc._pages_needing_judgement(lines, toc, medical=True)
        assert [i + 1 for i in predicted] == judged, f"seed {seed}"
        assert len(predicted) == len(set(predicted))
