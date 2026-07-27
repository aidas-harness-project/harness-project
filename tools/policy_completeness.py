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
# Korean insurance-policy structural anchors. The blacklist a boundary/exclusion
# span is checked against must cover the item markers a heading-only boundary
# would otherwise hide body text behind (Part 11C):
#   제1조 / 제1조의2        article, with 가지번호
#   ① .. ⑳                  circled-number paragraphs
#   ㉮ .. ㉺ / ㈀ ..         circled Korean item markers
#   1. / 1) / (1)           arabic item markers (line-anchored or parenthesised)
#   가. / 가) / (가)         Korean-consonant item markers
# The arabic/consonant "N." and "가." forms are line-anchored (^) so a decimal
# like "50.5" or a mid-sentence "다." ending doesn't false-positive; the
# parenthesised and circled forms are safe to match anywhere.
_ITEM_LETTERS = "가나다라마바사아자차카타파하"
_STRUCTURAL_ANCHOR_RE = re.compile(
    r"(?:"
    r"제\s*\d+\s*조(?:의\s*\d+)?(?:\s*\([^)]*\))?"        # 제1조 / 제1조의2
    r"|[①-⑳]"                                   # ①-⑳
    r"|[㉠-㉿]"                                   # ㉠-㉿ (incl. ㉮)
    r"|[㈀-㈞]"                                   # ㈀-㈞
    r"|^[ \t]*\(?\d+\)"                                   # (1) / 1)
    rf"|^[ \t]*\([{_ITEM_LETTERS}]\)"                     # (가)
    r"|^[ \t]*\d+\.[ \t]"                                 # 1.  (line-anchored)
    rf"|^[ \t]*[{_ITEM_LETTERS}][.)][ \t]"                # 가. / 가)
    r")",
    re.MULTILINE,
)
# A span that, after normalization, is ONLY a clause/article heading -- an
# article number and its parenthesised title, nothing else. Such a span cannot
# be the sole representation of a normalized boundary: the body it heads must
# have its own boundary span. Distinct from _HEADING_ONLY_RE in _cross_contract
# (that one guards evidence quotes; this one guards boundary spans).
_SPAN_HEADING_ONLY_RE = re.compile(
    r"^\s*제\s*\d+\s*조(?:의\s*\d+)?\s*(?:\([^)]{1,80}\))?\s*$"
)
_BLANKET_BODY_EXCLUSION_RE = re.compile(
    r"(?:covered\s+under|not\s+separately\s+normalized|"
    r"본문\s*(?:전체|내용)?.*(?:포함|반영|covered)|"
    r"조문.*(?:포함|반영).*(?:제외|생략))",
    re.IGNORECASE,
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
    # Per boundary: does it have at least one span that is NOT heading-only?
    # A normalized boundary represented solely by its article heading has no
    # body span to substantiate any condition (Part 11C).
    boundary_has_body_span = {uid: False for uid in boundary_set}
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
                quote = span.get("quote") or ""
                if not _SPAN_HEADING_ONLY_RE.match(quote.strip()):
                    boundary_has_body_span[uid] = True

    for uid, count in boundary_span_count.items():
        if count == 0:
            errors.append(f"boundary {uid!r} has no source page span")

    # A normalized boundary whose spans are ALL heading-only may only carry the
    # clause's identity (bucket 'clause') -- never a condition. A title cannot
    # substantiate the body condition it heads; that operative text must be its
    # own boundary span. (The legitimate pattern is a heading boundary mapped to
    # 'clause' PLUS separate body boundaries for each condition.)
    for uid in boundary_set:
        boundary = boundary_by_id.get(uid, {})
        if (boundary.get("disposition") == "normalized"
                and boundary_span_count.get(uid, 0) > 0
                and not boundary_has_body_span.get(uid, False)):
            condition_buckets = sorted({
                mapping.get("bucket")
                for mapping in boundary.get("normalized_mappings") or []
                if mapping.get("bucket") not in (None, "clause")
            })
            if condition_buckets:
                errors.append(
                    f"boundary {uid!r} ({boundary.get('label')}): every source "
                    "span is a clause/article heading only, yet it maps to "
                    f"condition bucket(s) {condition_buckets} -- a title cannot "
                    "substantiate the body condition it heads; map the condition "
                    "to the boundary covering its operative text, or route the "
                    "boundary to review_required")

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
            elif span.get("disposition") == "excluded":
                anchors = list(_STRUCTURAL_ANCHOR_RE.finditer(actual))
                if anchors:
                    errors.append(
                        f"{span.get('span_uid')}: excluded source span on page "
                        f"{page_num} contains {len(anchors)} article/paragraph/item "
                        "anchor(s) -- normative policy text must be represented as "
                        "boundaries and mapped, or routed to review_required")
                reason = span.get("exclusion_reason") or ""
                if _BLANKET_BODY_EXCLUSION_RE.search(reason):
                    errors.append(
                        f"{span.get('span_uid')}: blanket body exclusion reason is "
                        "not evidence of semantic normalization; split the source "
                        "into independently mapped boundaries")
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

    # Mapping resolution must be bidirectional.  The original gate only proved
    # that inventory -> normalized mappings resolved.  A normalized clause could
    # still self-declare unrelated source_boundary_uids and pass, leaving its
    # provenance address dangling (CASE_030 DOC_004/006/008).
    if normalized_contract is not None:
        clauses = normalized_contract.get("clauses") or []
        mapped_boundary_uids_by_clause: dict[str, set[str]] = {}
        for boundary in boundaries:
            if boundary.get("disposition") != "normalized":
                continue
            boundary_uid = boundary.get("boundary_uid")
            for mapping in boundary.get("normalized_mappings") or []:
                clause_uid = mapping.get("clause_uid")
                if clause_uid and boundary_uid:
                    mapped_boundary_uids_by_clause.setdefault(
                        clause_uid, set()).add(boundary_uid)

        for clause_index, clause in enumerate(clauses):
            clause_uid = clause.get("clause_uid")
            declared = set(clause.get("source_boundary_uids") or [])
            unknown = declared - boundary_set
            if unknown:
                errors.append(
                    f"clauses[{clause_index}].source_boundary_uids do not resolve "
                    f"in this inventory: {sorted(unknown)}")
            mapped = mapped_boundary_uids_by_clause.get(clause_uid, set())
            missing_from_clause = mapped - declared
            if missing_from_clause:
                errors.append(
                    f"clauses[{clause_index}] omits inventory boundaries that map "
                    f"to clause_uid {clause_uid!r}: "
                    f"{sorted(missing_from_clause)}")
            unbacked = declared - mapped
            if unbacked:
                errors.append(
                    f"clauses[{clause_index}] declares source boundaries that do "
                    f"not map back to clause_uid {clause_uid!r}: "
                    f"{sorted(unbacked)}")

    return errors


def _iter_clause_evidence(clause: dict):
    """Yield (location_label, ref) for a clause's own + all condition refs."""
    for ref in clause.get("evidence_references") or []:
        yield "clause", ref
    for bucket in (
        "payout_conditions", "exclusions", "reduction_conditions",
        "definitions", "obligations", "claim_requirements",
        "termination_conditions", "dispute_resolution_conditions",
        "coverage_start_conditions",
    ):
        for idx, item in enumerate(clause.get(bucket) or []):
            for ref in item.get("evidence_references") or []:
                yield f"{bucket}[{idx}]", ref


def check_clause_evidence_within_boundaries(
        normalized_contract: dict | None,
        inventory: dict,
        redacted_text: str | None) -> list[str]:
    """Every clause/condition evidence quote must sit inside the exact source
    range of one of the clause's OWN declared source boundaries, and must not
    overlap any excluded span (Part 11C).

    Before this, a clause could declare source_boundary_uids for provenance yet
    cite a quote physically located in a DIFFERENT boundary, or inside a span
    the inventory excluded as non-normative -- the quote-verbatim check only
    proved the quote existed *somewhere* on the page, not that it came from the
    range this clause claims. Matching is on the raw page text, so offsets line
    up with the inventory's exact-offset spans.

    Returns all errors (empty = clean). A missing normalized contract or
    unavailable source is not this function's failure to report -- callers
    handle those; here they simply mean 'nothing to bind'.
    """
    errors: list[str] = []
    if normalized_contract is None or redacted_text is None:
        return errors
    try:
        pages = split_pages(redacted_text)
    except Exception:
        return errors  # source-boundary trust is reported by the caller.

    spans = inventory.get("page_spans") or []
    # boundary_uid -> list of (page, start, end) for its boundary spans.
    boundary_spans: dict[str, list[tuple[int, int, int]]] = {}
    # page -> list of (start, end) excluded spans.
    excluded_by_page: dict[int, list[tuple[int, int]]] = {}
    for span in spans:
        page = span.get("page")
        start = span.get("start_char")
        end = span.get("end_char")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if span.get("disposition") == "boundary":
            uid = span.get("boundary_uid")
            if uid:
                boundary_spans.setdefault(uid, []).append((page, start, end))
        elif span.get("disposition") == "excluded":
            excluded_by_page.setdefault(page, []).append((start, end))

    for clause_index, clause in enumerate(
            normalized_contract.get("clauses") or []):
        declared = clause.get("source_boundary_uids") or []
        allowed = [
            (p, s, e)
            for uid in declared
            for (p, s, e) in boundary_spans.get(uid, [])
        ]
        for label, ref in _iter_clause_evidence(clause):
            loc = f"clauses[{clause_index}].{label}"
            page = ref.get("page")
            quote = ref.get("quote")
            if page is None or not quote or not quote.strip():
                continue  # structural completeness is checked elsewhere.
            page_text = pages.get(page)
            if page_text is None:
                continue  # page-existence is checked elsewhere.
            # Every raw occurrence of the quote on this page.
            occurrences = []
            start = page_text.find(quote)
            while start != -1:
                occurrences.append((start, start + len(quote)))
                start = page_text.find(quote, start + 1)
            if not occurrences:
                continue  # verbatim presence is checked by _cross_contract.

            # The quote must fall entirely within one of the clause's own
            # boundary spans on this page.
            within_allowed = any(
                p == page and s <= qs and qe <= e
                for (qs, qe) in occurrences
                for (p, s, e) in allowed
            )
            if allowed and not within_allowed:
                errors.append(
                    f"{loc}: evidence quote on page {page} does not fall within "
                    "any source span of the clause's declared "
                    f"source_boundary_uids {sorted(set(declared))} -- a clause may "
                    "only cite text inside its own boundaries")
            # And it must not overlap an excluded (non-normative) span.
            overlaps_excluded = any(
                not (qe <= xs or xe <= qs)
                for (qs, qe) in occurrences
                for (xs, xe) in excluded_by_page.get(page, [])
                # only penalise an occurrence that is otherwise the one being
                # relied on: if it also lies in an allowed span, the allowed
                # match above governs; here we flag a quote whose ONLY location
                # is an excluded span.
                if not any(
                    s <= qs and qe <= e
                    for (p, s, e) in allowed if p == page)
            )
            if overlaps_excluded:
                errors.append(
                    f"{loc}: evidence quote on page {page} overlaps a span the "
                    "inventory marked excluded (non-normative) -- excluded source "
                    "text may not substantiate a normalized condition")
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


# --- Parent-level whole-page coverage (policy_parent_coverage_DOC_XXX.json) ---
#
# The per-document boundary inventory above accounts only for the processed
# pages a segment already owns. It structurally cannot detect a page of the
# physical PARENT PDF that was never carved into any segment -- CASE_030's real
# hole, where 133 of 240 logical pages (front matter 1-11, gap page 56, and the
# whole appendix/별표 region 120-240) belonged to no document and slipped past
# the completeness gate entirely. This checks the parent's full 1..N logical
# page range.

PARENT_COVERAGE_SCHEMA = "policy_parent_coverage.schema.json"
_PARENT_COVERAGE_DOC_RE = re.compile(r"_(DOC_\d+)\.json$")


def doc_id_from_parent_coverage_filename(filename: str) -> str | None:
    match = _PARENT_COVERAGE_DOC_RE.search(filename)
    return match.group(1) if match else None


def check_policy_parent_coverage(
        data: dict,
        filename: str,
        manifest: dict | None,
        reference_table_for) -> list[str]:
    """Validate that a parent-coverage contract accounts for every logical page
    of the parent PDF exactly once, and that each productive disposition
    resolves to something that really exists.

    reference_table_for(document_id) -> the reference_table_{document_id}.json
    dict (or None). Used to confirm a reference_table disposition's table_uid is
    actually present in that document's reference-table contract.

    Returns all errors (empty = clean); callers refuse persistence/finalize on
    any non-empty list. Does NOT itself treat review_required/extraction_failed
    as errors -- unresolved_parent_pages() reports those at finalize time, the
    same split boundary inventory uses (persist a work-in-progress inventory,
    block only finalize).
    """
    errors: list[str] = []
    target_doc = doc_id_from_parent_coverage_filename(filename)
    if target_doc is None:
        errors.append(
            "parent-coverage filename must be "
            "policy_parent_coverage_<DOC_ID>.json")
    if data.get("parent_document_id") != target_doc:
        errors.append(
            f"parent_document_id {data.get('parent_document_id')!r} does not "
            f"match filename document {target_doc!r}")

    by_id = {d.get("document_id"): d for d in (manifest or {}).get("documents", [])}
    # The parent must be a physical (non-segment) insurance_policy document.
    parent_entry = by_id.get(target_doc)
    if parent_entry is None:
        errors.append(f"parent {target_doc!r} is not in the manifest")
    else:
        if parent_entry.get("document_role") == "segment":
            errors.append(
                f"parent {target_doc!r} is a segment -- parent coverage must be "
                "declared on the physical parent document, not a segment")
        if parent_entry.get("document_type") != "insurance_policy":
            errors.append(
                f"parent {target_doc!r} document_type is "
                f"{parent_entry.get('document_type')!r}, not 'insurance_policy'")
        source_total = parent_entry.get("source_total_pages")
        declared_physical = data.get("total_physical_pages")
        if not isinstance(source_total, int) or source_total < 1:
            errors.append(
                f"parent {target_doc!r} has no verified source_total_pages "
                "from document processing")
        elif declared_physical != source_total:
            errors.append(
                f"total_physical_pages {declared_physical!r} does not match "
                f"manifest source_total_pages {source_total}")

    total = data.get("total_logical_pages")
    total_physical = data.get("total_physical_pages")
    pages = data.get("pages") or []
    unpaged = data.get("unpaged_physical_pages") or []
    logicals = [p.get("logical_page") for p in pages]

    # Exact 1..total coverage: no dup, no gap, no out-of-range.
    seen = set()
    for lp in logicals:
        if lp in seen:
            errors.append(f"logical page {lp} appears more than once")
        seen.add(lp)
    if isinstance(total, int):
        for lp in logicals:
            if isinstance(lp, int) and (lp < 1 or lp > total):
                errors.append(
                    f"logical page {lp} is outside 1..{total}")
        missing = [lp for lp in range(1, total + 1) if lp not in seen]
        if missing:
            errors.append(
                f"logical pages not accounted for at all (coverage gap): "
                f"{missing[:20]}{'...' if len(missing) > 20 else ''}")

    physicals = [p.get("physical_page") for p in pages]
    physicals.extend(p.get("physical_page") for p in unpaged)
    seen_physical = set()
    for physical in physicals:
        if physical in seen_physical:
            errors.append(
                f"physical page {physical} appears more than once across "
                "logical and unpaged coverage")
        seen_physical.add(physical)
    if isinstance(total_physical, int):
        for physical in physicals:
            if isinstance(physical, int) and not (
                    1 <= physical <= total_physical):
                errors.append(
                    f"physical page {physical} is outside "
                    f"1..{total_physical}")
        missing_physical = [
            page for page in range(1, total_physical + 1)
            if page not in seen_physical
        ]
        if missing_physical:
            errors.append(
                "physical pages not accounted for at all: "
                f"{missing_physical[:20]}"
                f"{'...' if len(missing_physical) > 20 else ''}")

    # Each productive disposition must resolve to something real.
    for p in pages:
        lp = p.get("logical_page")
        disp = p.get("disposition")
        if disp == "owned_by_segment":
            owner = p.get("owner_document_id")
            entry = by_id.get(owner)
            if entry is None:
                errors.append(
                    f"page {lp}: owned_by_segment owner {owner!r} is not a "
                    "registered manifest document")
            elif entry.get("downstream_disposition") != "automated_text_pipeline":
                errors.append(
                    f"page {lp}: owner {owner!r} is not an automated-text "
                    "document")
            elif entry.get("document_type") != "insurance_policy":
                errors.append(
                    f"page {lp}: owner {owner!r} is not an insurance_policy "
                    "document")
            else:
                if entry.get("document_role") != "segment":
                    errors.append(
                        f"page {lp}: owner {owner!r} is not a segment")
                if entry.get("source_document_id") != target_doc:
                    errors.append(
                        f"page {lp}: owner {owner!r} does not derive from "
                        f"parent {target_doc!r}")
                mapping = next(
                    (item for item in entry.get("page_map") or []
                     if item.get("logical_page") == lp),
                    None,
                )
                if mapping is None:
                    errors.append(
                        f"page {lp}: owner {owner!r} page_map does not contain "
                        "this logical page")
                elif mapping.get("source_physical_page") != \
                        p.get("physical_page"):
                    errors.append(
                        f"page {lp}: coverage physical_page "
                        f"{p.get('physical_page')} disagrees with owner "
                        f"{owner!r} page_map physical page "
                        f"{mapping.get('source_physical_page')}")
        elif disp == "reference_table":
            rt_doc = p.get("reference_table_document_id")
            table_uid = p.get("table_uid")
            rt = reference_table_for(rt_doc) if rt_doc else None
            if rt is None:
                errors.append(
                    f"page {lp}: reference_table disposition points at "
                    f"{rt_doc!r} which has no reference_table contract")
            else:
                rt_entry = by_id.get(rt_doc)
                if rt_entry is None:
                    errors.append(
                        f"page {lp}: reference-table document {rt_doc!r} is "
                        "not registered in the manifest")
                else:
                    if rt_entry.get("document_role") != "segment":
                        errors.append(
                            f"page {lp}: reference-table document {rt_doc!r} "
                            "is not a segment")
                    if rt_entry.get("source_document_id") != target_doc:
                        errors.append(
                            f"page {lp}: reference-table document {rt_doc!r} "
                            f"does not derive from parent {target_doc!r}")
                    mapping = next(
                        (item for item in rt_entry.get("page_map") or []
                         if item.get("logical_page") == lp),
                        None,
                    )
                    if mapping is None:
                        errors.append(
                            f"page {lp}: reference-table document {rt_doc!r} "
                            "page_map does not contain this logical page")
                    elif mapping.get("source_physical_page") != \
                            p.get("physical_page"):
                        errors.append(
                            f"page {lp}: reference-table coverage physical "
                            f"page {p.get('physical_page')} disagrees with "
                            f"{rt_doc!r} page_map "
                            f"{mapping.get('source_physical_page')}")
                table = next(
                    (t for t in rt.get("tables", [])
                     if t.get("table_uid") == table_uid),
                    None,
                )
                if table is None:
                    errors.append(
                        f"page {lp}: table_uid {table_uid!r} is not present in "
                        f"reference_table_{rt_doc}.json")
                    continue
                if table.get("review_required") is True:
                    errors.append(
                        f"page {lp}: table_uid {table_uid!r} still has "
                        "review_required=true and cannot be a resolved "
                        "reference_table disposition")
                cells = [
                    cell
                    for row in table.get("rows") or []
                    for cell in row.get("cells") or []
                ]
                if any(cell.get("review_required") is True for cell in cells):
                    errors.append(
                        f"page {lp}: table_uid {table_uid!r} contains cells with "
                        "review_required=true")
                evidence_pages = {
                    ref.get("page")
                    for ref in table.get("evidence_references") or []
                    if isinstance(ref, dict)
                }
                evidence_pages.update(
                    ref.get("page")
                    for cell in cells
                    for ref in cell.get("evidence_references") or []
                    if isinstance(ref, dict)
                )
                if lp not in evidence_pages:
                    errors.append(
                        f"page {lp}: table_uid {table_uid!r} has no table/cell "
                        "evidence on this logical page; a UID cannot claim "
                        "unevidenced pages")
        elif disp == "administrative_excluded":
            refs = p.get("evidence_references") or []
            if not refs:
                errors.append(
                    f"page {lp}: administrative exclusion has no processed "
                    "page evidence")
            for ref in refs:
                if ref.get("document_id") != target_doc:
                    errors.append(
                        f"page {lp}: administrative exclusion evidence must "
                        f"cite parent {target_doc!r}")
                if ref.get("page") != lp:
                    errors.append(
                        f"page {lp}: administrative exclusion evidence cites "
                        f"logical page {ref.get('page')!r}")

    return errors


def unresolved_parent_pages(data: dict) -> list[str]:
    """Parent-coverage pages still in a blocking disposition. Empty = clear."""
    blockers = []
    for p in data.get("pages", []):
        disp = p.get("disposition")
        if disp in ("review_required", "extraction_failed"):
            blockers.append(
                f"logical page {p.get('logical_page')}: {disp} -- {p.get('reason')}")
    for p in data.get("unpaged_physical_pages", []):
        disp = p.get("disposition")
        if disp in ("review_required", "extraction_failed"):
            blockers.append(
                f"unpaged physical page {p.get('physical_page')}: "
                f"{disp} -- {p.get('reason')}")
    return blockers
