"""P0-7: canonical evidence names ONE exact source occurrence, not any match.

## The bypass this closes

`policy_uid_resolver._evidence_binding_errors` used to relate an element's
identity spans to its `evidence_references` like this:

    ref.get("document_id") == context.doc_id
    and ref.get("page") == record["logical_page"]
    and _normalize_ws(ref.get("quote")) == _normalize_ws(record["quote"])

An `evidence_reference` carried no offsets, so this was the strongest relation
available -- and it is satisfied by ANY occurrence of the quoted text on the
cited page. A policy that repeats a sentence across two sub-items of one
article (which `PAGE_1` below does, exactly as real Korean policy documents do)
could therefore be normalized with:

    condition.source_span_uids -> occurrence 2
    condition.evidence_references -> occurrence 1

and pass. Same document, same page, byte-identical quote. What the contract
proved was "this phrase exists in the document"; what it claimed to prove was
"this condition was built from THAT passage". Those are different statements,
and only the second is provenance.

P0-7 gives evidence the same `start_char`/`end_char` coordinates
`canonical_source_span` already used, resolves both sides independently against
the registered source revision, and requires the two sets of exact ranges to be
equal -- a bijection, so a missing, extra, duplicated, or differently-located
evidence range each has its own refusal.

## What this file deliberately asserts

* Both directions of the occurrence swap, not just one (section 1).
* The DAO's REAL write and finalization paths, not only the helper (section 7)
  -- a helper-only test proves a function is strict, not that anything calls it.
* That whitespace tolerance is gone (section 3): the previous rule collapsed
  whitespace, so a two-space source and a one-space quote were "equal". They
  are not the same bytes, and this is the relation that has to be exact.
* That canonical_v1 UID values did not move (section 6). Offsets select an
  occurrence and verify a citation; they never enter a hash. The frozen vectors
  in `test_canonical_uid_vectors.py` are the independent oracle for this.

Fixtures come from `test_canonical_uid_hierarchy`, so the two files cannot
drift into describing different canonical documents.
"""
import json

import pytest

import dao
import policy_uid
import policy_uid_resolver
import _cross_contract

from test_canonical_uid_hierarchy import (
    CLAUSE_HEADING,
    CONDITION_TEXT,
    PAGE_1,
    PAGE_2,
    _evidence,
    _page_text,
    _span,
    build_clauses,
    build_inventory,
    build_inventory_for_boundary_spans,
    canonical,  # noqa: F401 -- pytest fixture, used by name
)


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


@pytest.fixture(autouse=True)
def _no_extractors(monkeypatch):
    """Evidence verification must never shell out (requirement 4).

    Resolving evidence reads the already-registered revision and nothing else.
    Any subprocess launched during a check fails the test that launched it,
    rather than quietly re-running OCR or a PDF extractor -- and rather than
    costing a real model call.
    """
    import subprocess

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError(
            "evidence verification ran an external process -- it must read "
            f"only the registered source-text revision: {args!r}")

    for name in ("run", "check_output", "Popen", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, _boom)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _persist(name, data):
    _write_json(dao.case_dir("CASE_030") / name, data)


def _check_clauses(data):
    return dao._canonical_uid_errors(
        "CASE_030", "normalized_policy_clause_DOC_005.json",
        _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA, data)


def _seeded(pdf):
    """Persist the inventory the clause contract's PCs are parented on."""
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))


def _condition(data, index=0):
    return data["clauses"][0]["payout_conditions"][index]


# The two occurrences of CONDITION_TEXT on page 1 -- the repeated phrase the
# whole part is about. Asserted identical here so a fixture drift that made
# them differ would fail loudly rather than make every attack below pass for
# the wrong reason.
def _occurrences():
    first = _span(1, CONDITION_TEXT, occurrence=1)
    second = _span(1, CONDITION_TEXT, occurrence=2)
    assert first["quote"] == second["quote"]
    assert first["start_char"] != second["start_char"]
    return first, second


# =========================================================================
# 1. THE HEADLINE ATTACK: identity is one occurrence, evidence is the other
# =========================================================================

def test_identity_on_occurrence_2_with_evidence_on_occurrence_1_is_refused(
        canonical):
    """The exact bypass P0-7 closes, in its original direction.

    Before P0-7 this contract passed every gate in the repo: the UIDs recompute
    correctly (they are derived from the identity span, which is genuine), the
    evidence quote is real text on the cited page, and document/page/quote all
    agree. Only the OCCURRENCE differs, and nothing looked at it.
    """
    pdf = canonical
    _seeded(pdf)
    first, second = _occurrences()

    data = build_clauses(pdf, condition_spans=[[second]])
    condition = _condition(data)
    # Identity says occurrence 2; evidence says occurrence 1.
    condition["evidence_references"] = [_evidence(first)]

    # The pre-P0-7 relation is still satisfied -- that is the point.
    assert condition["evidence_references"][0]["quote"] == \
        condition["source_span_uids"][0]["quote"]
    assert condition["evidence_references"][0]["page"] == \
        condition["source_span_uids"][0]["page"]

    errors = _check_clauses(data)
    assert any("wrong occurrence" in e for e in errors), errors
    assert any("missing evidence for identity span" in e for e in errors), errors
    assert any("evidence not belonging to identity span" in e
               for e in errors), errors


def test_identity_on_occurrence_1_with_evidence_on_occurrence_2_is_refused(
        canonical):
    """The same attack reversed, so the check cannot be order-of-occurrence
    lucky -- e.g. by comparing against 'the first match on the page'."""
    pdf = canonical
    _seeded(pdf)
    first, second = _occurrences()

    data = build_clauses(pdf, condition_spans=[[first]])
    _condition(data)["evidence_references"] = [_evidence(second)]

    errors = _check_clauses(data)
    assert any("wrong occurrence" in e for e in errors), errors


