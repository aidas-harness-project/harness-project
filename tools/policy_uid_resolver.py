"""Recompute the whole canonical_v1 UID hierarchy from registered source text.

`policy_uid.py` decides what a single UID IS. This module decides what the
identity inputs for each kind ARE when they have to be read off a real
contract, and refuses every state where they cannot be read.

## The defect this closes (P0-2)

`dao._canonical_uid_errors` used to open with:

    spans = data.get("page_spans")
    if not spans:
        return []

Only `policy_boundary_inventory` has `page_spans`, so a normalized clause
contract, a reference table, an audit and a parent-coverage contract passed UID
verification by having nothing to verify -- and even inside the inventory only
the PS layer was recomputed: the PB on each boundary, and the PC/CI in each
`normalized_mappings` entry, were never touched. Six of the seven UID kinds
were therefore satisfied by the format regex alone, so

    PB-1111111111111111  PC-1111111111111111  CI-1111111111111111
    RT-1111111111111111  RR-1111111111111111  RC-1111111111111111

passed every gate as long as the same fabricated string was used consistently
in every artifact that referenced it. Cross-contract agreement is a check on
internal consistency; it is not a check on provenance, and a fabricated value is
perfectly self-consistent.

"Nothing to verify" must not mean "verified" under canonical_v1. Every function
here therefore reports an error for absent provenance rather than returning an
empty list.

## The hierarchy, and why each level's inputs are what they are

    PS   exact source span: (pdf, physical page, NFC bytes, occurrence)
    PB   one or more canonical PS -- a boundary IS its spans
    PC   parent PB + the clause's own canonical source spans
    CI   parent PC + the condition's own canonical source spans
    RT   the canonical PS of the table's source regions
    RR   parent RT + the row's exact source span
    RC   parent RR + the cell's exact source span (+ column_key)

Two properties fall out of this and are the reason for the shape:

  * **Every UID reduces to source bytes.** A parent UID is itself derived from
    spans, so the recursion bottoms out at text that must exist verbatim in the
    registered revision. There is no level at which a value can be asserted.

  * **Structure is identity, not just evidence.** RC includes its parent RR,
    which includes its own row span, so the transposition attack (`A 10 / B 20`
    extracted as `A 20 / B 10`) changes the cell UIDs even though every value
    still occurs on the page. Part 11E caught that with a row-containment check;
    here it also changes the identifiers themselves.

## Multi-span objects

A boundary can span several page spans; a clause or a table can cross pages; a
row or a cell can be split across lines or regions. The identity input for such
an object is the *canonical ordered list* of its span UIDs, joined with a
separator that cannot occur inside a UID.

Sort key is source position -- `(physical_page, start_char, end_char, span_uid)`
-- never submission order. Submission order is extraction order, which
`policy_uid.FORBIDDEN_UID_INPUTS` names explicitly: two agents extracting the
same clause in a different sequence must produce the same UID. `start_char` is
used only to ORDER and to select occurrences; it never enters a hash, so the
same span list at shifted offsets still yields the same UID.

## What this module never does

It never runs OCR, re-extracts a PDF, or calls any external tool. Its only
source of truth is the registered source-text revision already on disk, which
is what makes UID verification cheap enough to run on every write.
"""
from __future__ import annotations

import policy_uid

# Cannot occur inside a UID (they are prefix + hex), so a list of child UIDs
# has exactly one reading -- ["AB", "C"] and ["A", "BC"] cannot collide.
_SPAN_LIST_SEPARATOR = "\x1f"


class UidResolutionError(Exception):
    """The identity inputs for a UID could not be resolved from the source.

    Distinct from `policy_uid.UidInputError` (which means the inputs were not
    supplied) -- this means they were supplied and the registered source does
    not support them.
    """


