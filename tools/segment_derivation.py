"""DAO-owned segment page-map provenance (P0-6).

`segment_lineage.py` checks that a segment's declarations agree with each
other: page_map's logical pages match `segment_page_ranges`, which match the
`<<<PAGE page=N>>>` markers in the segment's own processed text, whose digest
matches `derived_text_sha256`. Every one of those is a comparison between two
values the extracting agent produced. Nothing re-derived the mapping from the
parent PDF.

That is a self-declaration consistency check, not provenance. The bypass it
leaves open is not subtle:

    truth:     logical 12 lives on parent physical page 19
    submitted: logical 12 -> physical 20

and then write `<<<PAGE page=12>>>` above physical page 20's text, set
`segment_page_ranges` to {12,12}, recompute `derived_text_sha256` over those
bytes, and copy a `physical_page_sha256` from the page you actually read. Every
existing check compares two of those five values against each other, and they
all agree, because they were all built from the same lie. The segment now
carries page 20's content under page 12's identity -- and every downstream
evidence quote, policy clause offset and canonical UID is keyed to it.

What closes it is the one comparison nobody was making: against the parent PDF.
The DAO opens the registered parent itself, extracts the physical page itself,
hashes what IT read, and reads the printed page number off that page itself. A
mapping is recorded only when the parent's own pages confirm it.

The receipt
-----------
`register_segment_derivation` (dao.py) issues a `segment_page_map_v2` receipt
into `_segment_derivation_index.json`, a DAO-owned file no agent-facing write
path can touch. The manifest's `page_map` becomes a PROJECTION of that receipt:
if the two disagree, finalization stops. A caller cannot write the projection
directly (`page_map` is sealed in `source_provenance.PROTECTED_MANIFEST_FIELDS`),
so the only way a page_map exists at all is that the DAO derived it.

Naming: the manifest field is `physical_page_sha256`, which reads as "the
digest of the PDF page" but has always been the digest of the TEXT EXTRACTED
FROM that page. The receipt uses the accurate name
`source_page_text_sha256`; the manifest keeps its historical field name for
compatibility and `receipt_projection_errors` maps between them. See the
schema description for the migration note.

What is bound, and to what
--------------------------
A receipt is only valid for the exact triple it was issued against:

    parent_source_pdf_sha256    the bytes of the registered raw parent
    segment_source_text_revision_sha256
                                the registered revision of the segment's text
    per-page source_page_text_sha256 + logical_page_evidence

Re-hash the parent and it disagrees -> stale (someone swapped the source).
Revise the segment text and it disagrees -> stale (the text is no longer the
one this mapping produced). Both are finalization blockers, not warnings.

Offsets
-------
An offset is a search hint and nothing else. `verify_candidate_mapping` uses it
only to pick which physical page to LOOK at; the page is accepted because its
own printed number says the right logical number. A page with no readable
printed number is never accepted on a consistent-offset argument -- it blocks.
There is deliberately no override field, because a writable
`logical_page_confirmed: true` would be the whole gate's off-switch.

OCR segments
------------
`verify_candidate_mapping` requires a deterministic text layer, which an
`ocr_segment` does not have. UID verification may not re-run OCR, so an OCR
segment cannot obtain a receipt here and therefore cannot finalize. That is
fail-closed and stated rather than papered over: P0-6 solves the
embedded-text case and refuses the OCR case, it does not solve both.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

RECEIPT_SCHEME = "segment_page_map_v2"
INDEX_SCHEMA = "segment_derivation_index.schema.json"

# Which derivation methods this verification can actually establish. An
# `ocr_segment` is absent on purpose: confirming its mapping means re-reading
# the parent's pages with an OCR reader, which UID verification is forbidden to
# do. Listing it here with a weaker check would be the fail-open version.
VERIFIABLE_METHODS = frozenset({"embedded_text_segment", "page_extraction"})

_PAGE_MARKER_RE = re.compile(r"(?m)^<<<PAGE page=(\d+)>>>\r?\n?")


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_source_page_text(text: str) -> str:
    """Canonical bytes for an embedded-text page derivation.

    This is deliberately much narrower than evidence quote normalization:
    line-ending transport differences and Unicode normalization are harmless,
    while spaces, punctuation, ordering and wording are identity-bearing and
    must remain exact.
    """
    return unicodedata.normalize(
        "NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def split_segment_revision(text: str) -> tuple[dict[int, str], list[str]]:
    """Strictly split a registered segment revision into page bodies.

    `extract_embedded_segment.py` joins complete parent-page texts with one
    assembly newline.  The newline is not part of either parent page, so this
    parser removes exactly one trailing separator from each page body.  It
    does not strip or collapse any other whitespace.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_PAGE_MARKER_RE.finditer(normalized))
    if not matches:
        return {}, [
            "segment revision has no <<<PAGE page=N>>> markers, so its page "
            "bodies cannot be bound to parent pages"
        ]
    errors: list[str] = []
    if normalized[:matches[0].start()].strip():
        errors.append(
            "segment revision contains non-whitespace content before the "
            "first page marker")
    pages: dict[int, str] = {}
    last_page = 0
    for index, match in enumerate(matches):
        logical = int(match.group(1))
        if logical <= last_page:
            errors.append(
                f"segment page markers are not strictly increasing: "
                f"logical {logical} follows {last_page}")
        last_page = logical
        start = match.end()
        end = (matches[index + 1].start()
               if index + 1 < len(matches) else len(normalized))
        body = normalized[start:end]
        if body.endswith("\n"):
            body = body[:-1]  # exactly the assembler's separator
        if not body.strip():
            errors.append(f"segment logical page {logical} has an empty body")
        if logical in pages:
            errors.append(
                f"segment logical page {logical} appears more than once")
        pages[logical] = canonical_source_page_text(body)
    return pages, errors


