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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# tools/trace.py, not the stdlib `trace` module.
import trace as trace_mod

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
    "insurer_response", "legal_opinion", "legal_reference", "other",
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

# A MEDICAL bundle announces its documents by form name, the way a policy
# bundle does by 약관 title. The vocabulary is open (hospitals do not share one
# publisher's set), so the rule leans on shape rather than an exhaustive list: a
# short, bare line ending in a form-name suffix. Measured across the real corpus
# -- CASE_003/005/024/112/907 -- these are the endings that actually occur.
# `내역` rather than `내역서`: CASE_112 writes 진료비 내역서(외래) and CASE_907
# writes 진료비 세부산정내역(외래) for the same kind of document. A vocabulary
# taken from one bundle encodes that bundle's word ending, not the form family.
_MEDICAL_TITLE_RE = re.compile(
    r"(진단서|확인서|내역서?|명세서|소견서|의뢰서|처방전|기록지|기록|증명서|보고서"
    r"|영수증|계산서"
    # 영수증/계산서 were missing until 2026-08-18, and their absence did not
    # merely fail to name a document -- it withheld the repeated-title merge
    # rule entirely. `text_anchor_boundaries` reaches `key != previous_title`
    # only when a title was recognised; an unrecognised one falls through to
    # the judge, which is asked page by page and answers "new document" each
    # time. Measured on CASE_050 (97p): pages 60-90 are 31 consecutive
    # "( 외래 ) 진료비 계산서 · 영수증" pages that became 31 separate documents,
    # each costing a judge call, and the case reached claim_analysis with 56
    # documents whose single M1 call then timed out at 180s.
    r"|REPORT|SUMMARY)"
    r"\s*(?:\([^)]*\))?\s*(?:\d+\s*/\s*\d+)?"
    # A clinic stamps its own annotation beside the title ("원본대조필 인",
    # "사본"), so unlike a 약관 name the title does not always end its line.
    # Only a short bare token may follow -- a sentence continuing past the
    # title still fails, which is what keeps body prose out.
    r"(?:\s+[가-힣A-Za-z]{1,6}){0,3}\s*$"
)

# A form's field labels sit above its name (DOC_216 "병록 번호", DOC_217
# "등 록 번 호"). They must never be read as the title, and they end in words
# that are otherwise unremarkable, so they are excluded by name.
_MEDICAL_FIELD_LABEL_RE = re.compile(
    r"^(등록번호|병록번호|연번호|환자의성명|환자성명|성명|주민등록번호|주민번호"
    r"|환자의주소|진료과목|성별/나이|생년월일)\s*[:：]?\s*$"
)

# How far into a page a form title may sit and still be its heading. Real
# corpus: the statutory 별지 서식 header puts 진 단 서 on line 2 (CASE_112
# DOC_214, CASE_907 DOC_005), and four field labels put 수 술 기 록 on line 5
# (DOC_217). Past that a match is a mention inside body text, not a heading --
# the window is what keeps the open medical vocabulary from firing on every
# continuation page.
_MEDICAL_TITLE_SCAN_LINES = 5

# The longest a line can be and still be a form title rather than a sentence
# that happens to end in one. The longest real title measured is
# "후유장애 진단서(Mc Bride)" at 24 characters.
_MEDICAL_TITLE_MAX_CHARS = 40

# A repeated table header is read within the same window a form title is, since
# a titled first page carries both (title on line 1, columns on line 3-4).
#
# Widened from 6 to 10 on 2026-08-20. CASE_488/DOC_005's 진료비 세부산정내역
# prints a BLANK LINE between every header row -- title(0), blank, 요양기관(2),
# blank, patient block(4), blank, columns(6) -- so the real column row sat one
# line past a 6-line window. The widest-line tiebreak then settled on the
# patient block, whose cell count differs between pages (6 vs 7, an
# 의사면허번호 column moves up), the leading-run comparison failed, and a
# 7-page form split into 7 documents. Blank-separated layouts need the slack;
# _is_patient_block_line below is what keeps the wider window from matching two
# unrelated forms on their patient blocks instead.
_TABLE_HEADER_SCAN_LINES = 10
# Fewer cells than this is a prose line that happens to contain a separator.
_TABLE_HEADER_MIN_CELLS = 3
# How many LEADING columns must agree to call two headers the same table. The
# measured continuation pairs on CASE_047 agree on 8-9; the deliberate floor
# below that leaves room for an OCR miss in the run while staying far above the
# 0 scored at every real boundary. Set this to 1 and a two-column header would
# fuse unrelated tables.
_TABLE_HEADER_MIN_MATCH = 6

# A form viewer sometimes appends page metadata or an issuance marker on the
# same physical line as the printed title.  These are layout annotations, not
# part of the title, and removing only these bounded end markers leaves the
# length/prose guard below intact.  Measured on CASE_047 redacted children:
# DOC_020 ends "진료비 세부산정내역  Page 1 / 1" and DOC_021/DOC_022 end
# "진료비 계산서·영수증  [재발행]".
_TITLE_LAYOUT_SUFFIX_RE = re.compile(
    r"(?:\s+(?:Page\s+\d+\s*/\s*\d+|\[재발행\]))+\s*$",
    re.IGNORECASE,
)


# Title suffixes that NAME A FORM, mapped to the document_type they determine.
# Deliberately not exhaustive: a title earns an entry only when the form it
# names has exactly one type. Order matters -- the longest match wins, so
# 내역서 is tested before 서.
_TITLE_TYPE_SUFFIXES = (
    ("진단서", "diagnosis_certificate"),
    ("소견서", "diagnosis_certificate"),
    ("기록지", "medical_record"),
    ("기록", "medical_record"),
    ("의무기록", "medical_record"),
    ("내역서", "receipt"),
    ("내역", "receipt"),
    ("명세서", "receipt"),
    ("영수증", "receipt"),
)

# Titles that name a GENRE rather than a form. "REPORT" says a report exists,
# not which kind: CASE_909's p6/p7 are imaging readings, but the same heading
# sits on lab and pathology reports too, and the model reading the page can tell
# them apart. Mapping these would encode one bundle's coincidence as a rule.
_GENRE_ONLY_TITLES = frozenset({"report", "summary", "보고서", "결과지", "판독지"})


def document_type_from_title(title: str | None) -> str | None:
    """The document_type a printed form title determines, or None.

    The split already records the publisher's own title at precision 1.0000, so
    asking a model to re-read the page and name a type is a second opinion on
    evidence already held exactly. That only holds where the title names the
    FORM; None means "no mapping", never a type, and the caller classifies from
    content as before. Absence of a mapping is not a verdict.
    """
    if not title:
        return None
    collapsed = re.sub(r"[\s·ㆍ・]+", "", title)
    if not collapsed or len(collapsed) > _MEDICAL_TITLE_MAX_CHARS:
        return None
    if collapsed.lower() in _GENRE_ONLY_TITLES:
        return None
    # Only a real title maps -- a sentence that merely mentions a form does not.
    if medical_form_title(title) is None:
        return None
    # Strip a trailing parenthetical/ordinal so "진료비 내역서(외래)" matches on
    # 내역서 rather than on whatever the scope note ends with.
    stem = re.sub(r"\([^)]*\)\s*\d*$", "", collapsed).strip()
    for suffix, doc_type in sorted(_TITLE_TYPE_SUFFIXES, key=lambda x: -len(x[0])):
        if stem.endswith(suffix):
            return doc_type
    return None


def medical_form_title(line: str) -> str | None:
    """The line itself if it reads as a medical form's name, else None.

    Korean official forms letter-space their titles ("진 단 서",
    "입 · 퇴 원 확 인 서", "수 술 기 록"), which no substring rule can match, so
    the line is compared with all whitespace and interpuncts removed while the
    ORIGINAL is returned -- downstream records the publisher's own words.
    """
    if not line:
        return None
    # Retain the publisher's title but remove only known end-of-line viewer /
    # issuance annotations before evaluating its shape.  This is deliberately
    # not a general substring scan: body prose still reaches the same anchored
    # title rule and length limit as before.
    line = _TITLE_LAYOUT_SUFFIX_RE.sub("", line).strip()
    collapsed = re.sub(r"[\s·ㆍ・]+", "", line)
    # Length is judged on the collapsed form: a title and its stamp are often
    # separated by a wide run of spaces used as layout ("후유장애 진단서(Mc
    # Bride)" + 40 spaces + "원본대조필 인"), which says nothing about how much
    # text is on the line.
    if len(collapsed) > _MEDICAL_TITLE_MAX_CHARS:
        return None
    if not collapsed or _MEDICAL_FIELD_LABEL_RE.match(collapsed):
        return None
    # Two readings, because letter-spacing and word-spacing are the same
    # character. Collapsing everything makes "진 단 서" matchable but also glues
    # a stamp annotation onto the title ("진단서원본대조필인"); keeping the
    # spaces makes the stamp a separate token but leaves "진 단 서" unmatchable.
    # A line is a title if EITHER reading says so.
    if _MEDICAL_TITLE_RE.search(collapsed):
        return line
    squeezed = re.sub(r"[ \t·ㆍ・]+", " ", line).strip()
    if _MEDICAL_TITLE_RE.search(squeezed):
        return line
    # Letter-spaced title AND a stamp beside it ("진 단 서    사본"): neither
    # reading alone works -- collapsing glues the stamp on, squeezing leaves the
    # title unmatchable. Rejoin only runs of single characters, which is what
    # letter-spacing is, and leave real words as separate tokens.
    unspaced = re.sub(r"(?:(?<=\s)|^)((?:[가-힣] ){1,}[가-힣])(?=\s|$)",
                      lambda m: m.group(1).replace(" ", ""), squeezed)
    return line if _MEDICAL_TITLE_RE.search(unspaced) else None


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


