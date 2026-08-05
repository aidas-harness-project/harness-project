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

This module implements the complete Stage 1 tool flow: contact-sheet rendering,
provider-backed boundary proposal, targeted full-page fallback, optional
long-segment refinement, human approval, and approved PDF splitting. The
orchestrator remains responsible for invoking these commands in order and for
waiting at the human gate.

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

import copy
import hashlib
import json
import re
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
    "insurance_certificate", "insurance_policy", "application_form",
    "diagnosis_certificate", "medical_record", "imaging_report", "receipt",
    "insurer_response", "other",
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


def parse_segmentation_response(
    raw: str,
    sheet_pages: list[int],
    *,
    require_output_contract: bool = False,
) -> dict:
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
    if require_output_contract:
        required = ("boundaries", "continuations", "needs_full_page")
        missing = [field for field in required if field not in parsed]
        if missing:
            return failed(
                "structured output omitted required field(s): "
                + ", ".join(missing)
            )

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


# ------------------------------------------------ text-layer boundary veto --

# A page whose first non-empty line matches CONTINUATION_START_RE is grammatically
# mid-document: an enumerated item ("11.", "가."), or a connective that cannot open
# a document ("그러나", "다만"). Korean policy bundles number every clause item, so
# these are high-precision. Deliberately NOT a general "does this look like a
# title" heuristic -- the veto only ever REMOVES a boundary the model proposed,
# and only on positive evidence that the page continues the previous one.
CONTINUATION_START_RE = re.compile(
    r"^(?:"
    r"\d+[.)]"            # "11." / "11)" -- clause item numbering
    r"|[가-힣][.)]\s"      # "가." / "나)" -- sub-item numbering
    r"|그러나|다만|또한|이 경우|위 제"   # connectives; cannot begin a document
    r")"
)


def continuation_pages_from_text_layer(pdf_path, page_count: int) -> set[int]:
    """Pages whose own embedded text proves they continue the previous page.

    Returns an empty set for a scanned bundle (no text layer) -- the veto simply
    does not apply there and the vision verdict stands unmodified.

    Why this exists: `merge_sheet_proposals` builds segments from the `boundaries`
    array alone and never checks a boundary claim against the page it describes.
    On CASE_112 that produced 16 segments starting mid-clause (DOC_003 p20/22/24/
    25/49/50/51/60/114/128/129/141, DOC_004 p161/162/174/175), 12 of which carried
    a `provisional_type_label` that literally read "continuation" while still being
    emitted as a boundary -- known-gaps.md item 34. Downstream, a segment starting
    mid-clause has no `제N조(` anchor for the policy normalizer to bind to, which
    is why DOC_039 (p49) and DOC_096 (p128) produced zero clauses.

    This is deterministic and costs no model call: the born-digital policy PDFs
    carry the publisher's own text (see ocr_extract.pdf_embedded_page_texts)."""
    try:
        import fitz
    except ImportError:
        return set()
    pages: set[int] = set()
    try:
        with fitz.open(str(pdf_path)) as document:
            limit = min(page_count, document.page_count)
            for index in range(limit):
                lines = [
                    line.strip()
                    for line in document[index].get_text().splitlines()
                    if line.strip()
                ]
                if not lines:
                    continue
                if CONTINUATION_START_RE.match(lines[0]):
                    pages.add(index + 1)
    except Exception:
        # An unreadable/encrypted PDF yields no evidence, so no veto. The vision
        # proposal stands rather than this masking a PDF-level error.
        return set()
    return pages


# ------------------------------------------- text-anchor boundary detection --

# A Korean policy bundle's document starts are announced by the publisher's own
# title line: "...보통약관", "...특별약관", "...추가특별약관", "...특약", optionally
# with a scope parenthetical ("(시설소유(관리)자 특별약관에 적용)") or an ordinal
# suffix ("주위재산 추가특별약관2", "창고업자 특별약관(Ⅰ)"). Anchored to end-of-line
# so a mid-sentence mention of the word 약관 cannot match.
DOCUMENT_TITLE_RE = re.compile(
    r"(보통약관|특별약관|특약)\s*(?:\([^)]*\))?\s*[0-9IVXⅠⅡⅢⅣ]*\s*$"
)

# A TOC page is recognised by density, not by a keyword: at least this many lines
# and at least this share of them bare titles/headings.
_TOC_MIN_LINES = 5
_TOC_TITLE_SHARE = 0.6

# The longest a line can be and still be a contents ENTRY rather than a body
# sentence that happens to open with a title or a 제N조 heading. The longest real
# title in CASE_112's two bundles is 38 chars ("티끌, 먼지 및 소음 특별약관(배상청구기준)");
# the body sentences this separates them from run 150-400. Anywhere in that gap
# behaves identically, so the exact value is not delicate.
_TOC_MAX_ENTRY_CHARS = 60

# "제3조", "제2관" -- an ARTICLE/SECTION heading, not a document start. Used only
# to recognise a table of contents; deliberately NOT used as a boundary signal.
# Measured on CASE_112: admitting these as boundaries drops precision 1.00 -> 0.91
# (21 false positives, e.g. every "제N조(준용규정)" that opens a page mid-document).
_ARTICLE_HEADING_RE = re.compile(r"^제\s*\d+\s*(?:관|조)")


# A running header/footer the publisher prints on every page (a slogan, a call
# centre number, a URL). It is page furniture, not content: on CASE_112 DOC_004
# it appears on 25 of the 28 short pages, sitting under body text, article
# headings and bare titles alike, so its presence says nothing about what kind
# of page it is on. Matched on the whole line -- a line that merely CONTAINS a
# company name is ordinary content ("회사는 삼성화재에 통지합니다").
_PAGE_FURNITURE_RE = re.compile(
    r"^(?:당신에게\s*좋은보험\s*삼성화재"
    r"|(?:https?://)?www\.[\w.\-]+"
    r"|(?:고객센터|고객상담실)\s*[\d\-\s]+"
    r"|\d{4}-\d{4})$"
)


def _page_content_lines(lines: list[str]) -> list[str]:
    """A page's lines with running headers/footers removed.

    Why this exists: the same physical page yields different line counts
    depending on who read it. CASE_112 DOC_004 p6's embedded text layer holds
    only the title, while OCR of that page also captures the publisher's
    running footer. `_is_toc_page`'s short-page rule requires EVERY line to be
    a title, so that one extra line flipped the verdict and moved a boundary --
    the single disagreement in 323 pages between the two readings.

    A page consisting only of furniture is returned unchanged: callers treat an
    empty page as evidence the bundle is not fully born-digital, and inventing
    that verdict from a footer-only page would reject a whole bundle."""
    kept = [line for line in lines if not _PAGE_FURNITURE_RE.match(line)]
    return kept if kept else lines


def _is_toc_page(lines: list[str]) -> bool:
    """A table-of-contents page: almost every line is a bare title or heading.

    A TOC lists the very titles this module keys on, so without this a 6-page TOC
    would emit ~60 spurious boundaries. Body pages fail the test because their
    lines are sentences, not bare titles.

    "Bare" is enforced by length, and a contents page must carry no body prose
    at all. Both guards exist because line COUNT is a property of the reader,
    not of the page: CASE_112 DOC_004 p31 is 20 short lines in the embedded text
    layer but 9 once OCR rejoins each wrapped sentence. Under a pure ratio that
    page -- four `제N조` headings, each followed by its own clause -- scored
    exactly at the 0.6 threshold and was read as contents, swallowing a real
    boundary the human baseline also cut. What actually separates the two is not
    a ratio: a contents ENTRY is short by construction and is never followed by
    the text it points at, whereas a body page pairs each heading with a
    sentence. So a page holding even one prose line is body, however many bare
    headings sit above it."""
    if not lines:
        return False
    titles = sum(1 for line in lines
                 if len(line) <= _TOC_MAX_ENTRY_CHARS and DOCUMENT_TITLE_RE.search(line))
    headings = sum(1 for line in lines
                   if len(line) <= _TOC_MAX_ENTRY_CHARS and _ARTICLE_HEADING_RE.match(line))
    if any(len(line) > _TOC_MAX_ENTRY_CHARS for line in lines):
        return False
    if len(lines) >= _TOC_MIN_LINES:
        return (titles + headings) >= max(
            _TOC_MIN_LINES, int(len(lines) * _TOC_TITLE_SHARE)
        )
    # A short tail page (e.g. one leftover title) counts only if it CONTINUES a
    # TOC run -- the caller enforces that, since it needs the neighbours.
    return titles == len(lines)


