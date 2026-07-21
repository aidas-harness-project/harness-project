"""Stage 1 document segmentation: split a raw bundle PDF into logical documents.

Source bundles concatenate several separate documents (claim form, diagnosis
certificate, medical records, receipts, insurer response) into one PDF. Treating
a bundle as one document means every later stage -- classification, field
extraction, evidence citation -- runs on wrong document boundaries.

This stage produces DOCUMENT STRUCTURE, NOT TEXT. It renders low-resolution
contact sheets, asks (or lets a human decide) where each document starts, records
a proposal for human approval, and only then splits the PDF. It never transcribes
text: segmentation_proposal.schema.json pins ``ocr_performed`` to a const false so
a proposal claiming otherwise fails validation. Real OCR remains
document-pipeline's checkpoint 1, on the resulting per-document PDFs.

This module currently holds the pure, I/O-free half of that flow (build step 2 of
docs/stage1-document-segmentation-plan.md); rendering, compositing, the provider
path, and the split itself land in later steps.

Two measurements from the real corpus drive the design (see the plan doc):

* Every page of every source PDF is a scan -- 0 of 110 pages in the largest
  bundle carry embedded text -- so there is no cheap text signal to segment on.
* Vision APIs cap an image's long edge (1568px) and total pixels (~1.15M), so a
  tall vertical strip starves its own width: 15 pages stacked leaves ~222px of
  width and unreadable Korean titles. A grid costs the same tokens per sheet
  whatever its shape, which makes packing pages into a grid both cheaper per page
  and more legible. Hence contact sheets, not strips.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent

# Separate from _ocr_scratch/ on purpose. That directory's scratch_dir() rmtrees
# on exit, but contact sheets must OUTLIVE the process: a human reads them to
# approve boundaries. It is also already serving four unrelated purposes.
SCRATCH_ROOT = ROOT / "_segmentation_scratch"

METHOD_VERSION = "segment_contact_sheet_v0.1"

# Anthropic vision downscales past either bound, so compositing beyond them buys
# nothing: the cells just get resampled smaller on the way in. Module-level so a
# test can pin them and so a future provider with different limits is a one-line
# change rather than a hunt through the geometry math.
LONG_EDGE_CAP = 1568
TOTAL_PIXEL_CAP = 1_150_000

# A4 at 72dpi -- every PDF in the corpus is this size.
DEFAULT_PAGE_WIDTH_PT = 595.0
DEFAULT_PAGE_HEIGHT_PT = 841.0

# Body content starts at 0.02-0.20 of page height across the sampled corpus, so a
# third of the page captures the title block and the form structure under it with
# margin to spare.
DEFAULT_CROP_RATIO = 0.33
MIN_CROP_RATIO = 0.2
MAX_CROP_RATIO = 0.6

# Settled by rendering the real bundle at 2x4/3x4/4x4 and looking: at a 387x177
# cell, titles (손해 사정서, 진 단 서, 후유장해진단서), letterheads and even body
# paragraphs read clearly, and a real boundary was visible directly (p1-13 carry
# one letterhead; p14 switches to a 진단서). An earlier concern that list items
# would break down at this size assumed a render-then-downscale pipeline;
# rendering each cell straight at final size via the zoom matrix avoids that loss.
# Still a flag, not a constant -- 3x4 is meaningfully larger for a harder bundle.
DEFAULT_GRID_COLS = 4
DEFAULT_GRID_ROWS = 4

# Mirrors common_component_output.schema.json#/$defs/document_type. Duplicated as
# a literal rather than read from the schema at import time so this module stays
# I/O-free; test_document_type_enum_matches_the_schema fails if they drift.
DOCUMENT_TYPES = frozenset({
    "insurance_certificate", "insurance_policy", "diagnosis_certificate",
    "medical_record", "imaging_report", "receipt", "insurer_response", "other",
})


class SegmentationError(Exception):
    """A caller error: an impossible geometry request, a malformed page range.

    Deliberately NOT raised for a model response we could not parse -- that is
    expected operational noise and is reported through the returned dict instead.
    """


# ------------------------------------------------------------- geometry --

def compute_sheet_geometry(
    *,
    cols: int = DEFAULT_GRID_COLS,
    rows: int = DEFAULT_GRID_ROWS,
    crop_ratio: float = DEFAULT_CROP_RATIO,
    page_width_pt: float = DEFAULT_PAGE_WIDTH_PT,
    page_height_pt: float = DEFAULT_PAGE_HEIGHT_PT,
    separator_px: int = 4,
    long_edge_cap: int = LONG_EDGE_CAP,
    total_pixel_cap: int = TOTAL_PIXEL_CAP,
) -> dict:
    """Sizes one contact sheet so it arrives at the vision API already within
    both caps.

    Landing under the caps ourselves is the point: anything larger is silently
    resampled on arrival, so we would pay to render detail the model never sees,
    and we would hand it a downscale we did not control. Sizing here instead lets
    the renderer draw each cell at its final size with a good resampler.

    Returns cell/sheet pixel dimensions plus the zoom to render a page at, so the
    renderer never produces an intermediate full-resolution image.
    """
    if cols < 1 or rows < 1:
        raise SegmentationError(f"grid must be at least 1x1, got {cols}x{rows}")
    if not (MIN_CROP_RATIO <= crop_ratio <= MAX_CROP_RATIO):
        raise SegmentationError(
            f"crop_ratio {crop_ratio} outside [{MIN_CROP_RATIO}, {MAX_CROP_RATIO}]"
        )
    if page_width_pt <= 0 or page_height_pt <= 0:
        raise SegmentationError("page dimensions must be positive")

    cropped_height_pt = page_height_pt * crop_ratio

    # Solve at the caps rather than rendering-then-shrinking: pick the largest
    # sheet that satisfies both, then divide back down to a cell.
    ideal_w = page_width_pt * cols
    ideal_h = cropped_height_pt * rows
    aspect = ideal_w / ideal_h

    if aspect >= 1:
        sheet_w = float(long_edge_cap)
        sheet_h = sheet_w / aspect
    else:
        sheet_h = float(long_edge_cap)
        sheet_w = sheet_h * aspect

    if sheet_w * sheet_h > total_pixel_cap:
        shrink = (total_pixel_cap / (sheet_w * sheet_h)) ** 0.5
        sheet_w *= shrink
        sheet_h *= shrink

    # Separators eat into the cells, not the sheet: the sheet size is fixed by the
    # caps above, so widening a separator makes cells smaller rather than pushing
    # the sheet over budget.
    total_sep_w = separator_px * (cols + 1)
    total_sep_h = separator_px * (rows + 1)
    cell_w = (sheet_w - total_sep_w) / cols
    cell_h = (sheet_h - total_sep_h) / rows

    if cell_w < 1 or cell_h < 1:
        raise SegmentationError(
            f"grid {cols}x{rows} with {separator_px}px separators leaves no room "
            f"for cells within the {long_edge_cap}px/{total_pixel_cap}px caps"
        )

    return {
        "cols": cols,
        "rows": rows,
        "pages_per_sheet": cols * rows,
        "crop_ratio": crop_ratio,
        "separator_px": separator_px,
        "cell_w": int(cell_w),
        "cell_h": int(cell_h),
        "sheet_w": int(sheet_w),
        "sheet_h": int(sheet_h),
        "total_pixels": int(sheet_w) * int(sheet_h),
        # The renderer multiplies the PDF's native size by this to land straight
        # on cell_w, skipping any intermediate bitmap.
        "zoom": cell_w / page_width_pt,
    }


def plan_sheets(page_count: int, pages_per_sheet: int) -> list[list[int]]:
    """Groups 1-based page numbers into per-sheet batches.

    The final batch is left short rather than padded; the compositor draws blank
    cells for the shortfall. Padding here by repeating pages would manufacture
    phantom document boundaries.
    """
    if page_count < 1:
        raise SegmentationError(f"page_count must be >= 1, got {page_count}")
    if pages_per_sheet < 1:
        raise SegmentationError(f"pages_per_sheet must be >= 1, got {pages_per_sheet}")
    return [
        list(range(start, min(start + pages_per_sheet, page_count + 1)))
        for start in range(1, page_count + 1, pages_per_sheet)
    ]


# -------------------------------------------------------------- parsing --

def _scan_for_json_object(raw: str) -> tuple[dict | None, str | None]:
    """Finds the first decodable JSON object in a response.

    Borrowed from redact_document._parse_redaction: models wrap JSON in prose
    often enough that a strict json.loads fails on output that is otherwise
    perfectly usable. Unlike that function this reports failure by return value
    -- see parse_segmentation_response for why.
    """
    decoder = json.JSONDecoder()
    start = 0
    saw_brace = False
    while True:
        brace = raw.find("{", start)
        if brace == -1:
            return None, (
                "response contained invalid JSON" if saw_brace
                else "response contained no JSON object"
            )
        saw_brace = True
        try:
            parsed, _ = decoder.raw_decode(raw[brace:])
        except json.JSONDecodeError:
            start = brace + 1
            continue
        if not isinstance(parsed, dict):
            start = brace + 1
            continue
        return parsed, None


def _coerce_page_list(value, sheet_pages: set[int], field: str) -> tuple[list[int], str | None]:
    """Validates one page-number array against the pages actually on the sheet.

    A page number outside the sheet means the model lost track of which image it
    was looking at, which makes the whole response untrustworthy rather than
    partially usable -- so it fails the sheet instead of being dropped quietly.
    """
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], f"{field} was not a list"
    pages: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return [], f"{field} contained a non-integer entry: {item!r}"
        if item not in sheet_pages:
            return [], f"{field} referenced page {item}, which is not on this sheet"
        pages.append(item)
    return sorted(set(pages)), None


def parse_segmentation_response(raw: str, sheet_pages: list[int]) -> dict:
    """Parses one sheet's model response. NEVER raises.

    Two precedents in this repo disagree on failure handling:
    redact_document._parse_redaction raises, while
    intake_case._parse_content_scan_verdict returns a dict and fails safe. This
    follows the second, for two reasons. One sheet failing must not discard the
    sheets around it, whose vision calls are already paid for. And there is no
    safe default segmentation: fail-safe here means proposing NOTHING for the
    affected pages so a human decides, never inventing a boundary.

    Returns ``{ok, boundaries, continuations, needs_full_page, warning}``.
    ``boundaries`` entries keep their metadata (type guess, confidence,
    evidence); the other two are plain page lists.
    """
    page_set = set(sheet_pages)

    def failed(warning: str) -> dict:
        return {
            "ok": False,
            "boundaries": [],
            "continuations": [],
            "needs_full_page": [],
            "warning": warning,
        }

    parsed, error = _scan_for_json_object(raw)
    if parsed is None:
        return failed(f"{error}: {raw[:200]!r}")

    raw_boundaries = parsed.get("boundaries", [])
    if not isinstance(raw_boundaries, list):
        return failed("boundaries was not a list")

    boundaries = []
    for entry in raw_boundaries:
        if not isinstance(entry, dict):
            return failed(f"boundaries contained a non-object entry: {entry!r}")
        page = entry.get("page")
        if isinstance(page, bool) or not isinstance(page, int):
            return failed(f"a boundary had a non-integer page: {page!r}")
        if page not in page_set:
            return failed(f"a boundary referenced page {page}, which is not on this sheet")
        confidence = entry.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= confidence <= 1.0
        ):
            return failed(f"boundary page {page} had confidence outside 0..1: {confidence!r}")
        # Normalize the enum-typed guess here rather than at manifest-write time.
        # The enum has 8 buckets for a corpus with far more real form types, so a
        # model naming a genuine document type outside it (청구서 -> "claim_form")
        # is expected, not a malfunction. Dropping the unknown value to None keeps
        # the manifest write valid; the model's own wording survives in
        # type_label, which is exactly why that field exists.
        type_guess = entry.get("type_guess")
        normalized_guess = type_guess if type_guess in DOCUMENT_TYPES else None
        boundaries.append({
            "page": page,
            "type_guess": normalized_guess,
            "type_label": entry.get("type_label") or (
                type_guess if isinstance(type_guess, str) else None
            ),
            "confidence": float(confidence) if confidence is not None else None,
            "evidence": entry.get("evidence"),
        })

    seen_pages = set()
    for entry in boundaries:
        if entry["page"] in seen_pages:
            return failed(f"page {entry['page']} was listed as a boundary twice")
        seen_pages.add(entry["page"])

    continuations, error = _coerce_page_list(parsed.get("continuations"), page_set, "continuations")
    if error:
        return failed(error)
    needs_full_page, error = _coerce_page_list(parsed.get("needs_full_page"), page_set, "needs_full_page")
    if error:
        return failed(error)

    # A page claimed as both a new document and a continuation of the previous one
    # is a contradiction. Boundary wins (see merge_sheet_proposals) but the
    # response is still recorded as suspect.
    contradictions = sorted(seen_pages & set(continuations))
    warning = None
    if contradictions:
        warning = (
            f"pages {contradictions} were listed as both a boundary and a "
            f"continuation; treating them as boundaries"
        )

    boundaries.sort(key=lambda item: item["page"])
    return {
        "ok": True,
        "boundaries": boundaries,
        "continuations": continuations,
        "needs_full_page": needs_full_page,
        "warning": warning,
    }


# ---------------------------------------------------------------- merge --

def merge_sheet_proposals(
    per_sheet: list[dict],
    page_count: int,
    *,
    sheet_pages: list[list[int]] | None = None,
) -> dict:
    """Stitches per-sheet responses into contiguous segments.

    The load-bearing idea: **segments come from the union of `boundaries` alone,
    and sheet edges mean nothing.** `continuations` only records that the model
    looked at a page. Treating it that way makes "a document spanning a sheet
    break" a non-problem rather than a special case -- a document running p1-14
    across a 12-page sheet boundary stays one segment because no boundary was
    reported at p13.

    Four edge cases, all settled with the project owner:

    A. p1 never reported as a boundary -> treat it as one. Page 1 of a bundle is
       by definition the first page of something; the model's silence does not
       change that. Recorded as a warning.
    B. A page in neither list -> leave it unassigned. Absorbing a page the model
       never mentioned would let a human approve a document without knowing it
       contains an unreviewed page; `split` halts on unassigned pages, so this
       guarantees the page is seen.
    C. A `needs_full_page` page -> the fallback re-checks it; whatever survives
       here flags its segment for human attention rather than splitting it.
    D. A page in both lists -> boundary wins. The error costs are asymmetric:
       over-splitting is undone by a human merging two segments, while
       over-merging only surfaces after OCR, classification, and extraction have
       all run on the wrong boundaries.
    """
    if page_count < 1:
        raise SegmentationError(f"page_count must be >= 1, got {page_count}")

    warnings: list[str] = []
    boundary_meta: dict[int, dict] = {}
    mentioned: set[int] = set()
    needs_full_page: set[int] = set()
    failed_pages: set[int] = set()

    for index, sheet in enumerate(per_sheet):
        covered = set(sheet_pages[index]) if sheet_pages and index < len(sheet_pages) else set()
        if not sheet.get("ok", False):
            # Only this sheet's pages are lost; the rest of the run still stands.
            failed_pages |= covered
            if sheet.get("warning"):
                warnings.append(f"sheet {index}: {sheet['warning']}")
            continue
        if sheet.get("warning"):
            warnings.append(f"sheet {index}: {sheet['warning']}")
        for entry in sheet.get("boundaries", []):
            page = entry["page"]
            if page in boundary_meta:
                warnings.append(f"page {page} was reported as a boundary by more than one sheet")
            boundary_meta.setdefault(page, entry)
            mentioned.add(page)
        mentioned |= set(sheet.get("continuations", []))
        needs_full_page |= set(sheet.get("needs_full_page", []))

    mentioned |= needs_full_page

    boundaries = sorted(boundary_meta)
    # Case A, but only when page 1 was actually covered by some sheet. Applying it
    # to a partial run -- sheets covering p65-80 of an 80-page document, say --
    # would fabricate a one-page segment at p1 that nothing ever looked at.
    page_1_was_examined = (
        1 in mentioned
        or (sheet_pages and any(1 in covered for covered in sheet_pages))
    )
    if boundaries and boundaries[0] != 1 and 1 not in failed_pages and page_1_was_examined:
        warnings.append(
            "page 1 was not reported as a document start; treating it as one "
            "since a bundle's first page necessarily begins some document"
        )
        boundaries.insert(0, 1)
    elif not boundaries and not failed_pages:
        warnings.append("no document boundaries were reported for any page")

    # Cases B and the failed-sheet rule: a page is only assignable if some sheet
    # actually accounted for it.
    assignable = {
        page for page in range(1, page_count + 1)
        if page in mentioned and page not in failed_pages
    }

    segments: list[dict] = []
    for position, start in enumerate(boundaries):
        if start in failed_pages:
            continue
        limit = boundaries[position + 1] if position + 1 < len(boundaries) else page_count + 1
        end = start
        # Extend while pages remain contiguous AND accounted for, so a gap ends
        # the segment instead of swallowing an unmentioned page.
        for page in range(start + 1, limit):
            if page not in assignable:
                break
            end = page
        if start not in assignable and start != 1:
            continue
        segment_pages = set(range(start, end + 1))
        meta = boundary_meta.get(start, {})
        segments.append({
            "segment_index": len(segments),
            "page_start": start,
            "page_end": end,
            "provisional_document_type": meta.get("type_guess"),
            "provisional_type_label": meta.get("type_label"),
            "confidence": meta.get("confidence"),
            "boundary_evidence": meta.get("evidence"),
            "review_status": "pending",
            # Case C: the segment carries the flag; it is not split at the page.
            "needs_full_page": bool(segment_pages & needs_full_page),
            "orientation_suspect": False,
            "assigned_document_id": None,
        })

    covered_pages = {p for seg in segments for p in range(seg["page_start"], seg["page_end"] + 1)}
    unassigned = sorted(set(range(1, page_count + 1)) - covered_pages)
    if unassigned:
        warnings.append(
            f"{len(unassigned)} page(s) could not be assigned to a document and "
            f"need human review: {unassigned[:20]}{'...' if len(unassigned) > 20 else ''}"
        )

    return {
        "segments": segments,
        "unassigned_pages": unassigned,
        "needs_full_page": sorted(needs_full_page),
        "warnings": warnings,
    }


# ----------------------------------------------------------- validation --

def validate_segments(segments: list[dict], page_count: int) -> list[str]:
    """Checks what JSON Schema structurally cannot: ordering, overlap, bounds.

    Returns error strings (empty means valid) rather than raising, so a caller can
    surface every problem at once instead of one per run. Gaps are NOT an error
    here -- they are legitimate mid-review state, recorded in unassigned_pages --
    but `split` refuses to run while any page is unassigned.
    """
    errors: list[str] = []
    if page_count < 1:
        return [f"page_count must be >= 1, got {page_count}"]

    seen_spans: list[tuple[int, int, int]] = []
    for index, segment in enumerate(segments):
        start = segment.get("page_start")
        end = segment.get("page_end")
        if not isinstance(start, int) or isinstance(start, bool):
            errors.append(f"segment {index}: page_start must be an integer, got {start!r}")
            continue
        if not isinstance(end, int) or isinstance(end, bool):
            errors.append(f"segment {index}: page_end must be an integer, got {end!r}")
            continue
        if start < 1:
            errors.append(f"segment {index}: page_start {start} is below page 1")
        if end > page_count:
            errors.append(f"segment {index}: page_end {end} exceeds the document's {page_count} pages")
        if end < start:
            errors.append(f"segment {index}: page_end {end} precedes page_start {start}")
        else:
            seen_spans.append((start, end, index))

    seen_spans.sort()
    for (start_a, end_a, index_a), (start_b, end_b, index_b) in zip(seen_spans, seen_spans[1:]):
        if start_b <= end_a:
            errors.append(
                f"segments {index_a} and {index_b} overlap: "
                f"{start_a}-{end_a} and {start_b}-{end_b}"
            )
    return errors


# ------------------------------------------------------------- manifest --

def build_manifest_entries(
    segments: list[dict],
    *,
    case_id: str,
    source_file_name: str,
    proposal_path: str,
    start_index: int,
    file_sizes: dict[int, int] | None = None,
) -> list[dict]:
    """Builds document_manifest.json entries for approved segments.

    Numbering continues from start_index rather than reusing the bundle's own id:
    the bundle entry survives as a superseded record, so its id stays taken.

    Sets only fields this stage owns. In particular `document_type` stays null
    even though a provisional guess exists -- checkpoint 1 owns that field and
    must classify against real OCR'd text, not a cropped thumbnail. That
    separation is why `provisional_document_type` is a distinct field rather than
    an early write to the real one.
    """
    entries = []
    for offset, segment in enumerate(segments):
        doc_id = f"DOC_{start_index + offset:03d}"
        file_name = f"{doc_id}.pdf"
        entries.append({
            "document_id": doc_id,
            "file_name": file_name,
            # Forward slashes regardless of host OS: the schema pattern requires
            # them and the value is compared against paths built elsewhere.
            "file_path": f"data/raw/{case_id}/{file_name}",
            "file_format": "pdf",
            "file_size_bytes": (file_sizes or {}).get(segment["page_start"], 0),
            "pre_flagged_type": None,
            "provisional_document_type": segment.get("provisional_document_type"),
            "source_file_name": source_file_name,
            "source_page_start": segment["page_start"],
            "source_page_end": segment["page_end"],
            "segmentation_proposal_path": proposal_path,
            "pages": None,
            "ocr_status": "pending",
            "ocr_text_path": None,
            "ocr_quality": None,
            "uncertain_region_count": None,
            "cross_validation_status": None,
            "redacted_text_path": None,
            "document_type": None,
            "classification_confidence": None,
        })
    return entries


# ------------------------------------------------------- render/compose --

# Pure red: scanned documents contain no saturated red, so separators cannot be
# confused with page content even after the model's own resampling.
SEPARATOR_COLOR = (255, 0, 0)
SHEET_BACKGROUND = (255, 255, 255)
LABEL_TEXT_COLOR = (255, 255, 255)

# 3px reads as antialiasing noise once the sheet is resampled; 4px survives.
DEFAULT_SEPARATOR_PX = 4

# Used only for the full-page fallback, where fidelity genuinely matters. Contact
# sheet cells are rendered straight at cell size via the geometry's zoom, because
# the long-edge cap makes any higher resolution pure waste.
DEFAULT_FALLBACK_DPI = 110


SEGMENT_PROMPT_VERSION = "segment_contact_sheet_v0.1"

# Verified against a real 4x4 sheet (p65-80 of the 110p bundle, 9 rotated cells):
# every rotated cell was read and the p74 boundary found at 0.92 confidence.
#
# Two constraints from llm_providers.py's recorded failures, both load-bearing:
#   * Send this through provider.transcribe_image, which prepends the working
#     "Read the image file at {path} and then:" imperative. The trailing-label
#     form ("Image: {path}") failed 9/9 with "no image was attached".
#   * No self-legitimizing framing -- no "this is a sanctioned step", no "do not
#     refuse". A prior version added that and the child model read it as a
#     prompt-injection signal and refused. A genuine layout question does not
#     argue for itself.
SEGMENT_PROMPT = """This image is a contact sheet: {cell_count} cells in a \
{cols}x{rows} grid, read left to right then top to bottom. Each cell shows the \
top portion of one page from a single scanned PDF. The red number in each cell's \
top-left corner is that page's number in the PDF -- use those numbers in your \
answer rather than counting cell positions.