class SourceContext:
    """Everything needed to recompute UIDs for ONE canonical document.

    Constructed by the DAO from state only the DAO can vouch for: the digest of
    the immutable original (hashed by the DAO itself, never accepted from a
    caller), the page text of the *registered* revision, and the logical ->
    physical page map. An agent cannot assemble one of these, which is the
    point.
    """

    def __init__(self, doc_id: str, source_pdf_sha256: str,
                 pages: dict, physical_page_for) -> None:
        self.doc_id = doc_id
        self.source_pdf_sha256 = source_pdf_sha256
        self.pages = pages
        self._physical_page_for = physical_page_for

    def physical_page(self, logical_page):
        return self._physical_page_for(logical_page)


def _resolve_span(context: SourceContext, span: dict, loc: str) -> dict:
    """Recompute one PS from a contract's span object.

    Returns the identity record other levels sort and hash. Raises rather than
    returning a partial record: a span that cannot be placed in the source is
    not a weaker span, it is not a span.
    """
    if not isinstance(span, dict):
        raise UidResolutionError(f"{loc}: span must be an object, got {span!r}")
    page = span.get("page")
    quote = span.get("quote")
    start = span.get("start_char")
    end = span.get("end_char")
    if quote is None or quote == "":
        raise UidResolutionError(
            f"{loc}: no quote -- a canonical span is identified by its exact "
            "source bytes, which cannot be inferred")
    if not isinstance(start, int) or not isinstance(end, int):
        raise UidResolutionError(
            f"{loc}: start_char/end_char must be integers to locate which "
            "occurrence of the quote is meant")
    page_text = context.pages.get(page)
    if page_text is None:
        raise UidResolutionError(
            f"{loc}: page {page!r} does not exist in the registered source "
            "revision, so no UID can be issued for it")
    if start < 0 or end > len(page_text) or start >= end:
        raise UidResolutionError(
            f"{loc}: offsets [{start}:{end}] are not a valid range in page "
            f"{page} (length {len(page_text)})")
    if page_text[start:end] != quote:
        raise UidResolutionError(
            f"{loc}: quote does not equal the registered page {page} text at "
            f"[{start}:{end}] -- the span does not say what it claims")
    physical = context.physical_page(page)
    if physical is None:
        raise UidResolutionError(
            f"{loc}: logical page {page} has no physical page mapping -- "
            "canonical identity is keyed to the immutable parent's physical "
            "page and must not fall back to the logical number")
    # The submitted ordinal, if any, is never trusted: it is re-derived from
    # the offset, which is itself verified against the registered bytes above.
    ordinal = policy_uid.ordinal_of_span_at(page_text, quote, start)
    submitted_ordinal = span.get("occurrence_ordinal")
    if submitted_ordinal is not None and submitted_ordinal != ordinal:
        raise UidResolutionError(
            f"{loc}: occurrence_ordinal {submitted_ordinal!r} disagrees with "
            f"the registered source, where this span is occurrence {ordinal} "
            "of its text on the page")
    uid = policy_uid.compute_uid(
        "span",
        source_pdf_sha256=context.source_pdf_sha256,
        physical_page=physical,
        span_text=quote,
        ordinal=ordinal,
    )
    return {
        "uid": uid,
        "physical_page": physical,
        "logical_page": page,
        "start_char": start,
        "end_char": end,
        "quote": quote,
        "ordinal": ordinal,
    }


def _sort_key(record: dict):
    """Canonical source order: page, then position, then UID as a tiebreak.

    Deliberately NOT the submitted array order -- see the module docstring.
    """
    return (record["physical_page"], record["start_char"],
            record["end_char"], record["uid"])


def _canonical_span_list(records: list) -> tuple[str, int]:
    """The identity string for a multi-span object, plus its first page.

    The page is the first span's physical page in canonical order, so the
    object's page is a property of the source rather than of which span the
    agent happened to list first.
    """
    ordered = sorted(records, key=_sort_key)
    uids = [record["uid"] for record in ordered]
    if len(set(uids)) != len(uids):
        raise UidResolutionError(
            "the same source span is listed twice -- a duplicate span cannot "
            f"contribute twice to an identity: {sorted(uids)}")
    return _SPAN_LIST_SEPARATOR.join(uids), ordered[0]["physical_page"]