def test_the_same_attack_on_the_clause_layer_is_refused(canonical):
    """PC as well as CI -- the binding is per element, not per bucket."""
    pdf = canonical
    boundary_spans = [_span(1, PAGE_1.rstrip("\n"))]
    inventory, pb = build_inventory_for_boundary_spans(pdf, boundary_spans)
    _persist("policy_boundary_inventory_DOC_005.json", inventory)
    first, second = _occurrences()

    clause_span = {
        "page": 1,
        "start_char": _span(1, CLAUSE_HEADING)["start_char"],
        "end_char": second["end_char"],
        "quote": _page_text(1)[
            _span(1, CLAUSE_HEADING)["start_char"]:second["end_char"]],
    }
    data = build_clauses(
        pdf, parent_boundary=pb, clause_spans=[clause_span],
        condition_spans=[[second]])
    # The clause's identity is the whole range; its evidence claims to be the
    # first repeated line instead.
    data["clauses"][0]["evidence_references"] = [_evidence(first)]

    errors = _check_clauses(data)
    assert any("clauses[0]" in e and "missing evidence for identity span" in e
               for e in errors), errors


# =========================================================================
# 2. The legitimate case still passes
# =========================================================================

def test_exact_matching_occurrences_pass(canonical):
    """Identity and evidence on the SAME exact range, on both occurrences.

    `build_clauses`' default is two conditions whose normalized `text` is
    byte-identical and which differ only by occurrence -- so this asserts the
    strict rule still admits the case it exists to describe.
    """
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    first, second = _occurrences()
    assert _condition(data, 0)["source_span_uids"] == [first]
    assert _condition(data, 1)["source_span_uids"] == [second]
    assert _check_clauses(data) == []


def test_the_evidence_quote_is_the_verbatim_registered_slice(canonical):
    """Not a rephrasing of the rule -- the actual bytes, read back from the
    revision the DAO resolves against."""
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    page_text = _page_text(1)
    for condition in data["clauses"][0]["payout_conditions"]:
        for reference in condition["evidence_references"]:
            sliced = page_text[
                reference["start_char"]:reference["end_char"]]
            assert sliced == reference["quote"]


# =========================================================================
# 3. Whitespace tolerance is gone
# =========================================================================
#
# The predecessor compared quotes through `_normalize_ws`, so "보험금을  지급"
# and "보험금을 지급" were the same evidence. Two spaces are not one space; a
# quote that differs from the source is a quote of something else, and the
# relation P0-7 exists to establish is exactness.

DOUBLE_SPACED = "회사는 보험금을  지급합니다"
TAB_PAGE = "가. 사고일부터\t180일 이내에 사망한 경우"
NEWLINE_PAGE = "나. 사고일부터\n180일 이내에 사망한 경우"

WS_PAGE_1 = (
    f"제3조(보험금의 지급) {DOUBLE_SPACED}.\n"
    f"{TAB_PAGE}\n"
    f"{NEWLINE_PAGE}\n"
)