def text_anchor_boundaries(pdf_path, page_count: int) -> dict[int, str | None] | None:
    """Document-start pages derived from the PDF's own text layer. No model call.

    Returns None when the bundle has no usable text layer (a real scan), which is
    the caller's signal to fall back to the vision proposal path. Otherwise
    returns {boundary_page: the title line that marked it}, always including page
    1 (whose value is None -- it starts a document by position, not by a title).

    Why this is preferred over the vision path when a text layer exists: the
    boundaries come from the publisher's own typed title lines rather than from a
    model reading a downscaled 4x4 contact-sheet crop. Measured against the
    human-approved CASE_112 baseline (DOC_003 145p, DOC_004 178p):

        precision 1.0000 (0 false positives across 173 predicted boundaries)
        recall    0.8317 vs the human baseline

    and the recall gap is not error: 27 of the 35 "missed" pages are pages the
    HUMAN baseline cut mid-clause (first line is an enumerated item or a
    connective), i.e. over-splits this method correctly declines to make. On the
    metrics that decide whether Stage 4 can normalize a clause, it beats the
    human baseline outright -- segments with no 제N조 anchor 11->6 (DOC_003) and
    13->8 (DOC_004), segments starting mid-clause 19->0 and 8->0.

    Article headings are deliberately excluded as boundary signals; see
    _ARTICLE_HEADING_RE."""
    try:
        import fitz
    except ImportError:
        return None
    try:
        with fitz.open(str(pdf_path)) as document:
            limit = min(page_count, document.page_count)
            if limit < 1:
                return None
            pages = [
                _page_content_lines([
                    line.strip()
                    for line in document[index].get_text().splitlines()
                    if line.strip()
                ])
                for index in range(limit)
            ]
    except Exception:
        # Unreadable/encrypted: no evidence, so no deterministic claim. The
        # caller falls back to vision rather than this masking a PDF-level error.
        return None

    # A bundle with any empty-text page is not fully born-digital; mixing a
    # deterministic verdict with pages we cannot read would produce boundaries
    # that are silently blind to part of the document.
    if not all(pages):
        return None

    raw_toc = [_is_toc_page(lines) for lines in pages]
    toc: list[bool] = []
    for index, flagged in enumerate(raw_toc):
        if flagged and len(pages[index]) < _TOC_MIN_LINES:
            # A short all-title page counts as contents only if it CONTINUES a
            # confirmed run. Chained against the already-resolved neighbour, not
            # the raw flag: otherwise a sequence of one-line title pages -- which
            # is what a dense run of short 특별약관 documents looks like -- would
            # bootstrap itself into a fake contents block and swallow real
            # boundaries.
            flagged = index > 0 and toc[index - 1]
        toc.append(flagged)

    boundaries: dict[int, str | None] = {1: None}
    for index, lines in enumerate(pages):
        page = index + 1
        if page == 1 or toc[index]:
            continue
        if toc[index - 1]:
            # First real page after the contents block always starts a document.
            boundaries[page] = lines[0]
        elif DOCUMENT_TITLE_RE.search(lines[0]):
            boundaries[page] = lines[0]
    return boundaries


def segments_from_boundaries(
    boundaries: dict[int, str | None] | set[int], page_count: int
) -> list[dict]:
    """Schema-shaped segment records for a deterministic boundary set.

    Accepts either the {page: title_line} mapping text_anchor_boundaries returns
    or a bare page set (titles then unknown)."""
    titles = boundaries if isinstance(boundaries, dict) else {}
    ordered = sorted(boundaries)
    segments = []
    for position, start in enumerate(ordered):
        end = ordered[position + 1] - 1 if position + 1 < len(ordered) else page_count
        title = titles.get(start)
        segments.append({
            "segment_index": position,
            "page_start": start,
            "page_end": end,
            # Type stays null on purpose: the title line is the publisher's own
            # words, but mapping it onto the document_type enum is a judgement
            # this stage does not make (P: provisional guesses are never trusted
            # downstream). The label is recorded verbatim for the human gate.
            "provisional_document_type": None,
            "provisional_type_label": title,
            "confidence": None,
            "boundary_evidence": (
                "page 1 of the bundle"
                if start == 1
                else f"embedded text layer: page begins with the title line {title!r}"
            ),
            "review_status": "pending",
            "needs_full_page": False,
            "orientation_suspect": False,
            "assigned_document_id": None,
        })
    return segments


# ---------------------------------------------------------------- merge --

def merge_sheet_proposals(
    per_sheet: list[dict],
    page_count: int,
    *,
    sheet_pages: list[list[int]] | None = None,
    text_layer_continuations: set[int] | None = None,
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

    `text_layer_continuations` (optional) narrows case D with real evidence: a
    page whose own embedded text begins mid-clause is not a document start, so a
    boundary claimed there is dropped and the page becomes interior to the
    preceding document. This is the only input that can REMOVE a boundary, it
    never adds one, and it applies solely where the page's own text proves the
    case -- so the asymmetry argument above is unchanged for every page without
    such evidence. Page 1 is never vetoed (case A: a bundle's first page begins
    something by definition, whatever its text looks like).
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

    # Text-layer veto: drop boundaries the page's own text contradicts. Applied
    # before case A so a vetoed page cannot be mistaken for the first boundary.
    # p1 is exempt -- a bundle's first page starts a document regardless.
    vetoed: list[int] = []
    if text_layer_continuations:
        for page in sorted(set(text_layer_continuations) & set(boundary_meta)):
            if page == 1:
                continue
            del boundary_meta[page]
            vetoed.append(page)
            # The page stays 'mentioned' -- it is real content, now interior to
            # the preceding document rather than the start of a new one.
            mentioned.add(page)

    boundaries = sorted(boundary_meta)
    if vetoed:
        warnings.append(
            f"{len(vetoed)} proposed boundary page(s) were dropped because the "
            f"page's own embedded text begins mid-document (an enumerated item or "
            f"a connective, so it continues the previous page): {vetoed[:20]}"
            f"{'...' if len(vetoed) > 20 else ''}"
        )
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

    # A page some sheet actually named (boundary or continuation) and that no
    # failed sheet lost.
    assignable = {
        page for page in range(1, page_count + 1)
        if page in mentioned and page not in failed_pages
    }

    # Continuation-absorption: a page BETWEEN two boundaries that no sheet named
    # is nonetheless interior to the earlier boundary's document, UNLESS a failed
    # sheet was responsible for it. This closes the sheet-boundary gap that made
    # CASE_026's p33-41 unassigned: the model correctly read p33 as continuing
    # the p28 문서 but forgot to list it in `continuations` at the sheet edge, so
    # merge tore a 14-page document apart. The load-bearing idea already is
    # "boundaries alone make segments, sheet edges mean nothing" -- so a
    # non-boundary page strictly inside a document IS part of it, whether or not
    # the model happened to enumerate it. Absorbed pages are still surfaced as a
    # warning (not silently swallowed), preserving case B's visibility: a human
    # sees exactly which pages were inferred rather than stated. A page after the
    # LAST boundary that no sheet named stays unassigned -- there is no enclosing
    # document to absorb it into, so the model genuinely dropped it.
    boundary_set = set(boundaries)
    absorbed: list[int] = []
    if boundaries:
        last_boundary = boundaries[-1]
        for page in range(1, page_count + 1):
            if page in assignable or page in failed_pages:
                continue
            if page in boundary_set:
                continue
            # Strictly inside the span of some document: after the first boundary
            # and not past the final one (the final segment's tail is open, so a
            # gap there is a real drop, not an interior page).
            if boundaries[0] < page <= last_boundary:
                assignable.add(page)
                absorbed.append(page)

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

    if absorbed:
        warnings.append(
            f"{len(absorbed)} page(s) no sheet named were absorbed into the "
            f"enclosing document as continuations (a model omission at a sheet "
            f"boundary, not a real gap): {sorted(absorbed)[:20]}"
            f"{'...' if len(absorbed) > 20 else ''}"
        )

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
            # Stage 1 writes a real child PDF into data/raw below.  That makes
            # this a physical document in the manifest model, even though the
            # immutable source bundle and approved source-page range remain
            # recorded separately.  Do not model this as a processed-text
            # segment: those have no raw file of their own and acquire a
            # source_document_id/page_map only through the DAO's derivation
            # receipt path after document processing.
            "document_role": "physical",
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
            "segmentation_status": "completed",
            "segmentation_reviewed_by": None,
            "segmentation_reviewed_at": None,
            "segmentation_review_note": None,
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


# v0.3: shortened prompt text (dropped the worked-example framing, kept every
# load-bearing instruction -- red page numbers, rotated cells, repeated-title
# rule, needs_full_page escape hatch). Bumping the version invalidates any
# sheet cached under v0.2's longer wording so a re-run re-asks under the new
# prompt rather than trusting a stale answer. The cache contract additionally
# fingerprints SEGMENT_PROMPT's actual text (see _segment_prompt_fingerprint)
# so a future in-place prompt edit that forgets to bump this constant still
# invalidates the cache instead of silently reusing stale verdicts.
SEGMENT_PROMPT_VERSION = "segment_contact_sheet_v0.3"
SEGMENT_OUTPUT_SCHEMA_VERSION = "segment_contact_sheet_output_v0.1"

# Provider-neutral output contract. Claude CLI enforces this natively with
# --json-schema; providers without native structured output still return through
# the same caller-side parser and one-correction gate. Keeping this contract in
# Stage 1 (rather than embedding Claude flags here) lets a future local vision
# model implement the provider method without changing segmentation logic.
SEGMENT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "boundaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer"},
                    "type_guess": {"type": ["string", "null"], "enum": [*sorted(DOCUMENT_TYPES), None]},
                    "type_label": {"type": ["string", "null"]},
                    "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    "evidence": {"type": ["string", "null"]},
                },
                "required": ["page"],
                "additionalProperties": False,
            },
        },
        "continuations": {"type": "array", "items": {"type": "integer"}},
        "needs_full_page": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["boundaries", "continuations", "needs_full_page"],
    "additionalProperties": False,
}

