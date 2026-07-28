"""Cross-contract invariants -- the checks JSON Schema structurally cannot make.

A JSON Schema validates one document against one shape. It cannot see a
sibling file (the processed policy text this contract claims to quote), it
cannot compare two entries of the same array to each other (clause_id
uniqueness/sequentiality), and it cannot parse the target *filename* to learn
which document the contract is supposed to be about. Every rule below lives in
exactly that blind spot.

This module currently dispatches one contract family:

  normalized_policy_clause_{DOC_ID}.json  (policy-pipeline output)

For it, "having a source" is not satisfied by a non-empty quote string. Every
clause AND every individual condition (payout/exclusion/reduction) must carry
at least one evidence reference whose {document_id, page, quote} all resolve
against the *actually processed* redacted policy text:

  1. document_id/page/quote all present (schema's strict_evidence_reference
     enforces presence; re-checked here so a caller that bypassed the schema
     still can't slip through).
  2. quote is non-whitespace real text.
  3. the filename's DOC_ID matches every reference's document_id -- a clause
     in normalized_policy_clause_DOC_002.json may not cite DOC_007.
  4. page is a real 1-based page that exists in the processed text.
  5. page+quote actually appear together in that page's processed text -- the
     quote is found on the page it claims, verbatim (whitespace-normalized).
  6. clause_ids are unique and sequential from C-1 (display order only).
  7. clause_uid and condition_uid values are globally unique within the
     document and are the stable downstream join keys.
  8. clause_kind agrees with its dedicated semantic bucket, preventing e.g.
     a disclosure obligation from being represented as a payout condition.

Page boundaries come from the `<<<PAGE page=N>>>` markers that checkpoint 2
embeds when it assembles redacted_text.md (same markers chunk_text.py slices
on). If those markers cannot be found or are not monotonic, page verification
CANNOT be trusted -- this module raises SourceUnavailable rather than
returning "no errors", so a caller never mistakes "couldn't check" for
"checked and clean". dao.write-contract turns that into a hard failure, not a
silent pass.

Failures are returned as a list of human-readable strings -- the same
contract as validate_instance() -- and dao.write-contract refuses the write on
any non-empty list (fail-before-persist, the rule already used for schema
errors). SourceUnavailable is raised, not returned, because it is a different
class of outcome: not "the contract is wrong" but "I cannot verify this
contract at all", which must never resolve to PASS.
"""
import re
from pathlib import Path

# The one contract family dispatched today. Keyed by the schema name the DAO
# passes to write-contract, so adding a family here is a one-line change plus
# its check function.
NORMALIZED_POLICY_CLAUSE_SCHEMA = "normalized_policy_clause.schema.json"
REFERENCE_TABLE_SCHEMA = "reference_table.schema.json"

_DOC_ID_IN_FILENAME_RE = re.compile(r"_(DOC_\d+)\.json$")
_CLAUSE_ID_RE = re.compile(r"^C-(\d+)$")
_PAGE_MARKER_RE = re.compile(r"(?m)^<<<PAGE page=(\d+)>>>\n?")
_HEADING_ONLY_RE = re.compile(
    r"^\s*제\s*\d+\s*조(?:의\s*\d+)?\s*(?:\([^)]{1,80}\))?\s*$"
)
_WORD_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)?\s*(?:%|년|월|일|회|원|영업일)?")
_DIRECT_QUOTE_TERMINAL_RE = re.compile(
    r"(?:다|니다|합니다|됩니다|않습니다|아니합니다|"
    r"지급|보상|한도|경우|때|사유|상태|금액|기준|"
    r"해야|하여야|알려야|의무|해지|취소|무효|소멸)"
    r"(?:[.!?。]|[)”’\"])?$"
)