def _overlap_errors(records: list, loc: str) -> None:
    """Refuse spans that overlap inside one object.

    Two overlapping spans double-count the same source bytes, so the object's
    identity would depend on how the extraction happened to split them -- the
    same failure mode as extraction order, in a different disguise.
    """
    by_page: dict = {}
    for record in records:
        by_page.setdefault(record["physical_page"], []).append(record)
    for page, page_records in by_page.items():
        ordered = sorted(page_records, key=_sort_key)
        for earlier, later in zip(ordered, ordered[1:]):
            if later["start_char"] < earlier["end_char"]:
                raise UidResolutionError(
                    f"{loc}: spans [{earlier['start_char']}:"
                    f"{earlier['end_char']}] and [{later['start_char']}:"
                    f"{later['end_char']}] on physical page {page} overlap -- "
                    "an identity may not double-count the same source bytes")


def resolve_spans(context: SourceContext, spans: list, loc: str) -> list:
    """Resolve a list of span objects into identity records, non-overlapping."""
    if not spans:
        raise UidResolutionError(
            f"{loc}: no source spans -- under canonical_v1 an element with no "
            "source provenance cannot be issued a UID, and absent provenance "
            "is a refusal rather than a skipped check")
    records = [
        _resolve_span(context, span, f"{loc}[{index}]")
        for index, span in enumerate(spans)
    ]
    _overlap_errors(records, loc)
    return records


def compute_derived_uid(kind: str, context: SourceContext, records: list,
                        parent_uid: str | None = None,
                        column_key: str | None = None) -> str:
    """The canonical UID for any kind whose identity is a set of source spans.

    `span_text` here is the canonical ordered list of child span UIDs, not raw
    page bytes: a boundary is not a single quote, it is exactly the spans it is
    made of. Feeding the concatenated *text* instead would make the identity
    depend on how the spans were sliced, which is extraction order again.
    """
    identity, physical_page = _canonical_span_list(records)
    return policy_uid.compute_uid(
        kind,
        source_pdf_sha256=context.source_pdf_sha256,
        physical_page=physical_page,
        span_text=identity,
        ordinal=1,
        parent_uid=parent_uid,
        column_key=column_key,
    )


def verify(kind: str, submitted, expected: str, loc: str) -> list:
    """Compare a submitted UID against the recomputed one."""
    if not submitted:
        return [
            f"{loc}: no {kind} UID -- canonical_v1 requires one, and an "
            "absent identifier is not a passing one"
        ]
    if submitted == expected:
        return []
    return [
        f"{loc}: {kind} UID {submitted!r} is not the canonical identifier for "
        f"its source element (expected {expected!r}) -- a UID is recomputed "
        "from registered source provenance, never accepted because several "
        "contracts agree on the same string"
    ]


# --- per-contract resolution ----------------------------------------------

def check_boundary_inventory(context: SourceContext, data: dict) -> list:
    """PS on every page span, then PB from the spans each boundary owns.

    A boundary's spans are the page spans that point at it (`disposition:
    boundary`, `boundary_uid` set) -- the inventory already carries that link,
    so PB needs no new field. What it did lack is any recomputation: the PB
    string was previously copied between the two places it appears and checked
    against nothing.
    """
    errors: list = []
    span_records: dict = {}
    by_boundary: dict = {}

    for index, span in enumerate(data.get("page_spans") or []):
        loc = f"page_spans[{index}]"
        try:
            record = _resolve_span(context, span, loc)
        except (UidResolutionError, policy_uid.UidInputError) as exc:
            errors.append(str(exc))
            continue
        errors.extend(verify("span", span.get("span_uid"), record["uid"], loc))
        span_records[index] = record
        if span.get("disposition") == "boundary" and span.get("boundary_uid"):
            by_boundary.setdefault(span["boundary_uid"], []).append(record)

    for index, boundary in enumerate(data.get("boundaries") or []):
        loc = f"boundaries[{index}]"
        submitted = boundary.get("boundary_uid")
        records = by_boundary.get(submitted) or []
        if not records:
            errors.append(
                f"{loc}: boundary {submitted!r} owns no page span in this "
                "contract, so its PB UID cannot be recomputed -- a boundary is "
                "identified by the source spans it is made of")
            continue
        try:
            expected = compute_derived_uid("boundary", context, records)
        except (UidResolutionError, policy_uid.UidInputError) as exc:
            errors.append(f"{loc}: {exc}")
            continue
        errors.extend(verify("boundary", submitted, expected, loc))
    return errors


