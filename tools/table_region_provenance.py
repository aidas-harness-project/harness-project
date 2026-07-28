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
This solves the embedded-text, ruled-table case and REFUSES everything else,
rather than approximating:

  * an image-only / OCR document has no deterministic text layer, and
    verification may not run OCR -> refused (`VERIFIABLE_EXTRACTION_METHODS`)
  * a whitespace-aligned "table" with no vector ruling is not a layout
    structure the detector can establish -> no candidate -> refused
  * two candidates matching one selector -> refused as ambiguous
  * a row band whose text cannot be located EXACTLY and UNIQUELY in the
    registered page text -> refused (`locate_row_text`)
  * merged cells / irregular row structure that make a band's row membership
    ambiguous -> refused

A refusal here is `review_required` territory: the table blocks
`policy_clause_processing` finalization. It is never read as "this page has no
table, so there is nothing to check" -- that reading is exactly how a
narrow-region bypass would survive its own fix.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

RECEIPT_SCHEME = "table_region_v1"
INDEX_SCHEMA = "table_region_index.schema.json"

DEFAULT_DETECTOR_PROFILE = "pymupdf_find_tables_lines_v1"

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

def locate_row_text(page_text: str, cell_texts, search_from: int = 0):
    """Map one detected band to an EXACT, UNIQUE range in the registered text.

    `find_tables()` gives geometry and cell strings; the contract's offsets
    index into the processed page body. Nothing guarantees those two describe
    the same bytes -- the processed text may be redacted, re-flowed, or simply
    a different extraction -- so this refuses whenever the correspondence is
    not exact.

    The band's cells must appear in the page text in order, each found from the
    previous one's end. The returned range runs from the first cell's start to
    the last cell's end, which is the row as the flat text expresses it.

    Returns `(start, end, error)`. A band that cannot be placed yields
    `(None, None, reason)` -- never a best guess, because a best-guess offset
    is indistinguishable from a correct one downstream.
    """
    values = [str(text or "").strip() for text in cell_texts]
    values = [value for value in values if value]
    if not values:
        return None, None, "the detected band carries no cell text"

    cursor = search_from
    first_start = None
    last_end = None
    for value in values:
        # Collapse the detector's internal newlines: a wrapped cell reads as
        # "A\nB" in the layout and "A B" (or "A\nB") in the flat text. Only the
        # cell's own leading token is used as the anchor, and the whole span is
        # verified afterwards, so this widens the SEARCH, never the claim.
        needle = value.split("\n")[0].strip()
        if not needle:
            continue
        position = page_text.find(needle, cursor)
        if position == -1:
            return None, None, (
                f"cell text {needle!r} could not be located in the registered "
                "page text at or after offset "
                f"{cursor} -- the PDF layout and the processed text do not "
                "describe the same bytes, so no exact region can be derived")
        if first_start is None:
            first_start = position
        last_end = position + len(needle)
        cursor = last_end
    if first_start is None or last_end is None:
        return None, None, "the detected band carries no locatable cell text"
    return first_start, last_end, None


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

def build_receipt(*, document_id, case_id, source_pdf_sha256,
                  source_text_revision_sha256, detector, extent, regions,
                  selector, segment_derivation_receipt_id, issued_at,
                  issued_by, run_id):
    """The receipt as it will be stored.

    Every field is something the DAO observed or computed. `selector` is
    recorded as the hint it was, so an audit can see what was asked for -- it
    is not a grant: the extent below came from the detector.
    """
    receipt = {
        "scheme": RECEIPT_SCHEME,
        "case_id": case_id,
        "document_id": document_id,
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


# --- currency --------------------------------------------------------------

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
