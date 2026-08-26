"""The prompt must tell the model what `complete`/`unambiguous: false` COSTS.

`is_trusted_value` requires all four of from_priority_source, has_exact_quote,
complete and unambiguous. A reading failing either flag is not published: the
field reports that no trusted value was established, and the quote reaches a
human only as an item to go and check.

The prompt did not say that. Rule 5 read, in full:

    `complete` is false when the document states only part of the value.
    `unambiguous` is false when the text could support more than one reading.
    Both default to true; set them false rather than picking one reading.

"set them false rather than picking one reading" instructs caution and reads as
a low-cost hedge -- flag your uncertainty, someone downstream will weigh it.
Nothing said the value is DISCARDED. So the model hedged, honestly and often,
on pages that were not actually unsettled.

Measured on the CASE_7* corpus (2026-08-26), 72 fields published
`partial_reading_only`, and splitting them by whether the values actually
compete:

    55  ONE value only -- nothing to be ambiguous between
    12  multi-cardinality, several values that can coexist
     5  genuinely competing (single-cardinality, >1 distinct value)

Checked against source text, three of the four decisive examples were flags the
page does not support:

  * CASE_7015 `documented_disability_duration` -- the source prints "본 장해는
    영구 장해임", a complete sentence. Flagged `complete: false`, apparently
    because the 후유장해진단서 form has no 영구/한시 checkbox and the physician
    wrote it in the 소견 block instead.
  * CASE_7002 `diagnosis_certificate_anchor` -- "병 명 | 우측 견봉돌기의 골절,
    폐쇄성", a printed label with one value under it. Flagged
    `unambiguous: false` with nothing on the page offering a second reading.
  * CASE_7001 `diagnosis_code` -- five codes (S32020, S335, M4316, M511,
    M4806) listed under 질병분류기호 with no 주상병 marked. This one is
    GENUINELY ambiguous, and the flag is right. It is what the flags are for.

These tests pin the guidance rather than any wording, because the failure was
not that rule 5 was wrong -- it was that rule 5 was silent about the
consequence, and silence is what a test can check for.
"""
from __future__ import annotations

import claim_analysis_extraction as extraction
import claim_analysis_selection as selection


FIELD_ROWS = [{"field_id": "primary_diagnosis", "value_shape": "text",
               "label": "Primary diagnosis", "label_ko": "주요 진단명"}]
PAGES = [{"page": 1, "text": "병 명 | 우측 견봉돌기의 골절, 폐쇄성"}]


def _prompt() -> str:
    return extraction.build_prompt(
        document_id="DOC_002", document_kind="diagnosis_certificate",
        document_type="diagnosis_certificate", pages=PAGES,
        field_rows=FIELD_ROWS)


def test_the_flags_really_do_discard_the_value() -> None:
    """The premise. If this stops holding, the guidance below is wrong and
    should be rewritten rather than kept passing."""
    common = dict(from_priority_source=True, has_exact_quote=True)
    assert selection.is_trusted_value(**common, complete=True, unambiguous=True)
    assert not selection.is_trusted_value(**common, complete=False,
                                          unambiguous=True)
    assert not selection.is_trusted_value(**common, complete=True,
                                          unambiguous=False)


def test_the_prompt_states_the_consequence_of_flagging() -> None:
    """The actual defect: the model was asked to flag uncertainty without being
    told the value would then be withheld."""
    prompt = _prompt()
    assert "NOT published" in prompt or "not published" in prompt, (
        "rule 5 does not tell the model that a flagged value is discarded, so "
        "flagging reads as a free hedge")


def test_the_prompt_scopes_ambiguity_to_the_page_being_read() -> None:
    """A reader of ONE document cannot know another document disagrees, and
    treating that possibility as ambiguity is what produced 55 suppressed
    fields holding a single uncontested value. Cross-document comparison is
    consistency_check's job, not this call's."""
    prompt = _prompt()
    assert "THIS PAGE" in prompt, (
        "ambiguity must be scoped to the page in hand")
    assert "comparing across documents" in prompt


def test_the_prompt_says_a_value_outside_its_form_field_is_still_complete() -> None:
    """CASE_7015: '본 장해는 영구 장해임' in the 소견 block was flagged incomplete
    because the form has no checkbox for it."""
    assert "본 장해는 영구 장해임" in _prompt()


def test_the_prompt_separates_wrong_field_from_ambiguous_value() -> None:
    """Being unsure the value belongs to this FIELD is not ambiguity about the
    VALUE. The right move is `not_mentioned`, not an asserted value carrying a
    flag that silently deletes it."""
    prompt = _prompt()
    assert "belongs to THIS FIELD" in prompt
    assert "not_mentioned" in prompt


def test_the_prompt_still_asks_for_the_flag_where_it_belongs() -> None:
    """The guidance must not read as 'stop flagging'. CASE_7001's five codes
    under 질병분류기호 with no 주상병 marked is exactly what the flag is for, and
    a prompt that discouraged it would trade one failure for its mirror."""
    prompt = _prompt()
    assert "질병분류기호" in prompt
    assert "unambiguous`: false" in prompt or "`unambiguous: false`" in prompt


def test_the_prompt_version_moved_with_the_wording() -> None:
    """`driver_runtime.receipt_matches` keys cached-unit reuse on this string,
    so a prompt change that did not move it would replay results produced under
    the old wording -- the change would appear to have been made and have no
    effect on a resumed run."""
    assert extraction.PROMPT_VERSION.endswith(".v0.2"), (
        f"rule 5 was rewritten on 2026-08-26; PROMPT_VERSION is "
        f"{extraction.PROMPT_VERSION!r}. Bump it when the prompt changes.")