def boundary_uid_index(context: SourceContext, inventory: dict) -> dict:
    """{recomputed PB UID: identity records} for a document's inventory.

    Used by the clause contract, whose PCs are parented on boundaries defined
    in a different file. Only boundaries whose PB recomputes correctly are
    included, so a clause cannot inherit legitimacy from a bad boundary.
    """
    index: dict = {}
    by_boundary: dict = {}
    for span_index, span in enumerate(inventory.get("page_spans") or []):
        if span.get("disposition") != "boundary" or not span.get("boundary_uid"):
            continue
        try:
            record = _resolve_span(
                context, span, f"page_spans[{span_index}]")
        except (UidResolutionError, policy_uid.UidInputError):
            continue
        by_boundary.setdefault(span["boundary_uid"], []).append(record)
    for submitted, records in by_boundary.items():
        try:
            expected = compute_derived_uid("boundary", context, records)
        except (UidResolutionError, policy_uid.UidInputError):
            continue
        if expected == submitted:
            index[expected] = records
    return index


_CONDITION_BUCKETS = (
    "payout_conditions",
    "exclusions",
    "reduction_conditions",
    "definitions",
    "obligations",
    "claim_requirements",
    "termination_conditions",
    "dispute_resolution_conditions",
    "coverage_start_conditions",
)


def check_normalized_clauses(context: SourceContext, data: dict,
                             boundary_index: dict) -> list:
    """PC from (parent PB + the clause's own spans), then CI from (PC + spans).

    The parent PB must be one this document's inventory actually defines and
    that recomputed correctly -- a clause may not name a boundary that only
    exists as a string. A clause with several parents sorts them, for the same
    reason span lists are sorted.
    """
    errors: list = []
    for clause_index, clause in enumerate(data.get("clauses") or []):
        cloc = f"clauses[{clause_index}]"
        parents = clause.get("source_boundary_uids") or []
        if not parents:
            errors.append(
                f"{cloc}: no source_boundary_uids -- a clause UID is derived "
                "from its parent boundary and cannot be computed without one")
            continue
        unknown = [uid for uid in parents if uid not in boundary_index]
        if unknown:
            errors.append(
                f"{cloc}: source_boundary_uids {sorted(unknown)} do not "
                "resolve to a canonical boundary of this document -- a parent "
                "UID must be one the boundary inventory independently "
                "recomputes, not a string that merely matches in shape")
            continue
        parent_key = _SPAN_LIST_SEPARATOR.join(sorted(parents))

        try:
            clause_records = resolve_spans(
                context, clause.get("source_span_uids"),
                f"{cloc}.source_span_uids")
            expected_clause = compute_derived_uid(
                "clause", context, clause_records, parent_uid=parent_key)
        except (UidResolutionError, policy_uid.UidInputError) as exc:
            errors.append(f"{cloc}: {exc}")
            continue
        submitted_clause = clause.get("clause_uid")
        clause_errors = verify(
            "clause", submitted_clause, expected_clause, cloc)
        errors.extend(clause_errors)
        if clause_errors:
            # Conditions are parented on the clause; recomputing them under a
            # UID already known to be wrong would report a second, derivative
            # failure that says nothing new.
            continue

        for bucket in _CONDITION_BUCKETS:
            for item_index, condition in enumerate(clause.get(bucket) or []):
                iloc = f"{cloc}.{bucket}[{item_index}]"
                try:
                    records = resolve_spans(
                        context, condition.get("source_span_uids"),
                        f"{iloc}.source_span_uids")
                    expected = compute_derived_uid(
                        "condition", context, records,
                        parent_uid=expected_clause)
                except (UidResolutionError, policy_uid.UidInputError) as exc:
                    errors.append(f"{iloc}: {exc}")
                    continue
                errors.extend(verify(
                    "condition", condition.get("condition_uid"), expected,
                    iloc))
    return errors


