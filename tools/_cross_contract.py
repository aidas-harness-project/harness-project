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
from dataclasses import dataclass
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

# --- Polarity (Part 11B / P1-2) -------------------------------------------
# Policy polarity is the outcome of an operative predicate, not the presence of
# a negative-looking substring.  In particular, `제한 없이 지급합니다` negates
# the RESTRICTION and is affirmative, while `지급을 제한합니다` asserts the
# restriction and is negative.  The old `_NEGATION_MARKERS` implementation
# collapsed both to "negative", and also silently treated mixed and double-
# negative propositions as if they had one settled meaning.
#
# The analyzer below is deliberately deterministic and local.  Validators must
# be pure: no LLM, tokenizer service, subprocess, or extractor is invoked.

AFFIRMATIVE = "affirmative"
RESTRICTIVE_OR_NEGATIVE = "restrictive_or_negative"
MIXED = "mixed"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class PolarityMatch:
    """One operative predicate and the scope that determines its outcome."""

    target_predicate: str
    outcome: str
    negation_scope: str
    start_char: int
    end_char: int
    matched_text: str
    pattern: str
    reason: str


@dataclass(frozen=True)
class PolarityAnalysis:
    """Deterministic classification plus audit-ready predicate diagnostics."""

    classification: str
    matches: tuple[PolarityMatch, ...]
    reason: str


_AMBIGUOUS_POLARITY_PATTERNS = (
    (
        re.compile(
            r"(?P<predicate>지급|보상|보장|제한|면책|제외|배제)\s*"
            r"(?:하|되)?지\s*않"
            r"(?:"
            r"(?:으)?면\s*안\s*(?:됩|된|됩니다|된다)|"
            r"(?:을|ㄹ)\s*수\s*없|"
            r"아도\s*되는\s*것\s*(?:은|는)?\s*(?:아니|아닙|아닌)|"
            r"는\s*것\s*(?:은|는)?\s*(?:아니|아닙|아닌)"
            r")"
        ),
        "modal_or_repeated_negation",
        "a negated payout/restriction predicate is governed by another "
        "negative obligation, impossibility, or modal construction; counting "
        "negations cannot safely establish the policy outcome",
    ),
    (
        re.compile(
            r"(?P<predicate>지급|보상|보장)\s*(?:하|되)?지\s*않는\s*"
            r"(?:것|사항)\s*(?:은|는|이|가)?\s*(?:아니|아닙|아닌|없)"
        ),
        "double_or_complex_negation",
        "a payout/coverage negation is itself negated; deterministic rules "
        "cannot safely collapse the resulting scope",
    ),
    (
        re.compile(
            r"(?P<predicate>지급|보상|보장)\s*(?:하|되)?지\s*않는\s*"
            r"경우\s*(?:를|을)\s*(?:제한|제외|배제)"
        ),
        "nested_predicate_scope",
        "a negative payout phrase is embedded as the object of a restriction; "
        "it does not state one unambiguous payout outcome",
    ),
)

_NEGATIVE_PAYOUT_PATTERNS = (
    (
        re.compile(
            r"(?P<predicate>지급|보상|보장)\s*(?:하|되)?지\s*(?:않|아니)"
        ),
        "target_negated",
        "the payout/coverage predicate is directly negated",
    ),
    (
        re.compile(
            r"(?P<predicate>지급|보상|보장)\s*(?:할|될)\s*수\s*없"
        ),
        "target_impossible",
        "the payout/coverage predicate is stated to be impossible",
    ),
    (
        re.compile(r"(?P<predicate>책임)\s*(?:을)?\s*지지\s*(?:않|아니)"),
        "target_negated",
        "the insurer's responsibility predicate is directly negated",
    ),
    (
        re.compile(r"(?P<predicate>책임)(?:이|은|는)?\s*없"),
        "target_absent",
        "the insurer's responsibility is stated to be absent",
    ),
)

_NEGATED_RESTRICTION_PATTERNS = (
    (
        re.compile(r"(?P<predicate>제한)\s*없이"),
        "restriction_negated",
        "the restriction is negated by `없이`, so it does not negate payout",
    ),
    (
        re.compile(
            r"(?P<predicate>제한|면책|제외|배제)\s*(?:하)?지\s*(?:않|아니)"
        ),
        "restriction_negated",
        "a restriction/exclusion predicate is directly negated",
    ),
    (
        re.compile(r"(?P<predicate>제한|면책|제외|배제)(?:이|가)?\s*없"),
        "restriction_absent",
        "a restriction/exclusion is stated to be absent",
    ),
)

_RESTRICTIVE_PREDICATE_RE = re.compile(
    r"(?P<predicate>제외|배제|면책|부지급|제한|감액|삭감|차감|금지)"
    r"(?=(?:합니다|한다|됩니다|된다|하여|하고|함|이|을|를|$|[.!?。]))"
)

_AFFIRMATIVE_PAYOUT_RE = re.compile(
    r"(?P<predicate>지급|보상|보장)"
    r"(?=(?:합니다|한다|됩니다|된다|하여|하고|하되|함|"
    r"할\s*수\s*있|$|[.!?。]))"
)

_REDUCTION_THEN_PAYMENT_RE = re.compile(
    r"(?:감액|삭감|차감|제한)하여\s*$"
)


def _match_overlaps(match, occupied: list[tuple[int, int]]) -> bool:
    return any(match.start() < end and start < match.end()
               for start, end in occupied)


def _polarity_match(match, outcome: str, scope: str,
                    reason: str) -> PolarityMatch:
    return PolarityMatch(
        target_predicate=match.groupdict().get("predicate")
        or match.group(0),
        outcome=outcome,
        negation_scope=scope,
        start_char=match.start(),
        end_char=match.end(),
        matched_text=match.group(0),
        pattern=match.re.pattern,
        reason=reason,
    )