def bind_segment_revision(verified_pages: list[dict],
                          segment_revision_text: str,
                          parent_text_for) -> tuple[list[dict], list[str]]:
    """Prove that the registered segment contains the mapped parent pages.

    A revision hash alone proves only that the segment stayed unchanged; it
    does not prove where those bytes came from.  This comparison closes that
    gap page by page before a receipt is issued.
    """
    segment_pages, errors = split_segment_revision(segment_revision_text)
    expected_pages = [page.get("logical_page") for page in verified_pages]
    actual_pages = list(segment_pages)
    if actual_pages != expected_pages:
        errors.append(
            f"segment revision pages {actual_pages} do not exactly match the "
            f"verified mapping pages {expected_pages}")

    bound: list[dict] = []
    for page in verified_pages:
        logical = page.get("logical_page")
        physical = page.get("source_physical_page")
        source_text = parent_text_for(physical)
        segment_text = segment_pages.get(logical)
        if source_text is None:
            errors.append(
                f"logical {logical}: parent physical page {physical} text is "
                "unavailable while binding the segment revision")
            continue
        if segment_text is None:
            continue
        canonical_source = canonical_source_page_text(source_text)
        source_digest = text_sha256(canonical_source)
        segment_digest = text_sha256(segment_text)
        if source_digest != page.get("source_page_text_sha256"):
            errors.append(
                f"logical {logical}: the verified source-page digest changed "
                "before the segment revision could be bound")
        if segment_digest != source_digest:
            errors.append(
                f"logical {logical}: registered segment page text does not "
                f"exactly equal parent physical page {physical} text "
                f"(segment {segment_digest}, parent {source_digest})")
        item = dict(page)
        item["segment_page_text_sha256"] = segment_digest
        bound.append(item)
    if errors:
        return [], errors
    return bound, []


def segment_revision_binding_errors(receipt: dict,
                                    segment_revision_text: str | None,
                                    location: str = "") -> list[str]:
    """Re-check a receipt against the current registered segment bytes."""
    prefix = f"{location}: " if location else ""
    if segment_revision_text is None:
        return [
            f"{prefix}the current registered segment revision cannot be read "
            "for page-by-page derivation verification"
        ]
    pages, errors = split_segment_revision(segment_revision_text)
    result = [f"{prefix}{error}" for error in errors]
    expected = [page.get("logical_page") for page in receipt.get("pages") or []]
    if list(pages) != expected:
        result.append(
            f"{prefix}current segment revision pages {list(pages)} do not "
            f"match receipt pages {expected}")
        return result
    for page in receipt.get("pages") or []:
        logical = page.get("logical_page")
        body = pages.get(logical)
        if body is None:
            continue
        actual = text_sha256(body)
        recorded_segment = page.get("segment_page_text_sha256")
        recorded_source = page.get("source_page_text_sha256")
        if actual != recorded_segment:
            result.append(
                f"{prefix}logical {logical}: current segment page digest "
                f"{actual!r} does not match receipt "
                f"{recorded_segment!r}")
        if recorded_segment != recorded_source:
            result.append(
                f"{prefix}logical {logical}: receipt does not prove exact "
                "derivation because its segment and parent page digests differ")
    return result


