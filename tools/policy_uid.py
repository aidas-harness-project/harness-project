"""Canonical source-local policy identifiers (Part 11J commit c).

Before this, nothing in the repository computed or verified a UID. The schemas
said `"Stable deterministic identifier derived from immutable source identity
... never from array position"`, but the only thing enforced was the regex
`^PC-[a-f0-9]{16,64}$` -- so `PC-` followed by sixteen hex digits of anything
passed: a counter, a hash of the array index, a random value. Parts 11F-11I
bound what a UID POINTS AT (contract digests, page maps, roles, snapshots);
none of them constrained where the UID itself came from.

That matters because UIDs are the downstream join keys. A UID derived from
array position means inserting one clause gives an existing clause a new UID
(the join breaks, loudly) and hands its old UID to a DIFFERENT clause (the join
silently re-points at the wrong text). The second failure produces no error at
any layer.

## Identity vs. evidence

The split is the whole design, and it was got wrong twice before landing.

    IDENTITY (decides the UID)
      scheme version
      UID kind
      source_pdf_sha256      -- which immutable original
      physical page number   -- which page of it
      NFC(span bytes)        -- which text
      occurrence ordinal     -- which one, when the same text repeats
      parent UID             -- for nested kinds

    EVIDENCE (verifies the claim, never feeds the UID)
      start_char / end_char  -- that the span really sits there
      quote                  -- that it reads as claimed
      physical_page_sha256   -- that page extraction has not moved

Two things are deliberately NOT identity inputs, both because they change for
reasons unrelated to the span:

  * **Whole-document or whole-page digests.** Hashing `redacted_text.md` would
    mean a typo fix on page 200 changes every UID on page 1. Hashing the page
    would shrink that to the page -- still every clause on it, for a
    one-character fix elsewhere.

  * **Offsets.** A one-character edit shifts every later offset on the page, so
    offset-derived UIDs move for spans whose bytes never changed. Offsets stay
    in the contracts and are still verified (Part 11E checks span text at its
    offsets); they simply do not decide identity.

The occurrence ordinal is what replaces offsets as the discriminator when the
same text appears twice on a page. It is SOURCE order -- a property of the
document, recomputable from the page text alone -- not EXTRACTION order, which
is a property of how an agent happened to process the page and is forbidden.

## Normalization

  * **Unicode NFC.** Not theoretical for Korean: `제3조` composed and decomposed
    are different byte sequences for identical text, and this pipeline has two
    independent readers that can disagree on which they emit.
  * **Line endings** to `\n`. A Windows checkout must not re-identify a clause.
  * **Whitespace** is NOT collapsed. Collapsing would make genuinely different
    source spans collide, and the span text still has to match the source at
    its recorded offsets for the Part 11E evidence check.
"""
from __future__ import annotations

import hashlib
import unicodedata

SCHEME = "canonical_v1"

# kind -> UID prefix. The prefix is part of the hashed input too, so the same
# bytes at the same place cannot collide across kinds.
PREFIXES = {
    "boundary": "PB",
    "span": "PS",
    "clause": "PC",
    "condition": "CI",
    "table": "RT",
    "row": "RR",
    "cell": "RC",
}

_UID_HEX_LENGTH = 16


class UidInputError(Exception):
    """A UID was requested without the identity inputs that define it.

    Raised rather than returning a placeholder: a UID computed from partial
    identity would be a stable-looking value that does not actually identify
    anything, which is worse than an error because it survives review.
    """


def normalize_text(text: str) -> str:
    """NFC + normalized line endings. Whitespace deliberately preserved."""
    if text is None:
        raise UidInputError("span text is required to identify a UID")
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", unified)


def occurrence_ordinal(page_text: str, span_text: str) -> int:
    """Which occurrence of `span_text` this is, counting from the page start.

    Source order, not extraction order: recomputable from the page alone, so it
    does not depend on how an agent walked the document. Non-overlapping,
    counted on normalized text so the count cannot differ from the identity
    input it discriminates.
    """
    haystack = normalize_text(page_text)
    needle = normalize_text(span_text)
    if not needle:
        raise UidInputError("span text is empty; nothing to identify")
    count = haystack.count(needle)
    if count == 0:
        raise UidInputError(
            "span text does not occur in the page it claims to come from -- "
            "a UID may not be issued for text the source does not contain")
    return count


