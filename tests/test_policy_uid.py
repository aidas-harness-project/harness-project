"""Part 11J commit c: canonical UID derivation.

These target the derivation FUNCTION directly, with fixed expected values and
explicit invariants, rather than only checking that the DAO agrees with itself.
A fixture and a validator that both call compute_uid() would still pass if
compute_uid returned sha256("") for everything; that shared-mistake blind spot
is what this file exists to cover.

The invariants under test are the ones the identity/evidence split was designed
around: a forbidden input must not change a UID, and a genuine source change
must.
"""
import pytest

import policy_uid as pu


PDF = "a" * 64
OTHER_PDF = "b" * 64
SPAN = "제3조(보험금의 지급) 회사는 보험금을 지급합니다."

PAGE = (
    "제1조(목적) 이 약관은 다음과 같습니다.\n"
    "제3조(보험금의 지급) 회사는 보험금을 지급합니다.\n"
    "부칙\n"
    "제3조(보험금의 지급) 회사는 보험금을 지급합니다.\n"
)


def _uid(**overrides):
    kwargs = {
        "source_pdf_sha256": PDF,
        "physical_page": 12,
        "span_text": SPAN,
    }
    kwargs.update(overrides)
    return pu.compute_uid(kwargs.pop("kind", "clause"), **kwargs)


# --- shape and determinism -------------------------------------------------

def test_uid_is_deterministic_and_prefixed_by_kind():
    assert _uid() == _uid()
    assert _uid().startswith("PC-")
    assert _uid(kind="boundary").startswith("PB-")
    assert _uid(kind="table").startswith("RT-")
    # 16 hex chars, matching the schemas' ^PC-[a-f0-9]{16,64}$
    assert len(_uid().split("-")[1]) == 16


def test_the_same_bytes_in_different_kinds_do_not_collide():
    """The kind is hashed, not just prefixed -- otherwise a boundary and the
    clause covering the identical span would share a digest body."""
    assert _uid(kind="clause")[3:] != _uid(kind="boundary")[3:]


# --- identity inputs MUST change the UID -----------------------------------

def test_a_different_source_document_changes_the_uid():
    assert _uid() != _uid(source_pdf_sha256=OTHER_PDF)


def test_a_different_page_changes_the_uid():
    assert _uid() != _uid(physical_page=13)


def test_different_span_text_changes_the_uid():
    assert _uid() != _uid(span_text=SPAN.replace("지급합니다", "지급하지 않습니다"))


def test_a_different_occurrence_changes_the_uid():
    """Identical text twice on a page must not share one identity."""
    assert _uid(ordinal=1) != _uid(ordinal=2)


def test_a_different_parent_changes_the_uid():
    assert _uid(kind="condition", parent_uid="PC-1111111111111111") != \
        _uid(kind="condition", parent_uid="PC-2222222222222222")


def test_a_different_column_changes_a_cell_uid():
    base = {"kind": "cell", "parent_uid": "RR-1111111111111111"}
    assert _uid(**base, column_key="지급률") != _uid(**base, column_key="장해분류")


def test_parts_cannot_be_re_split_into_a_collision():
    """Concatenating identity parts without a separator would let 'ab'+'c' and
    'a'+'bc' hash identically."""
    assert _uid(span_text="AB", ordinal=1) != _uid(span_text="A", ordinal=1)
    a = pu.compute_uid("clause", source_pdf_sha256=PDF, physical_page=1,
                       span_text="x", parent_uid="yz")
    b = pu.compute_uid("clause", source_pdf_sha256=PDF, physical_page=1,
                       span_text="xy", parent_uid="z")
    assert a != b


# --- forbidden inputs must NOT change the UID ------------------------------

def test_forbidden_inputs_are_not_accepted_as_arguments():
    """The enforceable form of the schemas' prose: there is no parameter to
    pass an index, a display id, or an offset through."""
    for forbidden in ("array_index", "clause_id", "display_id",
                      "extraction_order", "start_char", "end_char",
                      "page_sha256"):
        with pytest.raises(TypeError):
            pu.compute_uid("clause", source_pdf_sha256=PDF, physical_page=1,
                           span_text=SPAN, **{forbidden: 3})


def test_the_forbidden_input_list_is_stated_not_implied():
    assert any("array position" in item for item in pu.FORBIDDEN_UID_INPUTS)
    assert any("offset" in item for item in pu.FORBIDDEN_UID_INPUTS)
    assert any("normalized or rewritten wording" in item
               for item in pu.FORBIDDEN_UID_INPUTS)