# --- candidate verification -----------------------------------------------

def verify_candidate_mapping(logical_pages, page_offset_candidate,
                             page_text_for, printed_page_finder,
                             parent_total_pages=None):
    """Re-derive the mapping from the parent's own pages.

    `page_text_for(physical)` must return the text the DAO extracted from that
    physical page of the REGISTERED parent, or None. `printed_page_finder(text,
    logical)` returns the verbatim printed marker confirming that logical
    number, or None.

    Returns `(pages, errors)`. `pages` is only usable when `errors` is empty --
    a partially confirmed mapping is not a mapping, so the caller gets nothing
    to fall back on.
    """
    pages: list[dict] = []
    errors: list[str] = []
    seen_physical: dict[int, int] = {}

    for logical in logical_pages:
        if not isinstance(logical, int) or logical < 1:
            errors.append(f"logical page {logical!r} is not a positive integer")
            continue
        physical = logical + page_offset_candidate
        if physical < 1:
            errors.append(
                f"logical {logical} with candidate offset "
                f"{page_offset_candidate:+d} lands on physical page {physical}, "
                "which is not a page")
            continue
        if (isinstance(parent_total_pages, int)
                and physical > parent_total_pages):
            errors.append(
                f"logical {logical} -> physical {physical} exceeds the parent's "
                f"{parent_total_pages} physical pages")
            continue
        if physical in seen_physical:
            # Two logical pages resolving to one physical page would mint two
            # identities for the same source page.
            errors.append(
                f"logical {logical} and logical {seen_physical[physical]} both "
                f"resolve to physical page {physical}")
            continue
        seen_physical[physical] = logical

        page_record = page_text_for(physical)
        if page_record is None:
            errors.append(
                f"logical {logical} -> physical {physical}: the parent PDF has "
                "no such page, or its text could not be extracted")
            continue
        text = (page_record.get("text")
                if isinstance(page_record, dict) else page_record)
        evidence = printed_page_finder(page_record, logical)
        if evidence is None:
            errors.append(
                f"logical {logical} -> physical {physical}: no printed page "
                f"number in a verified header/footer region confirms logical "
                f"{logical} (candidate "
                f"offset {page_offset_candidate:+d} is unverified here) -- "
                "review this page; an offset that is merely consistent "
                "elsewhere does not prove this mapping")
            continue
        pages.append({
            "logical_page": logical,
            "source_physical_page": physical,
            # Named for what it is: the digest of the text the DAO extracted
            # from that physical page, not of the page's PDF bytes.
            "source_page_text_sha256": text_sha256(
                canonical_source_page_text(text)),
            "logical_page_evidence": evidence,
        })

    if errors:
        return [], errors
    return pages, []


