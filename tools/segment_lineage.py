"""Segment-lineage and manifest/classification-consistency checks.

These are the manifest invariants JSON Schema structurally cannot express (the
same blind-spot class as _cross_contract.py): comparing one manifest entry to
another (parent exists, no cycle), comparing declared page ranges to the actual
<<<PAGE page=N>>> markers in a sibling processed file, and comparing a
document's manifest type to its separately-written classification_result.

CASE_030 is why this exists. DOC_004/005/006 were carved from a 240-page policy
PDF but were never registered in document_manifest.json at all, yet
normalized_policy_clause_DOC_004/005/006.json cited them as sources -- a segment
masquerading as a first-class document with no verifiable lineage. And DOC_002
was 'insurance_policy' in the manifest while its classification_result said
'insurance_certificate' -- a silent manifest/classification disagreement.

All functions return a list of human-readable error strings (empty = clean),
the same contract as validate_instance()/_cross_contract, so the DAO can print
them and refuse the write/finalize on any non-empty list.

Page semantics: a segment's evidence references and its redacted-text markers
both use the LOGICAL (printed) page number -- the number physically printed on
the page ('N / 240'). page_map additionally records the parent PDF's physical
page index (source_physical_page) where known, but lineage verification is
against logical pages, because that is what the markers and the cross-contract
quote check both key on.
"""
import hashlib
import re

from policy_completeness import _TEXT_PROCESSED

_PAGE_MARKER_RE = re.compile(r"(?m)^<<<PAGE page=(\d+)>>>")


def _segments(manifest: dict) -> list:
    return [d for d in manifest.get("documents", []) if d.get("document_role") == "segment"]


def _by_id(manifest: dict) -> dict:
    return {d.get("document_id"): d for d in manifest.get("documents", [])}


def markers_in(redacted_text: str) -> list:
    """The logical page numbers declared by <<<PAGE page=N>>> markers, in order."""
    return [int(m.group(1)) for m in _PAGE_MARKER_RE.finditer(redacted_text)]


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pages_from_ranges(ranges: list) -> list:
    """Flatten [{start,end},...] to the explicit ordered page list. Returns None
    if any range is malformed (start/end missing or start>end) -- the caller
    reports that separately."""
    pages = []
    for r in ranges:
        start, end = r.get("start"), r.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start > end:
            return None
        pages.extend(range(start, end + 1))
    return pages


def _parent_chain_has_cycle(seg_id: str, by_id: dict) -> bool:
    """Follow source_document_id from seg_id; True if it loops (never reaches a
    physical/None parent). Bounded by the number of documents so a broken chain
    can't spin forever."""
    seen = set()
    current = seg_id
    steps = 0
    limit = len(by_id) + 1
    while current is not None:
        if current in seen:
            return True
        seen.add(current)
        entry = by_id.get(current)
        if entry is None:
            return False  # dangling parent -- reported separately, not a cycle
        current = entry.get("source_document_id") if entry.get("document_role") == "segment" else None
        steps += 1
        if steps > limit:
            return True
    return False


def validate_segment_lineage(manifest: dict, redacted_text_for) -> list:
    """Check every segment entry's lineage.

    redacted_text_for(document_id) -> the segment's derived redacted text (str)
    or None if it does not exist yet. The DAO supplies the real reader; tests
    pass a dict-backed stub.

    Enforced:
      - source_document_id exists in the manifest and is not the segment itself.
      - no cycle in the parent chain.
      - the parent is a physical document (a segment of a segment is not
        modeled; if that is ever needed it is a deliberate design change).
      - segment_page_ranges are individually valid (start<=end) and, across
        ranges, strictly increasing with no overlap.
      - page_map's logical_page list equals the flattened ranges exactly (no
        gap, duplicate, or extra) -- so a single 57-119 range can't stand in
        for the real non-contiguous {57-58,118-119}.
      - the declared logical pages equal the segment's redacted-text markers
        exactly (same set, same order) -- the lineage is verified against the
        actual processed text, not merely asserted.
    """
    errors = []
    by_id = _by_id(manifest)

    for seg in _segments(manifest):
        sid = seg.get("document_id")
        parent = seg.get("source_document_id")
        loc = f"segment {sid}"

        if parent == sid:
            errors.append(f"{loc}: source_document_id points at itself (no self-parent)")
        elif parent not in by_id:
            errors.append(f"{loc}: source_document_id {parent!r} is not a document in this manifest")
        else:
            parent_entry = by_id[parent]
            if parent_entry.get("document_role") == "segment":
                errors.append(
                    f"{loc}: parent {parent!r} is itself a segment -- a segment must derive "
                    f"from a physical document (segment-of-segment is not modeled)"
                )
            if _parent_chain_has_cycle(sid, by_id):
                errors.append(f"{loc}: lineage forms a cycle through source_document_id")

        ranges = seg.get("segment_page_ranges") or []
        range_pages = _pages_from_ranges(ranges)
        if range_pages is None:
            errors.append(f"{loc}: a segment_page_ranges entry is malformed (start/end missing or start>end)")
        else:
            # strictly increasing, non-overlapping across ranges
            prev_end = 0
            ordered_ok = True
            for r in ranges:
                if r["start"] <= prev_end:
                    ordered_ok = False
                prev_end = r["end"]
            if not ordered_ok:
                errors.append(
                    f"{loc}: segment_page_ranges overlap or are out of order "
                    f"-- non-contiguous ranges must be listed separately and increasing"
                )

        page_map = seg.get("page_map") or []
        map_pages = [e.get("logical_page") for e in page_map]
        if range_pages is not None and map_pages != range_pages:
            errors.append(
                f"{loc}: page_map logical pages {map_pages} do not match the flattened "
                f"segment_page_ranges {range_pages} (a single wide range cannot stand in "
                f"for a non-contiguous page set)"
            )
        physical_pages = [e.get("source_physical_page") for e in page_map]
        if any(not isinstance(p, int) or p < 1 for p in physical_pages):
            errors.append(
                f"{loc}: every page_map entry requires a positive "
                "source_physical_page -- logical-only lineage is not verifiable"
            )
        elif physical_pages != sorted(set(physical_pages)):
            errors.append(
                f"{loc}: source_physical_page values must be unique and strictly "
                f"increasing (got {physical_pages})"
            )
        elif parent in by_id:
            parent_total = by_id[parent].get("source_total_pages")
            if not isinstance(parent_total, int) or parent_total < 1:
                errors.append(
                    f"{loc}: physical parent {parent!r} has no verified "
                    "source_total_pages from document processing")
            elif physical_pages and physical_pages[-1] > parent_total:
                errors.append(
                    f"{loc}: source_physical_page {physical_pages[-1]} exceeds "
                    f"parent {parent!r} source_total_pages={parent_total}")

        expected_path = (
            f"data/processed/{manifest.get('case_id')}/{sid}/redacted_text.md")
        if seg.get("derived_text_path") != expected_path:
            errors.append(
                f"{loc}: derived_text_path must be {expected_path!r}, got "
                f"{seg.get('derived_text_path')!r}"
            )

        # Verify against the actual processed markers.
        text = redacted_text_for(sid)
        if text is None:
            errors.append(
                f"{loc}: no processed/derived text found -- lineage cannot be verified "
                f"against the segment's own <<<PAGE page=N>>> markers"
            )
        else:
            marker_pages = markers_in(text)
            declared = range_pages if range_pages is not None else map_pages
            if declared is not None and marker_pages != declared:
                errors.append(
                    f"{loc}: declared pages {declared} do not match the segment's redacted-text "
                    f"markers {marker_pages} -- the page range and the actual processed text disagree"
                )
            actual_digest = text_sha256(text)
            if seg.get("derived_text_sha256") != actual_digest:
                errors.append(
                    f"{loc}: derived_text_sha256 does not match the current "
                    "processed text -- the segment changed after lineage approval"
                )

    return errors