# Verified against a real 4x4 sheet (p65-80 of the 110p bundle, 9 rotated cells):
# every rotated cell was read and the p74 boundary found at 0.92 confidence.
#
# Two constraints from llm_providers.py's recorded failures, both load-bearing:
#   * Send this through the provider's structured-image seam, whose Claude
#     implementation prepends the working "Read the image file at {path} and
#     then:" imperative. The trailing-label
#     form ("Image: {path}") failed 9/9 with "no image was attached".
#   * No self-legitimizing framing -- no "this is a sanctioned step", no "do not
#     refuse". A prior version added that and the child model read it as a
#     prompt-injection signal and refused. A genuine layout question does not
#     argue for itself.
SEGMENT_PROMPT = """This image is a contact sheet: {cell_count} cells in a \
{cols}x{rows} grid, read left to right then top to bottom. Each cell shows the \
top portion of one page. The red number in each cell is that page's number -- \
use those numbers. Some cells were scanned rotated a quarter turn; read those \
cells at whatever orientation they are in. {blank_note}The PDF concatenates \
several separate documents. Identify which pages START a new document (its own \
title block, a different form layout, a different letterhead, a page-1-of-N \
reset). Repeated forms matter: if the SAME title is reprinted on consecutive \
pages, each such page is its own document, not a continuation. A page with no \
title that just continues the text or table above it is not a boundary. If a \
cell's top portion is not enough to judge, list it in needs_full_page rather \
than guessing."""


def _segment_prompt_fingerprint() -> str:
    """Hash of SEGMENT_PROMPT's literal text.

    Belt-and-suspenders alongside SEGMENT_PROMPT_VERSION: the version constant
    requires a human to remember to bump it on every wording change (missed
    once already -- this file's prompt text changed while the constant stayed
    at v0.2). Folding the actual text into the cache contract means an edit
    that forgets the bump still invalidates cached sheets instead of silently
    reusing verdicts produced under different wording.
    """
    return hashlib.sha256(SEGMENT_PROMPT.encode("utf-8")).hexdigest()[:16]


def build_segment_prompt(sheet_pages: list[int], geometry: dict) -> str:
    """Fills the sheet's actual shape into the prompt.

    The blank-cell note only appears on a short final sheet; stating it on a full
    sheet would invite the model to look for absent cells.
    """
    capacity = geometry["cols"] * geometry["rows"]
    blank_note = ""
    if len(sheet_pages) < capacity:
        blank_note = (
            f"Only the first {len(sheet_pages)} cells contain pages; ignore the rest. "
        )
    return SEGMENT_PROMPT.format(
        cell_count=capacity,
        cols=geometry["cols"],
        rows=geometry["rows"],
        blank_note=blank_note,
    )


def _call_structured_image(provider, image_path: Path, prompt: str):
    """Use the provider-neutral structured-image contract when available.

    Duck-typed test providers and older third-party providers can still expose
    only transcribe_image; Stage 1 then applies the same parser and correction
    gate to their text. Production providers should implement the structured
    method whenever their backend supports native schema enforcement.
    """
    analyze = getattr(provider, "analyze_image_structured", None)
    if callable(analyze):
        return analyze(
            image_path,
            prompt,
            SEGMENT_PROMPT_VERSION,
            SEGMENT_OUTPUT_SCHEMA,
        )
    return provider.transcribe_image(image_path, prompt, SEGMENT_PROMPT_VERSION)


def _result_text_for_parsing(result) -> str:
    structured = getattr(result, "structured_output", None)
    if isinstance(structured, dict):
        return json.dumps(structured, ensure_ascii=False)
    return result.text