def submitted_value_conflicts(pages, submitted_page_map):
    """Cross-check a caller's optional page_map against what the DAO derived.

    The caller's values are never inputs -- `pages` is already built entirely
    from what the DAO read. This exists so a submitted mapping that DISAGREES is
    reported as the finding it is, rather than silently discarded: a caller
    trying to record physical 20 for logical 12 should get a refusal naming the
    conflict, not a success whose stored value quietly differs from what it
    asked for.
    """
    if not submitted_page_map:
        return []
    derived = {p["logical_page"]: p for p in pages}
    errors: list[str] = []
    for entry in submitted_page_map:
        if not isinstance(entry, dict):
            errors.append(f"submitted page_map entry {entry!r} is not an object")
            continue
        logical = entry.get("logical_page")
        mine = derived.get(logical)
        if mine is None:
            errors.append(
                f"submitted page_map claims logical page {logical!r}, which is "
                "not in the verified mapping")
            continue
        claimed_physical = entry.get("source_physical_page")
        if (claimed_physical is not None
                and claimed_physical != mine["source_physical_page"]):
            errors.append(
                f"logical {logical}: submitted source_physical_page "
                f"{claimed_physical} but the parent PDF's own printed page "
                f"number puts logical {logical} on physical page "
                f"{mine['source_physical_page']} -- the submitted mapping is "
                "refused, not corrected")
        # The manifest's historical name and the receipt's accurate name are
        # both accepted from a caller, and both compared against the digest the
        # DAO computed over the text IT extracted.
        for key in ("source_page_text_sha256", "physical_page_sha256"):
            claimed = entry.get(key)
            if claimed is not None and claimed != mine["source_page_text_sha256"]:
                errors.append(
                    f"logical {logical}: submitted {key} {claimed!r} does not "
                    f"match the digest of the text the DAO extracted from "
                    f"physical page {mine['source_physical_page']} "
                    f"({mine['source_page_text_sha256']!r})")
        claimed_evidence = entry.get("logical_page_evidence")
        verified_evidence = mine["logical_page_evidence"]
        verified_quote = (
            verified_evidence.get("quote")
            if isinstance(verified_evidence, dict) else verified_evidence)
        if isinstance(claimed_evidence, dict):
            claimed_evidence = claimed_evidence.get("quote")
        if (claimed_evidence is not None
                and claimed_evidence.strip() != verified_quote.strip()):
            errors.append(
                f"logical {logical}: submitted logical_page_evidence "
                f"{claimed_evidence!r} is not the printed marker the DAO read "
                f"on physical page {mine['source_physical_page']} "
                f"({verified_quote!r})")
    return errors


# --- receipt construction and comparison ----------------------------------

def build_receipt(*, segment_document_id, parent_document_id,
                  parent_source_pdf_sha256, parent_source_total_pages,
                  segment_source_text_revision_sha256, derivation_method,
                  extractor, pages, page_offset_candidate, issued_at,
                  issued_by, run_id):
    """The receipt as it will be stored. Every field here is something the DAO
    observed or computed; nothing is copied from a caller's submission."""
    return {
        "scheme": RECEIPT_SCHEME,
        "document_id": segment_document_id,
        "parent_source_document_id": parent_document_id,
        "parent_source_pdf_sha256": parent_source_pdf_sha256,
        "parent_source_total_pages": parent_source_total_pages,
        "segment_source_text_revision_sha256": segment_source_text_revision_sha256,
        "derivation_method": derivation_method,
        # Recorded as the hint it was, so an audit can see what was tried.
        # It is not a grant: every page above carries its own confirmation.
        "page_offset_candidate": page_offset_candidate,
        "extractor": extractor,
        "pages": pages,
        "issued_at": issued_at,
        "issued_by": issued_by,
        "run_id": run_id,
    }


UNVERIFIED_OVERRIDE_SCHEME = "segment_page_map_unverified_v1"


def build_unverified_override_receipt(*, segment_document_id, parent_document_id,
                                      parent_source_pdf_sha256,
                                      segment_source_text_revision_sha256,
                                      logical_pages, page_offset,
                                      issued_at, issued_by, run_id,
                                      authorized_by, reason):
    """P0-6 human-authorized override receipt for an ocr_segment.

    Deliberately NOT a relaxed `build_receipt`: no `extractor`, no
    `parent_source_total_pages` requirement, no per-page digest or printed-page
    evidence, because none of those are derivable from an image-only parent
    PDF. Every page carries `verification_status: unverified_ocr_source`
    instead of a proof -- the offset is recorded as what a human asserted, not
    confirmed.
    """
    return {
        "scheme": UNVERIFIED_OVERRIDE_SCHEME,
        "document_id": segment_document_id,
        "parent_source_document_id": parent_document_id,
        "parent_source_pdf_sha256": parent_source_pdf_sha256,
        "derivation_method": "ocr_segment",
        "segment_source_text_revision_sha256": segment_source_text_revision_sha256,
        "page_offset_candidate": page_offset,
        "pages": [
            {
                "logical_page": lp,
                "source_physical_page": lp + page_offset,
                "verification_status": "unverified_ocr_source",
            }
            for lp in sorted(logical_pages)
        ],
        "human_override": {
            "authorized_by": authorized_by,
            "authorized_at": issued_at,
            "reason": reason,
        },
        "issued_at": issued_at,
        "issued_by": issued_by,
        "run_id": run_id,
    }


