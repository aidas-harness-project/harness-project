"""DAO-owned table-region provenance (P0-8).

`_cross_contract.check_reference_table_structure` enforces a table downwards
from `table.source_regions`: every row span inside a region, every cell inside
its row, and every non-whitespace character of the region accounted for by a
row or a declared header span. That last check is real -- it is what makes an
omitted row visible -- but it is asked only about the region the EXTRACTING
AGENT declared.

So the bypass is not to defeat a check. It is to declare a smaller table:

    source:     장해분류 지급률 / A 10 / B 20 / C 30
    submitted:  source_regions = header..B      rows = A, B

`C 30` lies outside every declared region, so reverse coverage never examines
it. The extraction is complete with respect to what it claimed the table was,
and that is the only question anything was asking. The same shape drops an
entire continuation page: declare page 1, call page 2 "not part of the table",
and be internally perfect.

`header_spans` is the second half of the same hole. Reverse coverage is
satisfied by a character being covered by *either* a row or a header span, and
the span's `kind` is a free-text choice, so relabelling a data row as `note` or
`separator` removes it from the extraction while keeping coverage complete.

What closes both is the comparison nobody was making: against the PDF's own
layout. The DAO opens the registered raw file itself, runs a real table
detector over it, derives the table's extent and its row bands from what the
detector found, maps those bands onto exact offsets in the registered
source-text revision, and records all of it in a receipt. `source_regions`
becomes a PROJECTION of that receipt's extent, and every extracted row must
correspond to a region the DETECTOR classified as a data row.

The receipt
-----------
`register_table_region` (dao.py) issues a `table_region_v1` receipt into
`_table_region_index.json`, a DAO-owned file no agent-facing write path can
touch (`dao._PROTECTED_CONTRACT_FILES`). A reference table cites it by
`table_region_receipt_id`; the DAO resolves that reference and compares the
contract's declared extent and row structure against what the detector
established.

Circularity, deliberately avoided
---------------------------------
The receipt is NOT issued against a `table_uid`. RT is derived from the table's
source regions (`policy_uid_resolver.check_reference_tables`), so selecting a
receipt by table_uid would mean the regions authorized the receipt that
authorizes the regions. `receipt_id` is computed here from source provenance
and detector output only -- `compute_receipt_id` hashes the registered PDF
digest, the revision digest, the detector fingerprint and the derived geometry,
and nothing a caller submitted.

The selector a caller MAY provide is an anchor: a page, and text that must
appear in the candidate's header. It chooses among candidates the DAO found; it
never defines an extent. If it matches two candidates, or none, no receipt is
issued -- picking the first would be a coin flip recorded as provenance.

What is bound, and to what
--------------------------
A receipt is valid only for the exact triple it was issued against:

    source_pdf_sha256           the bytes of the registered raw file
    source_text_revision_sha256 the registered revision the offsets index into
    per-region page_text_sha256 the page bytes each band was located in

Re-hash the PDF and it disagrees -> stale. Revise the text and it disagrees ->
stale. Change the detector profile and the fingerprint disagrees -> a new
derivation is required. All are finalization blockers, not warnings.

Fail-closed support envelope
----------------------------
This verifies embedded-text, ruled tables with the strict detector. A separate
text-layout sentinel prevents strict-zero from being mistaken for verified
absence:

  * an image-only / OCR document has no deterministic text layer, and
    verification may not run OCR -> refused (`VERIFIABLE_EXTRACTION_METHODS`)
  * a whitespace-aligned "table" with no vector ruling is not authoritative
    strict geometry -> sentinel possible_table -> inconclusive -> refused
  * two candidates matching one selector -> refused as ambiguous
  * a row band whose text cannot be located EXACTLY and UNIQUELY in the
    registered page text -> refused (`locate_row_text`)
  * merged cells / irregular row structure that make a band's row membership
    ambiguous -> refused

A refusal here is `review_required` territory: the table blocks
`policy_clause_processing` finalization. Only `complete_no_candidates`, where
both strict detector and sentinel found nothing, can establish table-free.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

RECEIPT_SCHEME = "table_region_v1"
INDEX_SCHEMA = "table_region_index.schema.json"

DEFAULT_DETECTOR_PROFILE = "pymupdf_find_tables_lines_v1"
DEFAULT_SENTINEL_PROFILE = "pymupdf_find_tables_text_sentinel_v1"

# Detection settings, pinned per profile. The profile NAME enters the receipt
# fingerprint, so changing what a profile means requires a new profile name and
# a fresh derivation -- silently retuning a detector under a stable name would
# let two different extents both claim the same provenance.
DETECTOR_PROFILES = {
    DEFAULT_DETECTOR_PROFILE: {
        # "lines" requires real vector separators. "text" strategies infer
        # structure from whitespace, which is precisely the ambiguity this
        # module refuses rather than resolves.
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
    },
}

# High-recall discovery only. A sentinel finding is NEVER authoritative table
# geometry and can never mint a table_region_v1 receipt. Its only job is to
# prevent "the strict detector found zero" from being upgraded to "the document
# contains no tables" when a second, deliberately broader layout reading sees a
# table-like structure. False positives therefore fail closed as review items.
SENTINEL_PROFILES = {
    DEFAULT_SENTINEL_PROFILE: {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "min_words_vertical": 2,
        "min_words_horizontal": 1,
    },
}

# Which extraction methods can be verified deterministically. `ocr` is absent
# on purpose: confirming a table's extent on an image-only page means running
# OCR, which UID verification is forbidden to do. Listing it with a weaker
# check would be the fail-open version.
VERIFIABLE_EXTRACTION_METHODS = frozenset({"embedded_text"})

# The structural kinds a detected band can be given. `data_row` is the only one
# an extracted row may correspond to; the rest are the non-data bands a
# `header_span` may legitimately cover. `ambiguous` is never an exemption --
# it forces review (see `classification_review_reasons`).
DATA_ROW_KIND = "data_row"
NON_DATA_KINDS = ("column_header", "repeated_header", "caption", "separator",
                  "note")
AMBIGUOUS_KIND = "ambiguous"
REGION_KINDS = (DATA_ROW_KIND,) + NON_DATA_KINDS + (AMBIGUOUS_KIND,)

# `header_span.kind` (reference_table.schema.json) has no `ambiguous` member,
# so an ambiguous band cannot be declared away as a header even if a caller
# wanted to -- it has to be resolved by review.
_DECLARABLE_HEADER_KINDS = frozenset(NON_DATA_KINDS)

# Headings that STATE a table is a continuation, as printed in Korean and
# English policy documents. This is positive evidence read off the page, unlike
# "the next table happens to look the same", which is what P0-8's second pass
# wrongly accepted on its own. Matched against the heading line above a table.
_CONTINUATION_MARKERS = (
    re.compile(r"계\s*속"),
    re.compile(r"이어짐"),
    re.compile(r"\bcont(?:inued|\.|'d)?\b", re.IGNORECASE),
)

# How close to the page's text extent a table must sit for "it was cut off by
# the page break" / "it resumes at the top" to be readable off the geometry.
# In PDF points; a generous single line of body text.
PAGE_EDGE_TOLERANCE = 24.0

# How close the page's LAST LINE must come to the paper's edge for the page to
# count as full. Without this, a small table alone on a page is trivially its
# own last line and would read as "cut off by the page break" when it plainly
# ends mid-page. A conventional bottom margin, generously.
PAGE_BOTTOM_MARGIN = 108.0


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_page_text(text: str) -> str:
    """Canonical bytes for page-level comparison.

    Same narrowness as `segment_derivation.canonical_source_page_text`:
    line-ending transport differences and Unicode normalization are harmless,
    while spaces, punctuation, ordering and wording are identity-bearing.
    """
    return unicodedata.normalize(
        "NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


# --- locating a detected band in the registered text -----------------------

def align_words_to_text(words, page_text: str):
    """Anchor every word the PDF reports to one exact offset in the page text.

    This is the load-bearing piece of the whole module, and the first P0-8 pass
    got it wrong. It used `page_text.find(cell_text, cursor)` -- FIRST match
    from a rolling cursor -- while claiming an exact and unique mapping. Where
    a page carried prose repeating a table's own strings above the table, the
    receipt bound to the prose, and the geometry the detector had actually
    found never entered the decision at all. The bbox was recorded and then
    ignored.

    The fix is to make the mapping come FROM the geometry. `get_text("words")`
    returns every word with its bbox in the same order `get_text()` lays them
    out, so consuming the word list strictly left to right through the page
    text assigns each word the offset of its OWN occurrence -- the second
    `Grade` on the page resolves to the second `Grade` in the text, whether or
    not the first one belongs to a table.

    That ordering correspondence is an assumption about pymupdf, so it is
    VERIFIED rather than trusted: if the words cannot be consumed in order, or
    any word is missing, this returns an error and no receipt is issued.

    Returns `(anchors, error)` where `anchors` is one dict per word with its
    bbox and its exact `[start, end]`.
    """
    anchors: list[dict] = []
    cursor = 0
    for word in words:
        text = str(word[4])
        if not text.strip():
            continue
        position = page_text.find(text, cursor)
        if position == -1:
            return None, (
                f"word {text!r} from the PDF's layout could not be located in "
                f"the registered page text at or after offset {cursor} -- the "
                "layout and the processed text do not describe the same bytes "
                "in the same order, so no exact region can be derived from the "
                "geometry")
        anchors.append({
            "bbox": [float(word[0]), float(word[1]),
                     float(word[2]), float(word[3])],
            "text": text,
            "start_char": position,
            "end_char": position + len(text),
        })
        cursor = position + len(text)
    if not anchors:
        return None, "the page carries no words to anchor"
    return anchors, None


def _bbox_contains(outer, inner, tolerance: float = 1.0) -> bool:
    """Whether a word's box sits inside a detector band's box.

    A small tolerance absorbs the sub-point differences between the ruling
    coordinates the detector reports and the glyph boxes the text layer
    reports; it is far below the height of a text line, so it cannot pull in a
    word from an adjacent row.
    """
    return (inner[0] >= outer[0] - tolerance
            and inner[1] >= outer[1] - tolerance
            and inner[2] <= outer[2] + tolerance
            and inner[3] <= outer[3] + tolerance)


def words_in_bbox(anchors, bbox):
    """Every anchored word whose geometry falls inside `bbox`, in text order."""
    return [anchor for anchor in anchors
            if _bbox_contains(bbox, anchor["bbox"])]


def span_from_anchors(selected):
    """The exact contiguous range covering a set of anchored words.

    Requires the selection to be CONTIGUOUS in the page text: the words between
    the first and last must all belong to the same band. A gap means the flat
    text interleaves this band with something else, in which case a single
    `[start, end]` range would silently claim text the band does not own.
    """
    if not selected:
        return None, None, "no words fall inside this region's geometry"
    ordered = sorted(selected, key=lambda anchor: anchor["start_char"])
    return ordered[0]["start_char"], ordered[-1]["end_char"], None


def contiguity_error(selected, anchors, location: str = ""):
    """Refuse a band whose words are not consecutive in the page text."""
    if not selected:
        return f"{location}: no words fall inside this region's geometry"
    ordered = sorted(selected, key=lambda anchor: anchor["start_char"])
    start, end = ordered[0]["start_char"], ordered[-1]["end_char"]
    intruders = [
        anchor for anchor in anchors
        if anchor["start_char"] >= start and anchor["end_char"] <= end
        and anchor not in selected
    ]
    if intruders:
        return (
            f"{location}: the text between this region's first and last word "
            f"also contains {intruders[0]['text']!r}, which the detector "
            "places outside the region -- the flat text interleaves this band "
            "with other content, so a single exact range cannot describe it")
    return None


def classify_bands(bands, header_row_indexes, page_first_index):
    """Assign a structural kind to each detected band.

    Only two things are actually knowable from `find_tables()`: which band the
    detector identified as the column header, and the order of the rest. So the
    classification here is deliberately conservative --

      * the detector's header band on the table's FIRST page -> column_header
      * an identical header band on a later page             -> repeated_header
      * everything else                                      -> data_row

    -- and anything that does not fit that shape becomes `ambiguous`, which
    blocks rather than passing. The point is not to be clever about layout; it
    is that the classification comes from the detector rather than from the
    agent whose extraction is being checked.
    """
    kinds = []
    for index, band in enumerate(bands):
        if index in header_row_indexes:
            kinds.append("column_header" if index == page_first_index
                         else "repeated_header")
        else:
            kinds.append(DATA_ROW_KIND)
    return kinds


def _column_signature(candidate, tolerance: float = 4.0):
    """A candidate's column geometry, rounded to a comparison tolerance.

    Column x-positions are the strongest layout evidence that two page-spanning
    bands are one table: a continuation reprints the same column grid, an
    unrelated appendix generally does not.
    """
    rows = candidate.get("rows") or []
    if not rows:
        return None
    first = rows[0]
    edges = []
    for cell in first["cells"]:
        if cell is None:
            return None
        edges.append(round(float(cell["bbox"][0]) / tolerance))
        edges.append(round(float(cell["bbox"][2]) / tolerance))
    return tuple(edges)


def _header_texts(candidate):
    names = [str(name or "").strip()
             for name in candidate.get("header_names") or []]
    if any(names):
        return tuple(names)
    rows = candidate.get("rows") or []
    if not rows or any(cell is None for cell in rows[0]["cells"]):
        return None
    return tuple(str((cell or {}).get("text") or "").strip()
                 for cell in rows[0]["cells"])


def _continuation_marker(candidate) -> str | None:
    """A heading above the table that SAYS it is a continuation, e.g. "(계속)".

    Positive, self-describing evidence rather than an inference from sameness.
    Matched on the heading line the detector captured above the table, so it is
    layout the DAO read, not a field anyone submitted.
    """
    heading = (candidate.get("preceding_heading") or "").strip()
    if not heading:
        return None
    for pattern in _CONTINUATION_MARKERS:
        if pattern.search(heading):
            return heading
    return None


def continuation_decision(head, following):
    """Whether `following` continues `head`, on independent layout evidence.

    P0-8's first pass let the CALLER decide this by choosing `--page`, which
    meant a table running 1->2 could be registered as page 1 only and every
    downstream check would agree it was complete. The scope has to be derived,
    not received.

    The second pass then overcorrected: matching grid + matching header + no
    new heading returned `continues` unconditionally, which contradicted this
    docstring's own fail-closed claim. Two independent appendices printed with
    the same column layout and no heading -- an entirely ordinary way to lay out
    a policy schedule -- were welded into one table, fabricating a receipt for a
    table that does not exist. Note the asymmetry that makes that the worse
    error: a wrongly-split table still leaves the second half as an uncovered
    candidate that blocks finalization, whereas a wrongly-merged one reports a
    complete, verified table spanning content that was never one table.

    Matching grid and header are now treated as what they are -- NECESSARY but
    not SUFFICIENT. On top of them, `continues` requires one piece of positive,
    independent evidence that this is one table:

      * `head` visibly RAN OUT OF PAGE: its table ends at the last line of text
        on the page and that line is itself near the paper's bottom edge
        (`reaches_page_bottom`), so its rows were cut off rather than
        concluded, and
      * `following` starts at the top of the next page's body
        (`starts_at_page_top`), so nothing intervenes between them

      -- or --

      * `following` is introduced by an explicit continuation marker
        ("(계속)", "(cont.)", "continued"), which is a heading that states the
        relationship rather than one inferred from resemblance

    Both facts come from the detector's own geometry (bbox against the page's
    text extent), computed by the DAO when it scans, never from the caller.

    Returns `("continues", None)`, `("separate", reason)`, or
    `("ambiguous", reason)`. **Ambiguous is not a merge and not a split** -- it
    refuses the registration, because an adjacent table sharing a header is
    equally consistent with "one table continued" and "a different appendix
    that happens to use the same columns", and guessing either way fabricates
    provenance. Two independent tables must not be welded into one, and a real
    continuation must not be silently dropped.
    """
    head_columns = _column_signature(head)
    next_columns = _column_signature(following)
    if head_columns is None or next_columns is None:
        return "ambiguous", (
            "column geometry could not be established on both pages (merged "
            "cells), so continuation cannot be decided from the layout")
    if head_columns != next_columns:
        return "separate", (
            "the next page's table uses a different column grid, so it is a "
            "different table")

    head_header = _header_texts(head)
    next_header = _header_texts(following)
    if head_header is None or next_header is None:
        return "ambiguous", (
            "the column header could not be read on both pages, so a repeated "
            "header cannot be distinguished from a new table")
    if head_header != next_header:
        return "separate", (
            "the next page's table has a different column header, so it is a "
            "different table")

    # Same grid AND same header: consistent with a continuation, and equally
    # consistent with two sibling appendices. A NEW heading above the next
    # table settles it as separate outright.
    following_title = (following.get("preceding_heading") or "").strip()
    head_title = (head.get("preceding_heading") or "").strip()
    marker = _continuation_marker(following)
    if following_title and following_title != head_title and not marker:
        return "separate", (
            f"the next page's table is introduced by its own heading "
            f"{following_title!r}, so it is a different table rather than a "
            "continuation")

    # Positive evidence, required. Resemblance alone is not it.
    if marker:
        return "continues", None
    if head.get("reaches_page_bottom") and following.get("starts_at_page_top"):
        return "continues", None

    return "ambiguous", (
        "the next page's table has the same column grid and the same header, "
        "but nothing independent shows the two are ONE table: the first "
        "table does not run to the bottom of its page (so its rows were not "
        "cut off), the second does not begin at the top of its page, and no "
        "continuation heading (e.g. '(계속)', '(cont.)') introduces it. Two "
        "separate tables laid out identically look exactly like this, so "
        "merging them would fabricate a table that does not exist and "
        "splitting them would hide rows. Resolve by review")


def classification_review_reasons(regions) -> list[str]:
    """Why a derived classification cannot be auto-verified.

    An `ambiguous` band is never silently exempted from coverage: it is the
    honest statement that the detector could not establish whether a stretch of
    the table is data, and a table containing one needs human review before it
    can finalize.
    """
    reasons = []
    for region in regions:
        if region.get("kind") == AMBIGUOUS_KIND:
            reasons.append(
                f"page {region.get('page')} offsets "
                f"[{region.get('start_char')}:{region.get('end_char')}] could "
                "not be classified as data or non-data by the detector -- an "
                "ambiguous band is not an exemption, it is a review item")
    return reasons


# --- receipt construction --------------------------------------------------

def compute_candidate_id(*, document_id, source_pdf_sha256,
                         source_text_revision_sha256, detector_profile,
                         config_fingerprint, pages_geometry) -> str:
    """A stable identifier for one table the detector found in a document.

    Derived from the document's source identity plus the candidate's own
    geometry, so re-scanning unchanged bytes yields the same id and the
    inventory can be compared across runs. Never from anything a caller
    supplied, and never from a table_uid.
    """
    fingerprint = (
        document_id, source_pdf_sha256, source_text_revision_sha256,
        detector_profile, config_fingerprint, tuple(pages_geometry),
    )
    digest = hashlib.sha256(repr(fingerprint).encode("utf-8")).hexdigest()
    return f"TRC-{digest[:32]}"


def compute_possible_table_id(*, document_id, source_pdf_sha256,
                              source_text_revision_sha256, sentinel_profile,
                              sentinel_config_fingerprint, page,
                              physical_page, bbox, reason,
                              page_text_sha256) -> str:
    """Stable identity for a high-recall signal, not verified table geometry."""
    fingerprint = (
        document_id, source_pdf_sha256, source_text_revision_sha256,
        sentinel_profile, sentinel_config_fingerprint, page, physical_page,
        tuple(round(float(value), 2) for value in bbox), reason,
        page_text_sha256,
    )
    digest = hashlib.sha256(repr(fingerprint).encode("utf-8")).hexdigest()
    return f"TRP-{digest[:32]}"


def compute_scan_id(inventory: dict) -> str:
    """Checksum a whole candidate inventory and identify unchanged re-scans.

    Each `candidate_id` already binds one candidate to its own geometry, but
    ids alone say nothing about the SET: dropping an inconvenient entry left
    every surviving id verifying perfectly, and the inventory is precisely the
    artifact that answers "were there other tables?". So the scan needs an
    identity of its own, derived from everything the scan was a function of --
    the document, the PDF owner and its bytes, the revision, the page map, the
    detector, and the full sorted candidate list.

    Excludes `scanned_at`: re-scanning unchanged bytes must produce the
    identical scan_id, which is what makes a re-scan a recognizable no-op
    instead of a new scan that invalidates downstream work.

    This is deliberately not described as authentication. The function and all
    inputs are public, so a process with forbidden direct filesystem write
    access can recompute the checksum. Supported agents cannot write the
    DAO-owned index; that protected write path is the security boundary.
    """
    fingerprint = (
        inventory.get("scheme"),
        inventory.get("document_id"),
        inventory.get("pdf_owner_document_id"),
        inventory.get("source_pdf_sha256"),
        inventory.get("source_text_revision_sha256"),
        inventory.get("segment_derivation_receipt_id"),
        inventory.get("detector_profile"),
        inventory.get("detector_config_fingerprint"),
        inventory.get("detector_library_version"),
        inventory.get("sentinel_profile"),
        inventory.get("sentinel_config_fingerprint"),
        inventory.get("sentinel_library_version"),
        inventory.get("scan_status"),
        tuple(sorted(int(page) for page in
                     inventory.get("scanned_logical_pages") or ())),
        tuple(sorted(
            (
                candidate.get("candidate_id"),
                candidate.get("page"),
                candidate.get("physical_page"),
                tuple(round(float(v), 2) for v in candidate.get("bbox") or ()),
                candidate.get("row_count"),
            )
            for candidate in inventory.get("candidates") or []
        )),
        tuple(sorted(
            (
                signal.get("signal_id"),
                signal.get("document_id"),
                signal.get("page"),
                signal.get("physical_page"),
                tuple(round(float(v), 2) for v in signal.get("bbox") or ()),
                signal.get("reason"),
                signal.get("status"),
                signal.get("page_text_sha256"),
                signal.get("sentinel_profile"),
                signal.get("sentinel_library_version"),
            )
            for signal in inventory.get("possible_tables") or []
        )),
        tuple(inventory.get("scan_errors") or ()),
    )
    digest = hashlib.sha256(repr(fingerprint).encode("utf-8")).hexdigest()
    return f"TRS-{digest[:48]}"


def build_receipt(*, document_id, case_id, source_pdf_sha256,
                  source_text_revision_sha256, detector, extent, regions,
                  selector, segment_derivation_receipt_id, issued_at,
                  issued_by, run_id, candidate_id=None,
                  covered_candidate_ids=None):
    """The receipt as it will be stored.

    Every field is something the DAO observed or computed. `selector` is
    recorded as the hint it was, so an audit can see what was asked for -- it
    is not a grant: the extent below came from the detector.
    """
    receipt = {
        "scheme": RECEIPT_SCHEME,
        "case_id": case_id,
        "document_id": document_id,
        "candidate_id": candidate_id,
        # Every inventory candidate this one receipt accounts for. A multi-page
        # table is detected once per page but extracted once, so coverage has
        # to know about all of them.
        "covered_candidate_ids": list(covered_candidate_ids
                                      if covered_candidate_ids is not None
                                      else ([candidate_id] if candidate_id
                                            else [])),
        "source_pdf_sha256": source_pdf_sha256,
        "source_text_revision_sha256": source_text_revision_sha256,
        "segment_derivation_receipt_id": segment_derivation_receipt_id,
        "detector": detector,
        "selector": selector,
        "extent": extent,
        "regions": regions,
        "issued_at": issued_at,
        "issued_by": issued_by,
        "run_id": run_id,
        "verification_status": "verified",
    }
    receipt["receipt_id"] = compute_receipt_id(receipt)
    return receipt


def compute_receipt_id(receipt: dict) -> str:
    """Derive the receipt's identity from provenance and detector output.

    Explicitly NOT from `table_uid`: RT is computed from the table's source
    regions, so keying the receipt on it would make the regions authorize the
    receipt that authorizes the regions. Timestamps, issuer and run are
    excluded so re-deriving identical bytes yields the identical id -- which is
    what makes re-registration a recognizable no-op instead of a new receipt.
    """
    fingerprint = _receipt_fingerprint(receipt)
    digest = hashlib.sha256(
        repr(fingerprint).encode("utf-8")).hexdigest()
    return f"TRR-{digest}"


def _receipt_fingerprint(receipt: dict) -> tuple:
    detector = receipt.get("detector") or {}
    return (
        receipt.get("scheme"),
        receipt.get("case_id"),
        receipt.get("document_id"),
        receipt.get("candidate_id"),
        tuple(receipt.get("covered_candidate_ids") or ()),
        receipt.get("source_pdf_sha256"),
        receipt.get("source_text_revision_sha256"),
        receipt.get("segment_derivation_receipt_id"),
        detector.get("profile"),
        detector.get("library"),
        detector.get("library_version"),
        detector.get("config_fingerprint"),
        tuple(
            (item.get("page"), item.get("start_char"), item.get("end_char"),
             item.get("quote"), item.get("page_text_sha256"),
             tuple(item.get("bbox") or []))
            for item in receipt.get("extent") or []
        ),
        tuple(
            (item.get("page"), item.get("kind"), item.get("start_char"),
             item.get("end_char"), item.get("quote"),
             tuple(item.get("bbox") or []),
             tuple(
                 (cell.get("page"), cell.get("start_char"),
                  cell.get("end_char"), cell.get("quote"))
                 for cell in item.get("cells") or []))
            for item in receipt.get("regions") or []
        ),
    )


def receipt_fingerprint(receipt: dict) -> tuple:
    """What makes two receipts the same derivation (excludes issuance data)."""
    return _receipt_fingerprint(receipt)


def detector_fingerprint(profile: str) -> str:
    config = DETECTOR_PROFILES.get(profile) or {}
    return hashlib.sha256(
        repr(sorted(config.items())).encode("utf-8")).hexdigest()[:32]


def sentinel_fingerprint(profile: str) -> str:
    config = SENTINEL_PROFILES.get(profile) or {}
    return hashlib.sha256(
        repr(sorted(config.items())).encode("utf-8")).hexdigest()[:32]


def current_pymupdf_version() -> str | None:
    """The detector runtime identity without opening or scanning a document."""
    try:
        import fitz  # pymupdf
    except ImportError:
        return None
    return getattr(fitz, "VersionBind", None) or None


# --- currency --------------------------------------------------------------

def index_integrity_errors(index: dict, case_id: str,
                           location: str = "") -> list[str]:
    """Re-derive the index's own claims before any receipt in it is trusted.

    The first P0-8 pass read the index with `json.loads` and used it. Every
    later check then compared a receipt's recorded fields against the world --
    but nothing checked the receipt against ITSELF, so `receipt_id` was only
    ever a string that matched. Editing a receipt's extent in place, keeping
    its id, produced a receipt that resolved and passed.

    `receipt_id` is a hash of exactly the fields that define the derivation, so
    recomputing it is a complete integrity check for the receipt body. Anything
    that fails here is a corrupt index, reported as such -- deliberately NOT
    degraded to "this document has no receipts", which would read as an
    ordinary missing-receipt error and invite re-issuing over a damaged file.
    """
    prefix = f"{location}: " if location else ""
    errors: list[str] = []
    receipts = index.get("tables")
    if not isinstance(receipts, list):
        return [f"{prefix}table region index integrity: `tables` is not a list"]

    seen: dict[str, dict] = {}
    for position, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            errors.append(
                f"{prefix}table region index integrity: entry {position} is "
                "not an object")
            continue
        receipt_id = receipt.get("receipt_id")
        recomputed = compute_receipt_id(receipt)
        if receipt_id != recomputed:
            errors.append(
                f"{prefix}table region index integrity: receipt "
                f"{receipt_id!r} does not hash to its own contents "
                f"(recomputed {recomputed!r}) -- the receipt body was changed "
                "after issuance, so the id no longer identifies the derivation "
                "it names")
            continue
        if receipt_id in seen:
            if seen[receipt_id] != receipt:
                errors.append(
                    f"{prefix}table region index integrity: receipt_id "
                    f"{receipt_id!r} appears twice with different contents")
            else:
                errors.append(
                    f"{prefix}table region index integrity: duplicate "
                    f"receipt_id {receipt_id!r}")
            continue
        seen[receipt_id] = receipt
        if receipt.get("case_id") != case_id:
            errors.append(
                f"{prefix}table region index integrity: receipt {receipt_id!r} "
                f"is bound to case {receipt.get('case_id')!r}, but sits in "
                f"{case_id}'s index")
    return errors


def detector_currency_errors(receipt: dict, location: str = "") -> list[str]:
    """Whether the receipt's detector is still the detector this build runs.

    P0-8's first pass recorded a detector fingerprint in every receipt and then
    never compared it -- the completion report claimed a profile change made a
    receipt stale, and the code did not implement that. An extent is a function
    of the detector that produced it, so retuning detection under a stable
    profile name silently re-authorized every previously-derived extent.

    Compared here, from metadata only: verification never re-runs
    `find_tables()`, which keeps this cheap enough for every write.
    """
    prefix = f"{location}: " if location else ""
    detector = receipt.get("detector") or {}
    profile = detector.get("profile")
    if profile not in DETECTOR_PROFILES:
        return [
            f"{prefix}the receipt was derived with detector profile "
            f"{profile!r}, which this build does not define -- a profile that "
            "no longer exists cannot be confirmed to produce this extent, so "
            "the derivation must be re-run"
        ]
    expected = detector_fingerprint(profile)
    recorded = detector.get("config_fingerprint")
    if recorded != expected:
        return [
            f"{prefix}detector profile {profile!r} has been reconfigured since "
            f"this receipt was issued (receipt {recorded!r}, current "
            f"{expected!r}) -- the table's extent is a function of the "
            "detector that derived it, so it must be re-derived"
        ]
    return []


def receipt_currency_errors(*, receipt: dict, current_pdf_sha256: str | None,
                            current_revision_sha256: str | None,
                            current_pages: dict | None,
                            segment_receipt_id: str | None = None,
                            location: str = "") -> list[str]:
    """Whether a receipt still describes the current world.

    A receipt is a statement about specific PDF bytes and specific processed
    bytes producing a specific extent. Either side moving makes it a true
    statement about a state that no longer exists, which downstream must not be
    allowed to rely on. `current_pdf_sha256` is recomputed from the raw file at
    check time, never read from a recorded field -- comparing a recorded field
    to a recorded field would prove only that two fields agree.
    """
    prefix = f"{location}: " if location else ""
    errors: list[str] = []

    if receipt.get("scheme") != RECEIPT_SCHEME:
        return [
            f"{prefix}table-region receipt scheme {receipt.get('scheme')!r} is "
            f"not {RECEIPT_SCHEME!r} -- an unrecognized scheme is refused "
            "rather than assumed equivalent"
        ]

    errors.extend(detector_currency_errors(receipt, location))

    recorded_pdf = receipt.get("source_pdf_sha256")
    if current_pdf_sha256 is None:
        errors.append(
            f"{prefix}the document's registered raw source cannot be read or "
            "hashed now -- the region receipt cannot be re-confirmed against "
            "the file it was issued for")
    elif recorded_pdf != current_pdf_sha256:
        errors.append(
            f"{prefix}the raw source changed: the receipt was issued against "
            f"{recorded_pdf!r}, the registered file now hashes to "
            f"{current_pdf_sha256!r} -- every region derived from the old "
            "bytes is void and must be re-registered")

    recorded_revision = receipt.get("source_text_revision_sha256")
    if current_revision_sha256 is None:
        errors.append(
            f"{prefix}the document has no current registered source-text "
            "revision -- there is nothing for the receipt's offsets to index "
            "into")
    elif recorded_revision != current_revision_sha256:
        errors.append(
            f"{prefix}the source text was revised after this receipt was "
            f"issued (receipt {recorded_revision!r}, current "
            f"{current_revision_sha256!r}) -- the regions were derived against "
            "text that is no longer current, so they must be re-registered")

    if segment_receipt_id is not None or receipt.get(
            "segment_derivation_receipt_id") is not None:
        recorded_segment = receipt.get("segment_derivation_receipt_id")
        if recorded_segment != segment_receipt_id:
            errors.append(
                f"{prefix}the P0-6 segment derivation this receipt was bound "
                f"to changed (receipt {recorded_segment!r}, current "
                f"{segment_receipt_id!r}) -- the logical/physical page mapping "
                "underneath these regions is no longer the one they were "
                "derived against")

    # Page-level digests: catch a revision whose pointer happens to match but
    # whose page bodies do not (a torn or partially-restored revision file).
    if current_pages is not None and not errors:
        for region in list(receipt.get("extent") or []) + list(
                receipt.get("regions") or []):
            page = region.get("page")
            body = current_pages.get(page)
            if body is None:
                errors.append(
                    f"{prefix}page {page} no longer exists in the registered "
                    "revision, but the receipt derives a region from it")
                continue
            recorded_digest = region.get("page_text_sha256")
            if recorded_digest and recorded_digest != text_sha256(
                    canonical_page_text(body)):
                errors.append(
                    f"{prefix}page {page}'s registered text no longer matches "
                    "the bytes this receipt's regions were derived from")

    return errors


# --- binding a contract to its receipt -------------------------------------

SCAN_SCHEME = "table_candidate_scan_v2"


def inventory_integrity_errors(inventory: dict, document_id: str,
                               location: str = "") -> list[str]:
    """Re-derive the inventory's own claims before any of it is relied on.

    Same reasoning as `index_integrity_errors`, one level up. Without this the
    inventory was a list somebody could edit: deleting the entry for a table
    nobody extracted left the remaining candidate_ids each verifying against
    their own geometry, so the omission was invisible exactly where it mattered
    most -- the artifact whose entire job is to say what else is in the
    document.

    Checked here, all fail-closed: the scan's own scheme, `scan_id` recomputed
    from the whole body, every `candidate_id` recomputed from its own geometry,
    no duplicate candidate ids, and the inventory bound to the document it sits
    under. Anything failing is reported as a corrupt scan, deliberately NOT as
    "no scan exists" -- that reading would invite re-scanning over a tampered
    file and calling the result clean.
    """
    prefix = f"{location}: " if location else ""
    errors: list[str] = []

    scheme = inventory.get("scheme")
    if scheme != SCAN_SCHEME:
        return [
            f"{prefix}table candidate scan scheme {scheme!r} is not "
            f"{SCAN_SCHEME!r} -- an unrecognized scan is refused rather than "
            "assumed equivalent"
        ]
    if inventory.get("document_id") != document_id:
        errors.append(
            f"{prefix}the candidate scan is bound to document "
            f"{inventory.get('document_id')!r}, not {document_id!r}")

    candidates = inventory.get("candidates")
    if not isinstance(candidates, list):
        return errors + [
            f"{prefix}table candidate scan integrity: `candidates` is not a "
            "list"]

    seen: set = set()
    for position, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            errors.append(
                f"{prefix}table candidate scan integrity: entry {position} is "
                "not an object")
            continue
        candidate_id = candidate.get("candidate_id")
        try:
            recomputed = compute_candidate_id(
                document_id=inventory.get("document_id"),
                source_pdf_sha256=inventory.get("source_pdf_sha256"),
                source_text_revision_sha256=inventory.get(
                    "source_text_revision_sha256"),
                detector_profile=inventory.get("detector_profile"),
                config_fingerprint=inventory.get(
                    "detector_config_fingerprint"),
                pages_geometry=(
                    candidate.get("page"), candidate.get("physical_page"),
                    tuple(round(float(v), 2)
                          for v in candidate.get("bbox") or ())),
            )
        except (TypeError, ValueError) as exc:
            errors.append(
                f"{prefix}table candidate scan integrity: candidate "
                f"{candidate_id!r} has unusable geometry: {exc}")
            continue
        if candidate_id != recomputed:
            errors.append(
                f"{prefix}table candidate scan integrity: candidate "
                f"{candidate_id!r} does not hash to its own geometry "
                f"(recomputed {recomputed!r}) -- the entry was changed after "
                "the scan")
            continue
        if candidate_id in seen:
            errors.append(
                f"{prefix}table candidate scan integrity: duplicate "
                f"candidate_id {candidate_id!r}")
            continue
        seen.add(candidate_id)
        if candidate.get("page") not in set(
                inventory.get("scanned_logical_pages") or ()):
            errors.append(
                f"{prefix}candidate {candidate_id!r} cites logical page "
                f"{candidate.get('page')}, which the scan does not claim to "
                "have examined")

    possible_tables = inventory.get("possible_tables")
    if not isinstance(possible_tables, list):
        return errors + [
            f"{prefix}table candidate scan integrity: `possible_tables` is not "
            "a list"]
    seen_signals: set = set()
    for position, signal in enumerate(possible_tables):
        if not isinstance(signal, dict):
            errors.append(
                f"{prefix}table candidate scan integrity: possible-table entry "
                f"{position} is not an object")
            continue
        signal_id = signal.get("signal_id")
        try:
            recomputed = compute_possible_table_id(
                document_id=signal.get("document_id"),
                source_pdf_sha256=inventory.get("source_pdf_sha256"),
                source_text_revision_sha256=inventory.get(
                    "source_text_revision_sha256"),
                sentinel_profile=signal.get("sentinel_profile"),
                sentinel_config_fingerprint=inventory.get(
                    "sentinel_config_fingerprint"),
                page=signal.get("page"),
                physical_page=signal.get("physical_page"),
                bbox=signal.get("bbox") or (),
                reason=signal.get("reason"),
                page_text_sha256=signal.get("page_text_sha256"),
            )
        except (TypeError, ValueError) as exc:
            errors.append(
                f"{prefix}table candidate scan integrity: possible-table "
                f"{signal_id!r} has unusable provenance: {exc}")
            continue
        if signal_id != recomputed:
            errors.append(
                f"{prefix}table candidate scan integrity: possible-table "
                f"{signal_id!r} does not hash to its own provenance "
                f"(recomputed {recomputed!r})")
            continue
        if signal.get("document_id") != document_id:
            errors.append(
                f"{prefix}possible-table {signal_id!r} is bound to document "
                f"{signal.get('document_id')!r}, not {document_id!r}")
        if signal.get("sentinel_profile") != inventory.get("sentinel_profile"):
            errors.append(
                f"{prefix}possible-table {signal_id!r} names sentinel profile "
                f"{signal.get('sentinel_profile')!r}, but its parent scan names "
                f"{inventory.get('sentinel_profile')!r}")
        if signal.get("sentinel_library_version") != inventory.get(
                "sentinel_library_version"):
            errors.append(
                f"{prefix}possible-table {signal_id!r} names PyMuPDF version "
                f"{signal.get('sentinel_library_version')!r}, but its parent "
                f"scan names {inventory.get('sentinel_library_version')!r}")
        if signal.get("page") not in set(
                inventory.get("scanned_logical_pages") or ()):
            errors.append(
                f"{prefix}possible-table {signal_id!r} cites logical page "
                f"{signal.get('page')}, which the scan does not claim to have "
                "examined")
        if signal.get("status") != "review_required":
            errors.append(
                f"{prefix}possible-table {signal_id!r} has status "
                f"{signal.get('status')!r}; sentinel findings are never "
                "self-verifying and must remain review_required")
        if signal_id in seen_signals:
            errors.append(
                f"{prefix}table candidate scan integrity: duplicate "
                f"possible-table signal_id {signal_id!r}")
        seen_signals.add(signal_id)

    status = inventory.get("scan_status")
    scan_errors = inventory.get("scan_errors")
    if not isinstance(scan_errors, list):
        errors.append(
            f"{prefix}table candidate scan integrity: `scan_errors` is not a "
            "list")
        scan_errors = []
    if status == "complete_no_candidates":
        if candidates or possible_tables or scan_errors:
            errors.append(
                f"{prefix}complete_no_candidates is inconsistent: it requires "
                "zero strict candidates, zero possible-table signals, and no "
                "scan errors")
    elif status == "complete_with_candidates":
        if not candidates or possible_tables or scan_errors:
            errors.append(
                f"{prefix}complete_with_candidates is inconsistent: it "
                "requires at least one strict candidate, no unresolved "
                "possible-table signals, and no scan errors")
    elif status == "inconclusive":
        if not possible_tables and not scan_errors:
            errors.append(
                f"{prefix}inconclusive scan has neither a possible-table "
                "signal nor an error explaining why it is inconclusive")
    elif status == "failed":
        if not scan_errors:
            errors.append(
                f"{prefix}failed scan has no recorded scan_errors")
    else:
        errors.append(
            f"{prefix}unknown table candidate scan_status {status!r}")

    recomputed_scan = compute_scan_id(inventory)
    if inventory.get("scan_id") != recomputed_scan:
        errors.append(
            f"{prefix}table candidate scan integrity: scan "
            f"{inventory.get('scan_id')!r} does not hash to its own contents "
            f"(recomputed {recomputed_scan!r}) -- the candidate list was "
            "changed after the scan, so it no longer states what the detector "
            "found. A candidate removed from a scan is the exact omission this "
            "inventory exists to make visible")
    return errors


def inventory_currency_errors(inventory: dict, *, current_pdf_sha256,
                              current_revision_sha256, current_pages,
                              segment_receipt_id=None,
                              location: str = "") -> list[str]:
    """Whether the scan still describes the document as it is now.

    A scan is a statement that THESE bytes, read with THIS detector, contain
    exactly these tables. Any input moving makes it a true statement about a
    state that no longer exists -- and a stale scan is worse than none, because
    a table added by a revision would sit outside it entirely while every
    surviving candidate still verified.
    """
    prefix = f"{location}: " if location else ""
    errors: list[str] = []

    profile = inventory.get("detector_profile")
    if profile not in DETECTOR_PROFILES:
        errors.append(
            f"{prefix}the scan was run with detector profile {profile!r}, "
            "which this build does not define -- it must be re-run")
    elif inventory.get("detector_config_fingerprint") != detector_fingerprint(
            profile):
        errors.append(
            f"{prefix}detector profile {profile!r} has been reconfigured since "
            "this scan was run -- which tables the detector finds is a "
            "function of its configuration, so the document must be re-scanned")

    sentinel_profile = inventory.get("sentinel_profile")
    if sentinel_profile not in SENTINEL_PROFILES:
        errors.append(
            f"{prefix}the scan used sentinel profile {sentinel_profile!r}, "
            "which this build does not define -- it must be re-run")
    elif inventory.get("sentinel_config_fingerprint") != sentinel_fingerprint(
            sentinel_profile):
        errors.append(
            f"{prefix}sentinel profile {sentinel_profile!r} has been "
            "reconfigured since this scan was run -- a broader detector may "
            "now find possible tables the old scan missed; re-scan")

    current_library = current_pymupdf_version()
    if current_library is None:
        errors.append(
            f"{prefix}the PyMuPDF detector runtime is unavailable, so neither "
            "the strict nor sentinel derivation can be confirmed")
    else:
        for label, recorded in (
                ("strict detector", inventory.get("detector_library_version")),
                ("sentinel", inventory.get("sentinel_library_version"))):
            if recorded != current_library:
                errors.append(
                    f"{prefix}{label} PyMuPDF version changed since the scan "
                    f"(scan {recorded!r}, current {current_library!r}) -- table "
                    "detection output is version-dependent; re-scan")

    if current_pdf_sha256 is None:
        errors.append(
            f"{prefix}the document's registered raw source cannot be read or "
            "hashed now, so the scan cannot be re-confirmed against it")
    elif inventory.get("source_pdf_sha256") != current_pdf_sha256:
        errors.append(
            f"{prefix}the raw source changed since this scan "
            f"(scan {inventory.get('source_pdf_sha256')!r}, current "
            f"{current_pdf_sha256!r}) -- a table added or removed by the new "
            "bytes would be invisible to it; re-scan")

    if current_revision_sha256 is None:
        errors.append(
            f"{prefix}the document has no current registered source-text "
            "revision to confirm the scan against")
    elif inventory.get("source_text_revision_sha256") != \
            current_revision_sha256:
        errors.append(
            f"{prefix}the source text was revised after this scan "
            f"(scan {inventory.get('source_text_revision_sha256')!r}, current "
            f"{current_revision_sha256!r}) -- re-scan")

    if segment_receipt_id is not None or inventory.get(
            "segment_derivation_receipt_id") is not None:
        recorded = inventory.get("segment_derivation_receipt_id")
        if recorded != segment_receipt_id:
            errors.append(
                f"{prefix}the P0-6 segment derivation this scan was bound to "
                f"changed (scan {recorded!r}, current {segment_receipt_id!r}) "
                "-- which physical pages this document owns is no longer what "
                "was scanned; re-scan")

    # The scan must have covered every logical page the document owns NOW. A
    # scan of pages 1-3 cannot speak for a page 4 that exists today, and
    # "page 4 was not scanned" must never read as "page 4 has no table".
    if current_pages is not None:
        scanned = {int(page) for page in
                   inventory.get("scanned_logical_pages") or ()}
        unscanned = sorted(set(current_pages) - scanned)
        if unscanned:
            errors.append(
                f"{prefix}logical page(s) {unscanned} of this document were "
                "never covered by the candidate scan -- an unscanned page is "
                "not a page without a table; re-scan the document")
        for signal in inventory.get("possible_tables") or []:
            page = signal.get("page")
            body = current_pages.get(page)
            if body is None:
                errors.append(
                    f"{prefix}possible-table {signal.get('signal_id')!r} cites "
                    f"logical page {page}, which is absent from the current "
                    "registered revision")
                continue
            expected = text_sha256(canonical_page_text(body))
            if signal.get("page_text_sha256") != expected:
                errors.append(
                    f"{prefix}possible-table {signal.get('signal_id')!r} is "
                    f"bound to different text on logical page {page}; re-scan")
    return errors


def candidate_coverage_errors(inventory: dict | None, receipts,
                              extracted_receipt_ids, human_reviewed_ids=(),
                              location: str = "") -> list[str]:
    """Every table the detector found must have a disposition.

    Verifying the receipts a contract happens to cite answers "is this table
    complete?" but never "were there other tables?". A document with two
    appendices could have one extracted and the other never mentioned by
    anyone, and every check would pass -- the same omission P0-8 closed at row
    level, one level up.

    A candidate is accounted for when it was extracted as a verified table, or
    when a genuine human review recorded a decision about it. There is
    deliberately NO third option and no agent-writable "not a table" field: a
    self-declared exemption is exactly the self-declaration this module exists
    to remove.

    **The human-review escape hatch is a SEAM, not a finished feature.**
    `human_review_ledger.schema.json`'s `artifact_kind` enum does not yet admit
    `table_candidate`, so today no such record can actually be written and the
    only way past this gate is to extract the table. That is deliberate for
    this pass -- fail-closed and stated -- and adding the kind is a schema +
    CLI change, not something an agent can reach by writing a field. See
    known-gaps.md.
    """
    prefix = f"{location}: " if location else ""
    if inventory is None:
        return [
            f"{prefix}no DAO-derived table candidate scan exists for this "
            "document -- without a scan of the whole document, a table the "
            "detector would have found but nobody extracted is invisible, and "
            "the ABSENCE of a scan is not evidence that there are no tables. "
            "Run `dao.py scan-table-candidates`"
        ]
    extracted = set(extracted_receipt_ids or ())
    reviewed = set(human_reviewed_ids or ())
    covered = set()
    for receipt in receipts or []:
        if receipt.get("receipt_id") not in extracted:
            continue
        covered.update(receipt.get("covered_candidate_ids")
                       or [receipt.get("candidate_id")])
    errors: list[str] = []
    for candidate in inventory.get("candidates") or []:
        candidate_id = candidate.get("candidate_id")
        if candidate_id in covered or candidate_id in reviewed:
            continue
        errors.append(
            f"{prefix}table candidate {candidate_id} (logical page "
            f"{candidate.get('page')}, {candidate.get('row_count')} row bands, "
            f"header {candidate.get('header_preview')!r}) was detected in the "
            "registered PDF but no reference_table extracts it -- a detected "
            "table with no disposition blocks finalization; its absence from "
            "the extraction is not evidence that it is unimportant. Register "
            "and extract it, or (not yet implemented -- see known-gaps.md) "
            "record an authenticated human review of it")
    return errors


def _span_key(span: dict) -> tuple:
    return (span.get("page"), span.get("start_char"), span.get("end_char"))


def _region_key(region: dict) -> tuple:
    return (region.get("page"), region.get("start_char"),
            region.get("end_char"))


def _fmt(region: dict) -> str:
    quote = (region.get("quote") or "").replace("\n", " ")
    if len(quote) > 40:
        quote = quote[:40] + "..."
    return (f"page {region.get('page')} "
            f"[{region.get('start_char')}:{region.get('end_char')}] "
            f"{quote!r}")


def contract_binding_errors(data: dict, receipts, pages: dict,
                            location_prefix: str = "tables") -> list[str]:
    """Every reason a reference table's structure disagrees with its receipt.

    This is the P0-8 gate proper. It answers the question the pre-P0-8 checks
    could not ask: is the declared region the table's real extent, and is every
    band the detector called a data row actually present as an extracted row?

    Relations enforced, all of them exact rather than "compatible with":

      1. `source_regions` == the receipt's extent, set-for-set
      2. every extracted row corresponds to a receipt `data_row`
      3. every receipt `data_row` corresponds to exactly one extracted row
      4. a `header_span` may only cover a band the receipt classified NON-data
      5. an `ambiguous` band forces review; it is never an exemption

    2 and 3 together are a bijection, so a missing row, a duplicated row and an
    invented row are each refused with their own message.
    """
    errors: list[str] = []
    by_id = {receipt.get("receipt_id"): receipt for receipt in receipts or []}

    for table_index, table in enumerate(data.get("tables") or []):
        tloc = f"{location_prefix}[{table_index}]"
        receipt_id = table.get("table_region_receipt_id")
        if not receipt_id:
            errors.append(
                f"{tloc}: no table_region_receipt_id -- the table's "
                "source_regions are an unverified self-declaration. A region "
                "is issued only by `dao.py register-table-region`, which "
                "re-derives the table's extent from the registered PDF's own "
                "layout; without it a table declared narrower than it really "
                "is passes every downward check while hiding rows")
            continue
        receipt = by_id.get(receipt_id)
        if receipt is None:
            errors.append(
                f"{tloc}: table_region_receipt_id {receipt_id!r} does not "
                f"resolve to a {RECEIPT_SCHEME} receipt issued for this "
                "document -- a receipt reference is checked against the "
                "DAO-owned index, never accepted as written")
            continue
        errors.extend(_table_binding_errors(table, receipt, pages, tloc))
    return errors


def _table_binding_errors(table: dict, receipt: dict, pages: dict,
                          tloc: str) -> list[str]:
    errors: list[str] = []

    review_reasons = classification_review_reasons(receipt.get("regions") or [])
    if review_reasons and table.get("review_required") is not True:
        for reason in review_reasons:
            errors.append(
                f"{tloc}: the derived table region contains a band the "
                f"detector could not classify ({reason}); such a table may "
                "only be written with review_required=true")

    # 1. The declared extent must BE the derived extent.
    declared = {_span_key(region)
                for region in table.get("source_regions") or []}
    derived = {_region_key(region) for region in receipt.get("extent") or []}
    for missing in sorted(derived - declared):
        match = next(region for region in receipt["extent"]
                     if _region_key(region) == missing)
        errors.append(
            f"{tloc}.source_regions: the table's derived extent includes "
            f"{_fmt(match)}, which the contract does not declare -- a region "
            "drawn narrower than the detected table hides every row outside it "
            "from coverage checking")
    for extra in sorted(declared - derived):
        match = next(
            region for region in table.get("source_regions") or []
            if _span_key(region) == extra)
        errors.append(
            f"{tloc}.source_regions: the contract declares {_fmt(match)}, "
            "which is not part of the extent the detector derived for this "
            "table -- a region may not be widened to swallow text outside the "
            "table")

    # 2/3. Rows against the receipt's data bands, as a bijection.
    #
    # Deliberately NOT skipped when the extent already disagreed. A narrowed
    # extent's whole purpose is to conceal specific rows, so reporting only
    # "the region is wrong" would leave the operator to work out WHICH rows
    # were hidden -- naming them is the actionable half of the finding.
    data_bands = {_region_key(region): region
                  for region in receipt.get("regions") or []
                  if region.get("kind") == DATA_ROW_KIND}
    non_data = {_region_key(region): region
                for region in receipt.get("regions") or []
                if region.get("kind") != DATA_ROW_KIND}

    claimed: dict[tuple, int] = {}
    for row_index, row in enumerate(table.get("rows") or []):
        rloc = f"{tloc}.rows[{row_index}]"
        span = row.get("source_span") or {}
        key = _span_key(span)
        if key in data_bands:
            if key in claimed:
                errors.append(
                    f"{rloc}: this row's source range {_fmt(span)} is already "
                    f"extracted as rows[{claimed[key]}] -- one detected data "
                    "row may not be claimed by more than one extracted row")
                continue
            claimed[key] = row_index
            continue
        if key in non_data:
            errors.append(
                f"{rloc}: source range {_fmt(span)} was classified by the "
                f"detector as {non_data[key].get('kind')!r}, not a data row -- "
                "a non-data band may not be extracted as a table row")
            continue
        errors.append(
            f"{rloc}: source range {_fmt(span)} corresponds to no data_row in "
            "the derived table region -- an extracted row must be a row the "
            "detector actually found in the PDF's layout")

    for key, band in sorted(data_bands.items()):
        if key not in claimed:
            errors.append(
                f"{tloc}: the detector derived a data row at {_fmt(band)} that "
                "no extracted row corresponds to -- this row exists in the "
                "source table and is MISSING from the extraction")

    # 4. A header span may only cover a band the receipt called non-data.
    for header_index, header in enumerate(table.get("header_spans") or []):
        hloc = f"{tloc}.header_spans[{header_index}]"
        span = header.get("span") or {}
        key = _span_key(span)
        if key in data_bands:
            errors.append(
                f"{hloc}: {_fmt(span)} is a data row in the derived table "
                f"region, but is declared as {header.get('kind')!r} -- "
                "reclassifying a data row as a header/note removes it from "
                "coverage checking, which is the same omission as dropping it")
            continue
        if key not in non_data:
            errors.append(
                f"{hloc}: {_fmt(span)} corresponds to no band the detector "
                "classified for this table -- a header span may not be "
                "asserted over text the derivation never covered")
            continue
        derived_kind = non_data[key].get("kind")
        if derived_kind == AMBIGUOUS_KIND:
            errors.append(
                f"{hloc}: {_fmt(span)} could not be classified by the "
                "detector, so it may not be declared "
                f"{header.get('kind')!r} -- an ambiguous band is a review "
                "item, not a coverage exemption")
        elif header.get("kind") not in _DECLARABLE_HEADER_KINDS:
            errors.append(
                f"{hloc}: kind {header.get('kind')!r} is not a declarable "
                "non-data kind")

    return errors