def check_classification_manifest_consistency(
        manifest: dict, classification_for, require_complete: bool = False) -> list:
    """For every document that has a classification_result, its manifest
    document_type must equal the classification's predicted_document_type.

    classification_for(document_id) -> the classification_result dict, or None
    if none exists. A document with no classification yet is skipped (not an
    error -- classification may simply not have run). A document whose manifest
    document_type is still null but which HAS a classification is an error
    (the manifest was never synced from the classification).
    """
    errors = []
    for doc in manifest.get("documents", []):
        did = doc.get("document_id")
        if doc.get("document_role") == "segment":
            # A segment inherits its physical parent's classification and must
            # not pretend to have undergone an independent classifier call.
            parent = _by_id(manifest).get(doc.get("source_document_id"))
            if parent and doc.get("document_type") != parent.get("document_type"):
                errors.append(
                    f"{did}: segment document_type {doc.get('document_type')!r} "
                    f"does not match parent {parent.get('document_id')} type "
                    f"{parent.get('document_type')!r}"
                )
            continue
        cls = classification_for(did)
        if cls is None:
            if (require_complete
                    and doc.get("downstream_disposition") in _TEXT_PROCESSED):
                errors.append(
                    f"{did}: automated physical document has no "
                    "classification_result -- document processing is incomplete"
                )
            continue
        predicted = cls.get("predicted_document_type")
        manifest_type = doc.get("document_type")
        if manifest_type is None:
            errors.append(
                f"{did}: classification_result predicts {predicted!r} but the manifest "
                f"document_type is still null -- the manifest was never synced from classification"
            )
        elif manifest_type != predicted:
            errors.append(
                f"{did}: manifest document_type {manifest_type!r} disagrees with "
                f"classification_result predicted_document_type {predicted!r}"
            )
    return errors


def is_registered_automated_source(manifest: dict, document_id: str) -> bool:
    """True if document_id is a manifest entry usable as an automated-text
    source: it exists AND its downstream_disposition is automated_text_pipeline.
    A normalized_policy_clause file may only cite such a document. A document
    absent from the manifest returns False -- exactly the CASE_030 hole where
    DOC_004/005/006 were cited but never registered.

    Deliberately NOT widened to text_only_no_normalization: this is the
    normalization gate, not a text-processing question. Writing a clause
    contract for a document declared as owing none is a contradiction, and it
    should fail loudly rather than quietly accept work the completion gate will
    never look at."""
    entry = _by_id(manifest).get(document_id)
    if entry is None:
        return False
    return entry.get("downstream_disposition") == "automated_text_pipeline"


def parent_processing_complete(manifest: dict, document_id: str) -> bool:
    """For a segment, whether its physical parent's processing is complete
    enough for the segment to be used downstream (parent ocr_status 'completed'
    or an embedded-text equivalent). For a physical document, trivially True.
    A missing parent returns False."""
    by_id = _by_id(manifest)
    entry = by_id.get(document_id)
    if entry is None:
        return False
    if entry.get("document_role") != "segment":
        return True
    parent = by_id.get(entry.get("source_document_id"))
    if parent is None:
        return False
    return (
        parent.get("ocr_status") in ("completed", "not_applicable")
        and parent.get("cross_validation_status") in ("agreed", "non_text_verified")
        # About TEXT, not normalization: the question is whether the parent's
        # processed text exists for the segment to rest on. A parent that is
        # text_only_no_normalization has exactly that.
        and parent.get("downstream_disposition") in _TEXT_PROCESSED
        and bool(parent.get("redacted_text_path"))
        and bool(parent.get("document_type"))
    )