# --- Polarity (Part 11B) --------------------------------------------------
# Negation/exclusion markers. A text carrying any of these expresses a
# negative/exclusionary proposition ("회사는 지급하지 않습니다"), the opposite
# outcome from a positive payout proposition ("회사는 지급합니다"). Polarity is
# outcome-determinative in policy language, so it is checked BEFORE the lexical
# floor -- a condition and its evidence sharing tokens but disagreeing on
# polarity is a contradiction, not weak support.
_NEGATION_MARKERS = (
    "않", "아니", "없", "제외", "면책", "부지급", "불가",
    "제한", "배제", "금한", "금합니다", "금지",
    "인정하지 않", "인정되지 않", "해당하지 않", "지급하지 아니",
)
# Predicate stems whose *positive* form is the operative promise of a payout
# clause. Used to detect a truncated quote that stops before the predicate is
# resolved one way or the other ("보험금을 지급하는 경우" -- the sentence has
# not yet said whether it IS or is NOT paid).
_TRUNCATED_PREDICATE_RE = re.compile(
    r"(?:지급|보상|지급하는|보상하는|지급되는|보상되는)\s*"
    r"(?:경우|때|사유|사항)?\s*$"
)
# Buckets whose normalized condition is inherently a POSITIVE payout/coverage
# proposition. A positive condition in one of these grounded only in negative
# evidence is a polarity contradiction.
_POSITIVE_BUCKETS = frozenset({
    "payout_conditions",
    "coverage_start_conditions",
})
# Buckets whose normalized condition is inherently NEGATIVE/exclusionary.
_NEGATIVE_BUCKETS = frozenset({
    "exclusions",
})


def _has_negation(text: str) -> bool:
    return any(marker in text for marker in _NEGATION_MARKERS)


def _quote_polarity(quote: str) -> str:
    """'negative' if the quote carries a negation/exclusion marker, else
    'positive'. A quote with no operative predicate at all is still classed
    'positive' here; the truncated-predicate check handles the incomplete
    case separately so the two failure modes report distinctly."""
    return "negative" if _has_negation(_normalize_ws(quote)) else "positive"


def _is_truncated_predicate(quote: str) -> bool:
    """True if the quote ends on an unresolved payout/coverage predicate --
    it names the operative verb but stops before saying paid vs not-paid.
    A quote that already carries a negation marker is NOT truncated (its
    polarity is resolved)."""
    normalized = _normalize_ws(quote)
    if _has_negation(normalized):
        return False
    return bool(_TRUNCATED_PREDICATE_RE.search(normalized))