def unverified_override_fingerprint(receipt: dict) -> tuple:
    """What makes two override receipts the same derivation (excludes
    timestamps/issued_by, matching receipt_fingerprint's no-op contract)."""
    return (
        receipt.get("scheme"),
        receipt.get("document_id"),
        receipt.get("parent_source_document_id"),
        receipt.get("parent_source_pdf_sha256"),
        receipt.get("segment_source_text_revision_sha256"),
        receipt.get("page_offset_candidate"),
        tuple(
            (p.get("logical_page"), p.get("source_physical_page"),
             p.get("verification_status"))
            for p in receipt.get("pages") or []
        ),
    )


def manifest_page_map_from_unverified_override(receipt: dict) -> list[dict]:
    """The manifest projection of an unverified-override receipt -- mirrors
    `manifest_page_map_from_receipt` but carries verification_status instead
    of a digest/evidence pair that would not exist for real."""
    return [
        {
            "logical_page": page["logical_page"],
            "source_physical_page": page["source_physical_page"],
            "verification_status": page["verification_status"],
        }
        for page in receipt.get("pages") or []
    ]


def receipt_fingerprint(receipt: dict) -> tuple:
    """What makes two receipts the same derivation.

    Timestamps and `issued_by` are excluded so re-registering identical bytes
    against an identical parent is recognized as a no-op instead of looking
    like a new derivation and cascading downstream for nothing.
    """
    return (
        receipt.get("scheme"),
        receipt.get("document_id"),
        receipt.get("parent_source_document_id"),
        receipt.get("parent_source_pdf_sha256"),
        receipt.get("parent_source_total_pages"),
        receipt.get("segment_source_text_revision_sha256"),
        receipt.get("derivation_method"),
        tuple(
            (p.get("logical_page"), p.get("source_physical_page"),
             p.get("source_page_text_sha256"),
             p.get("segment_page_text_sha256"),
             _evidence_fingerprint(p.get("logical_page_evidence")))
            for p in receipt.get("pages") or []
        ),
    )


def _evidence_fingerprint(evidence) -> tuple:
    if not isinstance(evidence, dict):
        return (evidence,)
    return (
        evidence.get("quote"),
        tuple(evidence.get("bbox") or []),
        evidence.get("region"),
        evidence.get("page_width"),
        evidence.get("page_height"),
        evidence.get("evidence_profile"),
    )


def manifest_page_map_from_receipt(receipt: dict) -> list[dict]:
    """The manifest projection of a receipt.

    The manifest keeps its pre-P0-6 field name `physical_page_sha256` (the
    schema, CASE_030's data, and `page_map_entry`'s additionalProperties:false
    all predate the rename), so the projection translates the receipt's
    accurate `source_page_text_sha256` into it. One function owns the
    translation, so the writer and every checker cannot disagree about it.
    """
    return [
        {
            "logical_page": page["logical_page"],
            "source_physical_page": page["source_physical_page"],
            "physical_page_sha256": page["source_page_text_sha256"],
            "logical_page_evidence": (
                page["logical_page_evidence"]["quote"]
                if isinstance(page.get("logical_page_evidence"), dict)
                else page["logical_page_evidence"]),
        }
        for page in receipt.get("pages") or []
    ]


def receipt_projection_errors(manifest_entry: dict, receipt: dict | None,
                              location: str = "") -> list[str]:
    """The manifest's page_map must be exactly the receipt's projection.

    Not "compatible with" -- identical, entry for entry, in order. A page_map
    that merely overlaps the receipt is a page_map somebody edited, and the
    whole point of the receipt is that the mapping is not editable.
    """
    prefix = f"{location}: " if location else ""
    if receipt is None:
        return [
            f"{prefix}no {RECEIPT_SCHEME} derivation receipt exists -- the "
            "manifest page_map is an unverified declaration. A page map is "
            "issued only by `dao.py register-segment-derivation`, which "
            "re-derives it from the registered parent PDF"
        ]
    expected = manifest_page_map_from_receipt(receipt)
    actual = manifest_entry.get("page_map") or []
    if actual == expected:
        return []
    errors = [
        f"{prefix}manifest page_map does not match the {RECEIPT_SCHEME} "
        "receipt's projection -- the manifest was changed outside the DAO "
        "derivation path, so it no longer describes the mapping the parent "
        "PDF confirmed"
    ]
    if len(actual) != len(expected):
        errors.append(
            f"{prefix}page_map has {len(actual)} entries; the receipt "
            f"verified {len(expected)}")
        return errors
    for index, (got, want) in enumerate(zip(actual, expected)):
        if got != want:
            errors.append(
                f"{prefix}page_map[{index}]: manifest records {got!r}, the "
                f"receipt verified {want!r}")
    return errors