def ordinal_of_span_at(page_text: str, span_text: str, start_char: int) -> int:
    """The 1-based ordinal of the occurrence beginning at `start_char`.

    The offset is used to SELECT which occurrence is meant, then discarded --
    it never enters the hash. That is the point: the offset answers "which one
    did you mean", the ordinal answers "which one is it", and only the second
    is stable when unrelated text on the page shifts.
    """
    haystack = normalize_text(page_text)
    needle = normalize_text(span_text)
    if not needle:
        raise UidInputError("span text is empty; nothing to identify")
    ordinal = 0
    cursor = 0
    while True:
        found = haystack.find(needle, cursor)
        if found < 0:
            break
        ordinal += 1
        if found >= start_char:
            return ordinal
        cursor = found + 1
    if ordinal == 0:
        raise UidInputError(
            "span text does not occur in the page it claims to come from -- "
            "a UID may not be issued for text the source does not contain")
    return ordinal


def compute_uid(kind: str, *, source_pdf_sha256: str, physical_page: int,
                span_text: str, ordinal: int = 1,
                parent_uid: str | None = None,
                column_key: str | None = None) -> str:
    """The canonical UID for one source-local element.

    Every argument is an identity input. Anything not listed here -- array
    index, display id, extraction order, normalized/rewritten wording, page or
    document digests, character offsets -- is excluded by construction, which
    is the enforceable form of the schemas' prose.
    """
    if kind not in PREFIXES:
        raise UidInputError(f"unknown UID kind {kind!r}")
    if not source_pdf_sha256:
        raise UidInputError(
            f"{kind}: source_pdf_sha256 is required -- it is the input that "
            "establishes WHICH immutable document this element belongs to, and "
            "there is no fallback (hashing processed text instead would make "
            "every UID in the document move whenever any page was corrected)")
    if not isinstance(physical_page, int) or physical_page < 1:
        raise UidInputError(
            f"{kind}: a 1-based physical page number is required, got "
            f"{physical_page!r}")
    if ordinal < 1:
        raise UidInputError(f"{kind}: occurrence ordinal is 1-based")

    parts = [
        SCHEME,
        kind,
        source_pdf_sha256,
        str(physical_page),
        normalize_text(span_text),
        str(ordinal),
        parent_uid or "",
        normalize_text(column_key) if column_key is not None else "",
    ]
    # \x00 cannot appear in any part, so no combination of values can be
    # re-split differently -- "ab|c" and "a|bc" must not collide.
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{PREFIXES[kind]}-{digest[:_UID_HEX_LENGTH]}"


# --- forbidden inputs, stated executably -----------------------------------
# Named so a test can assert the list, and so a future change that starts
# feeding one of these has to delete an explicit entry rather than quietly
# widen a tuple.
FORBIDDEN_UID_INPUTS = (
    "array position / index within a contract array",
    "display identifiers (clause_id, table_id, row number)",
    "extraction order (the order an agent happened to process elements in)",
    "normalized or rewritten wording (only source bytes may identify)",
    "character offsets (start_char / end_char)",
    "whole-page or whole-document digests",
)


def verify_uid(submitted: str, kind: str, **identity) -> list[str]:
    """Recompute and compare. A submitted UID is evidence of a claim, never
    the claim's authority -- same posture as upstream_policy_snapshot in 11I.

    Returns error strings (empty = matches), the same contract every other
    validator here uses, so the DAO can print and refuse on a non-empty list.
    """
    try:
        expected = compute_uid(kind, **identity)
    except UidInputError as exc:
        return [f"{kind} UID cannot be verified: {exc}"]
    if submitted == expected:
        return []
    return [
        f"{kind} UID {submitted!r} is not the canonical identifier for its "
        f"source element (expected {expected!r}) -- a UID is derived from "
        "immutable source identity, never chosen; see policy_uid.py for the "
        "identity inputs"
    ]
