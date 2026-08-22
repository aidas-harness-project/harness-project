"""Rule-first extraction for fields whose Korean form prints a fixed label.

The provider half of this stage (`claim_analysis_extraction`) asks a model to
read a page and report field values. That is the right tool for a narrative --
사고 경위 is stated differently in every record -- and the wrong one for a
statutory form, where the label is printed by the publisher and does not vary.
A 후유장애 진단서(Mc Bride) prints `노동능력상실율(%)` because the 별지 서식 says
it must; reading that line is a lookup, not a judgement.

Asking a model to perform a lookup costs a call and, worse, is not repeatable.
Measured on two runs of the SAME source document (CASE_705/DOC_013 and
CASE_713/DOC_012, identical but for markdown pipe framing): CASE_705 returned
13 / 영구 / 맥브라이드식 장애평가 and CASE_713 returned `unavailable` for all
three, while `disability_type` and `joint_range_of_motion` -- on the same page,
in the same call -- came back correctly on both. Nothing in the text explains
the difference. A regex does not have that failure mode.

Three rules govern what may live here, and they are what keep this module from
growing into a second, worse extractor:

* **The label must be printed by the form, not chosen by the writer.** A field
  whose value is phrased freely (`accident_mechanism`, `primary_diagnosis`)
  stays with the model. When the anchor is statutory the value space is
  usually closed too, which is what makes a short pattern honest here.
* **The quote must be verbatim and locatable.** Every value carries the exact
  substring it came from, so the driver's existing `locate_exact` gate applies
  unchanged. A rule that cannot cite is dropped exactly like a model reading
  that cannot cite -- `build_reference` is not relaxed for a deterministic
  source.
* **Silence stays silence.** A rule that does not match returns nothing and the
  field falls through to the provider. It never emits `not_mentioned`, because
  a rule's failure to match is not evidence the document said nothing -- only a
  reader that considered the whole page can say that.

What this module deliberately does NOT do is decide a stated absence. A
`explicitly_absent` verdict rests on reading a sentence that denies the thing,
and pattern-matching a negation ("수술 시행하지 않음" vs "수술 부위 감염 없음")
is exactly the judgement the model is better at. Rules assert or stay quiet.

The failure a unit test cannot show is a pattern firing where it should not --
a 소견서 quoting a rate in prose, a policy clause defining how one is
calculated. Swept across all 2416 processed documents in `data/processed`
(2026-08-22): 133 documents matched, every one of them the 후유장애
진단서(Mc Bride) page or the unsplit bundle still containing it. Nothing fired
in 511 insurance_policy, 352 receipt, 318 medical_record, 172 imaging_report or
123 insurer_response documents. Re-run that sweep before widening any pattern
here -- the label anchor is what keeps the rules this quiet, and a pattern
loosened to catch one more form is exactly how that stops being true.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

RULE_VERSION = "claim_analysis_deterministic.v0.1"

# A form line reads `노동능력상실율(%):13%` or, in a table row, `노동능력 상실율(%)
# | 13 |`. The label carries optional internal spacing (OCR of a form splits
# 노동능력 상실율 across a cell boundary) and the separator is `:` or `|`,
# because the same document prints both -- verified on CASE_705/DOC_013, whose
# page holds the colon form on one line and the pipe form on another.
_DISABILITY_RATE = re.compile(
    r"노동\s*능력\s*상실\s*[율률]\s*\(\s*%\s*\)\s*[:|]\s*(\d{1,3})\s*%?"
)

# `비고사항 (영구/한시) | 영구`. The parenthetical names both options, so the
# captured token must be read AFTER it -- a pattern anchored on the bare word
# 영구 would match inside the label itself and report a value the form never
# stated.
_DISABILITY_DURATION = re.compile(
    r"비고\s*사항\s*\(\s*영구\s*/\s*한시\s*\)\s*[:|]\s*(영구|한시)"
)

# The assessment standard is printed as a bracketed section heading. Both
# spellings occur (장애/장해 differ by publisher), and the heading is the whole
# citable unit -- the value IS the heading text, so the pattern captures it
# rather than a token inside it.
_DISABILITY_STANDARD = re.compile(
    r"\[\s*(맥브라이드식\s*장[애해]\s*평가|AMA\s*식?\s*장[애해]\s*평가"
    r"|국가배상법\s*시행령\s*장[애해]\s*평가)\s*\]"
)


def _match_on_pages(
    pattern: re.Pattern[str],
    pages: Sequence[Mapping[str, Any]],
) -> tuple[int, re.Match[str]] | None:
    """The first (page, match) this pattern produces, scanning in page order.

    Only the FIRST match is returned, and a second match anywhere in the
    document makes the read ambiguous rather than doubly confirmed: a form
    restating a rate in a later 소견 line is the ordinary case, but two DIFFERENT
    printed rates is a real conflict that belongs to the model and the
    consistency stage, not to a rule that would silently take the earlier one.
    """
    hits: list[tuple[int, re.Match[str]]] = []
    for page in pages:
        text = page.get("text")
        if not isinstance(text, str):
            continue
        page_no = page.get("page")
        if not isinstance(page_no, int):
            continue
        for match in pattern.finditer(text):
            hits.append((page_no, match))
    if not hits:
        return None
    values = {hit[1].group(1).strip() for hit in hits}
    if len(values) > 1:
        # Conflicting printed values -- defer to the provider, which can read
        # the surrounding sentences and say which one the form means.
        return None
    return hits[0]


def _quote_for(match: re.Match[str], text: str) -> str:
    """The verbatim line the match sits on.

    A quote must be checkable by a human, so it is the printed line rather than
    the regex's own span: `13` alone proves nothing, while
    `노동능력상실율(%):13%` shows the label that gives the number its meaning.
    Line bounds are used because `locate_exact` requires the quote to occur
    exactly once, and a full line is far likelier to be unique than a fragment.
    """
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def _assert(value: Any, page: int, quote: str) -> dict[str, Any]:
    """One deterministic reading, in the shape `parse_result` publishes.

    `complete`/`unambiguous` are true by construction: the rule fired on a
    printed label and refused the ambiguous case above, so there is no partial
    reading left to flag.
    """
    return {
        "presence": "asserted",
        "value": value,
        "page": page,
        "quote": quote,
        "complete": True,
        "unambiguous": True,
        "extraction_method": "deterministic_label_anchor",
        "rule_version": RULE_VERSION,
    }


def _rate(pages):
    hit = _match_on_pages(_DISABILITY_RATE, pages)
    if not hit:
        return None
    page_no, match = hit
    text = next(p["text"] for p in pages if p.get("page") == page_no)
    return _assert(int(match.group(1)), page_no, _quote_for(match, text))


def _duration(pages):
    hit = _match_on_pages(_DISABILITY_DURATION, pages)
    if not hit:
        return None
    page_no, match = hit
    text = next(p["text"] for p in pages if p.get("page") == page_no)
    return _assert(match.group(1), page_no, _quote_for(match, text))


def _standard(pages):
    hit = _match_on_pages(_DISABILITY_STANDARD, pages)
    if not hit:
        return None
    page_no, match = hit
    text = next(p["text"] for p in pages if p.get("page") == page_no)
    return _assert(match.group(1).strip(), page_no, _quote_for(match, text))


# field_id -> reader. Keyed by field so the driver can subtract exactly what a
# rule settled from what it still has to ask the model about.
RULES = {
    "documented_disability_rate": _rate,
    "documented_disability_duration": _duration,
    "documented_disability_standard": _standard,
}


def extract(
    pages: Sequence[Mapping[str, Any]],
    field_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict]:
    """Every requested field this document settles deterministically.

    Returns only what matched. A field with no rule, or a rule that did not
    fire, is simply absent from the result and remains the provider's job --
    this is a pre-pass, never a replacement.
    """
    if not pages or not field_rows:
        return {}
    settled: dict[str, dict] = {}
    for row in field_rows:
        field_id = row.get("field_id")
        rule = RULES.get(field_id)
        if rule is None:
            continue
        try:
            found = rule(pages)
        except (KeyError, StopIteration, ValueError):
            # A malformed page structure is the provider's problem to absorb,
            # not a reason to fail the stage: fall through to the model.
            continue
        if found is not None:
            settled[field_id] = found
    return settled