Some pages were scanned rotated a quarter turn, so their text runs sideways. \
Read those cells at whatever orientation they are in.

{blank_note}The PDF concatenates several separate documents. Identify which \
pages START a new document (a new title block, a different form layout, a \
different letterhead, a page-1-of-N reset) as opposed to continuing the previous \
one. A page that visually continues the document above it is not a boundary.

If a cell's top portion is not enough to judge, list that page in \
needs_full_page rather than guessing.

Reply with ONLY one JSON object:
{{"boundaries": [{{"page": N, "type_guess": "<one of: {types}>", \
"type_label": "<the document's name in its own words>", \
"confidence": 0.0-1.0, "evidence": "<what you saw>"}}],
 "continuations": [N, ...],
 "needs_full_page": [N, ...]}}"""


def build_segment_prompt(sheet_pages: list[int], geometry: dict) -> str:
    """Fills the sheet's actual shape into the prompt.

    The blank-cell note only appears on a short final sheet; stating it on a full
    sheet would invite the model to look for absent cells.
    """
    capacity = geometry["cols"] * geometry["rows"]
    blank_note = ""
    if len(sheet_pages) < capacity:
        blank_note = (
            f"Only the first {len(sheet_pages)} cells contain pages; the rest are "
            f"blank and should be ignored.\n\n"
        )
    return SEGMENT_PROMPT.format(
        cell_count=capacity,
        cols=geometry["cols"],
        rows=geometry["rows"],
        blank_note=blank_note,
        types=", ".join(sorted(DOCUMENT_TYPES)),
    )


def sheets_dir(case_id: str, doc_id: str) -> Path:
    """Stable per-document sheet directory -- deliberately not pid-tagged.

    Sheets are reviewed by a human after the process exits, and a resumed run
    should find the previous run's sheets rather than re-rendering 110 pages.
    """
    return SCRATCH_ROOT / f"{case_id}_{doc_id}"


def geometry_fingerprint(geometry: dict, *, page_count: int) -> str:
    """Identifies the parameters a cached sheet was rendered under.

    Without this, changing --crop-ratio or --grid silently reuses stale PNGs and
    the operator compares two runs that actually saw the same images -- a nasty
    and nearly invisible failure.
    """
    payload = json.dumps({
        "cols": geometry["cols"],
        "rows": geometry["rows"],
        "crop_ratio": geometry["crop_ratio"],
        "separator_px": geometry["separator_px"],
        "cell_w": geometry["cell_w"],
        "cell_h": geometry["cell_h"],
        "page_count": page_count,
        "method_version": METHOD_VERSION,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_label_font(size: int):
    from PIL import ImageFont

    for candidate in ("arial.ttf", "malgun.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def crop_top(image, crop_ratio: float):
    """Keeps the top fraction of a page.

    Titles and form headers sit in the top fifth across this corpus, so a third
    captures the identifying structure while letting four times as many pages
    share one sheet.
    """
    width, height = image.size
    keep = max(1, int(round(height * crop_ratio)))
    return image.crop((0, 0, width, min(keep, height)))


def render_page_images(
    pdf_path: Path,
    pages: list[int],
    *,
    zoom: float,
    rotate: int = 0,
    progress=None,
) -> dict[int, "object"]:
    """Renders the given 1-based pages at exactly the target zoom.

    Rendering directly at cell size skips the intermediate full-resolution bitmap
    entirely. tools/ocr_extract.split_to_page_images is deliberately NOT reused:
    its DPI is hard-coded in both backends and is a quality-affecting constant on
    the P8 OCR path, so parameterizing it would put a preview's convenience ahead
    of OCR's blast radius.

    ``rotate`` turns every page by that many degrees counter-clockwise (use -90
    for clockwise) before it reaches the compositor. Roughly half this corpus is
    scanned a quarter turn over, and which turn is not detectable, so the
    companion sheets are produced by rendering the same pages at -90 and +90 --
    see build_sheet_set. The zoom is adjusted so a rotated page still lands on
    the cell width rather than the cell height.
    """
    import fitz
    from PIL import Image

    # A quarter turn swaps the axes, so to land on cell_w AFTER rotating we have
    # to render to cell_w in the other axis first.
    effective_zoom = zoom
    if rotate % 180 != 0:
        effective_zoom = zoom * (DEFAULT_PAGE_WIDTH_PT / DEFAULT_PAGE_HEIGHT_PT)

    rendered: dict[int, object] = {}
    with fitz.open(pdf_path) as document:
        matrix = fitz.Matrix(effective_zoom, effective_zoom)
        for page_number in pages:
            if page_number < 1 or page_number > document.page_count:
                raise SegmentationError(
                    f"page {page_number} is outside {pdf_path.name}'s "
                    f"{document.page_count} pages"
                )
            pixmap = document[page_number - 1].get_pixmap(matrix=matrix)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            if rotate % 360:
                image = image.rotate(rotate, expand=True)
            rendered[page_number] = image
            if progress:
                progress(f"rendered page {page_number}")
    return rendered


def compose_contact_sheet(page_images: dict[int, "object"], sheet_pages: list[int], geometry: dict):
    """Lays cropped page tops into a grid with red separators and page numbers.

    Three choices worth keeping:

    * Every cell is fully boxed, including at the sheet edge. A cell bounded on
      only two sides is exactly where "is this the same document continuing?"
      ambiguity comes from.
    * Labels carry the ABSOLUTE source page number, so the model never counts
      grid positions to answer -- which is the entire reason the numbers exist.
    * A short final sheet keeps full canvas size with blank, unlabelled cells.
      Shrinking it would change the geometry between sheets and break the model's
      spatial expectation; repeating pages to fill would manufacture phantom
      boundaries.
    """
    from PIL import Image, ImageDraw

    cols, rows = geometry["cols"], geometry["rows"]
    cell_w, cell_h = geometry["cell_w"], geometry["cell_h"]
    sep = geometry["separator_px"]

    sheet = Image.new("RGB", (geometry["sheet_w"], geometry["sheet_h"]), SEPARATOR_COLOR)
    draw = ImageDraw.Draw(sheet)
    font = _load_label_font(max(11, cell_h // 14))
    flags = {"blank_pages": []}

    for index in range(cols * rows):
        col, row = index % cols, index // cols
        x = sep + col * (cell_w + sep)
        y = sep + row * (cell_h + sep)

        if index >= len(sheet_pages):
            # Blank filler: white, unboxed, unlabelled -- visually unambiguous.
            draw.rectangle([x, y, x + cell_w - 1, y + cell_h - 1], fill=SHEET_BACKGROUND)
            continue

        page_number = sheet_pages[index]
        source = page_images[page_number]
        cropped = crop_top(source, geometry["crop_ratio"])
        if cropped.size != (cell_w, cell_h):
            cropped = cropped.resize((cell_w, cell_h), Image.LANCZOS)
        if not cropped.convert("L").point(lambda v: 255 if v < 200 else 0).getbbox():
            flags["blank_pages"].append(page_number)

        sheet.paste(cropped, (x, y))

        label = f"p{page_number}"
        text_box = draw.textbbox((0, 0), label, font=font)
        chip_w = text_box[2] - text_box[0] + 10
        chip_h = text_box[3] - text_box[1] + 8
        draw.rectangle([x, y, x + chip_w, y + chip_h], fill=SEPARATOR_COLOR)
        draw.text((x + 5, y + 3), label, fill=LABEL_TEXT_COLOR, font=font)

    return sheet, flags


# Rendered for every sheet: the scanned orientation plus both quarter turns.
# Roughly half this corpus is scanned sideways and which way is NOT detectable --
# an attempt at it picked the wrong direction on 3 of 9 known-rotated pages, and
# upright control pages gave no usable baseline. Rather than guess, produce all
# three and let whoever is reading (human or model) use the legible one.
SHEET_VARIANTS = (("as_scanned", 0), ("cw", -90), ("ccw", 90))


def build_sheet_set(
    pdf_path: Path,
    out_dir: Path,
    *,
    geometry: dict | None = None,
    page_count: int | None = None,
    variants=SHEET_VARIANTS,
    progress=None,
) -> dict:
    """Renders every contact sheet in each orientation variant.

    A sideways page's top crop shows a table's left edge instead of its title, so
    a single as-scanned sheet leaves those pages unreadable with no recourse. The
    companion turns cost only render time -- no extra model calls, since the
    proposal path sends one variant per sheet.

    Returns ``{variant: [paths]}`` plus the geometry used.
    """
    import fitz

    geometry = geometry or compute_sheet_geometry()
    out_dir.mkdir(parents=True, exist_ok=True)

    if page_count is None:
        with fitz.open(pdf_path) as document:
            page_count = document.page_count

    batches = plan_sheets(page_count, geometry["pages_per_sheet"])
    produced: dict[str, list[Path]] = {}

    for variant, angle in variants:
        paths = []
        for index, pages in enumerate(batches):
            images = render_page_images(pdf_path, pages, zoom=geometry["zoom"], rotate=angle)
            sheet, _ = compose_contact_sheet(images, pages, geometry)
            path = out_dir / (
                f"sheet_{index:02d}_p{pages[0]:03d}-{pages[-1]:03d}_{variant}.png"
            )
            sheet.save(path)
            paths.append(path)
            if progress:
                progress(f"{variant} sheet {index} (p{pages[0]}-{pages[-1]})")
        produced[variant] = paths

    return {"geometry": geometry, "page_count": page_count, "sheets": produced}


# ------------------------------------------------------- provider path --

# The sheet variant actually SENT to the model. The companion turns exist for a
# human reading unreadable cells; the model is told to read sideways cells in
# place (SEGMENT_PROMPT), so it gets one variant per sheet. as_scanned is the
# honest default -- it is what the page really is, and rotating first would
# force a guess at which way, the exact guess build_sheet_set refuses to make.
PROPOSAL_VARIANT = "as_scanned"

# A page needing a full-page look is expected operational noise on a 100%-scan
# corpus. But if too many pages need it, the fallback stops being a cheap
# second look and becomes the main cost -- at which point the crop ratio or
# grid is simply wrong for this bundle and a human should retune, not pay to
# paper over it. Default cap: a quarter of the bundle. Mirrors the plan's
# saturation policy and the schema's full_page_fallback.saturated field.
DEFAULT_FALLBACK_CAP_RATIO = 0.25


def _resume_dir(case_id: str, doc_id: str) -> Path:
    """Per-sheet response cache, stable (NOT pid-tagged) so a re-run reuses it.

    One JSON per sheet holding the parsed result plus the raw response and
    provider metadata. Mirrors ocr_extract._resume_cache_dir, which came out of
    a real 75-page loss: an interrupted propose run must not re-pay for sheets
    it already called. Kept separate from the sheet-image dir so clearing one
    never clears the other.
    """
    return SCRATCH_ROOT / "_resume" / f"{case_id}_{doc_id}"


def _load_cached_sheet(cache_dir: Path, sheet_index: int) -> dict | None:
    path = cache_dir / f"sheet_{sheet_index:02d}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A half-written or corrupt cache entry re-calls that sheet rather than
        # being trusted -- the same fail-open ocr_extract uses.
        return None


def _save_cached_sheet(cache_dir: Path, sheet_index: int, payload: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Atomic: an interrupt mid-write leaves the old entry (or none), never a
    # half-sheet a later resume would trust.
    tmp = cache_dir / f"sheet_{sheet_index:02d}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_dir / f"sheet_{sheet_index:02d}.json")


def propose_boundaries(
    pdf_path: Path,
    *,
    case_id: str,
    doc_id: str,
    provider,
    geometry: dict | None = None,
    page_count: int | None = None,
    sheet_paths: list[Path] | None = None,
    fallback_cap_ratio: float = DEFAULT_FALLBACK_CAP_RATIO,
    resume: bool = True,
    progress=None,
) -> dict:
    """Runs one vision call per contact sheet and merges the responses.

    Provider-injected and returns a plain dict -- no sys.exit, no provider
    construction here -- so a test drives it with FixtureProvider and the CLI
    wrapper (or a future orchestrator) owns process exit. This mirrors
    run_checkpoint1.run_checkpoint1's contract deliberately.

    Every vision call goes through provider.transcribe_image, which prepends the
    "Read the image file at {path} and then:" imperative the recorded 9/9 label
    failure requires. One sheet's parse failure never discards the others (their
    calls are already paid for): a failed sheet's pages fall through to
    unassigned_pages via merge_sheet_proposals.

    Returns ``{segments, unassigned_pages, needs_full_page, warnings, method,
    contact_sheets, per_sheet}`` -- enough for build_proposal_document to
    assemble a schema-valid proposal without re-deriving anything.
    """
    import fitz

    geometry = geometry or compute_sheet_geometry()
    if page_count is None:
        with fitz.open(pdf_path) as document:
            page_count = document.page_count

    batches = plan_sheets(page_count, geometry["pages_per_sheet"])
    if sheet_paths is not None and len(sheet_paths) != len(batches):
        raise SegmentationError(
            f"got {len(sheet_paths)} sheet paths for {len(batches)} planned sheets"
        )

    cache_dir = _resume_dir(case_id, doc_id)
    fingerprint = geometry_fingerprint(geometry, page_count=page_count)

    per_sheet: list[dict] = []
    contact_sheets: list[dict] = []
    provider_metadata: dict | None = None

    for index, pages in enumerate(batches):
        sheet_path = sheet_paths[index] if sheet_paths else None
        cached = _load_cached_sheet(cache_dir, index) if resume else None
        # Invalidate a cache entry rendered under a different geometry: reusing
        # it would compare a run against sheets it never actually saw -- the
        # nasty, near-invisible bug geometry_fingerprint exists to prevent.
        if cached is not None and cached.get("fingerprint") != fingerprint:
            cached = None

        if cached is not None:
            parsed = cached["parsed"]
            if provider_metadata is None:
                provider_metadata = cached.get("provider_metadata")
            if progress:
                progress(f"sheet {index} (p{pages[0]}-{pages[-1]}) (cached)")
        else:
            prompt = build_segment_prompt(pages, geometry)
            result = provider.transcribe_image(
                Path(sheet_path), prompt, SEGMENT_PROMPT_VERSION
            )
            parsed = parse_segmentation_response(result.text, pages)
            provider_metadata = result.metadata()
            if resume:
                _save_cached_sheet(cache_dir, index, {
                    "fingerprint": fingerprint,
                    "parsed": parsed,
                    "raw_response": result.text,
                    "provider_metadata": provider_metadata,
                })
            if progress:
                status = "ok" if parsed.get("ok") else "parse-failed"
                progress(f"sheet {index} (p{pages[0]}-{pages[-1]}) [{status}]")

        per_sheet.append(parsed)
        contact_sheets.append({
            "sheet_index": index,
            "path": str(sheet_path) if sheet_path else "",
            "page_start": pages[0],
            "page_end": pages[-1],
            "page_numbers": list(pages),
        })

    merged = merge_sheet_proposals(per_sheet, page_count, sheet_pages=batches)

    fallback = _plan_fallback(
        merged["needs_full_page"], page_count, fallback_cap_ratio
    )
    if fallback["saturated"]:
        merged["warnings"].append(
            f"full-page fallback saturated: {len(fallback['pages'])} page(s) "
            f"needed a full-page look but the cap is {fallback['cap']}; the crop "
            f"ratio or grid is likely wrong for this bundle -- retune rather than "
            f"spend the extra calls. Fallback skipped; these pages stay flagged."
        )

    method = {
        "ocr_performed": False,
        "method_version": METHOD_VERSION,
        "mode": "vision_proposal",
        "provider_name": getattr(provider, "provider_name", None),
        "model_name": getattr(provider, "model_name", None),
        "prompt_version": SEGMENT_PROMPT_VERSION,
        "provider_metadata": provider_metadata,
        "render_dpi": DEFAULT_FALLBACK_DPI,
        "crop_ratio": geometry["crop_ratio"],
        "grid_cols": geometry["cols"],
        "grid_rows": geometry["rows"],
        "sheet_pixel_budget": {
            "long_edge": max(geometry["sheet_w"], geometry["sheet_h"]),
            "total_pixels": geometry["total_pixels"],
        },
        "contact_sheets": contact_sheets,
        "full_page_fallback": fallback,
    }

    return {
        "segments": merged["segments"],
        "unassigned_pages": merged["unassigned_pages"],
        "needs_full_page": merged["needs_full_page"],
        "warnings": merged["warnings"],
        "method": method,
        "contact_sheets": contact_sheets,
        "per_sheet": per_sheet,
    }


def _plan_fallback(needs_full_page: list[int], page_count: int, cap_ratio: float) -> dict:
    """Decides whether the full-page second look runs, and records the decision.

    The plan's step F: a run needing more full-page looks than the cap allows is
    a tuning signal, not a spend problem, so the whole fallback is SKIPPED and
    the pages stay flagged for a human. This returns the schema's
    full_page_fallback shape either way -- the artifact records what happened,
    which is diagnosable later without re-running.

    NOTE (step 5 scope): the actual second vision pass is not wired yet. This
    plans and gates it -- the pages that WOULD be re-checked, whether the run is
    saturated, the cap -- so the proposal honestly records the decision. Running
    the batched re-render + re-call lands with the split step (step 6/7), where
    a real E2E run first shows how often it even fires.
    """
    pages = sorted(set(needs_full_page))
    cap = int(page_count * cap_ratio)
    saturated = len(pages) > cap
    return {
        # Not triggered in step 5: planned-and-gated only, never executed yet.
        "triggered": False,
        "pages": pages,
        "saturated": saturated,
        "cap": cap,
    }


def build_proposal_document(
    proposal: dict,
    *,
    case_id: str,
    source_document_id: str,
    source_file_name: str,
    source_file_path: str,
    page_count: int,
    created_at: str,
    updated_at: str | None = None,
) -> dict:
    """Assembles a segmentation_proposal.schema.json instance from a
    propose_boundaries result.

    Kept separate from propose_boundaries so the assembly is a pure, testable
    dict transform -- and so a manual-mode skeleton (no provider) can build the
    same envelope with an empty segment list down the road. review_status starts
    pending on both the proposal and every segment: only a human advances them,
    and split refuses until they do.
    """
    return {
        "case_id": case_id,
        "source_document_id": source_document_id,
        "source_file_name": source_file_name,
        "source_file_path": source_file_path,
        "source_page_count": page_count,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "review_status": "pending",
        "reviewed_by": None,
        "reviewed_at": None,
        "rejection_reason": None,
        "method": proposal["method"],
        "segments": proposal["segments"],
        "unassigned_pages": proposal["unassigned_pages"],
        "warnings": proposal["warnings"],
    }


# ------------------------------------------------------- approve/split --

# What split refuses to proceed on. A proposal is ready only when a human moved
# the case-level gate to approved AND every segment is approved or edited AND no
# page is unassigned AND the ranges pass validate_segments. Any one failing
# halts the whole thing -- the same all-or-nothing gate intake_case uses, for
# the same reason: a single unreviewed boundary means a human has not actually
# seen the split they would be authorizing.
_SPLIT_READY_SEGMENT_STATES = {"approved", "edited"}


def apply_approval(
    proposal: dict,
    *,
    reviewer: str,
    now: str,
    segment_index: int | None = None,
    edit: tuple[int, int] | None = None,
) -> dict:
    """Returns a copy of the proposal with an approval applied. Pure -- no I/O.

    Three shapes, matching the CLI:
      * segment_index None            -> approve the CASE-level gate and every
                                          still-pending segment in one stroke.
      * segment_index set, edit None  -> approve just that segment as proposed.
      * segment_index set, edit set   -> change that segment's range to
                                          (start, end) and mark it 'edited', so
                                          the record shows the model was corrected.

    Editing a range does not re-run validate_segments here; split does that on
    the whole set before it touches anything, which is where an edit that
    introduced an overlap or a reversed range must be caught.
    """
    import copy

    updated = copy.deepcopy(proposal)
    segments = updated.get("segments", [])

    if segment_index is not None:
        if segment_index < 0 or segment_index >= len(segments):
            raise SegmentationError(
                f"segment_index {segment_index} out of range (0..{len(segments) - 1})"
            )
        seg = segments[segment_index]
        if edit is not None:
            start, end = edit
            seg["page_start"] = start
            seg["page_end"] = end
            seg["review_status"] = "edited"
        else:
            seg["review_status"] = "approved"
    else:
        # Case-level approval: advance the gate and sweep up pending segments.
        # A segment already 'edited' or 'rejected' keeps its state -- this only
        # promotes the ones a reviewer left pending, so a bulk approve never
        # silently un-rejects something.
        updated["review_status"] = "approved"
        updated["reviewed_by"] = reviewer
        updated["reviewed_at"] = now
        for seg in segments:
            if seg.get("review_status") == "pending":
                seg["review_status"] = "approved"

    updated["updated_at"] = now
    return updated


def split_readiness_errors(proposal: dict) -> list[str]:
    """Every reason this proposal is not ready to split (empty means ready).

    Reports all problems at once rather than one per run, like
    validate_segments -- a reviewer fixing a proposal wants the whole list, not
    a fix-one-rerun-find-the-next loop.
    """
    errors: list[str] = []
    if proposal.get("review_status") != "approved":
        errors.append(
            f"case-level review_status is {proposal.get('review_status')!r}, not 'approved'"
        )
    unassigned = proposal.get("unassigned_pages") or []
    if unassigned:
        errors.append(
            f"{len(unassigned)} page(s) are still unassigned and must be resolved "
            f"before splitting: {unassigned[:20]}{'...' if len(unassigned) > 20 else ''}"
        )
    segments = proposal.get("segments", [])
    if not segments:
        errors.append("proposal has no segments to split")
    for index, seg in enumerate(segments):
        state = seg.get("review_status")
        if state == "rejected":
            errors.append(f"segment {index} (p{seg.get('page_start')}-{seg.get('page_end')}) was rejected")
        elif state not in _SPLIT_READY_SEGMENT_STATES:
            errors.append(
                f"segment {index} (p{seg.get('page_start')}-{seg.get('page_end')}) is "
                f"{state!r}, not approved/edited"
            )
    page_count = proposal.get("source_page_count")
    if isinstance(page_count, int):
        errors.extend(validate_segments(segments, page_count))
    return errors


def _next_document_index(manifest: dict) -> int:
    """One past the highest DOC_NNN already in the manifest.

    The bundle's own id is never reused -- it survives as a superseded record,
    so its number stays taken and new documents number strictly after every
    existing one.
    """
    highest = 0
    for doc in manifest.get("documents", []):
        doc_id = doc.get("document_id", "")
        if doc_id.startswith("DOC_"):
            try:
                highest = max(highest, int(doc_id[4:]))
            except ValueError:
                continue
    return highest + 1


def split_bundle(
    proposal: dict,
    *,
    case_id: str,
    bundle_id: str,
    bundle_pdf_path: Path,
    proposal_path: str,
    manifest: dict,
    held_by: str,
    run_id: str,
    dao=None,
    progress=None,
) -> dict:
    """Splits an approved bundle into per-document PDFs and updates the manifest.

    Returns a status dict (no sys.exit, dao injected) -- run_checkpoint1's
    contract. Order matters for recoverability: the new documents list is built
    fully in memory, every child PDF is written to data/raw/, and only then is
    the manifest updated in ONE call. A half-written manifest is unrecoverable
    (an entry pointing at a file that does not exist), whereas an orphan
    DOC_XXX.pdf with no manifest entry is harmless and re-runnable -- so the
    manifest write is last and atomic.

    Idempotent: if the manifest already holds an entry with this bundle's
    source_file_name and one of the proposal's page ranges, the split already
    ran and this reports already_split without rewriting anything.

    Guardrail note: this WRITES to data/raw/. That is not a P-rule violation --
    source-cases/ is the immutable raw material; data/raw/ is intake's own
    output tree, and segmentation is part of intake, so it is a legitimate
    writer here. It only ever CREATES new DOC_XXX.pdf files; it never modifies
    the bundle PDF or any existing data/raw/ file.
    """
    import fitz

    if dao is None:
        import dao as dao  # noqa: PLW0127  (inject in tests; default to the real DAO)

    errors = split_readiness_errors(proposal)
    if errors:
        return {"status": "not_ready", "errors": errors}

    segments = proposal["segments"]
    source_file_name = proposal["source_file_name"]

    # Idempotency: a prior split leaves per-document entries carrying this
    # bundle's source_file_name. If any already match a proposed range, treat
    # the whole split as done rather than minting duplicate DOC_XXX.pdf files.
    proposed_ranges = {(s["page_start"], s["page_end"]) for s in segments}
    for doc in manifest.get("documents", []):
        if (doc.get("source_file_name") == source_file_name
                and (doc.get("source_page_start"), doc.get("source_page_end")) in proposed_ranges):
            return {"status": "already_split",
                    "message": f"{source_file_name} already has split entries in the manifest"}

    start_index = _next_document_index(manifest)
    raw_dir = ROOT / "data" / "raw" / case_id
    raw_dir.mkdir(parents=True, exist_ok=True)

    new_documents: list[dict] = []
    written_paths: list[Path] = []
    file_sizes: dict[int, int] = {}

    with fitz.open(bundle_pdf_path) as source:
        for offset, seg in enumerate(segments):
            doc_id = f"DOC_{start_index + offset:03d}"
            out_path = raw_dir / f"{doc_id}.pdf"
            with fitz.open() as out:
                # insert_pdf is 0-based inclusive; segments are 1-based inclusive.
                out.insert_pdf(source, from_page=seg["page_start"] - 1, to_page=seg["page_end"] - 1)
                out.save(out_path)
            written_paths.append(out_path)
            file_sizes[seg["page_start"]] = out_path.stat().st_size  # size AFTER save
            if progress:
                progress(f"wrote {doc_id}.pdf (p{seg['page_start']}-{seg['page_end']})")

    new_documents = build_manifest_entries(
        segments,
        case_id=case_id,
        source_file_name=source_file_name,
        proposal_path=proposal_path,
        start_index=start_index,
        file_sizes=file_sizes,
    )

    # Mark the bundle superseded rather than deleting it: deleting orphans the
    # _intake_record.json crosswalk and _source_ledger.json references and drops
    # the immutable-source -> logical-document audit trail. The schema requires
    # ocr_status not_applicable and a null redacted_text_path on a superseded
    # bundle, and a segmentation_proposal_path pointing at what superseded it.
    bundle_fields = {
        "downstream_disposition": "superseded_bundle",
        "ocr_status": "not_applicable",
        "redacted_text_path": None,
        "segmentation_proposal_path": proposal_path,
    }

    ok, message = dao.replace_manifest_documents(
        case_id, bundle_id, bundle_fields, new_documents, held_by, run_id,
        purpose=f"split {source_file_name} into {len(new_documents)} document(s)",
    )
    if not ok:
        # The child PDFs are on disk but the manifest did not update. They are
        # orphans -- harmless and overwritten on a clean re-run (same doc ids,
        # since start_index is recomputed from the unchanged manifest) -- so we
        # leave them rather than deleting work a retry can reuse. The caller
        # halts on a non-ok status; nothing downstream trusts these until the
        # manifest names them.
        return {"status": "manifest_write_failed", "message": message,
                "orphan_pdfs": [str(p) for p in written_paths]}

    return {
        "status": "split",
        "message": message,
        "new_document_ids": [d["document_id"] for d in new_documents],
        "new_pdf_paths": [str(p) for p in written_paths],
    }