def _correction_prompt(prompt: str, validation_error: str | None) -> str:
    """Exactly one caller-owned P4 correction prompt for a failed sheet."""
    detail = validation_error or "the response did not match the required output contract"
    return (
        f"{prompt}\n\n"
        "Your previous response could not be used because it failed the output "
        f"contract: {detail}. Re-evaluate the same image and return the required "
        "structured fields. Do not omit any of the three top-level arrays."
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

# A merged segment at or above this length is re-examined page by page by the
# refine pass. The over-merge failure mode is specifically the repeating-form
# runs (진료비 세부내역서, 영수증), which always surface as one long segment
# swallowing many one-page documents -- never as a short mistake. Short
# segments are left alone: re-checking them spends calls where the crop pass
# was already right.
#
# Settled at 4 by measurement on the 110p bundle (CASE_025): threshold 5 lifted
# recall 0.81 -> 0.91, and dropping to 4 lifted it further to 0.96 (F1 0.88 ->
# 0.94) by pulling in the 4-page 영수증 run (p97-100) that threshold 5 left
# merged -- it recovered p98/99/100 for the cost of one low-confidence false
# split (p26). Below 4 the re-check would start hitting genuine 2-3 page
# documents where the crop pass was already right.
DEFAULT_LONG_SEGMENT_THRESHOLD = 4


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
    refine: bool = False,
    refine_threshold: int = DEFAULT_LONG_SEGMENT_THRESHOLD,
    refine_scratch_dir: Path | None = None,
    resume: bool = True,
    prefer_text_anchor: bool = True,
    progress=None,
) -> dict:
    """Runs one vision call per contact sheet and merges the responses.

    Unless `prefer_text_anchor` is disabled, a bundle whose every page carries an
    embedded text layer skips the vision path entirely and derives its boundaries
    from the publisher's own title lines -- see text_anchor_boundaries for the
    measurement that justifies preferring it. Cost there is zero model calls.

    Provider-injected and returns a plain dict -- no sys.exit, no provider
    construction here -- so a test drives it with FixtureProvider and the CLI
    wrapper (or a future orchestrator) owns process exit. This mirrors
    run_checkpoint1.run_checkpoint1's contract deliberately.

    Every vision call goes through the provider-neutral structured-image seam.
    Claude's implementation prepends the "Read the image file at {path} and
    then:" imperative the recorded 9/9 label failure requires. One sheet's
    parse failure never discards the others (their calls are already paid for):
    a failed sheet's pages fall through to unassigned_pages via
    merge_sheet_proposals.

    Returns ``{segments, unassigned_pages, needs_full_page, warnings, method,
    contact_sheets, per_sheet}`` -- enough for build_proposal_document to
    assemble a schema-valid proposal without re-deriving anything.
    """
    import fitz

    geometry = geometry or compute_sheet_geometry()
    if page_count is None:
        with fitz.open(pdf_path) as document:
            page_count = document.page_count

    # Deterministic path first: it costs nothing to attempt, and when it applies
    # it is strictly better than the vision proposal (precision 1.0000 vs the
    # human baseline, zero mid-clause cuts). Attempted BEFORE any sheet is
    # rendered or any provider call is made, so a born-digital bundle spends no
    # tokens at all. Returns None for a scan, and the vision path runs unchanged.
    if prefer_text_anchor:
        anchored = text_anchor_boundaries(pdf_path, page_count)
        if anchored is not None:
            if progress:
                progress(
                    f"text layer covers all {page_count} page(s): deriving "
                    f"{len(anchored)} boundary/boundaries deterministically "
                    f"(0 model calls, vision path skipped)"
                )
            return _text_anchor_proposal(
                anchored, page_count=page_count, geometry=geometry
            )

    batches = plan_sheets(page_count, geometry["pages_per_sheet"])
    if sheet_paths is not None and len(sheet_paths) != len(batches):
        raise SegmentationError(
            f"got {len(sheet_paths)} sheet paths for {len(batches)} planned sheets"
        )

    cache_dir = _resume_dir(case_id, doc_id)
    fingerprint = geometry_fingerprint(geometry, page_count=page_count)
    cache_contract = {
        "geometry_fingerprint": fingerprint,
        "prompt_version": SEGMENT_PROMPT_VERSION,
        "prompt_fingerprint": _segment_prompt_fingerprint(),
        "output_schema_version": SEGMENT_OUTPUT_SCHEMA_VERSION,
        "provider_name": getattr(provider, "provider_name", None),
        "model_name": getattr(provider, "model_name", None),
    }

    per_sheet: list[dict] = []
    contact_sheets: list[dict] = []
    provider_metadata: dict | None = None

    for index, pages in enumerate(batches):
        sheet_path = sheet_paths[index] if sheet_paths else None
        cached = _load_cached_sheet(cache_dir, index) if resume else None
        # A cached verdict is reusable only under the exact image/prompt/schema/
        # provider contract that produced it, and only if it parsed successfully.
        # Older caches had only a geometry fingerprint and cached parse failures;
        # both must be re-called after this fix.
        if cached is not None and (
            cached.get("cache_contract") != cache_contract
            or not (cached.get("parsed") or {}).get("ok")
        ):
            cached = None

        if cached is not None:
            parsed = cached["parsed"]
            if provider_metadata is None:
                provider_metadata = cached.get("provider_metadata")
            if progress:
                progress(f"sheet {index} (p{pages[0]}-{pages[-1]}) (cached)")
        else:
            prompt = build_segment_prompt(pages, geometry)
            result = None
            parsed = None
            raw_responses: list[str] = []
            provider_errors: list[str] = []

            # P4 at the per-sheet output boundary: initial attempt plus exactly
            # one self-correction. A second failure is preserved as an
            # unassigned-page halt; it is never upgraded to a usable proposal.
            for attempt in range(2):
                attempt_prompt = prompt if attempt == 0 else _correction_prompt(
                    prompt, parsed.get("warning") if parsed else provider_errors[-1]
                )
                try:
                    result = _call_structured_image(
                        provider, Path(sheet_path), attempt_prompt
                    )
                except Exception as exc:
                    # Provider failures are stage failures, but one bad sheet
                    # must not discard already-paid successful sheets.
                    provider_errors.append(f"{type(exc).__name__}: {exc}")
                    parsed = {
                        "ok": False,
                        "boundaries": [],
                        "continuations": [],
                        "needs_full_page": [],
                        "warning": f"provider call failed: {exc}",
                    }
                else:
                    raw = _result_text_for_parsing(result)
                    raw_responses.append(raw)
                    parsed = parse_segmentation_response(
                        raw, pages, require_output_contract=True
                    )
                    provider_metadata = result.metadata()
                if parsed["ok"]:
                    break

            assert parsed is not None
            if not parsed["ok"]:
                parsed["warning"] = (
                    f"{parsed.get('warning')}; failed after exactly one "
                    "structured-output correction attempt"
                )
            if resume:
                _save_cached_sheet(cache_dir, index, {
                    "cache_contract": cache_contract,
                    "parsed": parsed,
                    "raw_responses": raw_responses,
                    "provider_errors": provider_errors,
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

    # Deterministic, no model call. Empty for a scanned bundle, in which case
    # merge behaves exactly as before.
    text_layer_continuations = continuation_pages_from_text_layer(pdf_path, page_count)
    if text_layer_continuations and progress:
        progress(
            f"text layer: {len(text_layer_continuations)} page(s) begin mid-document "
            f"and cannot be boundaries"
        )

    merged = merge_sheet_proposals(
        per_sheet, page_count, sheet_pages=batches,
        text_layer_continuations=text_layer_continuations,
    )

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
    elif fallback["pages"]:
        if progress:
            progress(
                f"re-checking {len(fallback['pages'])} crop-ambiguous page(s) full-page..."
            )
        resolution = resolve_needs_full_page(
            per_sheet,
            pages=fallback["pages"],
            pdf_path=pdf_path,
            provider=provider,
            scratch_dir=refine_scratch_dir,
            progress=progress,
        )
        per_sheet = resolution["per_sheet"]
        # Re-merge from the corrected per-sheet facts. A successful full-page
        # verdict replaces the crop-pass uncertainty with either a boundary or
        # a continuation; an unreadable/failed verdict stays in
        # needs_full_page and therefore remains visible to the human gate.
        merged = merge_sheet_proposals(
            per_sheet, page_count, sheet_pages=batches,
            text_layer_continuations=text_layer_continuations,
        )
        fallback.update({
            "triggered": True,
            "resolved_pages": resolution["resolved_pages"],
            "unresolved_pages": resolution["unresolved_pages"],
            "new_boundaries": resolution["new_boundaries"],
            "calls": resolution["calls"],
            "failure_reasons": {
                str(page): reason
                for page, reason in resolution["failure_reasons"].items()
            },
        })
        if resolution["unresolved_pages"]:
            merged["warnings"].append(
                f"full-page fallback could not resolve {len(resolution['unresolved_pages'])} "
                f"page(s); they remain flagged for human review: "
                f"{resolution['unresolved_pages']}. Failures: "
                f"{_format_full_page_failures(resolution['failure_reasons'])}"
            )

    # The full-page fallback that actually moves the number: re-examine each long
    # merged segment page by page and split it where a boundary was over-merged.
    # Unlike the needs_full_page path above (which the crop pass rarely triggers),
    # this targets segment LENGTH -- the shape an over-merged repeating-form run
    # always takes. Measured on the 110p bundle: recall 0.81 -> 0.96. Off by
    # default; the caller opts in, since it spends one call per interior page of
    # every long segment.
    refinement = None
    if refine:
        if progress:
            progress(f"refining long segments (length >= {refine_threshold}) full-page...")
        refinement = refine_long_segments(
            merged["segments"], pdf_path=pdf_path, provider=provider,
            threshold=refine_threshold, scratch_dir=refine_scratch_dir,
            progress=progress,
        )
        merged["segments"] = refinement["segments"]
        fallback = {
            **fallback,
            "triggered": True,
            # Preserve crop-ambiguous pages in `pages`; refinement has its own
            # explicit accounting so the artifact does not conflate the two
            # different reasons a full-page call happened.
            "refinement_pages": refinement["pages_examined"],
            "refinement_calls": refinement["calls"],
            "refinement_unresolved_pages": refinement["unresolved_pages"],
            "refinement_failure_reasons": {
                str(page): reason
                for page, reason in refinement["failure_reasons"].items()
            },
        }
        if refinement["new_boundaries"]:
            merged["warnings"].append(
                f"full-page refinement split {len(refinement['refined_indices'])} "
                f"long segment(s) at {len(refinement['new_boundaries'])} new "
                f"boundary/boundaries: {refinement['new_boundaries']}"
            )
        if refinement["unresolved_pages"]:
            merged["warnings"].append(
                f"full-page refinement could not classify "
                f"{len(refinement['unresolved_pages'])} page(s); they were not "
                f"silently counted as continuations: "
                f"{_format_full_page_failures(refinement['failure_reasons'])}"
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
        "refinement": refinement,
    }


def _text_anchor_proposal(
    boundaries: set[int], *, page_count: int, geometry: dict
) -> dict:
    """The propose_boundaries return shape for a deterministic, no-model run.

    Mirrors the vision path's contract exactly so build_proposal_document and
    _write_proposal need no branch. The model-specific fields are null/empty
    because no model ran -- that absence is the honest record, not a gap: the
    schema's `text_anchor` mode says so explicitly. needs_full_page is empty and
    the fallback is untriggered for the same reason (nothing was ambiguous; the
    text either named a title or it did not)."""
    return {
        "segments": segments_from_boundaries(boundaries, page_count),
        "unassigned_pages": [],
        "needs_full_page": [],
        "warnings": [],
        "method": {
            "ocr_performed": False,
            "method_version": METHOD_VERSION,
            "mode": "text_anchor",
            "provider_name": None,
            "model_name": None,
            "prompt_version": None,
            "provider_metadata": None,
            "render_dpi": None,
            "crop_ratio": geometry["crop_ratio"],
            "grid_cols": geometry["cols"],
            "grid_rows": geometry["rows"],
            "sheet_pixel_budget": None,
            "contact_sheets": [],
            "full_page_fallback": _plan_fallback([], page_count, 0.0),
        },
        "contact_sheets": [],
        "per_sheet": [],
        "refinement": None,
    }


def _plan_fallback(needs_full_page: list[int], page_count: int, cap_ratio: float) -> dict:
    """Decides whether the full-page second look runs, and records the decision.

    The plan's step F: a run needing more full-page looks than the cap allows is
    a tuning signal, not a spend problem, so the whole fallback is SKIPPED and
    the pages stay flagged for a human. This returns the schema's
    full_page_fallback shape either way -- the artifact records what happened,
    which is diagnosable later without re-running.

    The caller performs one full-page call per listed page when this is not
    saturated. Keeping the spend decision here makes the 25% cap independently
    testable and keeps saturation an all-or-nothing tuning signal.
    """
    pages = sorted(set(needs_full_page))
    cap = int(page_count * cap_ratio)
    saturated = len(pages) > cap
    return {
        "triggered": False,
        "pages": pages,
        "saturated": saturated,
        "cap": cap,
        "prompt_version": FULL_PAGE_PROMPT_VERSION,
        "output_schema_version": FULL_PAGE_OUTPUT_SCHEMA_VERSION,
        "resolved_pages": [],
        "unresolved_pages": list(pages),
        "new_boundaries": [],
        "calls": 0,
        "failure_reasons": {},
        "refinement_pages": [],
        "refinement_calls": 0,
        "refinement_unresolved_pages": [],
        "refinement_failure_reasons": {},
    }


# ------------------------------------------------ long-segment refinement --

# Asked of ONE full page, not a contact sheet. The crop-only first pass misses a
# boundary when a repeating form reprints its title every page and the model
# reads the run as continuation; a full page shows the whole title block and
# page-1-of-N markers a top-third crop cut off. Goes through the same
# provider-neutral structured-image seam as contact-sheet segmentation.
#
# Split-biased on purpose (owner decision 2026-07-21, known-gaps item 18): a
# reprinted title block IS a new-document signal, full stop -- we do NOT try to
# tell "own totals/dates" record pages apart from per-transaction receipts and
# keep records merged. Over-splitting is the cheap error (a human merges two
# segments from the sheets in seconds); over-merging only surfaces after OCR,
# classification, and extraction have all run on the wrong boundaries. The ONLY
# continuation is a page with NO title block of its own -- a bare table/body
# that visibly runs on from the page before it (a "page 2/N" with the header
# printed only on page 1). This deliberately splits repeating-form runs like
# CASE_026's 내역서 pages; the type-aware "records merge, receipts split" rule
# is the deferred alternative, not this.
FULL_PAGE_PROMPT = """This is one full page from a scanned PDF containing \
multiple documents. Decide whether THIS page starts a NEW document.

Choose NEW when this page has its OWN title block, a page-1-of-N marker, a \
different layout, or a different letterhead. A repeated title still means NEW: \
if consecutive pages each show the same title, treat EACH titled page as a \
separate document.

Choose continuation ONLY when this page has NO title block of its own and \
plainly continues a table or body text from the previous page. When unsure, \
choose NEW. Read quarter-turned pages in place.

Return only a JSON object with starts_new_document (boolean), type_label \
(string or null), confidence (0.0-1.0 or null), and evidence (string or null)."""

# v0.3 moves the full-page verdict onto the same provider-neutral structured
# image seam as the contact-sheet pass. The version bump invalidates every v0.2
# free-form verdict: those calls could return prose, and the old cache did not
# bind a verdict to provider/model/schema/image content.
FULL_PAGE_PROMPT_VERSION = "segment_full_page_v0.3"
FULL_PAGE_OUTPUT_SCHEMA_VERSION = "segment_full_page_output_v0.1"
FULL_PAGE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "starts_new_document": {"type": "boolean"},
        "type_label": {"type": ["string", "null"]},
        "confidence": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 1,
        },
        "evidence": {"type": ["string", "null"]},
    },
    "required": [
        "starts_new_document",
        "type_label",
        "confidence",
        "evidence",
    ],
    "additionalProperties": False,
}