def test_offsets_do_not_participate_in_identity():
    """The defect the ordinal exists to remove: a one-character edit earlier on
    the page shifts every later offset, and offset-derived UIDs would move for
    spans whose own bytes never changed."""
    early = pu.ordinal_of_span_at(PAGE, SPAN, PAGE.index(SPAN))
    shifted_page = PAGE.replace("제1조(목적) 이", "제1조(목적)  이")
    late = pu.ordinal_of_span_at(shifted_page, SPAN, shifted_page.index(SPAN))
    assert early == late == 1
    assert _uid(ordinal=early) == _uid(ordinal=late)


# --- normalization ---------------------------------------------------------

def test_nfc_and_nfd_of_the_same_korean_text_share_one_uid():
    """Not theoretical: two independent readers can emit different Unicode
    forms of identical text."""
    import unicodedata
    nfd = unicodedata.normalize("NFD", SPAN)
    assert nfd != SPAN                      # genuinely different bytes
    assert _uid(span_text=nfd) == _uid()


def test_line_endings_do_not_change_identity():
    text = "제3조\n제4조"
    assert _uid(span_text=text) == _uid(span_text="제3조\r\n제4조")
    assert _uid(span_text=text) == _uid(span_text="제3조\r제4조")


def test_whitespace_is_not_collapsed():
    """Collapsing would merge genuinely different source spans, and would break
    the Part 11E check that span text matches the source at its offsets."""
    assert _uid(span_text="제3조  회사는") != _uid(span_text="제3조 회사는")


# --- occurrence ordinals ---------------------------------------------------

def test_ordinal_counts_source_order_not_extraction_order():
    assert pu.occurrence_ordinal(PAGE, SPAN) == 2
    first = PAGE.index(SPAN)
    second = PAGE.index(SPAN, first + 1)
    assert pu.ordinal_of_span_at(PAGE, SPAN, first) == 1
    assert pu.ordinal_of_span_at(PAGE, SPAN, second) == 2


def test_a_span_absent_from_its_page_cannot_be_identified():
    """A UID for text the source does not contain would be a stable-looking
    identifier for nothing."""
    with pytest.raises(pu.UidInputError):
        pu.ordinal_of_span_at(PAGE, "존재하지 않는 문구", 0)
    with pytest.raises(pu.UidInputError):
        pu.occurrence_ordinal(PAGE, "존재하지 않는 문구")


# --- missing identity is an error, never a placeholder ---------------------

def test_a_missing_source_digest_is_refused_with_no_fallback():
    """The fallback that would 'work' here -- hashing processed text -- is the
    defect: a typo fix on page 200 would move every UID on page 1."""
    with pytest.raises(pu.UidInputError) as exc:
        pu.compute_uid("clause", source_pdf_sha256="", physical_page=1,
                       span_text=SPAN)
    assert "no fallback" in str(exc.value)


def test_a_missing_or_invalid_page_is_refused():
    for page in (0, -1, None, "12"):
        with pytest.raises(pu.UidInputError):
            pu.compute_uid("clause", source_pdf_sha256=PDF,
                           physical_page=page, span_text=SPAN)


def test_an_unknown_kind_is_refused():
    with pytest.raises(pu.UidInputError):
        pu.compute_uid("whatever", source_pdf_sha256=PDF, physical_page=1,
                       span_text=SPAN)


# --- verification ----------------------------------------------------------

def test_a_submitted_uid_is_recomputed_not_trusted():
    identity = {"source_pdf_sha256": PDF, "physical_page": 12,
                "span_text": SPAN}
    good = pu.compute_uid("clause", **identity)
    assert pu.verify_uid(good, "clause", **identity) == []
    errors = pu.verify_uid("PC-1111111111111111", "clause", **identity)
    assert any("not the canonical identifier" in e for e in errors), errors


def test_unverifiable_identity_is_an_error_not_a_pass():
    """'I could not check' must never read as 'checked and clean' -- the
    SourceUnavailable posture from the cross-contract checks."""
    errors = pu.verify_uid("PC-1111111111111111", "clause",
                           source_pdf_sha256="", physical_page=1,
                           span_text=SPAN)
    assert errors and "cannot be verified" in errors[0]
