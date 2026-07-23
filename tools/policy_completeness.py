"""Policy boundary inventory cross-contract and finalization gates.

The inventory closes the completeness blind spot left by quote-verbatim
validation: validating only what an agent chose to write cannot detect a
paragraph or condition the agent omitted entirely. Here every non-whitespace
character in every processed policy page must be covered by an exact-offset
span. A span is either attached to a boundary or explicitly excluded with a
reason, and every normalized boundary maps to a real clause/condition item.
"""
from __future__ import annotations

import re

from _cross_contract import split_pages


INVENTORY_SCHEMA = "policy_boundary_inventory.schema.json"
_DOC_ID_RE = re.compile(r"_(DOC_\d+)\.json$")
_STRUCTURAL_ANCHOR_RE = re.compile(
    r"(?:제\s*\d+\s*조(?:의\s*\d+)?(?:\s*\([^)]*\))?|[①-⑳]|^\s*\d+\.\s+)",
    re.MULTILINE,
)


def doc_id_from_inventory_filename(filename: str) -> str | None:
    match = _DOC_ID_RE.search(filename)
    return match.group(1) if match else None


def _mapping_exists(mapping: dict, normalized_contract: dict | None) -> bool:
    if normalized_contract is None:
        return False
    clause = next(
        (c for c in normalized_contract.get("clauses", [])
         if c.get("clause_uid") == mapping.get("clause_uid")),
        None,
    )
    if clause is None:
        return False
    bucket = mapping.get("bucket")
    condition_uid = mapping.get("condition_uid")
    if bucket == "clause":
        return condition_uid is None
    items = clause.get(bucket)
    return (
        isinstance(items, list)
        and isinstance(condition_uid, str)
        and any(item.get("condition_uid") == condition_uid for item in items)
    )


def check_policy_boundary_inventory(
        data: dict,
        filename: str,
        redacted_text: str | None,
        normalized_contract: dict | None) -> list[str]:
    """Validate filename/source identity, exact spans, full page coverage, and
    normalized mappings. Returns all errors; callers refuse persistence."""
    errors: list[str] = []
    target_doc = doc_id_from_inventory_filename(filename)
    if target_doc is None:
        errors.append(
            "inventory filename must be "
            "policy_boundary_inventory_<DOC_ID>.json")
    if data.get("source_document_id") != target_doc:
        errors.append(
            f"source_document_id {data.get('source_document_id')!r} does not "
            f"match filename document {target_doc!r}")
    if redacted_text is None:
        return errors + [
            f"no processed/redacted text found for {target_doc or filename}"]

    try:
        pages = split_pages(redacted_text)
    except Exception as exc:  # SourceUnavailable is rendered as a normal gate error.
        return errors + [f"processed page boundaries unavailable: {exc}"]

    boundaries = data.get("boundaries") or []
    boundary_ids = [b.get("boundary_uid") for b in boundaries]
    if len(boundary_ids) != len(set(boundary_ids)):
        errors.append("boundary_uid values must be unique")
    boundary_set = set(boundary_ids)
    boundary_by_id = {
        boundary.get("boundary_uid"): boundary for boundary in boundaries}

    spans = data.get("page_spans") or []
    span_ids = [s.get("span_uid") for s in spans]
    if len(span_ids) != len(set(span_ids)):
        errors.append("span_uid values must be unique")
    pages_with_spans = {s.get("page") for s in spans}
    if pages_with_spans != set(pages):
        errors.append(
            f"page_spans cover pages {sorted(p for p in pages_with_spans if isinstance(p, int))}, "
            f"but processed source pages are {sorted(pages)}")

    boundary_span_count = {uid: 0 for uid in boundary_set}
    by_page: dict[int, list[dict]] = {}
    for index, span in enumerate(spans):
        page = span.get("page")
        by_page.setdefault(page, []).append(span)
        if span.get("disposition") == "boundary":
            uid = span.get("boundary_uid")
            if uid not in boundary_set:
                errors.append(
                    f"page_spans[{index}] references unknown boundary_uid {uid!r}")
            else:
                boundary_span_count[uid] += 1

    for uid, count in boundary_span_count.items():
        if count == 0:
            errors.append(f"boundary {uid!r} has no source page span")

    for page_num, page_text in pages.items():
        occupied = [False] * len(page_text)
        ordered = sorted(
            by_page.get(page_num, []),
            key=lambda span: (span.get("start_char", -1), span.get("end_char", -1)),
        )
        previous_end = 0
        for span in ordered:
            start = span.get("start_char")
            end = span.get("end_char")
            if not isinstance(start, int) or not isinstance(end, int):
                continue  # schema reports the shape; avoid crashing here.
            if start >= end:
                errors.append(
                    f"{span.get('span_uid')}: start_char {start} must be < end_char {end}")
                continue
            if start < 0 or end > len(page_text):
                errors.append(
                    f"{span.get('span_uid')}: offsets [{start}:{end}] exceed page "
                    f"{page_num} length {len(page_text)}")
                continue
            if start < previous_end:
                errors.append(
                    f"{span.get('span_uid')}: overlaps a prior span on page {page_num}")
            previous_end = max(previous_end, end)
            actual = page_text[start:end]
            if actual != span.get("quote"):
                errors.append(
                    f"{span.get('span_uid')}: quote does not equal exact page "
                    f"{page_num} text at [{start}:{end}]")
            if span.get("disposition") == "boundary":
                anchors = list(_STRUCTURAL_ANCHOR_RE.finditer(actual))
                if len(anchors) > 1:
                    boundary = boundary_by_id.get(span.get("boundary_uid"), {})
                    errors.append(
                        f"{span.get('span_uid')}: boundary "
                        f"{boundary.get('boundary_uid')!r} spans {len(anchors)} "
                        "article/paragraph/item anchors -- split material "
                        "subparagraphs so each can map independently")
            for pos in range(start, end):
                if occupied[pos]:
                    # One overlap error is enough for this span; the ordered
                    # check above already emitted it.
                    break
                occupied[pos] = True

        uncovered = [
            pos for pos, char in enumerate(page_text)
            if not char.isspace() and not occupied[pos]
        ]
        if uncovered:
            pos = uncovered[0]
            snippet = page_text[pos:pos + 60].replace("\n", "\\n")
            errors.append(
                f"page {page_num} has uncovered non-whitespace source text at "
                f"char {pos}: {snippet!r}")

    for boundary in boundaries:
        if boundary.get("disposition") != "normalized":
            continue
        for mapping in boundary.get("normalized_mappings") or []:
            if not _mapping_exists(mapping, normalized_contract):
                errors.append(
                    f"boundary {boundary.get('boundary_uid')}: normalized mapping "
                    f"{mapping!r} does not resolve to a real clause/condition")

    return errors


def unresolved_boundaries(data: dict) -> list[str]:
    """Return blockers that prohibit policy stage finalization."""
    blockers = []
    for boundary in data.get("boundaries", []):
        disposition = boundary.get("disposition")
        if disposition in ("review_required", "extraction_failed"):
            blockers.append(
                f"{boundary.get('boundary_uid')} ({boundary.get('label')}): "
                f"{disposition} -- {boundary.get('reason')}")
    return blockers