def _call_structured_full_page(provider, image_path: Path, prompt: str):
    """Use native schema enforcement when the provider exposes it."""
    analyze = getattr(provider, "analyze_image_structured", None)
    if callable(analyze):
        return analyze(
            image_path,
            prompt,
            FULL_PAGE_PROMPT_VERSION,
            FULL_PAGE_OUTPUT_SCHEMA,
        )
    return provider.transcribe_image(
        image_path, prompt, FULL_PAGE_PROMPT_VERSION
    )


def _full_page_correction_prompt(validation_error: str | None) -> str:
    detail = validation_error or "the response did not match the output contract"
    return (
        f"{FULL_PAGE_PROMPT}\n\n"
        "Your previous response could not be used because it failed the output "
        f"contract: {detail}. Re-evaluate the same page and return all four "
        "required fields."
    )


def _full_page_cache_contract(provider) -> dict:
    return {
        "prompt_version": FULL_PAGE_PROMPT_VERSION,
        "output_schema_version": FULL_PAGE_OUTPUT_SCHEMA_VERSION,
        "provider_name": getattr(provider, "provider_name", None),
        "model_name": getattr(provider, "model_name", None),
    }


def parse_full_page_response(
    raw: str, *, require_output_contract: bool = False
) -> dict:
    """Parses a single-page boundary verdict. NEVER raises -- same fail-safe as
    parse_segmentation_response: an unreadable verdict leaves the page as it was
    (a continuation of the long segment), never inventing a split."""
    parsed, error = _scan_for_json_object(raw)
    if parsed is None:
        return {"ok": False, "starts_new_document": False, "warning": error}
    if require_output_contract:
        missing = [
            field for field in FULL_PAGE_OUTPUT_SCHEMA["required"]
            if field not in parsed
        ]
        if missing:
            return {
                "ok": False,
                "starts_new_document": False,
                "warning": f"response omitted required fields: {missing}",
            }
    starts = parsed.get("starts_new_document")
    if not isinstance(starts, bool):
        return {"ok": False, "starts_new_document": False,
                "warning": f"starts_new_document was not a boolean: {starts!r}"}
    confidence = parsed.get("confidence")
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
        or not 0.0 <= confidence <= 1.0
    ):
        confidence = None
    type_guess = parsed.get("type_label")
    return {
        "ok": True,
        "starts_new_document": starts,
        "type_label": type_guess if isinstance(type_guess, str) else None,
        "confidence": float(confidence) if confidence is not None else None,
        "evidence": parsed.get("evidence"),
    }


def _inspect_full_pages(
    pages: list[int],
    *,
    pdf_path: Path,
    provider,
    scratch_dir: Path | None = None,
    progress=None,
) -> dict:
    """Returns cached-or-fresh full-page boundary verdicts for ``pages``.

    Both targeted crop-ambiguity fallback and optional long-segment refinement
    use the same prompt, render, and per-page cache. Sharing this helper avoids
    paying twice when a page is first named in ``needs_full_page`` and later
    falls inside a long segment selected by ``--refine``.

    Every fresh page receives one initial attempt and exactly one correction
    attempt when the provider or domain parser rejects the result. A failure is
    returned as ``ok: false`` and is deliberately not cached. Callers keep the
    page unresolved; they never present that failure as a valid continuation.
    """
    if scratch_dir is not None:
        scratch_dir.mkdir(parents=True, exist_ok=True)

    zoom = LONG_EDGE_CAP / DEFAULT_PAGE_HEIGHT_PT
    cache_contract = _full_page_cache_contract(provider)
    verdict_cache: dict[int, dict] = {}
    cache_path = None
    if scratch_dir is not None:
        cache_path = scratch_dir / "_verdicts.json"
        if cache_path.exists():
            try:
                stored = json.loads(cache_path.read_text(encoding="utf-8"))
                if stored.get("cache_contract") == cache_contract:
                    verdict_cache = {
                        int(k): v for k, v in stored.get("verdicts", {}).items()
                    }
            except (OSError, json.JSONDecodeError):
                verdict_cache = {}

    def persist_verdicts():
        if cache_path is None:
            return
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {
                "cache_contract": cache_contract,
                "verdicts": {str(k): v for k, v in verdict_cache.items()},
            },
            ensure_ascii=False,
        ), encoding="utf-8")
        tmp.replace(cache_path)

    verdicts: dict[int, dict] = {}
    calls = 0
    for page in sorted(set(pages)):
        images = render_page_images(pdf_path, [page], zoom=zoom)
        image = images[page]
        image_fingerprint = hashlib.sha256(
            f"{image.mode}|{image.size}".encode("utf-8") + image.tobytes()
        ).hexdigest()[:16]
        temporary_path = scratch_dir is None
        if scratch_dir is not None:
            page_path = scratch_dir / f"fullpage_p{page:03d}.png"
            image.save(page_path)
        else:
            import tempfile
            fd = tempfile.NamedTemporaryFile(suffix=f"_fullpage_p{page:03d}.png", delete=False)
            page_path = Path(fd.name)
            fd.close()
            image.save(page_path)

        cached_entry = verdict_cache.get(page)
        if (
            isinstance(cached_entry, dict)
            and cached_entry.get("image_fingerprint") == image_fingerprint
            and (cached_entry.get("verdict") or {}).get("ok")
        ):
            verdict = cached_entry["verdict"]
            verdicts[page] = verdict
            if temporary_path:
                page_path.unlink(missing_ok=True)
            if progress:
                mark = "NEW" if verdict["starts_new_document"] else "cont"
                progress(f"  full-page p{page}: {mark} (cached)")
            continue

        verdict = {
            "ok": False,
            "starts_new_document": False,
            "warning": "full-page verdict was not attempted",
        }
        attempt_errors: list[str] = []
        try:
            for attempt in range(2):
                prompt = (
                    FULL_PAGE_PROMPT
                    if attempt == 0
                    else _full_page_correction_prompt(verdict.get("warning"))
                )
                try:
                    result = _call_structured_full_page(
                        provider, page_path, prompt
                    )
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
                    attempt_errors.append(error)
                    verdict = {
                        "ok": False,
                        "starts_new_document": False,
                        "warning": f"provider call failed: {error}",
                    }
                else:
                    raw = _result_text_for_parsing(result)
                    verdict = parse_full_page_response(
                        raw, require_output_contract=True
                    )
                calls += 1
                if verdict.get("ok"):
                    break
        finally:
            if temporary_path:
                page_path.unlink(missing_ok=True)

        if not verdict.get("ok"):
            verdict["warning"] = (
                f"{verdict.get('warning')}; failed after exactly one "
                "structured-output correction attempt"
            )
            if attempt_errors:
                verdict["provider_errors"] = attempt_errors
        verdicts[page] = verdict
        if verdict.get("ok"):
            verdict_cache[page] = {
                "image_fingerprint": image_fingerprint,
                "verdict": verdict,
            }
            persist_verdicts()
        if progress:
            if verdict.get("ok"):
                mark = "NEW" if verdict["starts_new_document"] else "cont"
                progress(
                    f"  full-page p{page}: {mark} "
                    f"(conf {verdict.get('confidence')})"
                )
            else:
                progress(
                    f"  full-page p{page}: ERROR ({verdict.get('warning')})"
                )

    return {
        "verdicts": verdicts,
        "calls": calls,
        "failure_reasons": {
            page: verdict.get("warning")
            for page, verdict in verdicts.items()
            if not verdict.get("ok")
        },
    }


def _format_full_page_failures(failure_reasons: dict[int, str]) -> str:
    """Bounded diagnostic text for proposal warnings and CLI output."""
    items = sorted(failure_reasons.items())
    shown = "; ".join(f"p{page}: {reason}" for page, reason in items[:5])
    if len(items) > 5:
        shown += f"; ... {len(items) - 5} more"
    return shown


def resolve_needs_full_page(
    per_sheet: list[dict],
    *,
    pages: list[int],
    pdf_path: Path,
    provider,
    scratch_dir: Path | None = None,
    progress=None,
) -> dict:
    """Replaces crop-pass uncertainty with a full-page boundary verdict.

    A resolved page becomes exactly one of boundary/continuation and is removed
    from ``needs_full_page``. Under the owner-set split-biased rule, a full-page
    title means boundary; no title means continuation. An unreadable verdict or
    provider failure changes nothing, so the proposal still flags the page and
    the human gate remains load-bearing.
    """
    import copy

    updated = copy.deepcopy(per_sheet)
    inspection = _inspect_full_pages(
        pages,
        pdf_path=pdf_path,
        provider=provider,
        scratch_dir=scratch_dir,
        progress=progress,
    )
    resolved_pages: list[int] = []
    unresolved_pages: list[int] = []
    new_boundaries: list[int] = []

    for page in sorted(set(pages)):
        verdict = inspection["verdicts"].get(page, {"ok": False})
        if not verdict.get("ok"):
            unresolved_pages.append(page)
            continue

        found = False
        for sheet in updated:
            needs = set(sheet.get("needs_full_page", []))
            if page not in needs:
                continue
            found = True
            needs.discard(page)
            sheet["needs_full_page"] = sorted(needs)
            sheet["continuations"] = sorted(
                p for p in set(sheet.get("continuations", [])) if p != page
            )
            sheet["boundaries"] = [
                item for item in sheet.get("boundaries", []) if item.get("page") != page
            ]
            if verdict["starts_new_document"]:
                sheet["boundaries"].append({
                    "page": page,
                    "type_guess": None,
                    "type_label": verdict.get("type_label"),
                    "confidence": verdict.get("confidence"),
                    "evidence": verdict.get("evidence"),
                })
                sheet["boundaries"].sort(key=lambda item: item["page"])
                new_boundaries.append(page)
            else:
                sheet["continuations"] = sorted(set(sheet["continuations"]) | {page})
        if found:
            resolved_pages.append(page)
        else:
            # Defensive: the caller derived pages from per_sheet, so this would
            # indicate an internal bookkeeping bug. Do not claim it was resolved.
            unresolved_pages.append(page)

    return {
        "per_sheet": updated,
        "resolved_pages": resolved_pages,
        "unresolved_pages": unresolved_pages,
        "new_boundaries": new_boundaries,
        "calls": inspection["calls"],
        "failure_reasons": inspection["failure_reasons"],
    }