@trace_mod.traced("segment.text_anchor", category="compute")
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

    return _boundaries_from_page_lines(pages)


def boundaries_from_page_texts(
    page_texts: list[str], *, medical: bool = False, judge=None,
    undecided: list[int] | None = None, judged: list | None = None,
) -> dict[int, str | None] | None:
    """The same boundary rule over page text a caller already has.

    text_anchor_boundaries() reads the PDF's embedded layer, which confines the
    deterministic path to born-digital bundles -- and this corpus is mostly
    scans (CASE_025's 110 pages and CASE_026's 59 carry zero embedded
    characters). Running OCR first gives a scan page text too, and the rule
    that turns text into boundaries does not care which reader produced it:
    measured on CASE_112's two policy bundles, boundaries derived from the OCR
    output are IDENTICAL to those from the embedded layer across all 323 pages.

    The intended input is REDACTED text. Segmentation must never read
    pre-redaction page text -- that is what dao.read-page-text guards behind
    checkpoint 2's one-shot capability -- and it does not need to: across
    CASE_112's 217 split children, the document title line survived redaction
    in every case, because redaction identifies PII values and substitutes only
    those spans, and a form's title is not PII.

    Returns None if any page is empty, matching the PDF path's fail-closed
    contract for a partially-readable bundle: a verdict blind to part of the
    document is worse than none, since the caller can fall back to vision only
    if it is told nothing was decided.
    """
    if not page_texts:
        return None
    lines = [
        _page_content_lines([line.strip() for line in text.splitlines() if line.strip()])
        for text in page_texts
    ]
    if medical != "auto":
        options = {"medical": medical, "judge": judge, "page_texts": page_texts}
        if judged is not None:
            options["judged"] = judged
        return _boundaries_from_page_lines(
            lines, **options)

    # `propose` runs BEFORE classification -- document_type is per-document and
    # cannot be known until after the split -- so the rule cannot be chosen by
    # type. Try both and keep the one that found something: the vocabularies do
    # not overlap, so the wrong rule reports nothing. Measured on real bundles:
    # CASE_909/DOC_005 (medical) gives 1 boundary under the policy rule and 10
    # under the medical one; CASE_112's DOC_003/DOC_004 (policy) give 84 and 89
    # under the policy rule and 1 each under the medical one.
    #
    # The results are NOT unioned. Merging would import the medical rule's
    # 5-line header window into policy text, where the strict first-line rule is
    # what measured precision 1.0000 across 173 boundaries.
    policy_options = {"page_texts": page_texts}
    if judged is not None:
        policy_options["judged"] = judged
    policy = _boundaries_from_page_lines(lines, **policy_options)

    # A 약관 bundle is settled HERE, before the medical pass runs at all.
    #
    # Two reasons, and the first is a correctness bug rather than a saving.
    # (a) The medical pass consults the judge, and a judge that declines a page
    #     splits it (the deliberate fail-safe direction). On a 145-page policy
    #     bundle that yields ONE BOUNDARY PER PAGE -- measured on CASE_134
    #     DOC_003: policy 84, medical-with-judge 145. `len(medical) > len(policy)`
    #     is then true, the medical result is returned, and a collapse placed
    #     after that comparison never runs. That is exactly how the first version
    #     of this fix passed a judge=None check and still split the bundle 85
    #     ways in the real run.
    # (b) Even when the policy result would have won, running the medical pass
    #     first pays its whole judge cost for a boundary set that is then
    #     discarded.
    #
    # This does NOT union the two rules -- the reason they are kept apart is
    # unchanged (the medical 5-line header window would cost the policy rule the
    # precision 1.0000 it measures across 173 boundaries). It only stops asking
    # the medical question about a document the policy rule has already
    # identified by the publisher's own 약관 titles.
    if policy is not None and _is_policy_bundle(policy):
        # A POLICY bundle is deliberately NOT split (pipeline.md / the
        # loss-adjustment-pipeline skill). Korean policy clauses print their
        # owning 약관 in the clause body, so downstream identification runs off
        # `clause_id` text, not the PDF a clause sits in -- CASE_021's four
        # policy matches distinguished 삼성화재/KB/한화 while all citing one
        # `document_id`. Splitting is pure cost, and `chunk_text` re-splits to
        # page granularity either way, so both arms deliver identical
        # downstream input.
        #
        # Measured, CASE_133 (RUN_20260812_133): the title rule found every
        # 약관 heading in two bundles -- 85 segments from 145p and 91 from 178p
        # -- which is the rule working exactly as designed, at precision
        # 1.0000. The defect was that nothing turned that correct boundary set
        # into the correct DOCUMENT decision. The split produced 174 policy
        # children (median 1 page, 98 of them single-page), cost 381
        # classification calls (1,819s) plus 344 judge calls (1,633s), and
        # `page_chunks.json` still held 367 chunks -- exactly the pre-split page
        # count. It also left every child unregistered in
        # `_revision_index.json` (children inherit their redacted text through
        # `_redistribute_parent_redaction`, which bypasses the
        # `write-redacted-text` path that registers a revision), which blocked
        # `policy_clause_processing` with 174 unregistered-revision errors.
        #
        # Collapsing in code rather than at the approval gate is deliberate: the
        # gate is a HUMAN, and `--auto-approve-segmentation` (documented for
        # timing/plumbing runs) skips exactly that judgement, so a rule enforced
        # only there silently fails on every automated run.
        if undecided is not None:
            undecided.clear()
        return {1: None}

    # Only the medical pass consults the judge, so only it can leave a page
    # undecided; `undecided` is threaded here rather than collected by a second
    # call so the flags describe the very pass that produced these boundaries.
    medical_options = {
        "medical": True, "judge": judge, "page_texts": page_texts,
        "undecided": undecided,
    }
    if judged is not None:
        medical_options["judged"] = judged
    medical_result = _boundaries_from_page_lines(lines, **medical_options)
    if policy is None or medical_result is None:
        # None is "no verdict, fall through to vision" and is not comparable to
        # a boundary count; if either reader declined, so does this.
        chosen = policy if medical_result is None else medical_result
        if chosen is policy and undecided is not None:
            undecided.clear()
        return chosen
    if len(medical_result) > len(policy):
        return medical_result
    # The POLICY result won, and it never consulted the judge -- so any pages
    # the medical pass could not settle describe boundaries that were then
    # discarded. Reporting them would flag pages for review that the returned
    # boundary set did not actually leave undecided.
    if undecided is not None:
        undecided.clear()
    return policy


# A policy bundle is recognised by ITS OWN boundary titles: the title rule
# already matched `...보통약관` / `...특별약관` / `...특약` on the pages it cut.
# Deriving the verdict from the boundaries this pass produced -- rather than
# from `document_type`, which does not exist yet -- is what makes this decidable
# at propose time. `propose` runs BEFORE classification (document_type is
# per-document and cannot be known until after the split), which is the
# chicken-and-egg that left this rule enforceable only by a human until now.
#
# Two boundaries, not two TITLES. Page 1 always opens a document and carries a
# null title on the policy path (line 1 IS the title, so recording it there
# would be redundant), so counting distinct title STRINGS undercounts every
# bundle by exactly one -- and a real 2-document bundle
# (보통약관 on p1 + 특별약관 on p3) would score 1 and never collapse. Caught by
# `test_a_policy_bundle_is_read_by_the_policy_rule`, which asserts {1, 3} on
# exactly that shape.
_POLICY_BUNDLE_MIN_BOUNDARIES = 2


def _is_policy_bundle(boundaries: dict[int, str | None]) -> bool:
    """Whether this boundary set describes a 약관 bundle that must stay whole.

    True when the policy rule cut beyond page 1 and at least one of those cuts
    is a 약관 title. A lone page-1 boundary is a single document, not a bundle,
    and collapsing it would change nothing anyway.

    Deliberately does NOT require every non-page-1 boundary to be a 약관 title.
    The policy path also opens a document on the first real page after a
    contents block (`_boundaries_from_page_lines`, the `toc[index - 1]` branch),
    and that page's line 1 is a cover title rather than a 약관 name. Measured on
    CASE_133/DOC_004: 89 boundaries, 88 of them 약관 titles and one -- p7,
    `영업배상책임보험` -- from exactly that TOC branch. Demanding unanimity scored
    that real bundle False and left it split.
    """
    if len(boundaries) < _POLICY_BUNDLE_MIN_BOUNDARIES:
        return False
    return any(
        title and DOCUMENT_TITLE_RE.search(title.strip())
        for page, title in boundaries.items() if page != 1
    )


