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
  6. clause_ids are unique and sequential from C-1 (C-1, C-2, ... with no
     gap, no duplicate, no reordering) -- the stable address every downstream
     stage resolves a clause by.

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

_DOC_ID_IN_FILENAME_RE = re.compile(r"_(DOC_\d+)\.json$")
_CLAUSE_ID_RE = re.compile(r"^C-(\d+)$")
_PAGE_MARKER_RE = re.compile(r"(?m)^<<<PAGE page=(\d+)>>>\n?")


class SourceUnavailable(Exception):
    """Raised when the processed source text needed to verify a contract is
    missing or has untrustworthy page boundaries. Distinct from a returned
    error list: a verification that cannot run must halt, never PASS."""


def has_cross_contract_check(schema_name: str) -> bool:
    """Does this schema have a cross-contract dispatch? dao.write-contract
    calls check_cross_contract only when this is True, so contracts with no
    registered checks are unaffected (schema validation still runs for them)."""
    return schema_name == NORMALIZED_POLICY_CLAUSE_SCHEMA


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
    across all three condition lists. The label is used only for error text.
    """
    for ref in clause.get("evidence_references") or []:
        yield "clause", ref
    for list_name in ("payout_conditions", "exclusions", "reduction_conditions"):
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