# --- finalization ----------------------------------------------------------

def receipt_currency_errors(*, receipt: dict, parent_entry: dict | None,
                            current_parent_pdf_sha256: str | None,
                            current_segment_revision_sha256: str | None,
                            location: str = "") -> list[str]:
    """Whether a receipt still describes the current world.

    A receipt is a statement about specific parent bytes producing specific
    segment bytes. Either side moving makes it a true statement about a state
    that no longer exists, which downstream must not be allowed to rely on.
    `current_parent_pdf_sha256` is the digest the DAO re-computes from the raw
    file at check time, never the manifest's recorded value -- checking a
    recorded field against a recorded field would prove only that two fields
    agree.
    """
    prefix = f"{location}: " if location else ""
    errors: list[str] = []

    if receipt.get("scheme") != RECEIPT_SCHEME:
        return [
            f"{prefix}derivation receipt scheme {receipt.get('scheme')!r} is "
            f"not {RECEIPT_SCHEME!r} -- an unrecognized scheme is refused "
            "rather than assumed equivalent"
        ]

    recorded_parent = receipt.get("parent_source_pdf_sha256")
    if current_parent_pdf_sha256 is None:
        errors.append(
            f"{prefix}the parent's registered raw source cannot be read or "
            "hashed now -- the derivation receipt cannot be re-confirmed "
            "against the file it was issued for")
    elif recorded_parent != current_parent_pdf_sha256:
        errors.append(
            f"{prefix}the parent raw source changed: the receipt was issued "
            f"against {recorded_parent!r}, the registered file now hashes to "
            f"{current_parent_pdf_sha256!r} -- every page mapping derived from "
            "the old bytes is void and must be re-registered")

    if parent_entry is not None:
        recorded_total = receipt.get("parent_source_total_pages")
        manifest_total = parent_entry.get("source_total_pages")
        if (isinstance(manifest_total, int)
                and recorded_total != manifest_total):
            errors.append(
                f"{prefix}the receipt was issued against a parent of "
                f"{recorded_total} physical pages; the manifest now records "
                f"{manifest_total}")

    recorded_revision = receipt.get("segment_source_text_revision_sha256")
    if current_segment_revision_sha256 is None:
        errors.append(
            f"{prefix}the segment has no current registered source-text "
            "revision -- there is nothing for the receipt to be bound to")
    elif recorded_revision != current_segment_revision_sha256:
        errors.append(
            f"{prefix}the segment's source text was revised after this "
            f"receipt was issued (receipt {recorded_revision!r}, current "
            f"{current_segment_revision_sha256!r}) -- the mapping was verified "
            "for text that is no longer current, so it must be re-registered")

    return errors


def unverified_override_currency_errors(*, receipt: dict,
                                        current_parent_pdf_sha256: str | None,
                                        current_segment_revision_sha256: str | None,
                                        location: str = "") -> list[str]:
    """Currency check for a P0-6 override receipt -- source identity only.

    No parent_source_total_pages / extractor / per-page digest checks: an
    override receipt never claimed any of those, so there is nothing there to
    go stale. What CAN go stale, and is still checked unconditionally, is
    whether this is still the same parent file and the same segment text the
    human authorized the override against.
    """
    prefix = f"{location}: " if location else ""
    errors: list[str] = []

    if receipt.get("scheme") != UNVERIFIED_OVERRIDE_SCHEME:
        return [
            f"{prefix}override receipt scheme {receipt.get('scheme')!r} is "
            f"not {UNVERIFIED_OVERRIDE_SCHEME!r} -- an unrecognized scheme is "
            "refused rather than assumed equivalent"
        ]
    if not isinstance(receipt.get("human_override"), dict) or not (
            receipt["human_override"].get("authorized_by")):
        errors.append(
            f"{prefix}override receipt has no valid human_override block -- "
            "an unverified mapping without recorded authorization is not a "
            "state that may finalize")

    recorded_parent = receipt.get("parent_source_pdf_sha256")
    if current_parent_pdf_sha256 is None:
        errors.append(
            f"{prefix}the parent's registered raw source cannot be read or "
            "hashed now -- the override cannot be re-confirmed against the "
            "file it was authorized for")
    elif recorded_parent != current_parent_pdf_sha256:
        errors.append(
            f"{prefix}the parent raw source changed: the override was "
            f"authorized against {recorded_parent!r}, the registered file now "
            f"hashes to {current_parent_pdf_sha256!r} -- it must be "
            "re-authorized")

    recorded_revision = receipt.get("segment_source_text_revision_sha256")
    if current_segment_revision_sha256 is None:
        errors.append(
            f"{prefix}the segment has no current registered source-text "
            "revision -- there is nothing for the override to be bound to")
    elif recorded_revision != current_segment_revision_sha256:
        errors.append(
            f"{prefix}the segment's source text was revised after this "
            f"override was authorized (receipt {recorded_revision!r}, current "
            f"{current_segment_revision_sha256!r}) -- it must be re-authorized")

    return errors