@pytest.fixture
def whitespace_case(canonical, isolated_dao, make_args):
    """Re-register DOC_005 with a page whose whitespace is deliberately odd.

    Registered through the real `write-redacted-text` path, so the bytes under
    test are bytes the production pipeline could actually produce.
    """
    text_file = isolated_dao / "whitespace.md"
    text_file.write_text(
        f"<<<PAGE page=1>>>\n{WS_PAGE_1}<<<PAGE page=2>>>\n{PAGE_2}",
        encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_009")) == 0
    return canonical


def _whitespace_seed(pdf):
    """Persist an inventory covering the whole whitespace page, return its PB.

    Built here rather than through `build_inventory_for_boundary_spans`, whose
    default boundary quotes the ORIGINAL page 1 -- text this fixture's revision
    no longer contains.
    """
    boundary_span = _span(1, WS_PAGE_1.rstrip("\n"))
    ps = policy_uid.compute_uid(
        "span", source_pdf_sha256=pdf, physical_page=1,
        span_text=boundary_span["quote"],
        ordinal=policy_uid.ordinal_of_span_at(
            _page_text(1), boundary_span["quote"],
            boundary_span["start_char"]))
    pb = policy_uid.compute_uid(
        "boundary", source_pdf_sha256=pdf, physical_page=1, span_text=ps,
        ordinal=1)
    _persist("policy_boundary_inventory_DOC_005.json", {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_005",
        "source_text_revision": {"documents": [{
            "document_id": "DOC_005",
            "revision_sha256": dao.revision_entry_for(
                "CASE_030", "DOC_005")["current_revision_sha256"]}]},
        "boundaries": [{
            "boundary_uid": pb,
            "boundary_level": "article",
            "disposition": "excluded_with_reason",
            "label": "제3조",
            "normalized_mappings": [],
            "reason": "held for the evidence layer under test",
            "review_required": False,
        }],
        "page_spans": [{
            "span_uid": ps,
            "page": 1,
            "start_char": boundary_span["start_char"],
            "end_char": boundary_span["end_char"],
            "quote": boundary_span["quote"],
            "disposition": "boundary",
            "boundary_uid": pb,
            "exclusion_reason": None,
        }],
    })
    return boundary_span, pb


def _whitespace_contract(pdf, evidence_quote):
    """A clause whose evidence quote is substituted, offsets left correct."""
    boundary_span, pb = _whitespace_seed(pdf)
    condition_span = _span(1, TAB_PAGE)
    data = build_clauses(
        pdf, parent_boundary=pb, clause_spans=[boundary_span],
        condition_spans=[[condition_span]])
    condition = _condition(data)
    condition["text"] = condition_span["quote"]
    reference = _evidence(condition_span)
    reference["quote"] = evidence_quote
    condition["evidence_references"] = [reference]
    return data


@pytest.mark.parametrize("substitute,label", [
    (TAB_PAGE.replace("\t", " "), "tab collapsed to a space"),
    (TAB_PAGE.replace("\t", "  "), "tab collapsed to two spaces"),
    (TAB_PAGE.replace("\t", ""), "tab deleted"),
    (f" {TAB_PAGE} ", "surrounding whitespace added"),
])
def test_whitespace_variants_of_the_right_passage_are_refused(
        whitespace_case, substitute, label):
    """`_normalize_ws` made every one of these equal to the source. They are
    not: the slice at the declared offsets is the source, and it says
    something byte-different."""
    data = _whitespace_contract(whitespace_case, substitute)
    errors = _check_clauses(data)
    assert any("quote/slice mismatch" in e for e in errors), (label, errors)


def test_a_double_space_in_the_source_must_be_quoted_with_both_spaces(
        whitespace_case):
    """The reverse direction: the SOURCE has the extra whitespace, and a
    tidied-up quote of it is still not that passage."""
    pdf = whitespace_case
    boundary_span, pb = _whitespace_seed(pdf)

    exact = _span(1, DOUBLE_SPACED)
    data = build_clauses(
        pdf, parent_boundary=pb, clause_spans=[boundary_span],
        condition_spans=[[exact]])
    condition = _condition(data)
    condition["text"] = exact["quote"]

    tidied = _evidence(exact)
    tidied["quote"] = DOUBLE_SPACED.replace("  ", " ")
    condition["evidence_references"] = [tidied]
    assert any("quote/slice mismatch" in e
               for e in _check_clauses(data)), _check_clauses(data)

    # And the byte-exact quote of the same range passes, so the refusal above
    # is about the whitespace and not about the passage.
    condition["evidence_references"] = [_evidence(exact)]
    assert _check_clauses(data) == []


def test_a_newline_inside_the_cited_passage_must_be_reproduced(
        whitespace_case):
    pdf = whitespace_case
    boundary_span, pb = _whitespace_seed(pdf)

    exact = _span(1, NEWLINE_PAGE)
    data = build_clauses(
        pdf, parent_boundary=pb, clause_spans=[boundary_span],
        condition_spans=[[exact]])
    condition = _condition(data)
    condition["text"] = exact["quote"]
    flattened = _evidence(exact)
    flattened["quote"] = NEWLINE_PAGE.replace("\n", " ")
    condition["evidence_references"] = [flattened]
    assert any("quote/slice mismatch" in e
               for e in _check_clauses(data)), _check_clauses(data)


# =========================================================================
# 4. Offset attacks
# =========================================================================

def _with_evidence(pdf, mutate):
    """A default-valid contract whose first condition's evidence is mutated."""
    _seeded(pdf)
    data = build_clauses(pdf)
    reference = _condition(data)["evidence_references"][0]
    mutate(reference)
    return data


def test_a_negative_start_char_is_refused(canonical):
    data = _with_evidence(canonical, lambda r: r.update({"start_char": -1}))
    assert any("invalid range" in e for e in _check_clauses(data)), \
        _check_clauses(data)


def test_an_end_char_past_the_page_is_refused(canonical):
    data = _with_evidence(
        canonical, lambda r: r.update({"end_char": 10 ** 6}))
    assert any("invalid range" in e for e in _check_clauses(data)), \
        _check_clauses(data)


def test_an_empty_range_is_refused(canonical):
    def _collapse(reference):
        reference["end_char"] = reference["start_char"]

    data = _with_evidence(canonical, _collapse)
    assert any("invalid range" in e for e in _check_clauses(data)), \
        _check_clauses(data)


def test_an_inverted_range_is_refused(canonical):
    def _invert(reference):
        reference["start_char"], reference["end_char"] = (
            reference["end_char"], reference["start_char"])

    data = _with_evidence(canonical, _invert)
    assert any("invalid range" in e for e in _check_clauses(data)), \
        _check_clauses(data)


@pytest.mark.parametrize("field", ["start_char", "end_char"])
def test_a_string_offset_is_refused(canonical, field):
    """Not an int -- and refused as such rather than coerced. A string that
    happens to parse as a number is still not an offset."""
    data = _with_evidence(canonical, lambda r: r.update({field: str(r[field])}))
    errors = _check_clauses(data)
    assert any("must be integers" in e for e in errors), errors


@pytest.mark.parametrize("field", ["start_char", "end_char"])
def test_a_boolean_offset_is_refused(canonical, field):
    """`bool` is an `int` subclass, so `isinstance(True, int)` is True and
    `page_text[True:...]` indexes at 1. Refused explicitly."""
    data = _with_evidence(canonical, lambda r: r.update({field: True}))
    errors = _check_clauses(data)
    assert any("must be integers" in e for e in errors), errors


def test_offsets_of_occurrence_1_with_the_quote_copied_from_occurrence_2(
        canonical):
    """Requirement D's subtlest case.

    The two occurrences are byte-identical, so copying the quote across is a
    no-op and the slice still matches -- which means this is NOT a quote/slice
    failure. It is caught by the binding relation instead: the range the
    evidence declares is not the range the identity claims.
    """
    pdf = canonical
    _seeded(pdf)
    first, second = _occurrences()
    data = build_clauses(pdf, condition_spans=[[second]])
    reference = _evidence(second)
    reference["start_char"] = first["start_char"]
    reference["end_char"] = first["end_char"]
    _condition(data)["evidence_references"] = [reference]

    errors = _check_clauses(data)
    assert not any("quote/slice mismatch" in e for e in errors), errors
    assert any("wrong occurrence" in e for e in errors), errors


def test_offsets_that_slice_a_different_real_passage_are_refused(canonical):
    """Offsets moved onto other genuine text while the quote stays put."""
    pdf = canonical
    _seeded(pdf)
    heading = _span(1, CLAUSE_HEADING)
    data = build_clauses(pdf)
    reference = _condition(data)["evidence_references"][0]
    reference["start_char"] = heading["start_char"]
    reference["end_char"] = heading["end_char"]
    assert any("quote/slice mismatch" in e
               for e in _check_clauses(data)), _check_clauses(data)


def test_evidence_without_offsets_is_refused_under_canonical_v1(canonical):
    """The migration boundary: the pre-P0-7 shape is not a valid canonical
    write. Legacy readability is asserted separately, in section 6."""
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    reference = _condition(data)["evidence_references"][0]
    del reference["start_char"]
    del reference["end_char"]
    errors = _check_clauses(data)
    assert any("missing exact offsets" in e for e in errors), errors


@pytest.mark.parametrize("field", ["start_char", "end_char"])
def test_half_an_offset_pair_is_refused(canonical, field):
    """One endpoint is not a range. The schema also states this
    (`dependentRequired`); the DAO does not rely on it having been checked."""
    def _drop(reference):
        del reference[field]

    data = _with_evidence(canonical, _drop)
    assert any("missing exact offsets" in e
               for e in _check_clauses(data)), _check_clauses(data)


def test_an_occurrence_ordinal_disagreeing_with_the_offsets_is_refused(
        canonical):
    """A caller-submitted derived value is never the authority.

    `occurrence_ordinal` is re-derived from the verified offsets, so declaring
    'this is occurrence 1' over occurrence 2's bytes cannot select the other
    passage -- it just refuses.
    """
    pdf = canonical
    _seeded(pdf)
    _, second = _occurrences()
    data = build_clauses(pdf, condition_spans=[[second]])
    reference = _evidence(second)
    reference["occurrence_ordinal"] = 1
    _condition(data)["evidence_references"] = [reference]
    errors = _check_clauses(data)
    assert any("occurrence_ordinal" in e for e in errors), errors


# =========================================================================
# 5. Bidirectional coverage
# =========================================================================

def test_two_identity_spans_with_one_evidence_reference_are_refused(canonical):
    """A multi-span element must evidence EVERY span it is made of."""
    pdf = canonical
    boundary_spans = [_span(1, PAGE_1.rstrip("\n")),
                      _span(2, PAGE_2.rstrip("\n"))]
    inventory, pb = build_inventory_for_boundary_spans(pdf, boundary_spans)
    _persist("policy_boundary_inventory_DOC_005.json", inventory)

    clause_spans = [_span(1, PAGE_1.rstrip("\n")),
                    _span(2, "제4조(별표) 장해지급률표")]
    data = build_clauses(
        pdf, parent_boundary=pb, clause_spans=clause_spans,
        condition_spans=[[_span(2, "장해지급률표")]])
    clause = data["clauses"][0]
    assert len(clause["evidence_references"]) == 2
    clause["evidence_references"] = [clause["evidence_references"][0]]

    errors = _check_clauses(data)
    assert any("missing evidence for identity span" in e for e in errors), errors


def test_an_extra_evidence_reference_is_refused(canonical):
    """Evidence for a passage the identity never claimed is not extra
    diligence; it is provenance the element does not have."""
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    condition = _condition(data)
    condition["evidence_references"].append(
        _evidence(_span(2, "제4조(별표) 장해지급률표")))
    errors = _check_clauses(data)
    assert any("evidence not belonging to identity span" in e
               for e in errors), errors


def test_duplicating_one_evidence_range_cannot_pad_the_count(canonical):
    """The count attack: two identity spans, and the SAME evidence twice.

    Without the duplicate check a per-span 'is there a matching reference'
    loop is satisfied for the covered span twice over, and a naive
    'len(evidence) >= len(spans)' would be satisfied too.
    """
    pdf = canonical
    boundary_spans = [_span(1, PAGE_1.rstrip("\n")),
                      _span(2, PAGE_2.rstrip("\n"))]
    inventory, pb = build_inventory_for_boundary_spans(pdf, boundary_spans)
    _persist("policy_boundary_inventory_DOC_005.json", inventory)

    clause_spans = [_span(1, PAGE_1.rstrip("\n")),
                    _span(2, "제4조(별표) 장해지급률표")]
    data = build_clauses(
        pdf, parent_boundary=pb, clause_spans=clause_spans,
        condition_spans=[[_span(2, "장해지급률표")]])
    clause = data["clauses"][0]
    clause["evidence_references"] = [
        _evidence(clause_spans[0]), _evidence(clause_spans[0])]
    assert len(clause["evidence_references"]) == len(clause_spans)

    errors = _check_clauses(data)
    assert any("duplicate evidence range" in e for e in errors), errors


def test_a_repeated_identical_evidence_reference_is_refused(canonical):
    """Even where the count is not in play, one range cited twice is a
    contradiction about how many passages the element rests on."""
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    condition = _condition(data)
    condition["evidence_references"] = condition["evidence_references"] * 2
    errors = _check_clauses(data)
    assert any("duplicate evidence range" in e for e in errors), errors


def test_a_mismatched_evidence_document_id_is_refused(canonical):
    """A reference naming a DIFFERENT document leaves the identity span with
    no evidence in this contract's own source -- which is the failure."""
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    _condition(data)["evidence_references"][0]["document_id"] = "DOC_099"
    errors = _check_clauses(data)
    assert any("missing evidence for identity span" in e for e in errors), errors


def test_a_mismatched_evidence_page_is_refused(canonical):
    """Right offsets, wrong page -- the slice on the other page is different
    text, so this fails at the verbatim check rather than silently resolving."""
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    _condition(data)["evidence_references"][0]["page"] = 2
    errors = _check_clauses(data)
    assert any("quote/slice mismatch" in e or "invalid range" in e
               for e in errors), errors


def test_a_page_absent_from_the_revision_is_refused(canonical):
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    _condition(data)["evidence_references"][0]["page"] = 99
    errors = _check_clauses(data)
    assert any("does not exist in the registered source revision" in e
               for e in errors), errors


# =========================================================================
# 6. Order independence, and UID invariance
# =========================================================================

def test_reversing_the_evidence_array_changes_nothing(canonical):
    """Submission order is extraction order, and extraction order is not a
    fact about the source."""
    pdf = canonical
    boundary_spans = [_span(1, PAGE_1.rstrip("\n")),
                      _span(2, PAGE_2.rstrip("\n"))]
    inventory, pb = build_inventory_for_boundary_spans(pdf, boundary_spans)
    _persist("policy_boundary_inventory_DOC_005.json", inventory)

    clause_spans = [_span(1, PAGE_1.rstrip("\n")),
                    _span(2, "제4조(별표) 장해지급률표")]
    condition_spans = [[_span(2, "장해지급률표")]]
    straight = build_clauses(
        pdf, parent_boundary=pb, clause_spans=clause_spans,
        condition_spans=condition_spans)
    assert _check_clauses(straight) == []

    shuffled = json.loads(json.dumps(straight))
    clause = shuffled["clauses"][0]
    clause["evidence_references"].reverse()
    clause["source_span_uids"].reverse()
    assert _check_clauses(shuffled) == []

    assert clause["clause_uid"] == straight["clauses"][0]["clause_uid"]
    assert (clause["payout_conditions"][0]["condition_uid"]
            == straight["clauses"][0]["payout_conditions"][0]["condition_uid"])


def test_evidence_offsets_do_not_enter_any_uid(canonical):
    """P0-7 must not have moved a single canonical_v1 identifier.

    Built two ways -- with and without evidence offsets -- and the UIDs are
    compared directly. `test_canonical_uid_vectors.py` is the independent
    oracle for the absolute values; this asserts the relative invariance the
    change itself is responsible for.
    """
    pdf = canonical
    _seeded(pdf)
    with_offsets = build_clauses(pdf)
    without = json.loads(json.dumps(with_offsets))
    for clause in without["clauses"]:
        for reference in clause["evidence_references"]:
            reference.pop("start_char"), reference.pop("end_char")
        for condition in clause["payout_conditions"]:
            for reference in condition["evidence_references"]:
                reference.pop("start_char"), reference.pop("end_char")

    assert (with_offsets["clauses"][0]["clause_uid"]
            == without["clauses"][0]["clause_uid"])
    assert ([c["condition_uid"]
             for c in with_offsets["clauses"][0]["payout_conditions"]]
            == [c["condition_uid"]
                for c in without["clauses"][0]["payout_conditions"]])
    # And only the offset-carrying one is a legal canonical contract.
    assert _check_clauses(with_offsets) == []
    assert _check_clauses(without)


def test_a_legacy_contract_without_offsets_still_validates_against_the_schema(
        canonical):
    """Schema readability is not what P0-7 tightened.

    A pre-P0-7 artifact must stay loadable and migratable -- the enforcement is
    the DAO's, on canonical_v1 writes and finalizations, not the schema's on
    every read. Removing the offsets must therefore leave the file
    schema-VALID and DAO-REFUSED, which is exactly the split this asserts.
    """
    import validate_output

    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    for reference in data["clauses"][0]["evidence_references"]:
        reference.pop("start_char"), reference.pop("end_char")
    for condition in data["clauses"][0]["payout_conditions"]:
        for reference in condition["evidence_references"]:
            reference.pop("start_char"), reference.pop("end_char")

    schemas, registry = dao.load_registry()
    assert validate_output.validate_instance(
        data, _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
        schemas, registry) == []
    assert _check_clauses(data)


def test_the_schema_refuses_half_an_offset_pair(canonical):
    """`dependentRequired` on strict_evidence_reference. Belt to the DAO's
    braces -- a lone endpoint is malformed on its face, not merely
    unenforceable."""
    import validate_output

    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    del data["clauses"][0]["evidence_references"][0]["end_char"]
    schemas, registry = dao.load_registry()
    assert validate_output.validate_instance(
        data, _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
        schemas, registry)


# =========================================================================
# 7. THE REAL DAO PATHS
# =========================================================================
#
# Everything above calls the checker. These call `dao.cmd_write_contract` and
# `dao._canonical_uid_finalize_blockers` -- the paths a real agent reaches --
# because a checker that is strict but unreached protects nothing.

def _write_clauses(isolated_dao, make_args, data, stage=None):
    data_file = isolated_dao / "clauses_under_test.json"
    _write_json(data_file, data)
    return dao.cmd_write_contract(make_args(
        case_id="CASE_030",
        filename="normalized_policy_clause_DOC_005.json",
        data_file=str(data_file),
        schema_name=_cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
        held_by="policy-pipeline", run_id="RUN_20260728_001", stage=stage))


# `cmd_write_contract` also runs the cross-contract SEMANTIC layer, which the
# checker-level tests above never reach. That layer requires a payout
# condition's quote to carry an operative marker ("지급"/"보상"/"보험금") and to
# end on a terminal predicate -- reasonable rules that the hierarchy fixture's
# condition text ("...사망한 경우") does not satisfy, because it was written for
# a checker that does not apply them.
#
# So the ACCEPT-path tests below register their own page, whose repeated
# sentence is a complete payout provision. That keeps the repeated-phrase
# attack surface identical -- the same sentence really does appear twice -- and
# isolates what these tests are actually about (the P0-7 gate) from a
# pre-existing semantic rule that would otherwise mask it.
PAYOUT_SENTENCE = "회사는 제1항의 사유가 발생한 경우 보험금을 지급합니다."
SEMANTIC_PAGE_1 = (
    "제3조(보험금의 지급)\n"
    f"가. {PAYOUT_SENTENCE}\n"
    f"나. {PAYOUT_SENTENCE}\n"
)


@pytest.fixture
def semantic_case(canonical, isolated_dao, make_args):
    """DOC_005 re-registered with a page whose repeated line is a real payout
    provision, so the cross-contract semantic layer is satisfied."""
    text_file = isolated_dao / "semantic.md"
    text_file.write_text(
        f"<<<PAGE page=1>>>\n{SEMANTIC_PAGE_1}<<<PAGE page=2>>>\n{PAGE_2}",
        encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_012")) == 0
    return canonical


def _semantic_contract(pdf, *, condition_occurrence=1):
    """Persist the matching inventory; return a clause contract over it."""
    boundary_span = _span(1, SEMANTIC_PAGE_1.rstrip("\n"))
    ps = policy_uid.compute_uid(
        "span", source_pdf_sha256=pdf, physical_page=1,
        span_text=boundary_span["quote"],
        ordinal=policy_uid.ordinal_of_span_at(
            _page_text(1), boundary_span["quote"],
            boundary_span["start_char"]))
    pb = policy_uid.compute_uid(
        "boundary", source_pdf_sha256=pdf, physical_page=1, span_text=ps,
        ordinal=1)
    _persist("policy_boundary_inventory_DOC_005.json", {
        "case_id": "CASE_030",
        "run_id": "RUN_20260728_001",
        "component": "policy-pipeline",
        "status": "success",
        "source_document_id": "DOC_005",
        "source_text_revision": {"documents": [{
            "document_id": "DOC_005",
            "revision_sha256": dao.revision_entry_for(
                "CASE_030", "DOC_005")["current_revision_sha256"]}]},
        "boundaries": [{
            "boundary_uid": pb,
            "boundary_level": "article",
            "disposition": "excluded_with_reason",
            "label": "제3조",
            "normalized_mappings": [],
            "reason": "held for the evidence layer under test",
            "review_required": False,
        }],
        "page_spans": [{
            "span_uid": ps, "page": 1,
            "start_char": boundary_span["start_char"],
            "end_char": boundary_span["end_char"],
            "quote": boundary_span["quote"],
            "disposition": "boundary", "boundary_uid": pb,
            "exclusion_reason": None,
        }],
    })
    condition_span = _span(
        1, PAYOUT_SENTENCE, occurrence=condition_occurrence)
    data = build_clauses(
        pdf, parent_boundary=pb, clause_spans=[boundary_span],
        condition_spans=[[condition_span]])
    condition = _condition(data)
    condition["text"] = PAYOUT_SENTENCE
    return data, condition_span


def test_write_contract_refuses_the_occurrence_swap_and_writes_nothing(
        canonical, isolated_dao, make_args, capsys):
    """The headline attack through the REAL write path.

    Requirement I, and the result the report has to name: an occurrence-1
    evidence reference under an occurrence-2 identity span is refused by
    `dao.cmd_write_contract`, and the target file does not appear on disk.
    """
    pdf = canonical
    _seeded(pdf)
    first, second = _occurrences()
    data = build_clauses(pdf, condition_spans=[[second]])
    _condition(data)["evidence_references"] = [_evidence(first)]

    target = dao.case_dir("CASE_030") / "normalized_policy_clause_DOC_005.json"
    assert not target.exists()
    assert _write_clauses(isolated_dao, make_args, data) == 1
    output = capsys.readouterr().out
    assert "non-canonical UIDs" in output
    assert "wrong occurrence" in output
    assert not target.exists(), "a refused write must not persist"


def test_write_contract_refuses_the_reverse_occurrence_swap(
        canonical, isolated_dao, make_args):
    pdf = canonical
    _seeded(pdf)
    first, second = _occurrences()
    data = build_clauses(pdf, condition_spans=[[first]])
    _condition(data)["evidence_references"] = [_evidence(second)]
    assert _write_clauses(isolated_dao, make_args, data) == 1
    assert not (dao.case_dir("CASE_030")
                / "normalized_policy_clause_DOC_005.json").exists()


def test_write_contract_refuses_evidence_with_no_offsets(
        canonical, isolated_dao, make_args, capsys):
    """Legacy shape is not a write bypass -- a canonical document cannot be
    written against by omitting the offsets, which is the shape a pre-P0-7
    agent would emit."""
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    for condition in data["clauses"][0]["payout_conditions"]:
        for reference in condition["evidence_references"]:
            reference.pop("start_char"), reference.pop("end_char")
    assert _write_clauses(isolated_dao, make_args, data) == 1
    assert "missing exact offsets" in capsys.readouterr().out


def test_write_contract_accepts_the_exact_binding(
        semantic_case, isolated_dao, make_args, capsys):
    """The gate is a gate, not a wall: the correct contract goes through.

    Written against the SECOND occurrence of a sentence that appears twice on
    the page -- the very shape the attack tests exploit -- so this proves the
    rule admits a legitimate repeated-phrase clause rather than refusing all
    of them.
    """
    data, condition_span = _semantic_contract(
        semantic_case, condition_occurrence=2)
    assert _span(1, PAYOUT_SENTENCE, occurrence=1)["quote"] == \
        condition_span["quote"]

    rc = _write_clauses(isolated_dao, make_args, data)
    assert rc == 0, capsys.readouterr().out
    assert (dao.case_dir("CASE_030")
            / "normalized_policy_clause_DOC_005.json").exists()


def test_write_contract_refuses_the_occurrence_swap_on_a_semantically_valid_clause(
        semantic_case, isolated_dao, make_args, capsys):
    """The same contract that just passed, with ONLY the occurrence changed.

    This is the cleanest statement of P0-7 through the real write path: two
    payloads identical in every field the pipeline's other layers inspect --
    same document, same page, same quote, same normalized text, same semantic
    markers, both schema-valid and both cross-contract-clean -- differing only
    in which occurrence the evidence offsets select. One is written; the other
    is refused.
    """
    data, condition_span = _semantic_contract(
        semantic_case, condition_occurrence=2)
    first = _span(1, PAYOUT_SENTENCE, occurrence=1)
    swapped = _evidence(first)
    assert swapped["quote"] == condition_span["quote"]
    _condition(data)["evidence_references"] = [swapped]

    assert _write_clauses(isolated_dao, make_args, data) == 1
    output = capsys.readouterr().out
    assert "non-canonical UIDs" in output
    assert "wrong occurrence" in output
    assert not (dao.case_dir("CASE_030")
                / "normalized_policy_clause_DOC_005.json").exists()


def test_finalization_refuses_an_occurrence_swap_written_around_the_dao(
        canonical, isolated_dao, make_args):
    """Finalization re-verifies rather than trusting the write gate.

    The bad contract is placed on disk directly -- simulating an artifact that
    predates the gate, or one written while the source still supported it --
    and `policy_clause_processing` must still refuse to finalize on it.
    """
    pdf = canonical
    _seeded(pdf)
    first, second = _occurrences()
    data = build_clauses(pdf, condition_spans=[[second]])
    _condition(data)["evidence_references"] = [_evidence(first)]
    _persist("normalized_policy_clause_DOC_005.json", data)

    blockers = dao._canonical_uid_finalize_blockers("CASE_030", "DOC_005")
    assert any("wrong occurrence" in b for b in blockers), blockers
    assert any("normalized_policy_clause_DOC_005.json" in b
               for b in blockers), blockers
    # The third assertion here used to be
    # `assert dao._policy_completion_blockers("CASE_030")`, checking that the
    # occurrence-swap blocker actually REACHED the finalize gate rather than
    # only existing in its helper -- a distinction this project has been
    # bitten by before. It is dropped rather than inverted because clause
    # normalization retired 2026-08-15: the completion gate's per-document
    # scope is permanently empty, so no path carries this blocker to it, and
    # asserting the empty result would pin the absence of a check instead of
    # the presence of one.
    #
    # What is verified above is unchanged and still load-bearing: an
    # occurrence swap written around the DAO is caught on re-verification,
    # not trusted from the write gate. If normalization is ever reinstated,
    # restore the reachability assertion with it -- the helper working proves
    # only that the helper works.


def test_finalization_refuses_evidence_with_no_offsets(canonical):
    pdf = canonical
    _seeded(pdf)
    data = build_clauses(pdf)
    for reference in data["clauses"][0]["evidence_references"]:
        reference.pop("start_char"), reference.pop("end_char")
    _persist("normalized_policy_clause_DOC_005.json", data)

    blockers = dao._canonical_uid_finalize_blockers("CASE_030", "DOC_005")
    assert any("missing exact offsets" in b for b in blockers), blockers


def test_finalization_accepts_the_exact_binding(canonical):
    pdf = canonical
    _seeded(pdf)
    _persist("normalized_policy_clause_DOC_005.json", build_clauses(pdf))
    assert dao._canonical_uid_finalize_blockers("CASE_030", "DOC_005") == []


# =========================================================================
# 8. Source revision changes
# =========================================================================

def test_offsets_valid_under_revision_a_go_stale_under_revision_b(
        semantic_case, isolated_dao, make_args, capsys):
    """Requirement G, end to end through the real revision path.

    REV-A: identity and evidence agree exactly, the contract passes the
    checker AND the real `write-contract` path.
    REV-B: page 1 gains a prefix, so every offset on it shifts. The unchanged
    contract must now be refused -- against the CURRENT registered revision,
    not the one it was written against -- and the refusal must come from
    reading that revision, never from re-extracting anything (the autouse
    subprocess guard proves the second half).
    """
    data, _ = _semantic_contract(semantic_case, condition_occurrence=2)
    assert _check_clauses(data) == [], "REV-A must be clean to start"
    assert _write_clauses(isolated_dao, make_args, data) == 0, \
        capsys.readouterr().out

    prefix = "제1조(목적) 이 약관은 다음과 같습니다.\n"
    text_file = isolated_dao / "rev_b.md"
    text_file.write_text(
        f"<<<PAGE page=1>>>\n{prefix}{SEMANTIC_PAGE_1}"
        f"<<<PAGE page=2>>>\n{PAGE_2}",
        encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_010")) == 0

    # The whole policy layer for this document is stale, not just the clause:
    # the inventory's own boundary spans shifted too, so the PC's declared
    # parent no longer recomputes and that is reported first. What matters for
    # P0-7 is that the contract is REFUSED against the current revision rather
    # than still passing on offsets nobody re-verified; which layer names it
    # first is not this test's subject. The evidence layer's own staleness is
    # isolated in the next test, where the inventory is re-derived and only the
    # clause's offsets are left behind.
    stale = _check_clauses(data)
    assert stale, "REV-A offsets must not still resolve under REV-B"

    _persist("normalized_policy_clause_DOC_005.json", data)
    assert dao._canonical_uid_finalize_blockers("CASE_030", "DOC_005")


def test_stale_evidence_offsets_alone_are_refused_after_a_revision(
        semantic_case, isolated_dao, make_args):
    """REV-B with a CURRENT inventory: the failure is the evidence itself.

    Everything else is brought forward to the new revision -- the inventory is
    re-derived, and the clause's identity spans are re-pointed at the same
    bytes' new positions -- so the only thing still carrying REV-A coordinates
    is the evidence. That is what must be refused, and it must be refused by
    reading the registered revision (the autouse subprocess guard again).
    """
    pdf = semantic_case
    data, _ = _semantic_contract(pdf, condition_occurrence=2)
    assert _check_clauses(data) == []

    prefix = "제1조(목적) 이 약관은 다음과 같습니다.\n"
    text_file = isolated_dao / "rev_b_evidence.md"
    text_file.write_text(
        f"<<<PAGE page=1>>>\n{prefix}{SEMANTIC_PAGE_1}"
        f"<<<PAGE page=2>>>\n{PAGE_2}",
        encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_013")) == 0

    # Re-derive everything EXCEPT the evidence offsets.
    current, _ = _semantic_contract(pdf, condition_occurrence=2)
    stale_clause = data["clauses"][0]
    fresh_clause = current["clauses"][0]
    fresh_clause["evidence_references"] = stale_clause["evidence_references"]
    fresh_clause["payout_conditions"][0]["evidence_references"] = \
        stale_clause["payout_conditions"][0]["evidence_references"]

    errors = _check_clauses(current)
    assert errors, "REV-A evidence offsets must not resolve under REV-B"
    assert any("quote/slice mismatch" in e for e in errors), errors

    # And the UIDs are untouched by the revision: same bytes, same occurrence.
    assert fresh_clause["clause_uid"] == stale_clause["clause_uid"]


def test_the_same_contract_retranslated_onto_revision_b_passes_again(
        canonical, isolated_dao, make_args):
    """The complement: staleness is about the OFFSETS being stale, not about
    a revision change poisoning the element forever. Re-point the ranges at
    the same bytes' new positions and the contract is canonical again -- with
    the same UIDs, since the bytes and occurrence did not change."""
    pdf = canonical
    _seeded(pdf)
    before = build_clauses(pdf)

    prefix = "제1조(목적) 이 약관은 다음과 같습니다.\n"
    text_file = isolated_dao / "rev_b2.md"
    text_file.write_text(
        f"<<<PAGE page=1>>>\n{prefix}{PAGE_1}<<<PAGE page=2>>>\n{PAGE_2}",
        encoding="utf-8")
    assert dao.cmd_write_redacted_text(make_args(
        case_id="CASE_030", doc_id="DOC_005", text_file=str(text_file),
        held_by="document-pipeline", run_id="RUN_20260728_011")) == 0
    _persist("policy_boundary_inventory_DOC_005.json", build_inventory(pdf))

    after = json.loads(json.dumps(before))
    offset = len(prefix)

    def _shift(items):
        for item in items:
            if item.get("page") == 1:
                item["start_char"] += offset
                item["end_char"] += offset

    for clause in after["clauses"]:
        _shift(clause["source_span_uids"])
        _shift(clause["evidence_references"])
        for condition in clause["payout_conditions"]:
            _shift(condition["source_span_uids"])
            _shift(condition["evidence_references"])

    assert _check_clauses(after) == []
    assert (after["clauses"][0]["clause_uid"]
            == before["clauses"][0]["clause_uid"])


# =========================================================================
# 9. The resolver helper itself
# =========================================================================

def _context():
    context, errors = dao._uid_source_context("CASE_030", "DOC_005")
    assert errors == []
    return context


def test_resolve_exact_evidence_reference_returns_the_occurrence_ordinal(
        canonical):
    """The helper's own contract: it re-derives the ordinal from the bytes."""
    _seeded(canonical)
    context = _context()
    first, second = _occurrences()
    assert policy_uid_resolver.resolve_exact_evidence_reference(
        context, _evidence(first), "loc")["ordinal"] == 1
    assert policy_uid_resolver.resolve_exact_evidence_reference(
        context, _evidence(second), "loc")["ordinal"] == 2


def test_resolve_exact_evidence_reference_rejects_a_non_object(canonical):
    _seeded(canonical)
    with pytest.raises(policy_uid_resolver.UidResolutionError):
        policy_uid_resolver.resolve_exact_evidence_reference(
            _context(), "회사는 보험금을 지급합니다", "loc")


def test_resolve_exact_evidence_reference_rejects_another_document(canonical):
    _seeded(canonical)
    reference = _evidence(_span(1, CONDITION_TEXT))
    reference["document_id"] = "DOC_777"
    with pytest.raises(policy_uid_resolver.UidResolutionError) as excinfo:
        policy_uid_resolver.resolve_exact_evidence_reference(
            _context(), reference, "loc")
    assert "not this contract's source document" in str(excinfo.value)


def test_resolve_exact_evidence_reference_maps_through_the_physical_page(
        canonical):
    """Canonical provenance is keyed to the physical page of the immutable
    parent, so the resolved record must carry it rather than the logical
    number the reference supplied."""
    _seeded(canonical)
    record = policy_uid_resolver.resolve_exact_evidence_reference(
        _context(), _evidence(_span(1, CONDITION_TEXT)), "loc")
    assert record["physical_page"] == dao._physical_page_for(
        "CASE_030", "DOC_005", 1)
    assert record["logical_page"] == 1