def refine_long_segments(
    segments: list[dict],
    *,
    pdf_path: Path,
    provider,
    threshold: int = DEFAULT_LONG_SEGMENT_THRESHOLD,
    fallback_dpi: int = DEFAULT_FALLBACK_DPI,
    scratch_dir: Path | None = None,
    progress=None,
) -> dict:
    """Re-examines every page inside a long segment full-page, splitting it
    wherever the model now says a new document starts.

    This is the full-page fallback aimed at the real over-merge failure, which
    is NOT a page the model flagged (needs_full_page) -- the crop pass merged
    those confidently. So the trigger is segment LENGTH, not the model's own
    doubt: a repeating-form run only ever over-merges into one long segment.

    A segment's first page keeps its known boundary; each interior page is
    rendered full-page and asked the single-page question. A 'yes' becomes a new
    boundary and the long segment is cut there. Fails safe: an unreadable or
    'no' verdict leaves the page as a continuation, so this can only ADD
    boundaries a human then reviews, never silently remove one.

    Returns ``{segments, refined_indices, pages_examined, new_boundaries,
    calls}`` -- segments is the new full list (short ones passed through
    untouched), the rest is for reporting and scoring.
    """
    new_segments: list[dict] = []
    refined_indices: list[int] = []
    pages_examined: list[int] = []
    new_boundaries: list[int] = []
    unresolved_pages: list[int] = []
    failure_reasons: dict[int, str] = {}
    calls = 0

    for seg in segments:
        start, end = seg["page_start"], seg["page_end"]
        length = end - start + 1
        if length < threshold:
            new_segments.append(dict(seg))
            continue

        refined_indices.append(seg.get("segment_index"))
        # Boundaries within this segment, always including its own start.
        cut_points = [start]
        cut_meta: dict[int, dict] = {}
        inspect_pages = list(range(start + 1, end + 1))
        inspection = _inspect_full_pages(
            inspect_pages,
            pdf_path=pdf_path,
            provider=provider,
            scratch_dir=scratch_dir,
            progress=progress,
        )
        pages_examined.extend(inspect_pages)
        calls += inspection["calls"]
        for page in inspect_pages:
            verdict = inspection["verdicts"][page]
            if not verdict.get("ok"):
                unresolved_pages.append(page)
                failure_reasons[page] = verdict.get(
                    "warning", "full-page verdict failed"
                )
                continue
            if verdict["starts_new_document"]:
                cut_points.append(page)
                new_boundaries.append(page)
                cut_meta[page] = verdict

        # Rebuild this segment as one-or-more from its cut points.
        for i, cut in enumerate(cut_points):
            seg_end = (cut_points[i + 1] - 1) if i + 1 < len(cut_points) else end
            if cut == start:
                # The original segment's head keeps its crop-pass metadata.
                piece = dict(seg)
                piece["page_start"], piece["page_end"] = cut, seg_end
            else:
                meta = cut_meta[cut]
                piece = {
                    "segment_index": None,  # renumbered below
                    "page_start": cut, "page_end": seg_end,
                    "provisional_document_type": None,
                    "provisional_type_label": meta.get("type_label"),
                    "confidence": meta.get("confidence"),
                    "boundary_evidence": meta.get("evidence"),
                    "review_status": "pending",
                    "needs_full_page": False,
                    "orientation_suspect": False,
                    "assigned_document_id": None,
                }
            new_segments.append(piece)

    # Renumber segment_index across the whole rebuilt list.
    for i, seg in enumerate(new_segments):
        seg["segment_index"] = i

    return {
        "segments": new_segments,
        "refined_indices": refined_indices,
        "pages_examined": pages_examined,
        "new_boundaries": sorted(new_boundaries),
        "unresolved_pages": sorted(set(unresolved_pages)),
        "failure_reasons": failure_reasons,
        "calls": calls,
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


def proposal_with_assignments(proposal: dict, document_ids: list[str]) -> dict:
    """Return a copy whose segments link to the documents created by a split."""
    import copy

    segments = proposal.get("segments", [])
    if len(segments) != len(document_ids):
        raise SegmentationError(
            f"cannot assign {len(document_ids)} document id(s) to "
            f"{len(segments)} segment(s)"
        )
    updated = copy.deepcopy(proposal)
    for segment, document_id in zip(updated["segments"], document_ids):
        segment["assigned_document_id"] = document_id
    return updated


def redistribute_ocr_pages(
    parent_ocr: dict, segments: list[dict], document_ids: list[str]
) -> list[dict]:
    """Hand each child segment the parent bundle's OCR pages it now owns.

    Pure -- no I/O, no provider call. When OCR runs BEFORE segmentation (so
    boundaries come from real page text rather than a contact-sheet crop), the
    text is produced against the bundle. Every downstream stage addresses a
    document by its own id and its own 1-based page numbers, so the split has
    to re-file those pages under the children.

    What is preserved verbatim: each page's `cross_validation` verdict. A P8
    result is a property of the physical page, so it follows the page to its
    new owner -- splitting must never invent agreement for a page that
    disagreed, nor spread one page's disagreement onto its siblings.

    What changes: `page` is renumbered from 1 within each child, and
    `text_path` is rewritten to the child's own directory. A child that kept
    the bundle's numbering would cite page numbers it does not have.

    Fails loud if the ranges do not account for every parent page exactly once.
    split_readiness_errors already refuses a proposal with an unassigned page,
    so a mismatch here means the approved ranges and the OCR record disagree
    about how long the bundle is -- truncating a document silently is exactly
    the corruption this raises instead.
    """
    if len(segments) != len(document_ids):
        raise SegmentationError(
            f"cannot assign {len(document_ids)} document id(s) to {len(segments)} segment(s)"
        )
    pages = parent_ocr.get("pages", [])
    covered = sum(seg["page_end"] - seg["page_start"] + 1 for seg in segments)
    if covered != len(pages):
        raise SegmentationError(
            f"segments cover {covered} page(s) but the OCR record for "
            f"{parent_ocr.get('document_id')} has {len(pages)}; refusing to "
            "redistribute a partial or overlapping set"
        )

    children: list[dict] = []
    for segment, doc_id in zip(segments, document_ids):
        start, end = segment["page_start"], segment["page_end"]
        if start < 1 or end > len(pages) or start > end:
            raise SegmentationError(
                f"segment p{start}-{end} is outside the OCR record's "
                f"1-{len(pages)} page range"
            )
        child_pages = []
        for offset, source_page in enumerate(pages[start - 1:end], start=1):
            page = copy.deepcopy(source_page)
            page["page"] = str(offset)
            case_id = parent_ocr.get("case_id") or _case_id_from_text_path(
                source_page.get("text_path"))
            page["text_path"] = (
                f"data/processed/{case_id}/{doc_id}/page_{offset:03d}.md"
            )
            child_pages.append(page)
        child = copy.deepcopy(parent_ocr)
        child["document_id"] = doc_id
        child["pages"] = child_pages
        children.append(child)
    return children


def _case_id_from_text_path(text_path: str | None) -> str:
    """The case id embedded in a processed-text path, for rewriting sibling paths.

    Read back from the parent's own recorded path rather than passed in, so the
    child's path is built from the same value the parent was actually written
    under -- a mismatch would point the child at a directory that does not
    exist."""
    parts = (text_path or "").split("/")
    return parts[2] if len(parts) > 2 else "UNKNOWN_CASE"


def _redistribute_parent_ocr(
    *, case_id: str, bundle_id: str, segments: list[dict],
    document_ids: list[str], progress=None,
) -> bool:
    """Copy the parent bundle's OCR pages onto its children. Returns whether it ran.

    False means there was no parent OCR record -- the split-first order, where
    each child is OCR'd on its own afterwards. That is not an error and must
    not become one: cases processed under the old order are not reprocessed, so
    both orders coexist.

    The parent's own record and page files are deliberately KEPT. A child's
    cross_validation verdict is inherited from the parent's, so auditing that
    inheritance later requires the source to still exist; the manifest marks the
    bundle superseded and nothing downstream reads it. Text is cheap, and a
    deleted original cannot be re-derived if a redistribution turns out wrong.
    """
    parent_record_path = ROOT / "outputs" / case_id / f"ocr_result_{bundle_id}.json"
    if not parent_record_path.exists():
        return False
    try:
        parent_ocr = json.loads(parent_record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentationError(
            f"{parent_record_path} exists but could not be read ({exc}); refusing "
            "to split as though the bundle had never been OCR'd, which would "
            "silently leave every child without page text"
        ) from exc

    children = redistribute_ocr_pages(parent_ocr, segments, document_ids)

    parent_pages = parent_ocr.get("pages", [])
    for child, segment in zip(children, segments):
        doc_id = child["document_id"]
        child_dir = ROOT / "data" / "processed" / case_id / doc_id
        child_dir.mkdir(parents=True, exist_ok=True)
        # The child's own text_path already points at where the page WILL live,
        # so the SOURCE is taken from the parent's approved page range instead
        # -- the same 1-based inclusive range redistribute_ocr_pages sliced.
        source_pages = parent_pages[segment["page_start"] - 1:segment["page_end"]]
        for offset, source_page in enumerate(source_pages, start=1):
            source = ROOT / source_page["text_path"]
            if not source.exists():
                raise SegmentationError(
                    f"{doc_id}: parent page file {source} is missing; the OCR "
                    "record and the processed tree disagree"
                )
            (child_dir / f"page_{offset:03d}.md").write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
        (ROOT / "outputs" / case_id / f"ocr_result_{doc_id}.json").write_text(
            json.dumps(child, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if progress:
            progress(f"redistributed {len(child['pages'])} OCR page(s) to {doc_id}")
    return True


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
    # bundle's source_file_name. Only a complete one-to-one range match is safe
    # to treat as done; a partial or ambiguous match must fail closed.
    proposed_ranges = {(s["page_start"], s["page_end"]) for s in segments}
    existing_by_range: dict[tuple[int, int], str] = {}
    for doc in manifest.get("documents", []):
        page_range = (doc.get("source_page_start"), doc.get("source_page_end"))
        if doc.get("source_file_name") != source_file_name or page_range not in proposed_ranges:
            continue
        if page_range in existing_by_range:
            return {
                "status": "inconsistent_existing_split",
                "message": f"multiple manifest entries match {source_file_name} pages {page_range}",
            }
        existing_by_range[page_range] = doc["document_id"]
    if existing_by_range:
        missing_ranges = proposed_ranges - existing_by_range.keys()
        if missing_ranges:
            return {
                "status": "inconsistent_existing_split",
                "message": f"only part of {source_file_name} is already split",
                "missing_ranges": sorted(missing_ranges),
            }
        document_ids = [
            existing_by_range[(segment["page_start"], segment["page_end"])]
            for segment in segments
        ]
        return {
            "status": "already_split",
            "message": f"{source_file_name} already has split entries in the manifest",
            "updated_proposal": proposal_with_assignments(proposal, document_ids),
        }

    start_index = _next_document_index(manifest)
    raw_dir = ROOT / "data" / "raw" / case_id
    raw_dir.mkdir(parents=True, exist_ok=True)

    new_documents: list[dict] = []
    written_paths: list[Path] = []
    file_sizes: dict[int, int] = {}

    for offset, seg in enumerate(segments):
        doc_id = f"DOC_{start_index + offset:03d}"
        out_path = raw_dir / f"{doc_id}.pdf"
        # select() keeps the page's own resources; insert_pdf() rebuilds them
        # into a fresh document and drops the glyphs of a page whose text is
        # drawn in a subset TrueType font with a broken/WinAnsi-mislabelled
        # encoding -- exactly the Korean cover pages here ("영업배상책임보험\n
        # 보통약관", 13 chars). Measured across both CASE_905 bundles (323
        # pages): insert_pdf silently lost the text layer on 3 cover pages,
        # select on 0. A cover that extracts 0 chars reads as a genuine scan
        # to ocr_extract.pdf_embedded_page_texts(), which then routes the whole
        # segment to vision OCR -- the failure is invisible except as cost.
        # select() mutates the document it is called on, so each segment opens
        # its own handle rather than sharing one across the loop.
        with fitz.open(bundle_pdf_path) as out:
            # select is 0-based; segments are 1-based inclusive.
            out.select(list(range(seg["page_start"] - 1, seg["page_end"])))
            out.save(out_path)
        written_paths.append(out_path)
        file_sizes[seg["page_start"]] = out_path.stat().st_size  # size AFTER save
        if progress:
            progress(f"wrote {doc_id}.pdf (p{seg['page_start']}-{seg['page_end']})")

    # If the bundle was OCR'd BEFORE segmentation, the page text exists against
    # the parent and each child must be handed the pages it now owns. Written
    # before the manifest for the same reason the child PDFs are: nothing reads
    # these until the manifest names the documents, so a failure here leaves
    # harmless orphans that a re-run overwrites, whereas a half-written manifest
    # is unrecoverable. Absent a parent OCR record this is skipped entirely and
    # the older order -- split first, OCR each child afterwards -- is unchanged;
    # both must coexist, since already-processed cases are not reprocessed.
    document_ids = [f"DOC_{start_index + offset:03d}" for offset in range(len(segments))]
    redistributed = _redistribute_parent_ocr(
        case_id=case_id,
        bundle_id=bundle_id,
        segments=segments,
        document_ids=document_ids,
        progress=progress,
    )

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
        "segmentation_status": "completed",
        "ocr_status": "not_applicable",
        "redacted_text_path": None,
        "segmentation_proposal_path": proposal_path,
    }

    ok, message = dao.replace_manifest_documents(
        case_id, bundle_id, bundle_fields, new_documents, held_by, run_id,
        stage="document_segmentation",
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
        "redistributed_ocr": redistributed,
        "updated_proposal": proposal_with_assignments(
            proposal, [d["document_id"] for d in new_documents]
        ),
    }


# ----------------------------------------------------------------- CLI --

def proposal_filename(source_document_id: str) -> str:
    """One file per bundle. schema_name_for strips the _DOC_NNN suffix back to
    segmentation_proposal.schema.json, so this writes through the ordinary
    write-contract path with no special DAO casing."""
    return f"segmentation_proposal_{source_document_id}.json"


def _write_proposal(case_id, source_document_id, proposal, held_by, run_id):
    """DAO-governed proposal write, in-process (not a write-contract subprocess).

    Same lock -> validate -> atomic-write contract write-contract itself uses:
    a schema failure here is a segment_case.py bug, not agent output, so it
    fails loud and persists nothing.
    """
    from dao import (case_dir, atomic_write_json, load_registry, validate_instance,
                     acquire_lock_blocking, release_lock)

    schemas, registry = load_registry()
    errors = validate_instance(proposal, "segmentation_proposal.schema.json", schemas, registry)
    if errors:
        raise SegmentationError(
            "assembled proposal fails its own schema -- this is a segment_case.py bug:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    filename = proposal_filename(source_document_id)
    target = case_dir(case_id) / filename
    lock = acquire_lock_blocking(target, held_by, run_id, f"write {filename}")
    if lock is not None:
        raise SegmentationError(
            f"{target} is locked by {lock['held_by']} (run {lock['run_id']})"
        )
    try:
        atomic_write_json(target, proposal)
    finally:
        release_lock(target)
    return target


def _read_proposal(case_id, source_document_id):
    from dao import read_contract_data
    return read_contract_data(case_id, proposal_filename(source_document_id))


def _manifest_bundle(case_id, doc_id):
    """The bundle's manifest entry, or None. Its file_path locates the PDF."""
    from dao import read_contract_data
    manifest = read_contract_data(case_id, "document_manifest.json")
    for doc in manifest.get("documents", []):
        if doc.get("document_id") == doc_id:
            return manifest, doc
    return manifest, None


def _stderr(msg):
    print(msg, file=sys.stderr)


def _cmd_sheets(args):
    """Mode A / the PoC default: render every contact sheet variant and stop.
    No model call. A human reads the sheets and either enters ranges by hand or
    runs `propose`."""
    _, bundle = _manifest_bundle(args.case_id, args.doc_id)
    if bundle is None:
        _stderr(f"error: {args.doc_id} not in {args.case_id}'s manifest")
        return 1
    pdf_path = ROOT / bundle["file_path"]
    cols, rows = _parse_grid(args.grid)
    geometry = compute_sheet_geometry(cols=cols, rows=rows, crop_ratio=args.crop_ratio)
    out_dir = sheets_dir(args.case_id, args.doc_id)
    result = build_sheet_set(pdf_path, out_dir, geometry=geometry, progress=_stderr)
    print(json.dumps({
        "status": "sheets_rendered",
        "sheet_dir": str(out_dir),
        "variants": {v: [str(p) for p in paths] for v, paths in result["sheets"].items()},
        "page_count": result["page_count"],
        "geometry": geometry,
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_propose(args):
    """Mode B: send one contact sheet per vision call and write the proposal."""
    from dao import now_iso
    from llm_providers import build_provider, parse_provider_config

    _, bundle = _manifest_bundle(args.case_id, args.doc_id)
    if bundle is None:
        _stderr(f"error: {args.doc_id} not in {args.case_id}'s manifest")
        return 1
    pdf_path = ROOT / bundle["file_path"]
    cols, rows = _parse_grid(args.grid)
    geometry = compute_sheet_geometry(cols=cols, rows=rows, crop_ratio=args.crop_ratio)

    out_dir = sheets_dir(args.case_id, args.doc_id)
    import fitz
    with fitz.open(pdf_path) as document:
        page_count = document.page_count

    # Deterministic path, tried before ANY sheet render or provider construction:
    # a born-digital bundle needs neither. Kept here rather than only inside
    # propose_boundaries because the CLI renders sheets and builds a provider up
    # front, and both are pure waste when the text layer already answers this.
    if not args.no_text_anchor:
        anchored = text_anchor_boundaries(pdf_path, page_count)
        if anchored is not None:
            _stderr(
                f"text layer covers all {page_count} page(s): deriving "
                f"{len(anchored)} boundary/boundaries deterministically "
                f"(0 model calls, no contact sheets rendered)"
            )
            result = _text_anchor_proposal(
                anchored, page_count=page_count, geometry=geometry
            )
            proposal = build_proposal_document(
                result, case_id=args.case_id, source_document_id=args.doc_id,
                source_file_name=(
                    bundle.get("source_file_name") or bundle.get("file_name")
                ),
                source_file_path=bundle["file_path"], page_count=page_count,
                created_at=now_iso(),
            )
            target = _write_proposal(
                args.case_id, args.doc_id, proposal, args.held_by, args.run_id
            )
            print(json.dumps({
                "status": "proposed",
                "proposal_path": str(target),
                "segment_count": len(proposal["segments"]),
                "unassigned_pages": [],
                "mode": "text_anchor",
                "model_calls": 0,
            }, ensure_ascii=False, indent=2))
            return 0

    # Reuse an already-rendered sheet set (sheets subcommand or a prior propose);
    # render the proposal variant if none exists. Only the as_scanned variant is
    # sent -- the model reads sideways cells in place (SEGMENT_PROMPT).
    batches = plan_sheets(page_count, geometry["pages_per_sheet"])
    sheet_paths = [
        out_dir / f"sheet_{i:02d}_p{pages[0]:03d}-{pages[-1]:03d}_{PROPOSAL_VARIANT}.png"
        for i, pages in enumerate(batches)
    ]
    if not all(p.exists() for p in sheet_paths):
        _stderr("rendering contact sheets (proposal variant)...")
        build_sheet_set(pdf_path, out_dir, geometry=geometry,
                        variants=((PROPOSAL_VARIANT, 0),), progress=_stderr)

    config = parse_provider_config(args)
    provider = build_provider(config)
    _stderr(f"proposing boundaries via {provider.provider_name}/{provider.model_name} "
            f"over {len(sheet_paths)} sheet(s)...")

    # Full-page render staging for the refine pass reuses the sheet dir, so the
    # per-page renders and verdict cache survive across runs like the sheets do.
    result = propose_boundaries(
        pdf_path, case_id=args.case_id, doc_id=args.doc_id, provider=provider,
        geometry=geometry, page_count=page_count, sheet_paths=sheet_paths,
        refine=args.refine, refine_threshold=args.refine_threshold,
        fallback_cap_ratio=args.fallback_cap_ratio,
        refine_scratch_dir=out_dir / "_fullpage",
        resume=not args.no_resume, prefer_text_anchor=False, progress=_stderr,
    )
    proposal = build_proposal_document(
        result, case_id=args.case_id, source_document_id=args.doc_id,
        source_file_name=bundle.get("source_file_name") or bundle.get("file_name"),
        source_file_path=bundle["file_path"], page_count=page_count,
        created_at=now_iso(),
    )
    target = _write_proposal(args.case_id, args.doc_id, proposal, args.held_by, args.run_id)
    failed_sheets = sum(1 for sheet in result["per_sheet"] if not sheet.get("ok"))
    partial = bool(failed_sheets or proposal["unassigned_pages"])
    out = {
        "status": "partial" if partial else "proposed",
        "proposal_path": str(target),
        "segment_count": len(proposal["segments"]),
        "unassigned_pages": proposal["unassigned_pages"],
        "failed_sheets": failed_sheets,
        "needs_full_page": result["needs_full_page"],
        "fallback_triggered": proposal["method"]["full_page_fallback"]["triggered"],
        "fallback_saturated": proposal["method"]["full_page_fallback"]["saturated"],
        "fallback_resolved_pages": proposal["method"]["full_page_fallback"].get(
            "resolved_pages", []
        ),
        "fallback_unresolved_pages": proposal["method"]["full_page_fallback"].get(
            "unresolved_pages", []
        ),
        "fallback_failure_reasons": proposal["method"]["full_page_fallback"].get(
            "failure_reasons", {}
        ),
        "warnings": proposal["warnings"],
    }
    if result.get("refinement"):
        out["refinement"] = {
            "long_segments_refined": len(result["refinement"]["refined_indices"]),
            "pages_examined": len(result["refinement"]["pages_examined"]),
            "new_boundaries": result["refinement"]["new_boundaries"],
            "unresolved_pages": result["refinement"]["unresolved_pages"],
            "failure_reasons": {
                str(page): reason
                for page, reason in result["refinement"]["failure_reasons"].items()
            },
            "vision_calls": result["refinement"]["calls"],
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if partial:
        _stderr(
            "Stage 1 halted with a partial proposal; unassigned pages or failed "
            "sheets must be resolved before downstream processing."
        )
        return 2
    return 0


def _cmd_show(args):
    proposal = _read_proposal(args.case_id, args.doc_id)
    if proposal is None:
        _stderr(f"error: no proposal for {args.case_id}/{args.doc_id}")
        return 1
    print(json.dumps(proposal, ensure_ascii=False, indent=2))
    return 0


def _cmd_approve(args):
    from dao import now_iso
    proposal = _read_proposal(args.case_id, args.doc_id)
    if proposal is None:
        _stderr(f"error: no proposal for {args.case_id}/{args.doc_id}")
        return 1
    edit = None
    if args.edit is not None:
        try:
            idx_str, span = args.edit.split("=")
            start_str, end_str = span.split("-")
            args.segment = int(idx_str)
            edit = (int(start_str), int(end_str))
        except ValueError:
            _stderr("error: --edit must look like N=start-end, e.g. 3=7-12")
            return 1
    updated = apply_approval(
        proposal, reviewer=args.reviewer, now=now_iso(),
        segment_index=args.segment, edit=edit,
    )
    _write_proposal(args.case_id, args.doc_id, updated, args.held_by or "segment_case.py",
                    args.run_id)
    print(json.dumps({"status": "approval_applied",
                      "review_status": updated["review_status"],
                      "not_ready": split_readiness_errors(updated)},
                     ensure_ascii=False, indent=2))
    return 0


def _cmd_split(args):
    proposal = _read_proposal(args.case_id, args.doc_id)
    if proposal is None:
        _stderr(f"error: no proposal for {args.case_id}/{args.doc_id}")
        return 1
    manifest, bundle = _manifest_bundle(args.case_id, args.doc_id)
    if bundle is None:
        _stderr(f"error: {args.doc_id} not in {args.case_id}'s manifest")
        return 1
    result = split_bundle(
        proposal, case_id=args.case_id, bundle_id=args.doc_id,
        bundle_pdf_path=ROOT / bundle["file_path"],
        proposal_path=f"outputs/{args.case_id}/{proposal_filename(args.doc_id)}",
        manifest=manifest, held_by=args.held_by, run_id=args.run_id, progress=_stderr,
    )
    updated_proposal = result.pop("updated_proposal", None)
    if updated_proposal is not None:
        _write_proposal(
            args.case_id, args.doc_id, updated_proposal,
            args.held_by, args.run_id,
        )
        result["assigned_document_ids"] = [
            segment["assigned_document_id"]
            for segment in updated_proposal["segments"]
        ]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("split", "already_split") else 1


def _parse_grid(grid: str) -> tuple[int, int]:
    try:
        cols, rows = grid.lower().split("x")
        return int(cols), int(rows)
    except ValueError:
        raise SegmentationError(f"--grid must look like COLSxROWS, e.g. 4x4, got {grid!r}")


def main(argv=None):
    import argparse
    from llm_providers import add_provider_args

    parser = argparse.ArgumentParser(
        description="Stage 1 document segmentation: split a raw bundle into logical documents."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _grid_and_crop(p):
        p.add_argument("--grid", default=f"{DEFAULT_GRID_COLS}x{DEFAULT_GRID_ROWS}",
                       help="contact-sheet grid, COLSxROWS (default 4x4)")
        p.add_argument("--crop-ratio", type=float, default=DEFAULT_CROP_RATIO,
                       help="fraction of each page height kept from the top (default 0.33)")

    p = sub.add_parser("sheets", help="render contact sheets and stop (no model call)")
    p.add_argument("case_id"); p.add_argument("doc_id")
    _grid_and_crop(p)
    p.set_defaults(fn=_cmd_sheets)

    p = sub.add_parser("propose", help="propose boundaries via a vision provider")
    p.add_argument("case_id"); p.add_argument("doc_id")
    _grid_and_crop(p)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--no-resume", action="store_true", help="ignore the per-sheet resume cache")
    p.add_argument("--no-text-anchor", action="store_true",
                   help="skip the deterministic text-layer path and always use vision, "
                        "even for a born-digital bundle (diagnostic/comparison only)")
    p.add_argument("--refine", action="store_true",
                   help="full-page re-examine long merged segments to recover over-merged "
                        "boundaries (measured recall 0.81 -> 0.96; costs one call per interior page)")
    p.add_argument("--refine-threshold", type=int, default=DEFAULT_LONG_SEGMENT_THRESHOLD,
                   help=f"minimum segment length to re-examine (default {DEFAULT_LONG_SEGMENT_THRESHOLD})")
    p.add_argument("--fallback-cap-ratio", type=float, default=DEFAULT_FALLBACK_CAP_RATIO,
                   help="fraction of the bundle allowed a full-page second look "
                        f"(default {DEFAULT_FALLBACK_CAP_RATIO}); raising it spends one "
                        "call per extra page, 1.0 removes the ceiling")
    add_provider_args(p)
    p.set_defaults(fn=_cmd_propose)

    p = sub.add_parser("show", help="print the current proposal")
    p.add_argument("case_id"); p.add_argument("doc_id")
    p.set_defaults(fn=_cmd_show)

    p = sub.add_parser("approve", help="approve the case gate, a segment, or an edited range")
    p.add_argument("case_id"); p.add_argument("doc_id")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--segment", type=int, help="approve only this segment index")
    p.add_argument("--edit", help="edit a range then approve it: N=start-end, e.g. 3=7-12")
    p.add_argument("--held-by"); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=_cmd_approve)

    p = sub.add_parser("split", help="split the approved bundle into per-document PDFs")
    p.add_argument("case_id"); p.add_argument("doc_id")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=_cmd_split)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except SegmentationError as exc:
        _stderr(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