_BUCKET_EVIDENCE_MARKERS = {
    "payout_conditions": ("지급", "보상", "보험금"),
    "exclusions": ("않", "아니", "제외", "면책", "부지급"),
    "reduction_conditions": ("감액", "삭감", "비율", "한도", "차감"),
    "definitions": ("정의", "말합니다", "뜻합니다", "의미합니다"),
    "obligations": ("의무", "해야", "하여야", "알려야", "제출하여야"),
    "claim_requirements": ("청구", "제출", "서류", "증명서"),
    "termination_conditions": ("해지", "취소", "무효", "소멸"),
    "dispute_resolution_conditions": (
        "분쟁", "소송", "관할", "소멸시효", "조정"),
    "coverage_start_conditions": ("보장", "개시", "효력"),
}
CONDITION_BUCKETS = (
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
KIND_BUCKETS = {
    "coverage": {"payout_conditions", "exclusions", "reduction_conditions"},
    "definition": {"definitions"},
    "obligation": {"obligations"},
    "procedure": {"claim_requirements"},
    "termination": {"termination_conditions"},
    "dispute_resolution": {"dispute_resolution_conditions"},
    "coverage_start": {"coverage_start_conditions"},
}


class SourceUnavailable(Exception):
    """Raised when the processed source text needed to verify a contract is
    missing or has untrustworthy page boundaries. Distinct from a returned
    error list: a verification that cannot run must halt, never PASS."""


def has_cross_contract_check(schema_name: str) -> bool:
    """Does this schema have a cross-contract dispatch? dao.write-contract
    calls check_cross_contract only when this is True, so contracts with no
    registered checks are unaffected (schema validation still runs for them)."""
    return schema_name in {
        NORMALIZED_POLICY_CLAUSE_SCHEMA,
        REFERENCE_TABLE_SCHEMA,
    }


def doc_id_from_filename(filename: str) -> str | None:
    """Parse the target DOC_ID out of normalized_policy_clause_DOC_XXX.json.

    Returns None when the filename doesn't carry a DOC_ID suffix -- which is
    itself a failure for this contract family (it is required to be per-doc),
    reported by the caller rather than raised here.
    """
    m = _DOC_ID_IN_FILENAME_RE.search(Path(filename).name)
    return m.group(1) if m else None


def _normalize_ws(text: str) -> str:
    """Collapse all runs of whitespace to single spaces and strip ends.

    Two independent transcriptions/renderings differ in incidental whitespace
    (line wraps, trailing spaces, NBSP vs space). Quote-in-source matching
    must be robust to that without becoming a fuzzy/semantic match -- this is
    the same normalization philosophy P8's comparator uses, applied to an
    exact substring test, not a similarity score.
    """
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


def split_pages(redacted_text: str) -> dict[int, str]:
    """Return {page_number: page_text} split on the <<<PAGE page=N>>> markers.

    Raises SourceUnavailable if no markers are found, if a marker's number
    isn't strictly increasing, or if any non-whitespace content sits before
    the first marker -- all three mean the page boundaries can't be trusted,
    so page-scoped verification must not silently pass. Mirrors chunk_text.py's
    line-anchored, monotonic, fail-loud contract.
    """
    matches = list(_PAGE_MARKER_RE.finditer(redacted_text))
    if not matches:
        raise SourceUnavailable(
            "no <<<PAGE page=N>>> markers found in redacted_text.md -- page "
            "boundaries cannot be established, so page+quote verification "
            "cannot be trusted"
        )
    if redacted_text[: matches[0].start()].strip():
        raise SourceUnavailable(
            "content found before the first <<<PAGE page=N>>> marker -- page "
            "boundaries cannot be trusted"
        )
    pages: dict[int, str] = {}
    last_page = 0
    for i, m in enumerate(matches):
        page_num = int(m.group(1))
        if page_num <= last_page:
            raise SourceUnavailable(
                f"page markers are not strictly increasing (saw page={page_num} "
                f"after page={last_page}) -- page boundaries cannot be trusted"
            )
        last_page = page_num
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(redacted_text)
        pages[page_num] = redacted_text[start:end]
    return pages


def _iter_all_references(clause: dict):
    """Yield (location_label, reference_dict) for every evidence reference in a
    clause -- the clause's own references and each condition's references,
    across all semantic condition lists. The label is used only for error text.
    """
    for ref in clause.get("evidence_references") or []:
        yield "clause", ref
    for list_name in CONDITION_BUCKETS:
        for idx, item in enumerate(clause.get(list_name) or []):
            for ref in item.get("evidence_references") or []:
                yield f"{list_name}[{idx}]", ref


def check_clause_ids(clauses: list[dict]) -> list[str]:
    """clause_ids must be unique and sequential C-1, C-2, ... in array order."""
    errors: list[str] = []
    seen: set[str] = set()
    for i, clause in enumerate(clauses):
        cid = clause.get("clause_id")
        expected = f"C-{i + 1}"
        if cid in seen:
            errors.append(f"clauses[{i}]: duplicate clause_id {cid!r}")
        seen.add(cid)
        if cid != expected:
            m = _CLAUSE_ID_RE.match(cid or "")
            if not m:
                errors.append(f"clauses[{i}]: clause_id {cid!r} is not of the form C-<n>")
            else:
                errors.append(
                    f"clauses[{i}]: clause_id {cid!r} breaks C-1 sequentiality "
                    f"(expected {expected!r} at extraction position {i + 1})"
                )
    return errors


def check_stable_ids_and_semantics(clauses: list[dict]) -> list[str]:
    """Validate stable-UID uniqueness and clause-kind/bucket separation."""
    errors: list[str] = []
    seen_clause_uids: set[str] = set()
    seen_condition_uids: set[str] = set()
    for index, clause in enumerate(clauses):
        clause_uid = clause.get("clause_uid")
        if clause_uid in seen_clause_uids:
            errors.append(
                f"clauses[{index}]: duplicate clause_uid {clause_uid!r}")
        if clause_uid:
            seen_clause_uids.add(clause_uid)

        kind = clause.get("clause_kind")
        populated = {
            bucket for bucket in CONDITION_BUCKETS
            if clause.get(bucket)
        }
        if kind in KIND_BUCKETS:
            allowed = KIND_BUCKETS[kind]
            wrong = populated - allowed
            if not populated.intersection(allowed):
                errors.append(
                    f"clauses[{index}]: clause_kind {kind!r} requires a "
                    f"non-empty bucket in {sorted(allowed)}")
            if wrong:
                errors.append(
                    f"clauses[{index}]: clause_kind {kind!r} cannot populate "
                    f"semantic bucket(s) {sorted(wrong)}; allowed={sorted(allowed)}")
        elif kind == "other" and clause.get("review_required") is not True:
            errors.append(
                f"clauses[{index}]: clause_kind 'other' requires review_required=true")

        for bucket in CONDITION_BUCKETS:
            for item_index, item in enumerate(clause.get(bucket) or []):
                condition_uid = item.get("condition_uid")
                if condition_uid in seen_condition_uids:
                    errors.append(
                        f"clauses[{index}].{bucket}[{item_index}]: duplicate "
                        f"condition_uid {condition_uid!r}")
                if condition_uid:
                    seen_condition_uids.add(condition_uid)
    return errors


def _meaningful_tokens(text: str) -> set[str]:
    """Return a conservative lexical floor for semantic-support checks.

    This is deliberately not a semantic classifier. It only catches the
    high-confidence failure where a normalized condition and its cited source
    share almost no content (especially a clause heading cited as if it were a
    condition). Human/agent semantic review remains necessary above this floor.
    """
    return set(_WORD_RE.findall(_normalize_ws(text)))


def _looks_like_heading_only(quote: str) -> bool:
    normalized = _normalize_ws(quote)
    return bool(_HEADING_ONLY_RE.match(normalized))


def check_condition_support(clauses: list[dict]) -> list[str]:
    """Enforce a deterministic evidence-quality floor for each condition."""
    errors: list[str] = []
    for clause_index, clause in enumerate(clauses):
        for bucket in CONDITION_BUCKETS:
            for item_index, item in enumerate(clause.get(bucket) or []):
                loc = f"clauses[{clause_index}].{bucket}[{item_index}]"
                refs = item.get("evidence_references") or []
                quotes = [
                    ref.get("quote", "") for ref in refs
                    if isinstance(ref, dict) and ref.get("quote", "").strip()
                ]
                level = item.get("support_level")
                if level == "insufficient":
                    errors.append(
                        f"{loc}: support_level 'insufficient' cannot be emitted "
                        "in a successful normalized policy contract")
                    continue
                if level == "composite":
                    if len(quotes) < 2:
                        errors.append(
                            f"{loc}: composite support requires at least two "
                            "source passages")
                    if item.get("review_required") is not True:
                        errors.append(
                            f"{loc}: composite support requires review_required=true")

                if quotes and all(_looks_like_heading_only(q) for q in quotes):
                    errors.append(
                        f"{loc}: condition evidence contains only clause heading(s), "
                        "which do not substantiate the normalized condition")
                    continue

                condition_text = _normalize_ws(item.get("text", ""))
                condition_tokens = _meaningful_tokens(condition_text)
                quote_text = _normalize_ws(" ".join(quotes))
                if level == "direct" and quote_text and not \
                        _DIRECT_QUOTE_TERMINAL_RE.search(quote_text):
                    errors.append(
                        f"{loc}: direct evidence ends before a complete policy "
                        "proposition; cite through the operative predicate or use "
                        "composite support with review_required=true")

                # A quote naming the payout/coverage predicate but stopping
                # before it resolves ("보험금을 지급하는 경우") is not complete
                # direct evidence of EITHER a positive or a negative
                # proposition. Route it to review; never treat it as proof.
                if level == "direct" and any(
                        _is_truncated_predicate(q) for q in quotes) and not any(
                        _DIRECT_QUOTE_TERMINAL_RE.search(_normalize_ws(q))
                        and not _is_truncated_predicate(q) for q in quotes):
                    errors.append(
                        f"{loc}: cited evidence stops on an unresolved operative "
                        "predicate (e.g. '지급하는 경우') -- it does not state "
                        "whether the benefit is paid or not; cite through the "
                        "predicate or route to review_required")

                markers = _BUCKET_EVIDENCE_MARKERS.get(bucket, ())
                if markers and quote_text and not any(
                        marker in quote_text for marker in markers):
                    errors.append(
                        f"{loc}: cited evidence does not contain an operative "
                        f"marker for semantic bucket {bucket!r}; the bucket label "
                        "must not supply meaning that is absent from the quote")

                condition_numbers = set(_NUMBER_RE.findall(condition_text))
                quote_numbers = set(_NUMBER_RE.findall(quote_text))
                missing_numbers = condition_numbers - quote_numbers
                if missing_numbers:
                    errors.append(
                        f"{loc}: normalized condition introduces numeric/temporal "
                        f"terms absent from its evidence: "
                        f"{sorted(missing_numbers)}")

                # Polarity is outcome-determinative in policy language, checked
                # BIDIRECTIONALLY (Part 11B). A condition and its evidence that
                # share tokens but disagree on paid-vs-not-paid is a
                # contradiction, not weak support -- and must not be "fixed" by
                # rewording the condition; route it to review or correct the
                # extraction instead.
                condition_has_negation = _has_negation(condition_text)
                quote_polarities = {_quote_polarity(q) for q in quotes}
                quote_has_negation = "negative" in quote_polarities
                quote_has_positive = "positive" in quote_polarities

                # (a) negative condition inferred from purely positive evidence.
                if condition_has_negation and quotes and not quote_has_negation:
                    errors.append(
                        f"{loc}: normalized condition asserts negation/exclusion "
                        "but every cited quote is positive/affirmative -- polarity "
                        "contradiction; the evidence does not support a negation")

                # (b) positive condition inferred from purely negative evidence.
                #     Applies to an inherently-positive bucket, OR to any
                #     non-exclusion bucket whose condition text carries no
                #     negation of its own (so the condition reads as an
                #     affirmative proposition) while its evidence is entirely
                #     exclusionary.
                inherently_positive = bucket in _POSITIVE_BUCKETS
                reads_positive = (
                    not condition_has_negation and bucket not in _NEGATIVE_BUCKETS)
                if (inherently_positive or reads_positive) and quotes \
                        and quote_has_negation and not quote_has_positive:
                    errors.append(
                        f"{loc}: normalized condition reads as a positive "
                        f"payout/coverage proposition but every cited quote is "
                        "negative/exclusionary -- polarity contradiction; a "
                        "'지급' condition may not be grounded solely in a "
                        "'지급하지 않는다' quote")

                # (c) composite evidence whose passages disagree on polarity is
                #     not silently accepted just because review_required is set:
                #     a mixed-polarity composite is a genuine ambiguity that a
                #     human must resolve, reported as its own blocker.
                if level == "composite" and quote_has_negation and \
                        quote_has_positive:
                    if item.get("review_required") is not True:
                        errors.append(
                            f"{loc}: composite evidence mixes positive and "
                            "negative polarity passages -- this ambiguity must be "
                            "routed to review_required, not merged into one "
                            "condition")

                if condition_tokens:
                    supported = {
                        token for token in condition_tokens if token in quote_text
                    }
                    coverage = len(supported) / len(condition_tokens)
                    if coverage < 0.5:
                        errors.append(
                            f"{loc}: cited source has insufficient lexical support "
                            f"for the normalized condition ({len(supported)}/"
                            f"{len(condition_tokens)} meaningful tokens); preserve "
                            "the source meaning, cite the complete supporting "
                            "passage, or route the item to review_required -- do "
                            "not rewrite the condition to raise this score")
    return errors


def check_normalized_policy_clause(data: dict, filename: str, redacted_text: str | None) -> list[str]:
    """Full cross-contract check for one normalized_policy_clause file.

    redacted_text is the processed policy text for the target document (from
    the DAO's processed layer). If it's None -- no processed text exists --
    that is SourceUnavailable, not a clean pass: this contract cannot be
    verified against a document that was never processed.
    """
    errors: list[str] = []

    target_doc = doc_id_from_filename(filename)
    if target_doc is None:
        errors.append(
            f"filename {filename!r} carries no DOC_ID suffix -- policy clause output "
            "must be named normalized_policy_clause_<DOC_ID>.json (one file per document)"
        )

    clauses = data.get("clauses")
    if not isinstance(clauses, list):
        # Schema already enforces this; guard so we don't crash below.
        return errors + ["clauses: missing or not an array"]

    errors.extend(check_clause_ids(clauses))
    errors.extend(check_stable_ids_and_semantics(clauses))
    errors.extend(check_condition_support(clauses))

    if redacted_text is None:
        raise SourceUnavailable(
            f"no processed/redacted text found for {target_doc or filename} -- the "
            "policy document this contract claims to quote has not been processed, "
            "so no source location can be verified"
        )

    # Raises SourceUnavailable if boundaries can't be trusted -- deliberate.
    pages = split_pages(redacted_text)
    normalized_pages = {n: _normalize_ws(t) for n, t in pages.items()}

    for i, clause in enumerate(clauses):
        for label, ref in _iter_all_references(clause):
            loc = f"clauses[{i}].{label}"
            doc_id = ref.get("document_id")
            page = ref.get("page")
            quote = ref.get("quote")

            if not doc_id:
                errors.append(f"{loc}: evidence reference is missing document_id")
            elif target_doc is not None and doc_id != target_doc:
                errors.append(
                    f"{loc}: evidence reference cites {doc_id} but this file is for "
                    f"{target_doc} -- a clause may not be grounded in another document"
                )

            if page is None:
                errors.append(f"{loc}: evidence reference is missing page")
            if not quote or not quote.strip():
                errors.append(f"{loc}: evidence reference has an empty/whitespace quote")

            # Only attempt source-location matching once the reference is
            # structurally complete AND belongs to the target document -- a
            # missing/foreign reference already produced its own error.
            if page is None or not quote or not quote.strip():
                continue
            if doc_id and target_doc is not None and doc_id != target_doc:
                continue
            if page not in normalized_pages:
                errors.append(
                    f"{loc}: page {page} does not exist in the processed text for "
                    f"{target_doc} (pages present: {sorted(normalized_pages)})"
                )
                continue
            if _normalize_ws(quote) not in normalized_pages[page]:
                errors.append(
                    f"{loc}: quote not found on page {page} of the processed text -- "
                    "the cited quote does not appear verbatim on the page it claims "
                    f"(quote={quote[:60]!r}...)"
                )

    return errors


def check_reference_table(
        data: dict, filename: str, redacted_text: str | None) -> list[str]:
    """Validate stable table addresses and cell-level source grounding."""
    errors: list[str] = []
    target_doc = doc_id_from_filename(filename)
    if target_doc is None:
        errors.append(
            "reference-table filename must be reference_table_<DOC_ID>.json")
    if data.get("source_document_id") != target_doc:
        errors.append(
            f"source_document_id {data.get('source_document_id')!r} does not "
            f"match filename document {target_doc!r}")
    if redacted_text is None:
        raise SourceUnavailable(
            f"no processed/redacted text found for {target_doc or filename}")
    pages = split_pages(redacted_text)
    normalized_pages = {number: _normalize_ws(text)
                        for number, text in pages.items()}

    seen_table_uids: set[str] = set()
    seen_row_uids: set[str] = set()
    seen_cell_uids: set[str] = set()
    for table_index, table in enumerate(data.get("tables") or []):
        expected = f"T-{table_index + 1}"
        if table.get("table_id") != expected:
            errors.append(
                f"tables[{table_index}]: table_id must be sequential display "
                f"label {expected!r}")
        table_uid = table.get("table_uid")
        if table_uid in seen_table_uids:
            errors.append(
                f"tables[{table_index}]: duplicate table_uid {table_uid!r}")
        if table_uid:
            seen_table_uids.add(table_uid)

        column_keys = [column.get("column_key")
                       for column in table.get("columns") or []]
        if len(column_keys) != len(set(column_keys)):
            errors.append(
                f"tables[{table_index}]: column_key values must be unique")

        references = [
            (f"tables[{table_index}].title", table.get("title", ""), ref)
            for ref in table.get("evidence_references") or []
        ]
        for row_index, row in enumerate(table.get("rows") or []):
            row_uid = row.get("row_uid")
            if row_uid in seen_row_uids:
                errors.append(
                    f"tables[{table_index}].rows[{row_index}]: duplicate "
                    f"row_uid {row_uid!r}")
            if row_uid:
                seen_row_uids.add(row_uid)
            cells = row.get("cells") or []
            cell_keys = [cell.get("column_key") for cell in cells]
            if len(cell_keys) != len(set(cell_keys)):
                errors.append(
                    f"tables[{table_index}].rows[{row_index}]: duplicate "
                    "column_key in row")
            if set(cell_keys) != set(column_keys):
                errors.append(
                    f"tables[{table_index}].rows[{row_index}]: cells must "
                    "cover every declared column exactly once")
            for cell_index, cell in enumerate(cells):
                cell_uid = cell.get("cell_uid")
                if cell_uid in seen_cell_uids:
                    errors.append(
                        f"tables[{table_index}].rows[{row_index}].cells"
                        f"[{cell_index}]: duplicate cell_uid {cell_uid!r}")
                if cell_uid:
                    seen_cell_uids.add(cell_uid)
                references.extend(
                    (f"tables[{table_index}].rows[{row_index}].cells"
                     f"[{cell_index}]", cell.get("value", ""), ref)
                    for ref in cell.get("evidence_references") or []
                )

        for loc, value, ref in references:
            doc_id = ref.get("document_id")
            page = ref.get("page")
            quote = ref.get("quote", "")
            if doc_id != target_doc:
                errors.append(
                    f"{loc}: evidence cites {doc_id!r}, expected {target_doc!r}")
                continue
            if page not in normalized_pages:
                errors.append(
                    f"{loc}: page {page!r} does not exist in processed source")
                continue
            normalized_quote = _normalize_ws(quote)
            if normalized_quote not in normalized_pages[page]:
                errors.append(
                    f"{loc}: quote not found on page {page} of processed source")
                continue
            if _normalize_ws(value) not in normalized_quote:
                errors.append(
                    f"{loc}: cited quote does not contain the table title/cell "
                    f"value {value!r}")

    # Row/column source structure and reverse row coverage (Part 11E). Cell-level
    # grounding above proves each value exists; this proves the ROW does.
    errors.extend(check_reference_table_structure(data, pages))
    return errors


def _span_text_errors(span: dict, pages: dict[int, str], loc: str) -> list[str]:
    """Verify an exact-offset source span against the real page text."""
    errors: list[str] = []
    page = span.get("page")
    start = span.get("start_char")
    end = span.get("end_char")
    quote = span.get("quote", "")
    if page not in pages:
        errors.append(f"{loc}: page {page!r} does not exist in processed source")
        return errors
    page_text = pages[page]
    if not isinstance(start, int) or not isinstance(end, int):
        errors.append(f"{loc}: start_char/end_char must be integers")
        return errors
    if start >= end:
        errors.append(f"{loc}: start_char {start} must be < end_char {end}")
        return errors
    if start < 0 or end > len(page_text):
        errors.append(
            f"{loc}: offsets [{start}:{end}] exceed page {page} length "
            f"{len(page_text)}")
        return errors
    if page_text[start:end] != quote:
        errors.append(
            f"{loc}: quote does not equal the exact page {page} text at "
            f"[{start}:{end}]")
    return errors


def check_reference_table_structure(
        data: dict, pages: dict[int, str]) -> list[str]:
    """Validate row/column source structure and reverse row coverage (11E).

    Cell-level grounding cannot prove a ROW: with source rows 'A 10' and
    'B 20', an extraction of 'A 20' / 'B 10' has every individual value present
    on the page. A row must therefore carry the exact source range of the
    physical row it came from; every cell value must be found INSIDE that range,
    in the order the columns declare; and the table's whole source region must
    be accounted for by rows plus explicitly-declared header spans, which is how
    a source row omitted from the extraction becomes visible.
    """
    errors: list[str] = []
    for table_index, table in enumerate(data.get("tables") or []):
        tloc = f"tables[{table_index}]"
        column_keys = [c.get("column_key") for c in table.get("columns") or []]

        # A table predating v0.2 carries none of the source structure. Report
        # that once, clearly, instead of cascading offset errors for every
        # absent span -- the schema already names the missing property, and a
        # second wave of "page None does not exist" only obscures it.
        if "source_regions" not in table:
            errors.append(
                f"{tloc}: no source_regions -- the table declares no source "
                "region, so its rows cannot be checked for completeness "
                "(re-extract with row/table source structure)")
            continue

        regions = table.get("source_regions") or []
        for region_index, region in enumerate(regions):
            errors.extend(_span_text_errors(
                region, pages, f"{tloc}.source_regions[{region_index}]"))

        header_spans = table.get("header_spans") or []
        for header_index, header in enumerate(header_spans):
            errors.extend(_span_text_errors(
                header.get("span") or {}, pages,
                f"{tloc}.header_spans[{header_index}].span"))

        rows = table.get("rows") or []
        row_spans: list[tuple[int, int, int]] = []
        for row_index, row in enumerate(rows):
            rloc = f"{tloc}.rows[{row_index}]"
            if "source_span" not in row:
                errors.append(
                    f"{rloc}: no source_span -- a row without its own source "
                    "range cannot be distinguished from values that merely "
                    "appear somewhere on the page")
                continue
            primary = row.get("source_span") or {}
            spans = row.get("source_spans") or [primary]
            valid_spans = []
            for span_index, span in enumerate(spans):
                sloc = (
                    f"{rloc}.source_spans[{span_index}]"
                    if row.get("source_spans")
                    else f"{rloc}.source_span")
                span_errors = _span_text_errors(span, pages, sloc)
                errors.extend(span_errors)
                if span_errors:
                    continue
                valid_spans.append(span)

            if not valid_spans:
                continue
            valid_spans.sort(key=lambda item: (
                item.get("page"), item.get("start_char"),
                item.get("end_char")))
            first = valid_spans[0]
            if row.get("source_spans") and any(
                    primary.get(key) != first.get(key)
                    for key in ("page", "start_char", "end_char", "quote")):
                errors.append(
                    f"{rloc}: source_span must equal the first source range "
                    "in source_spans")

            for span in valid_spans:
                page = span.get("page")
                start = span.get("start_char")
                end = span.get("end_char")
                row_spans.append((page, start, end))

                # Every row range, not only the legacy first range, must sit
                # inside the table regions. Otherwise Part 11E and the UID
                # resolver would validate two different rows.
                if regions and not any(
                        region.get("page") == page
                        and region.get("start_char", 0) <= start
                        and end <= region.get("end_char", 0)
                        for region in regions):
                    errors.append(
                        f"{rloc}: source span [{start}:{end}] on page {page} "
                        "is outside every declared source_region of this table")

            row_quote = " ".join(
                span.get("quote", "") for span in valid_spans)

            # Every cell value must be present in THIS row's source range, in
            # the order the columns are declared. This is what makes a
            # transposed value detectable.
            cursor = 0
            ordered_keys = [
                key for key in column_keys
                if any(cell.get("column_key") == key
                       for cell in row.get("cells") or [])
            ]
            for key in ordered_keys:
                cell = next(
                    cell for cell in row.get("cells") or []
                    if cell.get("column_key") == key)
                value = (cell.get("value") or "").strip()
                if not value:
                    continue
                position = row_quote.find(value, cursor)
                if position == -1:
                    if value in row_quote:
                        errors.append(
                            f"{rloc}.cells[{key!r}]: value {value!r} appears in "
                            "the row but not in declared column order -- the "
                            "column assignment disagrees with the source layout")
                    else:
                        errors.append(
                            f"{rloc}.cells[{key!r}]: value {value!r} is not "
                            "present in this row's source span "
                            f"{row_quote[:40]!r} -- a cell may not be taken from "
                            "another row; do not rearrange values to make a "
                            "table validate")
                    continue
                cursor = position + len(value)

        # Reverse coverage: every non-whitespace char of every declared region
        # must be covered by a row span or a header span. An uncovered stretch
        # is a source row that exists but was never extracted.
        covered: dict[int, list[tuple[int, int]]] = {}
        for page, start, end in row_spans:
            covered.setdefault(page, []).append((start, end))
        for header in header_spans:
            span = header.get("span") or {}
            if isinstance(span.get("start_char"), int) and \
                    isinstance(span.get("end_char"), int):
                covered.setdefault(span.get("page"), []).append(
                    (span["start_char"], span["end_char"]))

        for region_index, region in enumerate(regions):
            page = region.get("page")
            start = region.get("start_char")
            end = region.get("end_char")
            if page not in pages or not isinstance(start, int) or \
                    not isinstance(end, int) or start >= end or \
                    end > len(pages[page]):
                continue  # already reported by _span_text_errors
            page_text = pages[page]
            occupied = [False] * (end - start)
            for cstart, cend in covered.get(page, []):
                for pos in range(max(cstart, start), min(cend, end)):
                    occupied[pos - start] = True
            uncovered = [
                pos for pos in range(start, end)
                if not page_text[pos].isspace() and not occupied[pos - start]
            ]
            if uncovered:
                first = uncovered[0]
                snippet = page_text[first:first + 60].replace("\n", "\\n")
                errors.append(
                    f"{tloc}.source_regions[{region_index}]: source text at page "
                    f"{page} char {first} is in the table's region but belongs to "
                    f"no extracted row and no declared header span -- a source "
                    f"row appears to be missing from the extraction: {snippet!r}")
    return errors


def unresolved_reference_table_reviews(data: dict) -> list[str]:
    """Return every table/cell that still requires human or layout review.

    Reference-table contracts may be persisted while work is in progress, but
    policy finalization must not treat a table UID's mere existence as proof
    that the table was completely reconstructed.
    """
    blockers: list[str] = []
    for table_index, table in enumerate(data.get("tables") or []):
        if table.get("review_required") is True:
            blockers.append(
                f"tables[{table_index}] {table.get('table_uid')}: "
                "review_required=true")
        for row_index, row in enumerate(table.get("rows") or []):
            for cell_index, cell in enumerate(row.get("cells") or []):
                if cell.get("review_required") is True:
                    blockers.append(
                        f"tables[{table_index}].rows[{row_index}].cells"
                        f"[{cell_index}] {cell.get('cell_uid')}: "
                        "review_required=true")
    return blockers