def segment_finalization_blockers(manifest, receipt_for,
                                  current_parent_digest_for,
                                  current_segment_revision_for,
                                  current_segment_text_for) -> list[str]:
    """Every reason a manifest's segments may not be finalized (P0-6).

    Applied at `document_processing` and `policy_clause_processing`
    finalization. A segment with no receipt is a BLOCKER, never a skipped
    check: "the proof field is absent, so there is nothing to verify" is the
    exact reasoning that let CASE_030's evidence-free page maps look fine.

    Legacy segments stay READABLE -- nothing here refuses a read or a
    migration -- but they cannot pass a stage.
    """
    by_id = {d.get("document_id"): d for d in manifest.get("documents", [])}
    blockers: list[str] = []

    for entry in manifest.get("documents", []):
        if entry.get("document_role") != "segment":
            continue
        seg_id = entry.get("document_id")
        location = f"segment {seg_id}"
        method = entry.get("derivation_method")
        receipt = receipt_for(seg_id)

        if method not in VERIFIABLE_METHODS:
            # ocr_segment lands here. A real segment_page_map_v2 receipt is
            # structurally impossible for it (see the module docstring) -- the
            # only way past this point is a recorded, human-authorized
            # segment_page_map_unverified_v1 override, checked explicitly
            # rather than silently accepting any non-verifiable method.
            if receipt is not None and receipt.get("scheme") == UNVERIFIED_OVERRIDE_SCHEME:
                blockers.extend(unverified_override_currency_errors(
                    receipt=receipt,
                    current_parent_pdf_sha256=current_parent_digest_for(
                        receipt.get("parent_source_document_id")),
                    current_segment_revision_sha256=current_segment_revision_for(seg_id),
                    location=location,
                ))
                projection = manifest_page_map_from_unverified_override(receipt)
                if (entry.get("page_map") or []) != projection:
                    blockers.append(
                        f"{location}: manifest page_map does not match the "
                        f"{UNVERIFIED_OVERRIDE_SCHEME} receipt's projection -- "
                        "the manifest was changed outside the DAO override path")
                continue
            blockers.append(
                f"{location}: derivation_method {method!r} cannot be verified "
                "against the parent PDF deterministically (UID verification "
                "may not re-run OCR), so no page-map derivation receipt can be "
                "issued for it and the stage cannot finalize. This is a known "
                "P0-6 limitation -- a human-authorized "
                f"{UNVERIFIED_OVERRIDE_SCHEME} override can be recorded via "
                "`dao.py record-unverifiable-segment-derivation` if the risk "
                "has been explicitly accepted; absent that, this is not a "
                "passing state")
            continue

        projection_errors = receipt_projection_errors(entry, receipt, location)
        if projection_errors:
            blockers.extend(projection_errors)
            continue

        parent_id = receipt.get("parent_source_document_id")
        if parent_id != entry.get("source_document_id"):
            blockers.append(
                f"{location}: the receipt was issued for parent "
                f"{parent_id!r} but the manifest now names "
                f"{entry.get('source_document_id')!r} as the source document")
            continue

        blockers.extend(receipt_currency_errors(
            receipt=receipt,
            parent_entry=by_id.get(parent_id),
            current_parent_pdf_sha256=current_parent_digest_for(parent_id),
            current_segment_revision_sha256=current_segment_revision_for(seg_id),
            location=location,
        ))
        blockers.extend(segment_revision_binding_errors(
            receipt, current_segment_text_for(seg_id), location))

    return blockers