def processed_boundaries_with_undecided(
    case_id: str, doc_id: str, page_count: int, judge=None,
    judged: list | None = None,
) -> tuple[dict[int, str | None] | None, list[int]]:
    """Boundaries AND the pages the judge could not settle, from ONE pass.

    These must come from the same pass. They used to be two independent calls
    (`processed_text_boundaries` then `processed_undecided_pages`), each
    re-asking the LLM tier about the same page pairs with no memo between them,
    which was wrong twice over:

      * it paid for every judged page TWICE, and
      * the two passes could disagree. A page judged in the first pass and
        failed in the second (or vice versa) produced boundaries from one set
        of verdicts and flags from the other, so `undecided_pages` did not
        describe the boundaries actually used.

    That is not hypothetical. On CASE_961 (cold Stage 2 run, 2026-08-12) nine
    consecutive judge calls failed inside ~0.05s each. The bundle split into 13
    segments instead of 11 -- p4/p5 and p18/p19 each over-split -- and the
    proposal recorded `undecided_pages: []`, so a boundary set produced by
    failed calls looked fully decided at the human approval gate. Over-splitting
    is the deliberately safe direction, but only because the gate can see it;
    silently, it just propagates a wrong document_type downstream.
    """
    if judge is None:
        return processed_text_boundaries(case_id, doc_id, page_count), []
    texts = _processed_page_texts(case_id, doc_id, page_count)
    if texts is None:
        return None, []
    collected: list[int] = []
    # Same entry point processed_text_boundaries uses, so this stays one
    # implementation of the boundary rules rather than a second copy that can
    # drift; `undecided` is threaded through to collect the flags from the very
    # pass that produced these boundaries.
    options = {"medical": "auto", "judge": judge, "undecided": collected}
    if judged is not None:
        options["judged"] = judged
    boundaries = boundaries_from_page_texts(texts, **options)
    return boundaries, sorted(set(collected))


def processed_undecided_pages(
    case_id: str, doc_id: str, page_count: int, judge=None
) -> list[int]:
    """Pages the LLM tier could not settle, for the same text the boundaries came
    from. Empty when no judge ran or no processed text exists.

    Kept for callers that want only the flags. Anything that also needs the
    boundaries must use `processed_boundaries_with_undecided` instead -- asking
    for both through two calls re-judges every page and lets the two answers
    disagree.
    """
    if judge is None:
        return []
    texts = _processed_page_texts(case_id, doc_id, page_count)
    if texts is None:
        return []
    return undecided_pages(texts, medical="auto", judge=judge)


@trace_mod.traced("segment.read_processed_text", category="io")
def _processed_page_texts(case_id: str, doc_id: str, page_count: int) -> list[str] | None:
    """A document's processed page text, redacted layer preferred. None if the
    text does not cover the whole document."""
    processed = ROOT / "data" / "processed" / case_id / doc_id
    redacted = processed / "redacted_text.md"
    if redacted.exists():
        try:
            pages = _split_page_markers(redacted.read_text(encoding="utf-8"))
        except OSError:
            return None
        return pages if len(pages) == page_count else None
    page_files = [processed / f"page_{n:03d}.md" for n in range(1, page_count + 1)]
    if not page_files or not all(p.exists() for p in page_files):
        return None
    try:
        return [p.read_text(encoding="utf-8") for p in page_files]
    except OSError:
        return None


class _LazyJudge:
    """Builds the provider on first use, so an unused judge costs nothing.

    The deterministic path's whole claim is that it constructs no provider and
    renders no sheet; passing an eagerly-built provider as the LLM tier's judge
    would quietly break that for every bundle, including the ones whose titles
    answer every page."""

    def __init__(self, factory):
        self._factory = factory
        self._provider = None

    def _resolve(self):
        if self._provider is None:
            self._provider = self._factory()
        return self._provider

    @property
    def provider_name(self):
        return self._resolve().provider_name

    @property
    def model_name(self):
        return self._resolve().model_name

    def classify_document(self, prompt, prompt_version):
        return self._resolve().classify_document(prompt, prompt_version)


def processed_text_boundaries(
    case_id: str, doc_id: str, page_count: int, judge=None
) -> dict[int, str | None] | None:
    """Boundaries from a bundle's already-processed text, or None if absent.

    Prefers `redacted_text.md` over the raw `page_NNN.md` files. Raw page text
    still carries claimant PII -- dao.read-page-text guards it behind
    checkpoint 2's one-shot capability precisely so no analysis stage reads it
    -- and preferring the redacted layer costs nothing: across CASE_112's 217
    split children the document title line survived redaction in every case.

    Returns None when the text does not cover the whole bundle, so the caller
    falls back to the PDF layer and then to vision. Partial coverage is exactly
    the state where a deterministic verdict would be silently blind to part of
    the document.
    """
    texts = _processed_page_texts(case_id, doc_id, page_count)
    if texts is None:
        return None
    return boundaries_from_page_texts(texts, medical="auto", judge=judge)


def _split_page_markers(text: str) -> list[str]:
    """Split combined redacted text on the `<<<PAGE page=N>>>` markers.

    Checkpoint 2 embeds these so page boundaries stay recoverable from the
    single combined file; chunk_text.py relies on the same markers."""
    pages: list[str] = []
    current: list[str] = []
    started = False
    for line in text.splitlines():
        if line.startswith("<<<PAGE"):
            if started:
                pages.append("\n".join(current))
            current = []
            started = True
            continue
        current.append(line)
    if started:
        pages.append("\n".join(current))
    return pages


def undecided_pages(
    page_texts: list[str], *, medical: bool = False, judge=None
) -> list[int]:
    """Pages whose boundary the judge was asked about and could not answer.

    These were split (the fail-safe direction) but not decided, so the human
    approval gate should see them: unlike a title match, there is no evidence
    on the page a reviewer can check. Empty when no judge ran.
    """
    collected: list[int] = []
    _boundaries_from_page_lines([
        _page_content_lines([line.strip() for line in text.splitlines() if line.strip()])
        for text in page_texts
    ], medical=medical, judge=judge, page_texts=page_texts, undecided=collected)
    return collected


# How many boundary judgements to keep in flight. Each is an independent
# (page N-1, page N) comparison against the provider, so the ceiling is the
# provider's own concurrency, not ours -- `llm_providers` already holds a global
# in-flight semaphore, and this pool queues behind it rather than around it.
# Measured cause, CASE_701: 28 judgements ran strictly back-to-back across three
# bundles (segment.judge self-time equalled its own wall span on every one --
# 48.8/48.8, 77.0/77.0, 29.4/29.4), spending 169s of that stage's 411s waiting
# on calls that never needed to wait for each other.
_JUDGE_WORKERS = 8


def _pages_needing_judgement(
    pages: list[list[str]], toc: list[bool], *, medical: bool,
) -> list[int]:
    """Indexes whose boundary the deterministic rules cannot settle.

    Safe to compute before the boundary loop runs because every gate on the
    path to the judge reads page text and `toc` only. The one loop-carried
    value, `previous_title`, is read solely by the titled-page branch -- which
    always `continue`s before reaching the judge -- so no verdict can change
    which pages appear here. That is what makes prefetching legitimate rather
    than a race: this returns the same list the sequential loop would ask about,
    in the same order.

    Non-medical bundles never reach the judge, so the answer there is empty.
    """
    if not medical:
        return []
    needed: list[int] = []
    for index, lines in enumerate(pages):
        if index == 0 or toc[index]:
            continue
        title = _medical_header_title(lines)
        if title is not None and _title_key(title) is not None:
            continue
        if title is None and _continues_table(
            _table_header_cells(pages[index - 1]), _table_header_cells(lines)
        ):
            continue
        needed.append(index)
    return needed


def _prefetch_judgements(
    indexes: list[int], page_texts: list[str], judge,
) -> dict[int, dict | None]:
    """Fetch every boundary verdict up front, concurrently.

    Returns index -> verdict, with None kept for the unusable ones so the
    caller's fail-toward-splitting branch behaves exactly as it did serially.
    `_judge_boundary` already converts every failure mode to None and records
    provider errors on the judge, so nothing is swallowed by running it here.
    """
    if not indexes:
        return {}
    if len(indexes) == 1:
        return {indexes[0]: _judge_boundary(
            page_texts[indexes[0] - 1], page_texts[indexes[0]], judge)}

    verdicts: dict[int, dict | None] = {}
    workers = min(_JUDGE_WORKERS, len(indexes))
    with trace_mod.span("pool.judge", category="compute",
                        worker_count=workers, items=len(indexes)):
        # run_in_context: concurrent.futures does not carry contextvars into
        # workers, so a raw submit would orphan every segment.judge span at
        # parent None -- and this pool is now the stage's largest single cost,
        # exactly the part a trace must be able to explain.
        submit = trace_mod.run_in_context(_judge_boundary)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(submit, page_texts[i - 1], page_texts[i], judge): i
                for i in indexes
            }
            for future, index in futures.items():
                # _judge_boundary catches provider failures itself; anything
                # escaping it is a real bug and must not be turned into a
                # silent "undecided", which would look like an ambiguous page.
                verdicts[index] = future.result()
    return verdicts