def check_reference_tables(context: SourceContext, data: dict) -> list:
    """RT from the table's source regions, RR from (RT + row span), RC from
    (RR + cell span + column_key).

    The cell's own exact span is what distinguishes two cells holding the same
    value: `10` in row A and `10` in row B are different source bytes at
    different offsets, so they resolve to different PS and therefore different
    RC -- and because RC is parented on RR, swapping the two values between
    rows changes both cell UIDs as well.
    """
    errors: list = []
    for table_index, table in enumerate(data.get("tables") or []):
        tloc = f"tables[{table_index}]"
        try:
            region_records = resolve_spans(
                context, table.get("source_regions"),
                f"{tloc}.source_regions")
            expected_table = compute_derived_uid(
                "table", context, region_records)
        except (UidResolutionError, policy_uid.UidInputError) as exc:
            errors.append(f"{tloc}: {exc}")
            continue
        submitted_table = table.get("table_uid")
        table_errors = verify("table", submitted_table, expected_table, tloc)
        errors.extend(table_errors)
        if table_errors:
            continue

        for row_index, row in enumerate(table.get("rows") or []):
            rloc = f"{tloc}.rows[{row_index}]"
            row_spans = row.get("source_spans")
            if not row_spans and row.get("source_span"):
                # v0.2 single-span rows stay expressible; a multi-region row
                # uses source_spans. One code path, so the UID of a one-span
                # row is identical either way.
                row_spans = [row["source_span"]]
            try:
                row_records = resolve_spans(
                    context, row_spans, f"{rloc}.source_span(s)")
                expected_row = compute_derived_uid(
                    "row", context, row_records, parent_uid=expected_table)
            except (UidResolutionError, policy_uid.UidInputError) as exc:
                errors.append(f"{rloc}: {exc}")
                continue
            submitted_row = row.get("row_uid")
            row_errors = verify("row", submitted_row, expected_row, rloc)
            errors.extend(row_errors)
            if row_errors:
                continue

            for cell_index, cell in enumerate(row.get("cells") or []):
                cloc = f"{rloc}.cells[{cell_index}]"
                try:
                    cell_records = resolve_spans(
                        context, cell.get("source_spans"),
                        f"{cloc}.source_spans")
                except (UidResolutionError, policy_uid.UidInputError) as exc:
                    errors.append(f"{cloc}: {exc}")
                    continue
                containment = _cell_containment_errors(
                    cell_records, row_records, cloc)
                if containment:
                    errors.extend(containment)
                    continue
                try:
                    expected_cell = compute_derived_uid(
                        "cell", context, cell_records,
                        parent_uid=expected_row,
                        column_key=cell.get("column_key"))
                except (UidResolutionError, policy_uid.UidInputError) as exc:
                    errors.append(f"{cloc}: {exc}")
                    continue
                errors.extend(verify(
                    "cell", cell.get("cell_uid"), expected_cell, cloc))
    return errors


def _cell_containment_errors(cell_records: list, row_records: list,
                             loc: str) -> list:
    """A cell's source bytes must sit inside its own row's source bytes.

    Without this, a cell could carry a perfectly valid span taken from a
    different row: the RC would recompute correctly (it is a real span) while
    naming a parent row it does not belong to. Part 11E checks the same
    relationship on cell *values*; this checks it on exact spans, which is what
    the UID is actually derived from.
    """
    errors = []
    for record in cell_records:
        inside = any(
            record["physical_page"] == row["physical_page"]
            and row["start_char"] <= record["start_char"]
            and record["end_char"] <= row["end_char"]
            for row in row_records
        )
        if not inside:
            errors.append(
                f"{loc}: source span [{record['start_char']}:"
                f"{record['end_char']}] on physical page "
                f"{record['physical_page']} lies outside its own row's source "
                "span -- a cell may not be identified by bytes belonging to "
                "another row")
    return errors