def _analyze_policy_polarity(
        text: str, assumed_outcome: str | None = None) -> PolarityAnalysis:
    """Classify Korean policy language by predicate and negation scope.

    `assumed_outcome` is used only for normalized condition objects whose
    schema bucket intentionally stores a trigger without repeating the outcome
    (e.g. an exclusion condition text of `고의로 자신을 해친 경우`).  Evidence
    quotes never receive this fallback: they must state the operative predicate
    themselves, so the bucket cannot manufacture meaning absent from source.
    """
    source = str(text or "")

    # Double negation and nested scope are not reduced by counting markers.
    # Return immediately: even if another token looks affirmative, the passage
    # still contains an unresolved proposition that must reach review.
    ambiguous_matches = []
    for pattern, scope, reason in _AMBIGUOUS_POLARITY_PATTERNS:
        ambiguous_matches.extend(
            _polarity_match(match, AMBIGUOUS, scope, reason)
            for match in pattern.finditer(source)
        )
    if ambiguous_matches:
        return PolarityAnalysis(
            AMBIGUOUS,
            tuple(sorted(ambiguous_matches,
                         key=lambda item: (item.start_char, item.end_char))),
            "the text contains double negation or nested predicate scope; "
            "automatic polarity would overstate what the sentence establishes",
        )

    matches: list[PolarityMatch] = []
    occupied: list[tuple[int, int]] = []

    for pattern, scope, reason in _NEGATIVE_PAYOUT_PATTERNS:
        for match in pattern.finditer(source):
            if _match_overlaps(match, occupied):
                continue
            matches.append(_polarity_match(
                match, RESTRICTIVE_OR_NEGATIVE, scope, reason))
            occupied.append((match.start(), match.end()))

    for pattern, scope, reason in _NEGATED_RESTRICTION_PATTERNS:
        for match in pattern.finditer(source):
            if _match_overlaps(match, occupied):
                continue
            # `제한이 없는 경우에도 ... 지급하지 않습니다` uses absence of
            # restriction as a subordinate condition.  It is not a second,
            # affirmative outcome competing with the directly-negated payout.
            if re.match(r"\s*(?:는|은)?\s*경우", source[match.end():]):
                continue
            matches.append(_polarity_match(
                match, AFFIRMATIVE, scope, reason))
            occupied.append((match.start(), match.end()))

    for match in _RESTRICTIVE_PREDICATE_RE.finditer(source):
        if _match_overlaps(match, occupied):
            continue
        matches.append(_polarity_match(
            match, RESTRICTIVE_OR_NEGATIVE, "restriction_asserted",
            "a restriction, exclusion, or reduction predicate is asserted"))
        occupied.append((match.start(), match.end()))

    for match in _AFFIRMATIVE_PAYOUT_RE.finditer(source):
        if _match_overlaps(match, occupied):
            continue
        # `50% 감액하여 지급합니다` is one restrictive proposition, not a
        # positive promise plus a separate reduction.  Conversely
        # `지급하되 ... 제외합니다` retains both matches and becomes mixed.
        if _REDUCTION_THEN_PAYMENT_RE.search(source[:match.start()]):
            continue
        matches.append(_polarity_match(
            match, AFFIRMATIVE, "target_asserted",
            "the payout/coverage predicate is affirmatively asserted"))
        occupied.append((match.start(), match.end()))

    matches.sort(key=lambda item: (item.start_char, item.end_char))
    outcomes = {match.outcome for match in matches}
    if AFFIRMATIVE in outcomes and RESTRICTIVE_OR_NEGATIVE in outcomes:
        return PolarityAnalysis(
            MIXED, tuple(matches),
            "the text asserts both affirmative and restrictive outcomes; "
            "they must be split or reviewed, not collapsed into one condition",
        )
    if RESTRICTIVE_OR_NEGATIVE in outcomes:
        return PolarityAnalysis(
            RESTRICTIVE_OR_NEGATIVE, tuple(matches),
            "the operative payout, responsibility, exclusion, or restriction "
            "predicate yields a restrictive outcome",
        )
    if AFFIRMATIVE in outcomes:
        return PolarityAnalysis(
            AFFIRMATIVE, tuple(matches),
            "the operative payout/coverage promise is asserted, or a "
            "restriction/exclusion is negated",
        )

    if assumed_outcome in (AFFIRMATIVE, RESTRICTIVE_OR_NEGATIVE):
        return PolarityAnalysis(
            assumed_outcome, (),
            "the normalized condition stores only the trigger; its schema "
            "bucket supplies the intended outcome while source evidence must "
            "still state that outcome explicitly",
        )
    return PolarityAnalysis(
        AMBIGUOUS, (),
        "no operative payout, coverage, responsibility, restriction, "
        "exclusion, or reduction predicate was found",
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


def _quote_polarity(quote: str) -> str:
    """Backward-compatible scalar view of the structured analysis."""
    return _analyze_policy_polarity(quote).classification


def _is_truncated_predicate(quote: str) -> bool:
    """True if the quote ends on an unresolved payout/coverage predicate --
    it names the operative verb but stops before saying paid vs not-paid.
    A quote that already resolves the payout predicate as negative is NOT
    truncated."""
    normalized = _normalize_ws(quote)
    analysis = _analyze_policy_polarity(normalized)
    if analysis.classification == RESTRICTIVE_OR_NEGATIVE and any(
            match.target_predicate in ("지급", "보상", "보장")
            for match in analysis.matches):
        return False
    return bool(_TRUNCATED_PREDICATE_RE.search(normalized))
_BUCKET_EVIDENCE_MARKERS = {
    "payout_conditions": ("지급", "보상", "보험금"),
    "exclusions": (
        "않", "아니", "제외", "배제", "면책", "부지급", "책임", "불가"),
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


# A line wrap that falls BETWEEN two Hangul syllables is a rendering artifact,
# not a word boundary: Korean does not hyphenate, so a PDF that breaks a line
# mid-word leaves a newline inside `우측 손목` where the document says `우측 손목`.
_HANGUL_WRAP_RE = re.compile("(?<=[가-힣])\n(?=[가-힣])")
# Separator between the two readings a normalized page carries. Contains
# characters `_normalize_ws` would have collapsed, so no real quote can span
# the join and match across both copies.
_WRAP_ALT_SEP = "\n<<WRAP_ALT>>\n"
# A running page-number footer (`- 3 -`) printed AFTER the body text, on its
# own line at the very end of a page. Stripped only when joining a page to its
# successor, so a sentence broken by the page break can be read continuously;
# never removed from a page examined on its own. Anchored to end-of-text so a
# figure like "- 3 -" inside a table row is untouched.
_PAGE_FOOTER_RE = re.compile(r"\n\s*-\s*\d+\s*-\s*$")


def _heal_wraps(text: str) -> str:
    """Remove line wraps that split a Korean word, leaving everything else.

    Deliberately narrow. Only a newline directly between two Hangul syllables
    is removed -- never a space, never a wrap next to punctuation, a digit, a
    Latin letter or a box-drawing character. That is what keeps this a
    rendering fix rather than a loosening of the fabrication check: an ASCII
    table row assembled across box columns still contains those characters and
    still fails, as does a quote whose spacing was altered.
    """
    return _HANGUL_WRAP_RE.sub("", text)


def quote_matches_page(quote: str, page_text: str) -> bool:
    """The citation gate: is `quote` verbatim on this page?

    Whitespace-normalized substring containment, applied after healing Korean
    mid-word line wraps on BOTH sides. The healing exists because the
    normalization above collapses whitespace runs rather than removing them,
    so a wrapped `관하/여` normalizes to `관하 여` and can never match the
    `관하여` a reader (or a model) correctly quotes.

    Measured 2026-08-16 on CASE_038: 311 of 367 processed pages (85%) contain
    at least one such wrap, 1,374 in total, and DOC_006's accident-circumstance
    sentence CANNOT be quoted without crossing one -- the driver's real
    pipeline run failed there twice, after spending its P4 correction, on a
    quote that is genuinely present in the source. Only 2 of those 367 pages
    contain box-drawing tables, so this closes the dominant artifact while
    leaving the rare one refused.

    A wrap is accepted under BOTH readings -- as nothing (`요골
과절` ->
    `요골과절`) and as a space (`요골 과절`) -- because which one a correct
    quote uses depends on whether the wrap fell inside a word or at a real
    word boundary, and the page text alone cannot say. Healing only one way
    REFUSED a citation the old rule accepted: a page reading `요골
과절`
    heals to `요골과절` while the quote legitimately writes `요골 과절`.
    That regression was caught by an existing test, which is why the plain
    comparison is tried first.

    This WIDENS what the gate accepts, so it is written as one shared function
    rather than repeated at each call site: a citation rule that differs
    between the DAO and a driver is a rule nobody can reason about.
    """
    if _normalize_ws(quote) in _normalize_ws(page_text):
        return True
    return _normalize_ws(_heal_wraps(quote)) in _normalize_ws(_heal_wraps(page_text))


def quote_in_normalized_page(quote: str, normalized_page: str) -> bool:
    """`quote_matches_page` for callers holding a PRE-normalized page.

    `normalized_page` must come from `normalize_page_for_quotes`, which keeps
    both readings of a wrap; the quote is compared under both for the same
    reason. Kept beside the pair so the three cannot drift apart.
    """
    return (_normalize_ws(quote) in normalized_page
            or _normalize_ws(_heal_wraps(quote)) in normalized_page)


def _spans_normalized_pair(quote: str, normalized_pages, page) -> bool:
    """Page-pair fallback for callers holding a normalized page map.

    The map's entries are already whitespace-normalized (and carry both wrap
    readings), so the raw text needed by `quote_spans_page_pair` is gone. The
    same two rules still apply: a quote lying wholly on N+1 is refused as a
    mis-numbered citation, and the pair is only consulted after N failed.
    """
    nxt = normalized_pages.get(page + 1) if hasattr(normalized_pages, "get") else None
    if nxt is None:
        return False
    if quote_in_normalized_page(quote, nxt):
        return False
    joined = _PAGE_FOOTER_RE.sub("", normalized_pages[page].rstrip()).rstrip() + " " + nxt
    return quote_in_normalized_page(quote, joined)


def quote_spans_page_pair(quote: str, page_text: str,
                          next_page_text: str | None) -> bool:
    """Last resort: does `quote` sit across the N/N+1 page boundary?

    Called ONLY when the quote already failed against page N alone (both
    readings). A document's sentence can run past a page break -- CASE_038's
    DOC_008 ends page 3 with `...보이므로, 피` and opens page 4 with
    `보험자는 ...`, so `피보험자는` (which inverts the legal subject if
    read as `보험자는`) exists in the document but on NO single page. 105 of
    CASE_038's 350 page boundaries continue Hangul text across the break.

    Deliberately not a sentence-completeness test. Whether page N "ends
    mid-sentence" is unreliable here: running headers and footers sit after
    the body text (`...보이므로, 피

- 3 -
`), and form/table pages do
    not end in punctuation at all. Trying only after a real failure needs no
    such judgment.

    The final clause is what keeps this from weakening the gate: a quote that
    fits entirely inside page N+1 is REFUSED, because that is a
    wrong-page-number citation, not a spanning one -- the live example being a
    CASE_038 run citing DOC_019's N1611 row on page 1 when it is on page 2.
    """
    if next_page_text is None:
        return False
    if quote_matches_page(quote, next_page_text):
        return False  # wholly on N+1: a mis-numbered citation, not a span
    # Join with exactly one newline and no surrounding blank lines, so a word
    # split by the page break (`...지무하지` / `않는다`) presents to
    # `_heal_wraps` as the single Hangul-newline-Hangul it heals. A blank line
    # between the halves would leave `피\\n\\n보험자는`, which is not that
    # pattern and would silently defeat the whole function.
    head = _PAGE_FOOTER_RE.sub("", page_text.rstrip()).rstrip()
    joined = head + "\n" + next_page_text.lstrip()
    return quote_matches_page(quote, joined)


def normalize_page_for_quotes(text: str) -> str:
    """The page side of `quote_matches_page`, for callers that pre-build a
    {page: normalized_text} map. Kept beside it so the two halves of one rule
    cannot drift: a caller that healed the quote but not the page would refuse
    exactly the citations this exists to accept.

    Carries BOTH readings of every wrap, joined, so a quote written either way
    is found. Concatenation is safe here because the test is containment, not
    equality, and the separator prevents a match spanning the two copies."""
    plain = _normalize_ws(text)
    healed = _normalize_ws(_heal_wraps(text))
    return plain if plain == healed else plain + _WRAP_ALT_SEP + healed


def normalize_quote_for_pages(quote: str) -> str:
    """The quote side of the same rule."""
    return _normalize_ws(_heal_wraps(quote))


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
                # BIDIRECTIONALLY (Part 11B / P1-2).  Classification is based on
                # the operative predicate and the scope of its negation -- never
                # on marker presence or lexical overlap.  A normalized condition
                # may store only its trigger, so its bucket supplies a fallback
                # outcome; evidence receives no such fallback and must state the
                # operative proposition in source.
                expected_condition_outcome = None
                if bucket in _POSITIVE_BUCKETS:
                    expected_condition_outcome = AFFIRMATIVE
                elif bucket in _NEGATIVE_BUCKETS or \
                        bucket == "reduction_conditions":
                    expected_condition_outcome = RESTRICTIVE_OR_NEGATIVE

                # The deterministic Korean analyzer checks the bucket against
                # the operative predicate. Only an EXPLICIT contradiction is an
                # error.
                #
                # An unsettled reading (MIXED/AMBIGUOUS) is deliberately NOT
                # blocked. It usually means the operative predicate sits
                # outside the condition -- "1. 계약자의 고의로 생긴 손해에 대한
                # 배상책임" is a bare noun phrase whose 보상/부보상 verb lives in
                # the article lead -- so the analyzer is reporting the limits of
                # what this fragment states, not a defect in the contract. It
                # was previously an error routed to human review, which bought
                # nothing: no downstream stage reads the bucket at all.
                # denial-response and claim-analysis match clauses on
                # document_id/clause_uid, source location, and the quoted text;
                # the buckets are an internal classification with no consumer,
                # so gating a whole clause on one unresolvable fragment cost
                # review time to protect a field nobody reads.
                if expected_condition_outcome is not None:
                    condition_analysis = _analyze_policy_polarity(
                        condition_text, expected_condition_outcome)

                    # The bucket is the contract's semantic assertion.  It is a
                    # third party to the comparison, not merely a fallback for a
                    # trigger-only condition.  A condition that explicitly says
                    # "do not pay" cannot live in payout_conditions even if its
                    # evidence repeats the same wrong outcome, and an affirmative
                    # "not exempt" condition cannot live in exclusions.  Never
                    # repair this by rewriting text or moving the bucket.
                    condition_polarity = condition_analysis.classification
                    if condition_polarity in (
                            AFFIRMATIVE, RESTRICTIVE_OR_NEGATIVE) and \
                            condition_polarity != expected_condition_outcome:
                        errors.append(
                            f"{loc}: bucket-condition polarity mismatch -- "
                            f"semantic bucket {bucket!r} requires "
                            f"{expected_condition_outcome}, but the condition's "
                            f"explicit operative predicate is "
                            f"{condition_polarity}; correct the extraction or "
                            "route to review, but do not rewrite the condition "
                            "or move it automatically")

                    # Evidence is checked the same way as the condition: an
                    # unsettled quote is not an error (a cited fragment often
                    # carries no operative verb of its own), but two quotes that
                    # explicitly contradict each other still are.
                    quote_analyses = [
                        _analyze_policy_polarity(quote) for quote in quotes]
                    unsettled_quotes = [
                        analysis for analysis in quote_analyses
                        if analysis.classification in (MIXED, AMBIGUOUS)
                    ]

                    settled_quote_polarities = {
                        analysis.classification for analysis in quote_analyses
                        if analysis.classification in (
                            AFFIRMATIVE, RESTRICTIVE_OR_NEGATIVE)
                    }
                    if settled_quote_polarities == {
                            AFFIRMATIVE, RESTRICTIVE_OR_NEGATIVE}:
                        errors.append(
                            f"{loc}: cited evidence mixes positive and negative "
                            "polarity passages -- this ambiguity must be routed "
                            "to review_required or split into separate "
                            "conditions, not merged because tokens overlap")

                    if not unsettled_quotes and len(
                            settled_quote_polarities) == 1 and \
                            condition_polarity in (
                                AFFIRMATIVE, RESTRICTIVE_OR_NEGATIVE):
                        evidence_polarity = next(iter(
                            settled_quote_polarities))
                        if condition_polarity != evidence_polarity:
                            if condition_polarity == AFFIRMATIVE:
                                errors.append(
                                    f"{loc}: normalized condition reads as a "
                                    "positive payout/coverage proposition but "
                                    "every cited quote is "
                                    "negative/exclusionary -- polarity "
                                    "contradiction; a '지급' condition may not "
                                    "be grounded solely in a '지급하지 않는다' "
                                    "quote")
                            else:
                                errors.append(
                                    f"{loc}: normalized condition asserts a "
                                    "negative/restrictive outcome but every "
                                    "cited quote is positive/affirmative -- "
                                    "polarity contradiction; the evidence does "
                                    "not support the restriction")

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


def check_normalized_policy_clause(
        data: dict, filename: str, redacted_text: str | None) -> list[str]:
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
    normalized_pages = {n: normalize_page_for_quotes(t) for n, t in pages.items()}

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
            if not quote_in_normalized_page(quote, normalized_pages[page])                     and not _spans_normalized_pair(quote, normalized_pages, page):
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
    normalized_pages = {number: normalize_page_for_quotes(text)
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
            if not quote_in_normalized_page(quote, normalized_pages[page]):
                errors.append(
                    f"{loc}: quote not found on page {page} of processed source")
                continue
            normalized_quote = _normalize_ws(quote)
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

"""
sibling file, and it cannot compare two entries of the same array to each
other. Three real classes of corruption live in exactly that blind spot, all
of them reproduced against the committed CASE_903/CASE_021 outputs before
this module existed:

1. ORPHAN CROSS-REFERENCES. denial_validation_result.json refers to denial
   reasons by id. Putting `reason_id: "DR_999"` there -- an id no
   denial_reason_result.json ever defined -- validated cleanly, as did
   dropping validations for real reasons entirely, or duplicating one. So a
   Phase 2 output could silently validate nothing, or claim to have validated
   something that does not exist, and every schema check still said PASS.

2. DUPLICATE IDS. Two entries both calling themselves `DR_1` validated. Ids
   are how every downstream stage and the whole evaluation contract address a
   reason; two rows sharing one address means whichever is read last wins,
   silently.

3. SIBLING-COMPARING FIELD RULES. `candidate_codes` is the Top-1/Top-3
   evaluation input. Its first entry must be the assigned `taxonomy_code`,
   entries must be distinct, and confidence must not increase down a "ranked"
   list. Each is a comparison between array members, which `items` cannot
   express.

Scope note: this checks INTERNAL CONSISTENCY -- that ids resolve, are unique,
and that ranked lists are actually ranked. It deliberately does not re-judge
classification quality; whether R04 was the right code is a matter for
evaluation, not a write-time gate.

Failures are returned as strings, same contract as validate_instance(), and
dao.write-contract refuses the write on any -- the fail/don't-persist rule
already used for schema errors.
"""
import hashlib
import json
from pathlib import Path

# Files that carry ids other contracts point at, and the checks each gets.
DENIAL_REASONS = "denial_reason_result.json"
DENIAL_VALIDATION = "denial_validation_result.json"

# Stages whose output is derived from denial_reason_result.json. If that
# contract is rewritten, whatever these produced describes a reason/match set
# that no longer exists -- see upstream_hash() and stale_downstream().
DERIVED_FROM_DENIAL_REASONS = {
    DENIAL_VALIDATION: "denial_validation",
    "screening_report.json": "screening_report",
}

UPSTREAM_HASH_FIELD = "source_denial_contract_hash"


def upstream_hash(data: dict) -> str:
    """A content hash of denial_reason_result.json's MEANING, not its bytes.

    Only the parts downstream contracts actually resolve against are hashed:
    each reason's id, decision_type, taxonomy_code, and its owned
    policy_match_ids. Hashing the whole file would invalidate every downstream
    contract when an unrelated field changed -- a reworded
    insurer_claim_summary, a confidence nudge, a new warning -- and an
    invalidation that fires on noise gets ignored, which is worse than none.

    Sorted and separator-pinned so the digest depends on content rather than
    key order or whitespace.
    """
    material = [
        {
            "reason_id": r.get("reason_id"),
            "decision_type": r.get("decision_type"),
            "taxonomy_code": r.get("taxonomy_code"),
            "policy_match_ids": sorted(
                m.get("policy_match_id") for m in (r.get("policy_matches") or [])
                if m.get("policy_match_id") is not None),
        }
        for r in (data.get("denial_reasons") or [])
    ]
    material.sort(key=lambda item: item["reason_id"] or "")
    # Acceptances are part of the meaning too: adding, dropping, or repricing
    # one changes whether the outcome was total or split, which a screening
    # report states outright. Appended as a separate keyed element rather than
    # mixed into the reason list, and omitted entirely when there are none, so
    # every contract written before accepted_coverages existed keeps its exact
    # digest -- a compatibility break here would mark real, unchanged
    # downstream work stale for a field it never had.
    accepted = [
        {
            "accepted_coverage_id": a.get("accepted_coverage_id"),
            "coverage_name": a.get("coverage_name"),
            "payment_status": a.get("payment_status"),
            "accepted_amount": a.get("accepted_amount"),
            "policy_match_ids": sorted(
                m.get("policy_match_id")
                for m in (a.get("policy_matches") or [])
                if m.get("policy_match_id") is not None),
        }
        for a in (data.get("accepted_coverages") or [])
    ]
    if accepted:
        accepted.sort(key=lambda item: item["accepted_coverage_id"] or "")
        material = {"denial_reasons": material, "accepted_coverages": accepted}
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stale_downstream(case_dir: Path, new_hash: str) -> list[tuple[str, str, str | None]]:
    """Downstream contracts on disk whose recorded upstream hash no longer
    matches. Returns (filename, stage_name, recorded_hash) per stale file.

    A contract with no recorded hash is treated as stale rather than fine: it
    was written before this check existed, so nothing establishes which
    reason set it was derived from -- assuming it is current is exactly the
    assumption that goes wrong.
    """
    stale = []
    for filename, stage in DERIVED_FROM_DENIAL_REASONS.items():
        existing = _load(case_dir, filename)
        if existing is None:
            continue
        recorded = existing.get(UPSTREAM_HASH_FIELD)
        if recorded != new_hash:
            stale.append((filename, stage, recorded))
    return stale


def _load(case_dir: Path, filename: str):
    """Returns parsed JSON, or None when the file isn't there yet.

    A missing sibling is not an error: stages run in order, and Phase 2
    contracts legitimately do not exist while Phase 1 is still running. Only
    a PRESENT sibling that disagrees is a failure.
    """
    path = case_dir / filename
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{filename} is not valid JSON: {exc}") from exc


def _duplicates(ids):
    seen, dupes = set(), []
    for i in ids:
        if i in seen and i not in dupes:
            dupes.append(i)
        seen.add(i)
    return dupes


def check_denial_reason_result(data: dict) -> list[str]:
    """Internal invariants of denial_reason_result.json itself."""
    errors = []
    reasons = data.get("denial_reasons") or []

    reason_ids = [r.get("reason_id") for r in reasons]
    for dupe in _duplicates(reason_ids):
        errors.append(f"denial_reasons: duplicate reason_id {dupe!r} -- ids must be unique, "
                      "downstream contracts address reasons by id")

    # id-less matches are legacy (pre-policy_match_id, e.g. CASE_021) and have
    # no address to collide on -- reporting several of them as "duplicate None"
    # would flag a legacy shape as corruption. Same carve-out as the
    # validation path below; the schema requires the id on new writes.
    # Acceptances carry policy_matches too, and their ids share one namespace
    # with the reasons' -- a downstream contract resolves a policy_match_id
    # without knowing which side of the outcome it came from, so a collision
    # across the two lists is exactly as ambiguous as one within either.
    accepted = data.get("accepted_coverages") or []
    match_ids = [m.get("policy_match_id")
                 for owner in (*reasons, *accepted)
                 for m in (owner.get("policy_matches") or [])
                 if m.get("policy_match_id") is not None]
    for dupe in _duplicates(match_ids):
        errors.append(f"policy_matches: duplicate policy_match_id {dupe!r} -- ids must be unique "
                      "across the whole contract, not just within one reason")

    for dupe in _duplicates([a.get("accepted_coverage_id") for a in accepted]):
        errors.append(
            f"accepted_coverages: duplicate accepted_coverage_id {dupe!r} -- "
            "ids must be unique")

    # An acceptance and a denial naming the same coverage is a contradiction in
    # the same breath: the response cannot both pay and refuse one coverage.
    # Far likelier is that the agent filed one decision twice, which would show
    # a reader a split outcome that never happened.
    denied_coverages = {
        (r.get("decided_coverage") or "").strip()
        for r in reasons if (r.get("decided_coverage") or "").strip()}
    for entry in accepted:
        name = (entry.get("coverage_name") or "").strip()
        if name and name in denied_coverages:
            errors.append(
                f"accepted_coverages: {name!r} is also the decided_coverage of "
                "a denial/reduction reason -- one coverage cannot be both "
                "accepted and denied; split it into distinct coverages or "
                "record it as a reduction with the amount actually paid")

    for reason in reasons:
        rid = reason.get("reason_id", "?")
        code = reason.get("taxonomy_code")
        candidates = reason.get("candidate_codes") or []

        if candidates:
            top = candidates[0].get("taxonomy_code")
            if top != code:
                errors.append(f"{rid}: candidate_codes[0] is {top!r} but taxonomy_code is {code!r} "
                              "-- the assigned code must be the top-ranked candidate")

            codes = [c.get("taxonomy_code") for c in candidates]
            for dupe in _duplicates(codes):
                errors.append(f"{rid}: candidate_codes lists {dupe!r} more than once")

            confidences = [c.get("confidence") for c in candidates]
            if any(a is not None and b is not None and b > a
                   for a, b in zip(confidences, confidences[1:])):
                errors.append(f"{rid}: candidate_codes confidences {confidences} are not "
                              "non-increasing -- a ranked list must actually be ranked")

        label = reason.get("taxonomy_label")
        if label is not None and code is not None:
            expected = _codebook_label(code)
            # startswith, not equality: a label may carry a case-specific
            # parenthetical after the canonical text (CASE_024 records
            # "기왕증 / 기존 질환 기여도 (골다공증 기여도 반영)"), which is a
            # useful annotation rather than a competing label. What this
            # rejects is a label for a DIFFERENT code, or invented wording --
            # the failure mode where a contract quietly renames an R-code.
            if expected is not None and not label.startswith(expected):
                errors.append(f"{rid}: taxonomy_label {label!r} does not match the codebook label "
                              f"for {code} ({expected!r}) -- a label may append case-specific "
                              "detail but must not restate the code's meaning differently")

    return errors


def _check_upstream_hash(data: dict, reasons_doc: dict, filename: str) -> list[str]:
    """A downstream contract must declare which denial_reason_result it was
    built from, and that must be the one on disk right now.

    This runs inside the DOWNSTREAM file's lock (see dao.cmd_write_contract),
    and reads upstream at that moment -- so the hash compared is the same one
    in effect when the write lands. There is no window between checking and
    writing for upstream to change underneath. Lock order is single-file:
    downstream is locked, upstream is only read, so no two callers can hold
    locks in opposing order.
    """
    current = upstream_hash(reasons_doc)
    recorded = data.get(UPSTREAM_HASH_FIELD)
    if recorded is None:
        return [f"{filename}: {UPSTREAM_HASH_FIELD} is missing -- a derived contract must record "
                f"which {DENIAL_REASONS} it was built from (expected {current!r}), otherwise "
                "nothing can tell later whether it went stale"]
    if recorded != current:
        return [f"{filename}: {UPSTREAM_HASH_FIELD} is {recorded!r} but {DENIAL_REASONS} now "
                f"hashes to {current!r} -- this was derived from a reason/match set that has "
                "since changed. Re-run the stage against the current contract; do not write "
                "a validation of reasons that no longer exist."]
    return []


def _normalized_policy_filename(document_id: str) -> str:
    return f"normalized_policy_clause_{document_id}.json"


def _policy_match_source_errors(label, doc_id, match, redacted_text_for):
    """Verify a policy match against the processed source text directly.

    The non-normalized half of `check_policy_matches` step 2. A match on a
    `text_only_no_normalization` document has no clause contract to resolve
    against, so its `policy_clause_evidence_references` ARE the link: the page
    must exist in the processed text and the quote must appear verbatim on it.

    Fails closed in both directions that matter. No processed text at all means
    the document was never processed, so nothing can be verified and the match
    is refused -- the same outcome as before this path existed, for the case
    that genuinely warranted it. No clause evidence references at all is also
    refused: without them there is neither a clause contract nor a location,
    which is precisely the unverifiable link the fail-safe rule forbids.
    """
    errors = []
    if redacted_text_for is None:
        return [
            f"{label}: {_normalized_policy_filename(doc_id)} does not exist and "
            "no processed-text reader was supplied, so this match cannot be "
            "verified against anything"]
    refs = [ref for ref in match.get("policy_clause_evidence_references") or []
            if ref.get("document_id") == doc_id]
    if not refs:
        return [
            f"{label}: {doc_id} has no normalized clause contract, so the match "
            "must be grounded by policy_clause_evidence_references on that "
            "document -- none are present, leaving the link unverifiable"]
    redacted_text = redacted_text_for(doc_id)
    if redacted_text is None:
        return [
            f"{label}: no processed/redacted text found for {doc_id} -- the "
            "policy document this match cites has not been processed, so no "
            "source location can be verified"]
    try:
        pages = split_pages(redacted_text)
    except SourceUnavailable as exc:
        return [f"{label}: {exc}"]
    normalized_pages = {n: normalize_page_for_quotes(t) for n, t in pages.items()}
    for ref in refs:
        errors.extend(
            f"{label}: {error}" for error in
            _location_errors(doc_id, ref.get("page"), ref.get("quote"),
                             normalized_pages,
                             "policy_clause_evidence_references entry"))
    return errors


def _location_errors(doc_id, page, quote, normalized_pages, what):
    """Does this page+quote actually name a place in the processed text?

    The one verification both source-addressed forms rest on -- a
    `policy_match`'s clause evidence and a `matched_clause_ref`/`clause_ref` in
    source form. Shared so the two can never drift into checking the same claim
    to different standards.
    """
    if page is None or not quote or not quote.strip():
        return [f"{what} is missing page or quote -- a reference to a "
                "non-normalized document is grounded by its location, so both "
                "are required"]
    if page not in normalized_pages:
        return [f"page {page} does not exist in the processed text for "
                f"{doc_id} (pages present: {sorted(normalized_pages)})"]
    if not quote_in_normalized_page(quote, normalized_pages[page]):
        # Only after page N fails on its own; refuses a quote lying wholly on
        # N+1, so a mis-numbered citation is still an error.
        if not _spans_normalized_pair(quote, normalized_pages, page):
            return [f"quote not found on page {page} of {doc_id}'s processed text "
                    "-- the cited quote does not appear verbatim on the page it "
                    f"claims (quote={quote[:60]!r}...)"]
    return []


def source_addressed_ref_errors(ref, redacted_text_for) -> list[str]:
    """Verify a source-addressed clause reference against the processed text.

    `matched_clause_ref` (coverage_result) and `clause_ref`
    (requirement_matching_result) accept two forms: a canonical UID into a
    normalized clause contract, or -- for a `text_only_no_normalization`
    document -- a `{document_id, page, quote}` location. The UID form is
    verified by recomputing UIDs from source; this is the source form's
    equivalent, and it is deliberately the same check
    `_policy_match_source_errors` applies to a denial policy match.

    Fails closed: no reader and no processed text both mean nothing can be
    verified, which for a reference whose page+quote IS its address is the
    unresolvable state the fail-safe rule refuses.
    """
    doc_id = (ref or {}).get("document_id")
    if redacted_text_for is None:
        return ["no processed-text reader was supplied, so this "
                "source-addressed reference cannot be verified against "
                "anything"]
    redacted_text = redacted_text_for(doc_id)
    if redacted_text is None:
        return [f"no processed/redacted text found for {doc_id} -- the policy "
                "document this reference cites has not been processed, so no "
                "source location can be verified"]
    try:
        pages = split_pages(redacted_text)
    except SourceUnavailable as exc:
        return [str(exc)]
    normalized_pages = {n: normalize_page_for_quotes(t) for n, t in pages.items()}
    return _location_errors(doc_id, ref.get("page"), ref.get("quote"),
                            normalized_pages, "the reference")


def check_policy_matches(data: dict, case_dir: Path,
                         redacted_text_for=None) -> list[str]:
    """Every policy match must resolve to a real clause in a real policy file,
    at the location it claims.

    Before this, a match only had to be well-shaped: `document_id` and
    `clause_id` were free strings nobody resolved, and
    `policy_clause_evidence_references` could point at a different document
    than the match itself. So a match could name DOC_001 while citing DOC_999,
    or cite a 제99조 that exists in no policy document, and still validate --
    presenting an insurer's denial as anchored to a policy clause that was
    never checked to exist.

    The project's stated preference is fail-safe: a missing link is safer than
    a wrong one. So an unresolvable match is an error, not a warning, and this
    applies to `agent_inferred` matches exactly as to `insurer_cited` ones --
    an inferred link is the one most in need of checking, not least.

    A match with no `policy_match_id` is skipped (see the legacy note in
    check_denial_validation_result) -- CASE_021 predates the field.
    """
    errors = []
    # Acceptances carry policy_matches on the same terms as reasons do. An
    # acceptance grounded in a clause that does not exist is no better than a
    # denial grounded that way, so both go through the identical check rather
    # than the accepted side being trusted for being good news.
    owners = [(r.get("reason_id", "?"), r)
              for r in data.get("denial_reasons") or []]
    owners += [(a.get("accepted_coverage_id", "?"), a)
               for a in data.get("accepted_coverages") or []]
    for rid, reason in owners:
        for match in reason.get("policy_matches") or []:
            mid = match.get("policy_match_id")
            if mid is None:
                continue
            doc_id = match.get("document_id")
            clause_id = match.get("clause_id")
            label = f"{rid}/{mid}"

            # 1. Clause evidence must cite the document the match names.
            for ref in match.get("policy_clause_evidence_references") or []:
                ref_doc = ref.get("document_id")
                if ref_doc != doc_id:
                    errors.append(
                        f"{label}: policy_clause_evidence_references cites document_id "
                        f"{ref_doc!r} but the match is on {doc_id!r} -- a clause citation must "
                        "come from the policy document the match claims")

            if doc_id is None or clause_id is None:
                continue

            # 2. Where the link gets verified depends on whether this document
            #    was normalized. Normalization is opt-IN
            #    (`text_only_no_normalization`), so most policy documents have
            #    no clause contract, and requiring one here would have made
            #    citing an ordinary processed policy document impossible.
            #
            #    The fail-safe principle in this function's docstring is
            #    unchanged: an unresolvable match is still an error. What
            #    changes is that "resolvable" has two forms. A normalized
            #    document is verified against its clause contract (steps 3-4
            #    below). A non-normalized one is verified against the PROCESSED
            #    TEXT -- the cited page must exist and the cited quote must
            #    appear verbatim on it. That is the same check step 4 performs,
            #    against the same bytes the normalized clause's own locations
            #    were themselves verified against; it is not a weaker standard,
            #    it just skips the intermediary.
            policy_doc = _load(case_dir, _normalized_policy_filename(doc_id))
            if policy_doc is None:
                errors.extend(_policy_match_source_errors(
                    label, doc_id, match, redacted_text_for))
                continue

            # 3. The clause must exist in it.
            clauses = policy_doc.get("clauses") or []
            clause = next((c for c in clauses if c.get("clause_id") == clause_id), None)
            if clause is None:
                errors.append(
                    f"{label}: clause_id {clause_id!r} does not exist in "
                    f"{_normalized_policy_filename(doc_id)} "
                    f"(known: {[c.get('clause_id') for c in clauses]})")
                continue

            # 4. The cited location must be one the normalized clause records.
            #    Compared as (page, quote) pairs: a page alone would accept any
            #    text on the right page, and a quote alone would accept the
            #    right words attributed to the wrong page.
            clause_locations = {
                (r.get("page"), (r.get("quote") or "").strip())
                for r in _clause_evidence(clause)
            }
            for ref in match.get("policy_clause_evidence_references") or []:
                if ref.get("document_id") != doc_id:
                    continue  # already reported in step 1
                here = (ref.get("page"), (ref.get("quote") or "").strip())
                if here not in clause_locations:
                    errors.append(
                        f"{label}: policy clause evidence (page {here[0]}, quote "
                        f"{here[1][:40]!r}...) does not match any evidence reference recorded "
                        f"for clause {clause_id!r} in {_normalized_policy_filename(doc_id)}")

    return errors


def _clause_evidence(clause: dict) -> list[dict]:
    """Every evidence reference a normalized clause carries -- its own, plus
    those on its payout_conditions / exclusions / reduction_conditions, since
    a match may legitimately cite the specific condition it turns on rather
    than the clause header."""
    refs = list(clause.get("evidence_references") or [])
    for key in ("payout_conditions", "exclusions", "reduction_conditions"):
        for item in clause.get(key) or []:
            if isinstance(item, dict):
                refs.extend(item.get("evidence_references") or [])
    return refs


_CODEBOOK_CACHE: dict | None = None


def _codebook_label(code: str):
    """The canonical label for an R-code, read from the common schema's
    x-codebook metadata -- the one machine-readable source, so a typo in a
    contract cannot quietly invent a new label for an existing code."""
    global _CODEBOOK_CACHE
    if _CODEBOOK_CACHE is None:
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "common_component_output.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        node = schema.get("$defs", {}).get("taxonomy_code", {})
        book = node.get("x-codebook") or {}
        labels = {}
        if isinstance(book, dict):
            for code_key, meta in book.items():
                if isinstance(meta, dict) and "label_ko" in meta:
                    labels[code_key] = meta["label_ko"]
        _CODEBOOK_CACHE = labels
    return _CODEBOOK_CACHE.get(code)


def check_denial_validation_result(data: dict, case_dir: Path) -> list[str]:
    """denial_validation_result.json against the reasons it claims to validate.

    This is the check that motivated the module: a validation naming DR_999,
    or omitting a real reason, or listing one twice, all validated cleanly
    before.
    """
    errors = []
    reasons_doc = _load(case_dir, DENIAL_REASONS)
    if reasons_doc is None:
        return [f"{DENIAL_VALIDATION} cannot be written before {DENIAL_REASONS} exists -- "
                "it validates that contract's reasons and has nothing to resolve ids against"]

    errors.extend(_check_upstream_hash(data, reasons_doc, DENIAL_VALIDATION))

    reasons = reasons_doc.get("denial_reasons") or []
    known_reason_ids = [r.get("reason_id") for r in reasons]

    # Matches are OWNED by a reason, so the map is per-reason, not one global
    # set. Checking membership in a flat set only asks "does this id exist
    # somewhere", which lets DR_1's PM_1 be verified under DR_2 -- the
    # verification would read as done while the match it actually belongs to
    # was never checked under its own reason.
    #
    # Only id-bearing matches participate. Pre-policy_match_id contracts
    # (CASE_021 was written before the field existed) have nothing to address,
    # so demanding a verification for them would report a legacy shape as
    # corruption. The schema is what requires the id on new writes; this layer
    # resolves ids that exist rather than retro-failing ones that never did.
    matches_by_reason = {
        r.get("reason_id"): [m.get("policy_match_id") for m in (r.get("policy_matches") or [])
                             if m.get("policy_match_id") is not None]
        for r in reasons
    }

    # An accepted coverage owns policy_matches on the same terms a reason does,
    # and check_policy_matches (same module) already verifies both sides
    # identically. This layer did not: owner_of_match was built from
    # denial_reasons alone, so an acceptance-owned match could not be recorded
    # under a reason (reason_id is ^DR_[0-9]+$, the write was rejected) and
    # raised nothing at all when omitted -- the contract reported every match
    # verified while one had never been checked. That is the failure mode the
    # completeness message below names, allowed by the check that prints it.
    # upstream_hash() folds accepted_coverages in, so the two layers of this
    # same file disagreed about whether acceptances are part of the contract.
    accepted = reasons_doc.get("accepted_coverages") or []
    matches_by_acceptance = {
        a.get("accepted_coverage_id"): [m.get("policy_match_id")
                                        for m in (a.get("policy_matches") or [])
                                        if m.get("policy_match_id") is not None]
        for a in accepted
    }
    owner_of_match = {mid: oid
                      for mids_by_owner in (matches_by_reason, matches_by_acceptance)
                      for oid, mids in mids_by_owner.items() for mid in mids}

    # `match_source` is duplicated into the validation contract (schema v0.3)
    # so a consumer reading only that file can tell what `verified` means:
    # the clause exists where claimed, NOT that the insurer cited it. A copied
    # field is a field that can drift, and a validation claiming
    # `insurer_cited` for a match the insurer never made would invert exactly
    # the distinction the duplication exists to preserve -- so the copy is
    # checked against its source rather than trusted.
    source_of_match = {}
    for owner_matches in (
            [m for r in reasons for m in (r.get("policy_matches") or [])],
            [m for a in accepted for m in (a.get("policy_matches") or [])]):
        for m in owner_matches:
            if m.get("policy_match_id") is not None:
                source_of_match[m["policy_match_id"]] = m.get("match_source")

    validations = data.get("validations") or []
    seen_reason_ids = [v.get("reason_id") for v in validations]

    for orphan in [i for i in seen_reason_ids if i not in known_reason_ids]:
        errors.append(f"validations: reason_id {orphan!r} does not exist in {DENIAL_REASONS} "
                      f"(known: {known_reason_ids})")
    for dupe in _duplicates(seen_reason_ids):
        errors.append(f"validations: reason_id {dupe!r} appears more than once -- "
                      "exactly one validation per denial reason")
    for missing in [i for i in known_reason_ids if i not in seen_reason_ids]:
        errors.append(f"validations: denial reason {missing!r} has no validation -- "
                      "every reason must be validated, silently skipping one hides an unrebutted denial")

    seen_match_ids = []
    for validation in validations:
        rid = validation.get("reason_id")
        owned = matches_by_reason.get(rid, [])
        verified_here = [pmv.get("policy_match_id")
                         for pmv in (validation.get("policy_match_validations") or [])
                         if pmv.get("policy_match_id") is not None]
        seen_match_ids.extend(verified_here)

        for mid in verified_here:
            if mid in owned:
                continue
            owner = owner_of_match.get(mid)
            if owner is None:
                errors.append(f"{rid}: policy_match_id {mid!r} does not exist in {DENIAL_REASONS} "
                              f"(all known: {sorted(owner_of_match)})")
            else:
                errors.append(f"{rid}: policy_match_id {mid!r} belongs to {owner!r}, not {rid!r} -- "
                              "a match may only be verified under the reason that owns it")

        for dupe in _duplicates(verified_here):
            errors.append(f"{rid}: policy_match_id {dupe!r} verified more than once "
                          "within this validation")

        # Only demand completeness for a reason that actually has a validation
        # here; a missing validation is already reported above, and repeating
        # it once per owned match would bury that finding in noise.
        if rid in matches_by_reason:
            for missing in [m for m in owned if m not in verified_here]:
                errors.append(f"{rid}: policy match {missing!r} has no verification -- "
                              "an unverified match must not be presentable as checked")

    # Acceptance side: same ownership and completeness rules, own array.
    acceptance_entries = data.get("acceptance_match_validations") or []
    seen_acceptance_ids = [a.get("accepted_coverage_id") for a in acceptance_entries]

    for orphan in [i for i in seen_acceptance_ids if i not in matches_by_acceptance]:
        errors.append(f"acceptance_match_validations: accepted_coverage_id {orphan!r} does not exist "
                      f"in {DENIAL_REASONS} (known: {sorted(matches_by_acceptance)})")
    for dupe in _duplicates(seen_acceptance_ids):
        errors.append(f"acceptance_match_validations: accepted_coverage_id {dupe!r} appears more than "
                      "once -- exactly one entry per accepted coverage")

    for entry in acceptance_entries:
        aid = entry.get("accepted_coverage_id")
        owned = matches_by_acceptance.get(aid, [])
        verified_here = [pmv.get("policy_match_id")
                         for pmv in (entry.get("policy_match_validations") or [])
                         if pmv.get("policy_match_id") is not None]
        seen_match_ids.extend(verified_here)

        for mid in verified_here:
            if mid in owned:
                continue
            owner = owner_of_match.get(mid)
            if owner is None:
                errors.append(f"{aid}: policy_match_id {mid!r} does not exist in {DENIAL_REASONS} "
                              f"(all known: {sorted(owner_of_match)})")
            else:
                errors.append(f"{aid}: policy_match_id {mid!r} belongs to {owner!r}, not {aid!r} -- "
                              "a match may only be verified under the owner that holds it")

        for dupe in _duplicates(verified_here):
            errors.append(f"{aid}: policy_match_id {dupe!r} verified more than once within this entry")

        if aid in matches_by_acceptance:
            for missing in [m for m in owned if m not in verified_here]:
                errors.append(f"{aid}: policy match {missing!r} has no verification -- "
                              "an unverified match must not be presentable as checked")

    # An acceptance owning matches but having no entry at all is the exact
    # silent-omission case: nothing above fires, because every loop is driven
    # by what the validation contract chose to include.
    for aid, owned in matches_by_acceptance.items():
        if owned and aid not in seen_acceptance_ids:
            errors.append(f"accepted coverage {aid!r} owns policy matches {sorted(owned)} but has no "
                          "acceptance_match_validations entry -- an acceptance's policy basis must be "
                          "verified on the same terms as a denial's, and omitting it would let the "
                          "contract report full verification while this match was never checked")

    # The duplicated match_source must still agree with its source. Driven off
    # every validation in the contract, denial- and acceptance-side alike,
    # since both carry the field through the same $ref.
    for owner_entry in [*validations, *acceptance_entries]:
        oid = owner_entry.get("reason_id") or owner_entry.get("accepted_coverage_id")
        for pmv in (owner_entry.get("policy_match_validations") or []):
            mid = pmv.get("policy_match_id")
            if mid is None or mid not in source_of_match:
                continue  # orphan/unknown id already reported above
            upstream = source_of_match[mid]
            claimed = pmv.get("match_source")
            if upstream is not None and claimed != upstream:
                errors.append(
                    f"{oid}: policy_match_id {mid!r} records match_source "
                    f"{claimed!r} but {DENIAL_REASONS} says {upstream!r} -- "
                    "this field says whether the INSURER cited the clause or "
                    "an agent proposed it, so a mismatch either credits the "
                    "insurer with an argument it never made or discards one it "
                    "did. Copy it from the upstream contract; do not restate it")

    # Cross-validation duplicates (the same match verified under two different
    # reasons) are caught here; the per-validation loop above only sees one.
    for dupe in _duplicates(seen_match_ids):
        errors.append(f"policy_match_validations: policy_match_id {dupe!r} verified more than once "
                      "across validations")

    return errors


def check_screening_report(data: dict, case_dir: Path) -> list[str]:
    """screening_report's reason_ids must resolve AND land in the right section.

    The denial/reduction split is the whole point of the section pair: a
    denial says the insurer paid nothing on that ground, a reduction says it
    paid less. Existence-only checking let a reduction be summarized under
    denial (and vice versa), which inverts what the insurer actually decided
    while every id still resolved -- the screening report is the triage
    document a human reads first, so a reason filed under the wrong heading
    misdirects the entire review.
    """
    reasons_doc = _load(case_dir, DENIAL_REASONS)
    if reasons_doc is None:
        return []

    reasons = reasons_doc.get("denial_reasons") or []
    known = [r.get("reason_id") for r in reasons]
    decision_of = {r.get("reason_id"): r.get("decision_type") for r in reasons}

    errors = _check_upstream_hash(data, reasons_doc, "screening_report.json")
    for ref in _collect_reason_ids(data):
        if ref not in known:
            errors.append(f"screening_report references reason_id {ref!r}, which does not exist "
                          f"in {DENIAL_REASONS} (known: {known})")

    position = data.get("insurer_position") or {}
    sections = {
        "denial": (position.get("denial") or {}).get("reason_ids") or [],
        "reduction": (position.get("reduction") or {}).get("reason_ids") or [],
    }

    for section, ids in sections.items():
        for rid in ids:
            actual = decision_of.get(rid)
            if actual is None:
                continue  # unresolvable id already reported above
            if actual != section:
                errors.append(
                    f"insurer_position.{section}.reason_ids lists {rid!r}, whose decision_type is "
                    f"{actual!r} -- a {actual} must not be summarized as a {section}")
        for dupe in _duplicates(ids):
            errors.append(f"insurer_position.{section}.reason_ids lists {dupe!r} more than once")

    both = set(sections["denial"]) & set(sections["reduction"])
    for rid in sorted(both):
        errors.append(f"insurer_position: reason_id {rid!r} appears under BOTH denial and "
                      "reduction -- a reason has exactly one decision_type")

    # The acceptance side, on the same terms. A report that summarizes a
    # contract carrying accepted_coverages but stays silent about them shows a
    # structural reader has_denial:true and nothing else, i.e. a total denial --
    # which is the misreading the upstream field exists to prevent.
    accepted = reasons_doc.get("accepted_coverages") or []
    known_accepted = [a.get("accepted_coverage_id") for a in accepted]
    listed = ((position.get("acceptance") or {}).get("accepted_coverage_ids")
              or [])
    for aid in listed:
        if aid not in known_accepted:
            errors.append(
                f"insurer_position.acceptance lists {aid!r}, which does not "
                f"exist in {DENIAL_REASONS} (known: {known_accepted})")
    for dupe in _duplicates(listed):
        errors.append(
            f"insurer_position.acceptance lists {dupe!r} more than once")
    if accepted and "has_acceptance" in position:
        # Only checked when the report opts into the field at all, so a report
        # written before it existed is not retroactively wrong.
        missing = [a for a in known_accepted if a not in listed]
        if missing:
            errors.append(
                f"{DENIAL_REASONS} records accepted coverages {missing} that "
                "insurer_position.acceptance omits -- a partial outcome "
                "summarized as if only the denial happened reads as a total "
                "denial")
        if not position.get("has_acceptance"):
            errors.append(
                "insurer_position.has_acceptance is false but "
                f"{DENIAL_REASONS} records {len(accepted)} accepted "
                "coverage(s)")
    elif listed and not accepted:
        errors.append(
            "insurer_position.acceptance lists coverages but "
            f"{DENIAL_REASONS} records none")

    return errors


def _collect_reason_ids(node) -> list[str]:
    """reason_ids appear at more than one depth in screening_report; walk for
    them rather than hard-coding a path that a schema revision would break."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("reason_ids", "denial_reason_ids") and isinstance(value, list):
                found.extend(v for v in value if isinstance(v, str))
            elif key == "reason_id" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_reason_ids(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_reason_ids(item))
    return found


def check(filename: str, data: dict, case_dir: Path,
          redacted_text_for=None) -> list[str]:
    """Dispatch for dao.write-contract. Unknown filenames return [] -- this
    layer is additive, never a gate a new contract has to register with.

    `redacted_text_for` is a doc_id -> processed text reader supplied by the
    DAO (which owns processed-path resolution; this module must not read
    `data/processed` itself). Needed to verify a policy match on a document
    with no normalized clause contract.
    """
    base = Path(filename).name
    if base == DENIAL_REASONS:
        return check_denial_reason_result(data) + check_policy_matches(
            data, case_dir, redacted_text_for)
    if base == DENIAL_VALIDATION:
        return check_denial_validation_result(data, case_dir)
    if base.startswith("screening_report"):
        return check_screening_report(data, case_dir)
    return []