def _boundaries_from_page_lines(
    pages: list[list[str]], *, medical: bool = False, judge=None,
    page_texts: list[str] | None = None, undecided: list[int] | None = None,
    judged: list | None = None,
) -> dict[int, str | None] | None:
    """Boundary set for pages already reduced to their content lines.

    `medical` swaps the policy rule (a 약관 title, strictly on line 1) for the
    medical one (a form name within the header window). They are kept separate
    rather than unioned: the policy rule measured precision 1.0000 across 173
    boundaries BECAUSE it is narrow, and the medical vocabulary is open, so
    applying the wider rule to 약관 text would spend that precision for nothing.
    """
    # A bundle with any empty-text page is not fully readable; mixing a
    # deterministic verdict with pages we cannot read would produce boundaries
    # that are silently blind to part of the document.
    if not pages or not all(pages):
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
    previous_title: str | None = None
    if undecided is None:
        undecided = []
    if page_texts is None:
        page_texts = ["\n".join(lines) for lines in pages]
    if medical:
        # Page 1 starts a document by position, but its title is still worth
        # recording -- unlike the policy path, where line 1 IS the title and a
        # null on page 1 loses nothing.
        boundaries[1] = _medical_header_title(pages[0])
        previous_title = _title_key(boundaries[1])
    # Every verdict fetched before the loop starts, so the calls overlap. The
    # loop below stays sequential and reads them from this map -- `previous_title`
    # is still threaded one page at a time, and takes the same values it would
    # have taken had each call blocked in place.
    prefetched = (
        _prefetch_judgements(
            _pages_needing_judgement(pages, toc, medical=bool(medical)),
            page_texts, judge)
        if judge is not None else {}
    )
    for index, lines in enumerate(pages):
        page = index + 1
        if page == 1 or toc[index]:
            continue
        if medical:
            title = _medical_header_title(lines)
            key = _title_key(title)
            if title is not None and key is not None:
                # A repeated title alone does not settle this: CASE_907's
                # 7-page statement reprints its title as a running header,
                # while CASE_047's three statements each reprint theirs. The
                # patient block is what separates them -- stated once per
                # document, so a page carrying title AND patient block is a
                # reissue, and a page carrying only the title continues.
                # 영수증/계산서 are excluded by operator decision: a run of
                # outpatient receipts is kept as ONE document even though each
                # is separately issued and each reprints the patient block.
                # ... unless the numbered item run CONTINUES across the page.
                # Measured on CASE_488/DOC_005 p9-15: a 7-page
                # 진료비 세부산정내역 reprints its patient block on every page,
                # so the block test above read all seven as reissues and
                # produced seven documents. Its item numbers run 01 -> 06 -> 12
                # -> 16 across those pages, because the statement is one billing
                # run continuing.
                #
                # A re-ISSUED statement restarts at 01, which is exactly what
                # separates this from CASE_047's three same-titled statements
                # (pages 16/23/30, each opening with 01.진찰료). Merging those
                # broke claim_analysis, so the distinction must be the numbering
                # rather than the header -- both cases reprint the same columns.
                if key != previous_title or (
                    _reprints_patient_block(lines)
                    and not any(word in key for word in _MERGED_FORM_TITLES)
                    and not _continues_item_numbering(pages[index - 1], lines)
                ):
                    boundaries[page] = title
                previous_title = key
                continue
            # A titleless page that reprints the previous page's table header is
            # that table running over, and the header is evidence the judge would
            # be shown anyway. Settled here so a multi-page 진료비 세부내역서
            # costs no calls: on CASE_047 DOC_001 this is 15 of the 34 judged
            # pages. Only for a page with NO title -- a page carrying its own
            # form name has already been decided above, and a generic heading
            # (REPORT) still goes to the judge, since two studies printed with
            # the same table are still two documents.
            if title is None and _continues_table(
                _table_header_cells(pages[index - 1]), _table_header_cells(lines)
            ):
                continue
            # Reaches the judge only when the rule genuinely cannot decide: a
            # generic form-KIND heading that says nothing about WHICH document
            # this is, or a page with no recognisable title. The second is not
            # automatically a continuation -- CASE_907 DOC_005 p18 opens a court
            # compensation table with no medical form name -- but most untitled
            # pages are, so the judge is what separates them. Measured on that
            # 19-page bundle: 3 untitled pages (p5, p18, p19) plus 2 generic
            # headings, so the deterministic rule still answers most of it.
            if judge is None:
                # Without a judge, a generic title still starts a document (a
                # repeat of it is no evidence of a reprint) and an untitled page
                # still continues -- the measured behaviour, unchanged.
                if title is not None:
                    boundaries[page] = title
                continue
            if judged is not None:
                judged.append(page)
            # Already fetched above. The fallback call covers a caller that
            # reached this page outside the prefetch's view; it cannot fire on
            # the normal path, where the two agree on the page set by
            # construction, but a silent extra call is cheaper than a KeyError.
            if index in prefetched:
                verdict = prefetched[index]
            else:
                verdict = _judge_boundary(
                    page_texts[index - 1], page_texts[index], judge)
            if verdict is None:
                # Fail toward splitting. Over-splitting is undone by a human
                # merging two segments at the approval gate; over-merging fuses
                # two documents into one document_type and propagates
                # downstream. Recorded as undecided so the gate knows this
                # boundary was not actually decided.
                boundaries[page] = title
                undecided.append(page)
            elif verdict["starts_new_document"]:
                boundaries[page] = verdict.get("title") or title
                previous_title = _title_key(boundaries[page])
            continue
        if toc[index - 1]:
            # First real page after the contents block always starts a document.
            boundaries[page] = lines[0]
        elif DOCUMENT_TITLE_RE.search(lines[0]):
            boundaries[page] = lines[0]
    return boundaries


# Titles that name a form's KIND rather than the document itself. A repeat of
# one is no evidence of a reprint: CASE_907/CASE_112 DOC_005 p6 and p7 are both
# headed "REPORT" but read different studies (MR HAND vs Right Wrist AP), each
# with its own patient block and Conclusion, and the human baseline records two
# documents. Merging on such a title turned a real boundary into a continuation
# on both bundles measured. Deliberately a short list of bare form-kind words --
# deciding this in general is the LLM tier's job, and this is the floor under it.
_GENERIC_FORM_TITLES = frozenset({"report", "summary", "결과지", "판독지", "기록지"})

# Field labels that make up a form's PATIENT BLOCK -- the identity header a
# statement prints under its title. Used to tell a reissued form from the same
# form running over, because the title alone cannot: both repeat it.
#
# Two real bundles disagree on what a repeated title means, so no title list
# could settle this. CASE_907 DOC_005 p9-15 is ONE 7-page 진료비 세부산정내역
# whose title is a running page header (the human baseline records a single
# document; treating each reprint as a start scored precision 0.6250).
# CASE_047 p16/23/30 are THREE separate 진료비 세부내역서. What separates them
# is that a reissued form reprints its patient block under the title, while a
# continuation page goes straight from the title to the column header -- the
# patient identity is stated once per document, not once per page.
#
# Measured consequence of getting it wrong (CASE_047, 2026-08-18): merging the
# three fused them into one 21-page DOC_012, and claim_analysis failed on it --
# "quote is not present on page 3 -- that text appears on page(s) 10, 17" --
# because the same header and item names recur at three offsets inside the
# fused document. The over-merge cost a 591.8s M1 call that produced nothing.
_PATIENT_BLOCK_LABELS = ("등록번호", "환자성명", "환자 성명", "환자등록번호",
                         "환자구분", "진료기간")
# How many of those labels must appear for a line to BE a patient block rather
# than a table column that happens to be named one of them.
_PATIENT_BLOCK_MIN_LABELS = 2

# Forms kept as ONE document across a run of separately-issued copies, by
# operator decision rather than by evidence: a case can carry dozens of
# outpatient receipts and downstream wants them as one 진료비 record, not
# thirty. They reprint the patient block like any reissued form, so they are
# named here rather than detected.
_MERGED_FORM_TITLES = ("영수증", "계산서")

BOUNDARY_JUDGE_PROMPT_VERSION = "boundary_judge_v0.1"

BOUNDARY_JUDGE_PROMPT = """You are deciding whether one page of a scanned Korean insurance-claim
document bundle STARTS A NEW DOCUMENT, or CONTINUES the one before it.

A bundle concatenates separate documents (진단서, 검사 판독지, 진료비 명세서,
법원 기준표 …). A new document normally opens with its own form title and its
own header block (patient/registration fields, a fresh table). A continuation
page carries on the previous page's content -- a table running over, a numbered
list continuing -- and often repeats the same running header.

Two cases matter most, because they are why the deterministic rule could not
decide this page:
- The two pages may share a generic heading (e.g. "REPORT") while being
  different documents: separate studies, each with its own patient block and
  its own conclusion. Same heading is NOT evidence of continuation.
- A page may carry no recognisable form title at all and still start a new
  document.

Reply with ONLY a JSON object, no other text, in exactly this shape:
{{"starts_new_document": <true|false>, "confidence": <0-1>,
  "title": "<the document's own title if this page starts one, else null>"}}

--- Previous page ---
{previous}

--- Page being judged ---
{current}
"""


_JUDGE_FAILURE_ATTR = "_harness_judge_failures"


def _record_judge_failure(judge, exc: BaseException) -> None:
    """Attach a provider failure to the judge object itself.

    On the judge rather than in a module global because the failures belong to
    ONE proposal run: a module-level list would accumulate across runs in a
    long-lived process and would need clearing at exactly the right moment,
    which is a second thing to get wrong. Best-effort -- a judge that rejects
    attribute assignment must not turn a swallowed provider error into a
    crash, since the caller's fail-toward-splitting behaviour is still correct
    without the diagnostic.
    """
    try:
        failures = getattr(judge, _JUDGE_FAILURE_ATTR, None)
        if failures is None:
            failures = []
            setattr(judge, _JUDGE_FAILURE_ATTR, failures)
        failures.append(f"{type(exc).__name__}: {str(exc)[:200]}")
    except Exception:
        pass


def judge_failures(judge) -> list[str]:
    """Provider failures recorded against this judge, newest last."""
    return list(getattr(judge, _JUDGE_FAILURE_ATTR, ()) or ())


def _judge_boundary(previous_text: str, current_text: str, judge) -> dict | None:
    """Ask the model whether `current_text` starts a document. None if unusable.

    None covers every way the answer can fail to be an answer -- provider
    error, unparseable text, missing field, wrong type. The caller treats all
    of them identically (split and flag), so distinguishing them here would be
    a distinction nothing acts on.
    """
    prompt = BOUNDARY_JUDGE_PROMPT.format(
        previous=previous_text[:_JUDGE_TEXT_LIMIT],
        current=current_text[:_JUDGE_TEXT_LIMIT],
    )
    # Spanned so a retained trace records each model-backed boundary decision.
    # The caller also appends the page number before this call, keeping the
    # proposal's `model_calls` truthful even when a later rule result is not
    # chosen as the boundary set.
    # Scoped to the call alone -- the parsing below is free, and widening the
    # span would only blur where the time actually goes.
    try:
        with trace_mod.span("segment.judge", category="compute"):
            result = judge.classify_document(prompt, BOUNDARY_JUDGE_PROMPT_VERSION)
    except Exception as exc:
        # A provider failure is NOT the same as "the model considered it and
        # could not say". Both split and both get flagged -- the caller treats
        # them identically, which is correct -- but a run where the CLI failed
        # 9 times in a row inside 0.05s each is a broken run, not an ambiguous
        # bundle, and nothing said so: CASE_961 recorded 13 segments instead of
        # 11 with no visible sign the calls had failed at all. Recording it on
        # the judge lets `propose` report the difference.
        _record_judge_failure(judge, exc)
        return None
    raw = (getattr(result, "text", "") or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not isinstance(
            parsed.get("starts_new_document"), bool):
        return None
    return parsed


# How much of a page to show the judge. A form's identity is established by its
# header and the shape of what follows; sending a whole 3000-character billing
# table adds cost, not signal.
_JUDGE_TEXT_LIMIT = 1200


def _title_key(title: str | None) -> str | None:
    """Comparison form for deciding whether two pages carry the SAME title.

    Whitespace is ignored: OCR reads the same printed header as "진료비
    세부산정내역 (퇴원)" on one page and "진료비 세부산정내역(퇴원)" on the next,
    and a reprint differing by a space is still a reprint.

    Returns None for a generic form-kind word, which makes it un-mergeable --
    None never equals the previous key, so such a page always starts a document.
    """
    if title is None:
        return None
    collapsed = re.sub(r"\s+", "", title)
    if collapsed.lower() in _GENERIC_FORM_TITLES:
        return None
    return collapsed


def _reprints_patient_block(lines: list[str]) -> bool:
    """Whether this page restates the patient identity under its title.

    A form states who it is about once per DOCUMENT. A page that repeats the
    title AND the patient block is therefore a reissue of the form; one that
    repeats only the title is the same document continuing onto another page.
    Scanned within the title window, and requiring two labels so a table column
    named 진료기간 cannot pass for the block.
    """
    for line in lines[1:_MEDICAL_TITLE_SCAN_LINES]:
        collapsed = re.sub(r"\s+", "", line)
        hits = sum(1 for label in _PATIENT_BLOCK_LABELS
                   if re.sub(r"\s+", "", label) in collapsed)
        if hits >= _PATIENT_BLOCK_MIN_LABELS:
            return True
    return False


_ITEM_NUMBER_RE = re.compile(r"^\s*(\d{2})\s*\.\s*\S")


def _numbered_items(lines: list[str]) -> list[int]:
    """The leading "NN." item numbers a billing page prints, in order."""
    found = []
    for line in lines:
        match = _ITEM_NUMBER_RE.match(line)
        if match is not None:
            found.append(int(match.group(1)))
    return found


def _continues_item_numbering(previous: list[str], current: list[str]) -> bool:
    """Whether `current` resumes `previous`'s numbered item run.

    A 진료비 세부산정내역 numbers its sections (01.진찰료, 02.입원료,
    06.비급여주사료 …) and the run continues across the pages of ONE statement.
    Measured on CASE_488/DOC_005: pages 9-15 read 01 -> 06 -> 12 -> 16, while
    CASE_047's three separately-issued statements each restart at 01.

    Compared as "does not go backwards" rather than "is strictly higher",
    because one numbered section routinely spans a page break: CASE_488's
    boundaries read 06 -> 06 and 12 -> 12, the same section continuing. A
    reissue goes BACKWARDS (…20 -> 04), and a second statement under the same
    title restarts at 01 -- both of which still split, which is what keeps
    CASE_047 pages 16/23/30 three documents after the repeated-title merge
    fused them into one and broke claim_analysis.

    A page with no numbered items at all (a continuation whose rows are all
    dated sub-entries) carries no evidence either way and does not merge; the
    patient-block rule above decides it.
    """
    before, after = _numbered_items(previous), _numbered_items(current)
    if not before or not after:
        return False
    if after[0] > before[-1]:
        return True
    # Equal numbers are the ambiguous case: one section spanning the break
    # (CASE_488 p9->p10 reads 06 -> 06) versus a reissue whose first section
    # simply repeats (CASE_047's statements each print 01 alone). They are told
    # apart by whether the PREVIOUS page actually advanced -- a page that ran
    # 01…06 is mid-statement, while one that only ever showed 01 is a fresh
    # form's opening page.
    return after[0] == before[-1] and before[-1] > before[0]


def _is_patient_block_line(cells: list[str]) -> bool:
    """Whether a delimiter-split line is the patient block, not a column row.

    The block ("환자등록번호  환자성명  진료기간  병실  환자구분") is delimited
    exactly like the column row above the table, so widening the header window
    to reach forms that blank-line-separate their rows would otherwise let the
    block win the widest-line tiebreak. It is told apart by vocabulary, using
    the same labels and threshold `_reprints_patient_block` already applies --
    a table column merely NAMED 진료기간 stays below the two-label floor.
    """
    hits = sum(1 for cell in cells
               if any(re.sub(r"\s+", "", label) == cell
                      for label in _PATIENT_BLOCK_LABELS))
    return hits >= _PATIENT_BLOCK_MIN_LABELS


def _table_header_cells(lines: list[str]) -> list[str]:
    """The column-name row in a page's header window, as normalised cells.

    A tabular form (진료비 세부내역서, 세부산정내역) prints its column names on
    the FIRST page only; every continuation page repeats just the header and
    resumes the rows. Such a page has no form title, so the title rule cannot
    decide it and it would otherwise cost a judge call each.

    Identified structurally, not by vocabulary: three or more delimiter-split
    cells, none of which begins with a digit. The digit test is what separates
    the header from the data rows below it -- a data row starts with a date, an
    amount or a numbered item code ("01.진찰료 | 2025-10-16 | ...").
    """
    best: list[str] = []
    for line in lines[:_TABLE_HEADER_SCAN_LINES]:
        cells = [re.sub(r"\s+", "", cell)
                 for cell in re.split(r"[|\t]+|\s{2,}", line) if cell.strip()]
        if len(cells) < _TABLE_HEADER_MIN_CELLS or any(
            re.match(r"^[\d,./-]", cell) for cell in cells
        ):
            continue
        if _is_patient_block_line(cells):
            continue
        # The patient block above the table is delimited the same way
        # ("등록번호  환자성명  진료기간"), so the first qualifying line is not
        # necessarily the column row -- take the WIDEST one in the window
        # instead. Data rows are already excluded by the digit test above, so
        # the widest survivor is the header rather than a row of values.
        if len(cells) > len(best):
            best = cells
    return best


def _continues_table(previous: list[str], current: list[str]) -> bool:
    """Whether `current`'s table header is a reprint of `previous`'s.

    Compared as a leading-cell run rather than for equality, because OCR reads
    the same printed header differently on every page. Measured on CASE_047
    DOC_001 (97p): the 진료비 세부내역서 runs agree on 8-9 leading columns
    (항목·일자·코드·명칭·단가·수량·횟수·일수 ...) while diverging in the
    merged 금액 columns to their right -- one page reads
    "가산후총액 | 본인부담금 | 공단부담금", the next
    "가산총액\\t금액 분류부담금\\t...". Requiring equality would have matched
    none of the 15 continuation pairs; the leading run matched every one, and
    scored 0 at all three real document boundaries (p22->23, p29->30, p36->37).
    """
    if len(previous) < _TABLE_HEADER_MIN_MATCH or len(current) < _TABLE_HEADER_MIN_MATCH:
        return False
    return _leading_run_match(previous, current) >= _TABLE_HEADER_MIN_MATCH


def _leading_run_match(previous: list[str], current: list[str]) -> int:
    """Length of the leading run two headers agree on, tolerating a JOINED cell.

    Positional equality is not enough because a transcription may join two
    adjacent columns into one cell. Measured on CASE_488/DOC_005: p9 reads
    "금액 | 횟수 일수 | 총액" while p10 reads "금액 | 횟수 | 일수 | 총액" --
    the same printed header, split differently. Strict comparison stopped at 5
    (floor 6) and separated a page from its own continuation.

    A join is accepted only while the concatenation spells the SAME run, so two
    unrelated headers still diverge on their first differing cell and score
    below the floor. Counted as the number of printed columns consumed, so a
    joined pair counts once on each side.
    """
    i = j = matched = 0
    while i < len(previous) and j < len(current):
        before, after = previous[i], current[j]
        if before == after:
            i, j, matched = i + 1, j + 1, matched + 1
            continue
        # one side joined what the other split: consume cells from the shorter
        # side until it spells the longer one
        if before.startswith(after):
            joined, parts, k = before, after, j + 1
            while k < len(current) and len(parts) < len(joined):
                parts += current[k]
                k += 1
            if parts == joined:
                i, j, matched = i + 1, k, matched + 1
                continue
        elif after.startswith(before):
            joined, parts, k = after, before, i + 1
            while k < len(previous) and len(parts) < len(joined):
                parts += previous[k]
                k += 1
            if parts == joined:
                i, j, matched = k, j + 1, matched + 1
                continue
        break
    return matched


def _medical_header_title(lines: list[str]) -> str | None:
    """The form name in a page's header window, or None if it has none."""
    for line in lines[:_MEDICAL_TITLE_SCAN_LINES]:
        title = medical_form_title(line)
        if title is not None:
            return title
    return None


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
            # Says "text layer", not "embedded text layer": the same rule now
            # runs over the PDF's own layer, raw OCR output, or redacted
            # processed text, and all three were measured to produce the
            # identical boundary set. Naming one source would be wrong for two.
            "boundary_evidence": (
                "page 1 of the bundle"
                if start == 1
                else f"text layer: page begins with the title line {title!r}"
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
    redaction_redistributed: bool = False,
    child_files_written: bool = True,
) -> list[dict]:
    """Builds document_manifest.json entries for approved segments.

    Numbering continues from start_index rather than reusing the bundle's own id:
    the bundle entry survives as a superseded record, so its id stays taken.

    Sets only fields this stage owns. In particular `document_type` stays null
    even though a provisional guess exists -- checkpoint 1 owns that field and
    must classify against real OCR'd text, not a cropped thumbnail. That
    separation is why `provisional_document_type` is a distinct field rather than
    an early write to the real one.

    `child_files_written=False` records a child that has no PDF of its own
    because it inherited the parent's already-processed pages. It stays
    `document_role: physical` -- it IS a range of a real physical file, named by
    source_file_name + source_page_start/end -- and only file_path/
    file_size_bytes go null. It is deliberately NOT modelled as
    `document_role: segment`, whose contract is a DAO-issued page_map verified
    against the parent PDF page by page; that verification exists for text
    newly extracted from a parent, whose claimed page mapping has no other
    witness, and it cannot be satisfied by a scan at all (see
    segment_derivation.py: an ocr_segment gets no receipt because UID
    verification may not re-run OCR).
    """
    entries = []
    for offset, segment in enumerate(segments):
        doc_id = f"DOC_{start_index + offset:03d}"
        file_name = f"{doc_id}.pdf"
        entries.append({
            "document_id": doc_id,
            "file_name": file_name,
            # A range of a real physical file either way. When a child PDF was
            # written this points at it; when the child inherited the parent's
            # pages there is no file to point at, and the page range below is
            # what identifies it. Not a processed-text `segment` -- see the
            # docstring for why that contract does not apply here.
            "document_role": "physical",
            # Forward slashes regardless of host OS: the schema pattern requires
            # them and the value is compared against paths built elsewhere.
            "file_path": (f"data/raw/{case_id}/{file_name}"
                          if child_files_written else None),
            "file_format": "pdf",
            "file_size_bytes": ((file_sizes or {}).get(segment["page_start"], 0)
                                if child_files_written else None),
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
            # Set when the bundle was redacted before the split and its text
            # was cut into per-child files: downstream stages (chunking above
            # all) locate a document's redacted text through this field, so a
            # child that owns the file but not the path is invisible to them.
            "redacted_text_path": (
                f"data/processed/{case_id}/{doc_id}/redacted_text.md"
                if redaction_redistributed else None),
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
        # Processed text first, then the PDF's own layer. A scan has no embedded
        # text, so text_anchor_boundaries returns None for it and the whole
        # corpus of scanned bundles used to fall through to vision -- but once
        # checkpoint 1 has read the bundle, that same scan HAS page text, and
        # the boundary rule does not care which reader produced it (CASE_112:
        # embedded, raw OCR and redacted text all yield the identical 173
        # boundaries across 323 pages).
        # The provider doubles as the LLM tier's judge: it decides the pages
        # the title rules cannot settle (a generic heading like REPORT, or no
        # title at all), reading the page text rather than a contact sheet.
        anchored, anchored_undecided = processed_boundaries_with_undecided(
            case_id, doc_id, page_count, judge=provider)
        if anchored is not None and progress:
            progress(
                f"processed text covers all {page_count} page(s): deriving "
                f"{len(anchored)} boundary/boundaries deterministically "
                f"(0 model calls, vision path skipped)"
            )
        if anchored is None:
            anchored = text_anchor_boundaries(pdf_path, page_count)
            if anchored is not None and progress:
                progress(
                    f"text layer covers all {page_count} page(s): deriving "
                    f"{len(anchored)} boundary/boundaries deterministically "
                    f"(0 model calls, vision path skipped)"
                )
        if anchored is not None:
            return _text_anchor_proposal(
                anchored, page_count=page_count, geometry=geometry,
                undecided=anchored_undecided,
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


def propose_from_page_texts(
    page_texts: list[str], *, medical="auto", judge=None, geometry: dict | None = None
) -> dict:
    """A deterministic proposal built from page text, with uncertainty recorded.

    The single place that pairs the boundary set with the pages the LLM tier
    could not settle, so a caller cannot get one without the other -- which is
    how `undecided_pages` ended up computed but never surfaced.
    """
    geometry = geometry or compute_sheet_geometry()
    boundaries = boundaries_from_page_texts(page_texts, medical=medical, judge=judge)
    if boundaries is None:
        return {}
    undecided = (undecided_pages(page_texts, medical=medical, judge=judge)
                 if judge is not None else [])
    return _text_anchor_proposal(
        boundaries, page_count=len(page_texts), geometry=geometry, undecided=undecided)


def _text_anchor_proposal(
    boundaries: set[int], *, page_count: int, geometry: dict,
    undecided: list[int] | None = None,
) -> dict:
    """The propose_boundaries return shape for a deterministic, no-model run.

    Mirrors the vision path's contract exactly so build_proposal_document and
    _write_proposal need no branch. The model-specific fields are null/empty
    because no model ran on the SHEETS -- that absence is the honest record,
    not a gap: the schema's `text_anchor` mode says so explicitly.

    `undecided` names pages the LLM tier was asked about and could not answer.
    Those boundaries split (the fail-safe direction) but carry no evidence a
    reviewer can check on the page, unlike a title match, so they are surfaced
    through needs_full_page -- which already means "this segment's boundary
    needed more than the default look" on the vision path. Reusing it keeps one
    flag for one meaning instead of two fields a reviewer has to learn."""
    undecided = set(undecided or ())
    return {
        "segments": [
            {**seg, "needs_full_page": seg["page_start"] in undecided}
            for seg in segments_from_boundaries(boundaries, page_count)
        ],
        "unassigned_pages": [],
        "needs_full_page": sorted(undecided),
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
    unresolved = proposal.get("needs_full_page") or []
    fallback = (proposal.get("method") or {}).get("full_page_fallback") or {}
    if fallback.get("saturated"):
        errors.append(
            "full-page fallback is saturated; retune the crop/grid and create a "
            "new proposal before splitting"
        )
    if unresolved:
        errors.append(
            f"{len(unresolved)} page(s) still need a full-page review before "
            f"splitting: {unresolved[:20]}{'...' if len(unresolved) > 20 else ''}"
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
            # Keep the parent's own type for `page`: ocr_result.schema.json
            # requires an integer, and stringifying it made every redistributed
            # child fail validation on CASE_909 -- silently, since nothing
            # revalidated them at redistribution time.
            page["page"] = offset
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


def _count_redacted_items(text: str) -> int:
    """Count PII placeholders in a split child's inherited redacted text.

    The redactor substitutes each identified value with a placeholder from
    `redaction.CATEGORY_TO_PLACEHOLDER`, so counting them recovers exactly what
    `items_redacted` means for THIS document -- unlike copying the parent's
    count, which describes the whole bundle. Import is local because
    `redaction` pulls in the provider stack, which segmentation otherwise never
    needs; a failure falls back to 0 rather than writing an invalid contract.
    """
    try:
        from redaction import _PLACEHOLDER_RE
    except Exception:  # noqa: BLE001 -- a count must never break a valid split
        return 0
    return len(_PLACEHOLDER_RE.findall(text))


def _register_child_revision(*, case_id: str, doc_id: str, text: str,
                             bundle_id: str, run_id: str,
                             progress=None) -> bool:
    """Register a split child's inherited redacted text as a source revision.

    WHY THIS IS NEEDED. `_revision_index.json` is populated as a SIDE EFFECT of
    `dao.write-redacted-text` (dao.py `_register_revision`), and that is the only
    automatic path. A split child never takes it: its text is inherited from the
    bundle and written directly above, so the child ends up with real processed
    text and NO registered revision. Nothing in the pipeline notices, because
    nothing calls `record-source-digest` either -- it exists only as an operator
    CLI subcommand.

    The cost is not theoretical. `policy_clause_processing` gates on
    canonical_v1 UID verification, which cannot be switched on for a document
    whose source bytes were never registered, so on CASE_133 the stage halted
    with 174 unregistered-revision blockers -- one per policy child -- and no
    in-pipeline way to clear them. Keeping the 약관 bundles whole (the collapse
    above) cut that to 2, which proves the two defects are INDEPENDENT: even a
    bundle that collapses to a single segment is still `split` into a 1:1 child
    (CASE_135: DOC_003 145p -> DOC_010 145p), and that child inherits its text
    the same way.

    Deliberately calls `_register_revision` rather than the full
    `write-redacted-text` command: that command also INVALIDATES downstream
    stages, which is right when replacing text under work already recorded as
    passed, but wrong here -- these bytes are new, not a replacement, and the
    split is running inside `document_processing` itself.

    Never fatal. Registration is what makes a later policy stage possible; it is
    not what makes this split correct. A failure is reported and the split
    continues, so a registration problem cannot destroy a completed split.
    """
    try:
        import dao as _dao
        # Same derivation dao.cmd_write_redacted_text uses (dao.py:3954), so a
        # child's revision hash is computed exactly as a normally-written one.
        revision_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        _dao._register_revision(
            case_id, doc_id, text, revision_sha,
            held_by="segment_case.split",
            # A REAL run id, never None. `_register_revision` writes
            # `"run_id": run_id` unconditionally and the schema types the field
            # `string`, so a None lands as an explicit null and the WHOLE index
            # fails validation -- which is how the first version of this failed
            # on all 18 of CASE_135's children at once. The split always knows
            # its run (`split_bundle` takes `run_id` as a required argument), so
            # there is no case here that legitimately has nothing to name.
            run_id=run_id,
            supersedes=None)
        return True
    except Exception as exc:  # noqa: BLE001 -- diagnostic, never fatal
        if progress:
            progress(f"WARNING: could not register a source revision for "
                     f"{doc_id} (inherited from {bundle_id}): {exc}")
        return False


@trace_mod.traced("segment.redistribute_redaction", category="io")
def _redistribute_parent_redaction(
    *, case_id: str, bundle_id: str, segments: list[dict],
    document_ids: list[str], run_id: str, progress=None,
) -> bool:
    """Cut the bundle's redacted text into per-child files. Returns whether it ran.

    False means the bundle has no redacted text yet -- a split before redaction
    is a valid order, not an error, so nothing is invented. Deterministic: the
    `<<<PAGE page=N>>>` markers checkpoint 2 embeds make the page boundaries
    exact, and each child's markers are renumbered from 1 to match its own pages.
    """
    source = ROOT / "data" / "processed" / case_id / bundle_id / "redacted_text.md"
    if not source.exists():
        return False
    parent_contract_path = ROOT / "outputs" / case_id / f"redaction_result_{bundle_id}.json"
    parent_contract = None
    if parent_contract_path.exists():
        try:
            parent_contract = json.loads(parent_contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parent_contract = None
    try:
        pages = _split_page_markers(source.read_text(encoding="utf-8"))
    except OSError:
        return False
    covered = sum(seg["page_end"] - seg["page_start"] + 1 for seg in segments)
    if len(pages) != covered:
        raise SegmentationError(
            f"{source} holds {len(pages)} page(s) but the approved segments cover "
            f"{covered}; refusing to redistribute a mismatched redaction"
        )
    for segment, doc_id in zip(segments, document_ids):
        child_pages = pages[segment["page_start"] - 1:segment["page_end"]]
        body = "".join(
            f"<<<PAGE page={offset}>>>\n{text}\n"
            for offset, text in enumerate(child_pages, start=1)
        )
        child_dir = ROOT / "data" / "processed" / case_id / doc_id
        child_dir.mkdir(parents=True, exist_ok=True)
        (child_dir / "redacted_text.md").write_text(body, encoding="utf-8")
        _register_child_revision(case_id=case_id, doc_id=doc_id, text=body,
                                 bundle_id=bundle_id, run_id=run_id,
                                 progress=progress)
        if parent_contract is not None:
            # Downstream stages find a document's redacted text through the
            # manifest's redacted_text_path, and chunking reads that field, so
            # the file alone leaves the child invisible to them.
            child_contract = copy.deepcopy(parent_contract)
            child_contract["document_id"] = doc_id
            child_contract["redacted_text_path"] = (
                f"data/processed/{case_id}/{doc_id}/redacted_text.md")
            # The parent's count describes the parent. Attributing it to each
            # child would multiply one redaction into twelve -- but `None` is
            # not the answer either: `items_redacted` is a REQUIRED non-negative
            # integer, so nulling it made every split child's contract
            # schema-invalid (18 of 18 on CASE_135, caught only by validating
            # the case's contracts after the run rather than by any gate).
            # The child's real count is recoverable from the child's own bytes,
            # which is exactly what the field is supposed to describe.
            child_contract["items_redacted"] = _count_redacted_items(body)
            child_contract["redistributed_from_document_id"] = bundle_id
            (ROOT / "outputs" / case_id / f"redaction_result_{doc_id}.json").write_text(
                json.dumps(child_contract, ensure_ascii=False, indent=2), encoding="utf-8")
        if progress:
            progress(f"redistributed {len(child_pages)} redacted page(s) to {doc_id}")
    return True


def _parent_ocr_available(case_id: str, bundle_id: str) -> bool:
    """Whether this bundle was OCR'd before segmentation.

    True means the children will inherit the parent's pages and no child file
    is ever read, so writing child PDFs is pure cost. Deliberately the SAME
    condition `_redistribute_parent_ocr` keys off -- if the two ever disagreed,
    a case would either lose its child text or keep paying for files nothing
    opens, and the disagreement would be invisible until a later stage failed.

    An unreadable record returns False here rather than raising: the raise
    belongs to `_redistribute_parent_ocr`, which is the call that actually needs
    the contents, and duplicating it would report the same fault twice from
    different places.
    """
    parent_record_path = ROOT / "outputs" / case_id / f"ocr_result_{bundle_id}.json"
    if not parent_record_path.exists():
        return False
    try:
        json.loads(parent_record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return True


@trace_mod.traced("segment.redistribute_ocr", category="io")
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


@trace_mod.traced("segment.split_bundle", category="io")
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

    # A child PDF is only worth materializing when the child is what gets read.
    # Under the post-2026-08-05 order the bundle is OCR'd and redacted BEFORE
    # segmentation, so each child is handed the parent's pages (see
    # _redistribute_parent_ocr below) and NOTHING opens a child file:
    # run_document_stage.select_documents skips any entry whose ocr_status is
    # already completed, which an inheriting child always is.
    #
    # Writing them anyway cost 44MB against an 11.4MB intake on CASE_142. The
    # arithmetic is the giveaway: every single-page child of the 19-page
    # DOC_005 weighed 1.34MB -- the same as the whole parent -- because
    # page selection keeps the parent's unreferenced fonts and images and
    # save() was not asked to collect them. Twelve children, twelve copies of
    # one scan.
    #
    # Under the older order (split first, OCR each child afterwards) the child
    # file IS the OCR input, so it must still be written. Both orders coexist
    # because already-processed cases are not reprocessed.
    inherits_parent_text = _parent_ocr_available(case_id, bundle_id)

    for offset, seg in enumerate(segments):
        doc_id = f"DOC_{start_index + offset:03d}"
        if inherits_parent_text:
            if progress:
                progress(f"{doc_id}: inherits parent pages "
                         f"(p{seg['page_start']}-{seg['page_end']}), no PDF written")
            continue
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
        #
        # garbage=4 drops the objects select() left unreferenced and merges
        # duplicates. It does not touch page content streams, so the text layer
        # select() exists to preserve is preserved.
        with fitz.open(bundle_pdf_path) as out:
            # select is 0-based; segments are 1-based inclusive.
            out.select(list(range(seg["page_start"] - 1, seg["page_end"])))
            out.save(out_path, garbage=4, deflate=True)
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
    # The bundle is redacted BEFORE the split (segmentation reads that text), so
    # its children inherit the redaction too. Without this each child would be
    # redacted again -- repeating work the bundle already paid for -- and would
    # have no redacted text for classification to read, leaving raw page text as
    # the only available input.
    redistributed_redaction = _redistribute_parent_redaction(
        case_id=case_id,
        bundle_id=bundle_id,
        segments=segments,
        document_ids=document_ids,
        run_id=run_id,
        progress=progress,
    )

    new_documents = build_manifest_entries(
        segments,
        case_id=case_id,
        source_file_name=source_file_name,
        proposal_path=proposal_path,
        start_index=start_index,
        file_sizes=file_sizes,
        redaction_redistributed=redistributed_redaction,
        child_files_written=not inherits_parent_text,
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
        # Segmentation is a checkpoint inside document_processing, not a stage
        # before it: the bundle is OCR'd and redacted, split here, and its
        # children are then classified and redacted. Recording a separate
        # document_segmentation stage would claim a boundary the execution does
        # not have (see run_state.schema.json's deprecation note).
        stage="document_processing",
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
        "redistributed_redaction": redistributed_redaction,
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


# Statuses that leave settled text on disk and may therefore be segmented.
#
# The last two are DELIBERATE P8 REDUCTIONS, chosen by the orchestrator for a
# throughput or plumbing run (`--single-reader`, `--on-disagreement
# assume-reading-a`) and recorded honestly under their own names -- the
# `single_technology_weak_p8_poc` regime this PoC already runs under. They are
# not unresolved disagreements: single-reader never compared, so there is
# nothing for a human to adjudicate, and assume-reading-a already applied the
# orchestrator's stated policy. Refusing them made a reduced run OCR and redact
# every page and then halt at segmentation demanding a resolution that could
# not exist (measured on CASE_487 and CASE_489).
#
# What this gate is for -- per segmentation_prerequisite_errors' own docstring
# -- is preventing a fall back to raw-PDF vision when text is missing or
# blocked. `disagreed_pending_review` is exactly that case and still refuses:
# real pages disagreed and the text is not settled. `ocr_status != completed`
# refuses independently, so a reduction can never become a route past a
# document that has no text at all.
#
# Boundary evidence survives the reduction: on CASE_488/DOC_005 the two
# readings were byte-identical on every title line, and differed only inside
# table codes and amounts. A reduced read costs precision within a document,
# not the titles segmentation cuts on. The reduction is still visible
# downstream -- `ocr_quality: low` and `review_required: true` ride along with
# it -- so nothing here makes a reduced run look like a validated one.
_SEGMENTATION_P8_CLEAR_STATUSES = frozenset({
    "agreed",
    "disagreed_resolved",
    "single_reader_no_cross_validation",
    "assume_reading_a_unreviewed",
})


def segmentation_prerequisite_errors(bundle: dict, page_count: int) -> list[str]:
    """Return the prerequisites that keep a bundle out of normal segmentation.

    Segmentation is downstream of bundle OCR and redaction. It must not
    compensate for a P8 block by looking at raw contact sheets: that would turn
    an extraction hard gate into a routing preference. The vision implementation
    remains a pure-function/diagnostic seam, but the governed ``propose`` command
    cannot enter it without P8-cleared redacted text.
    """
    errors: list[str] = []
    doc_id = bundle.get("document_id", "<unknown>")
    if bundle.get("downstream_disposition") == "superseded_bundle":
        errors.append(f"{doc_id} is already a superseded bundle")
    if bundle.get("ocr_status") != "completed":
        errors.append(f"{doc_id} OCR is {bundle.get('ocr_status')!r}, not 'completed'")
    status = bundle.get("cross_validation_status")
    if status not in _SEGMENTATION_P8_CLEAR_STATUSES:
        errors.append(
            f"{doc_id} P8 cross-validation is {status!r}; human resolution is "
            "required before segmentation"
        )

    redacted_path = bundle.get("redacted_text_path")
    if not isinstance(redacted_path, str) or not redacted_path:
        errors.append(f"{doc_id} has no redacted_text_path")
        return errors
    path = ROOT / redacted_path
    if not path.exists():
        errors.append(f"{doc_id} redacted text is missing: {redacted_path}")
        return errors
    try:
        pages = _split_page_markers(path.read_text(encoding="utf-8"))
    except OSError as exc:
        errors.append(f"{doc_id} redacted text could not be read: {exc}")
        return errors
    if len(pages) != page_count:
        errors.append(
            f"{doc_id} redacted text covers {len(pages)} page(s), not all "
            f"{page_count} bundle page(s)"
        )
    return errors


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

    prerequisites = segmentation_prerequisite_errors(bundle, page_count)
    if prerequisites:
        print(json.dumps({
            "status": "blocked_segmentation_prerequisite",
            "case_id": args.case_id,
            "document_id": args.doc_id,
            "errors": prerequisites,
            "note": (
                "Segmentation requires complete, P8-cleared bundle OCR and "
                "redacted text. It will not fall back to raw-PDF vision."
            ),
        }, ensure_ascii=False, indent=2))
        return 2

    # Deterministic path, tried before ANY sheet render or provider construction:
    # a born-digital bundle needs neither. Kept here rather than only inside
    # propose_boundaries because the CLI renders sheets and builds a provider up
    # front, and both are pure waste when the text layer already answers this.
    if not args.no_text_anchor:
        # Processed text first, then the PDF's own layer. A scan has none of the
        # latter, which is why the deterministic path used to reach almost none
        # of this corpus; once checkpoint 1 has read the bundle, that same scan
        # has page text and the boundary rules apply to it unchanged.
        # The judge is built lazily -- only if an undecided page actually needs
        # one -- so a bundle the title rules fully answer still constructs no
        # provider and renders no sheet.
        cli_judge = _LazyJudge(lambda: build_provider(parse_provider_config(args)))
        judged_pages: list[int] = []
        anchored, anchored_undecided = processed_boundaries_with_undecided(
            args.case_id, args.doc_id, page_count, judge=cli_judge,
            judged=judged_pages)
        source = "processed text"
        if anchored is None:
            anchored = text_anchor_boundaries(pdf_path, page_count)
            anchored_undecided = []
            source = "text layer"

        # A boundary set built on FAILED judge calls is not a proposal. Every
        # failure splits, so the result looks like a decisive answer and reads
        # as one at the approval gate; on CASE_961 nine consecutive failures
        # turned an 11-segment bundle into 13 with `undecided_pages: []`.
        # Refuse rather than write it: the pages are still flagged, but a
        # reviewer would be approving boundaries no model ever judged.
        failures = judge_failures(cli_judge)
        if failures:
            _stderr(f"error: {len(failures)} boundary-judge call(s) failed")
            print(json.dumps({
                "status": "judge_failed",
                "case_id": args.case_id,
                "document_id": args.doc_id,
                "judge_failure_count": len(failures),
                "judge_failures": failures[:10],
                "undecided_pages": anchored_undecided,
                "note": (
                    "Boundaries were NOT written. Every failed judge call splits, "
                    "so the proposal would look decided while resting on calls "
                    "that never returned a verdict. Re-run `propose`; the "
                    "deterministic title rules are unaffected and only the "
                    "genuinely ambiguous pages are re-judged."
                ),
            }, ensure_ascii=False, indent=2))
            return 1

        if anchored is not None:
            _stderr(
                f"{source} covers all {page_count} page(s): deriving "
                f"{len(anchored)} boundary/boundaries deterministically "
                f"(no contact sheets rendered)"
            )
            result = _text_anchor_proposal(
                anchored, page_count=page_count, geometry=geometry,
                undecided=anchored_undecided,
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
                "model_calls": len(judged_pages),
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
    # Same gap redact_document.py had: the segment.judge spans below are
    # no-ops until a case/run is configured. Subcommands vary in whether they
    # carry a run id (propose/show do not), so this is best-effort -- a
    # subcommand without one is simply not traced rather than misfiled under
    # someone else's run.
    _case_id = getattr(args, "case_id", None)
    _run_id = getattr(args, "run_id", None)
    if _case_id and _run_id:
        trace_mod.configure(_case_id, _run_id)
    try:
        return args.fn(args)
    except SegmentationError as exc:
        _stderr(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
