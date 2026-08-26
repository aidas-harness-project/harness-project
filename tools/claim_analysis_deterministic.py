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
    r"노동\s*능력\s*상실\s*[율률]\s*\(\s*%\s*\)\s*[:|]\s*(?P<value>\d{1,3})\s*%?"
)

# The SECOND printed shape for the same field, and the reason the sweep in this
# module's docstring had to be re-run before adding it.
#
# A 후유장해진단서 written against the insurer's 장해분류표 rather than McBride
# prints the rate inside the classification item itself:
#
#     한 다리의 3대관절 중 1관절의 기능에 뚜렷한 장해를 남긴 때 (지급율 10%)
#     척추에 뚜렷한 기형을 남긴때 (지급율 30%)
#
# That is still a printed form value -- the item and its rate are both the
# 약관's text, quoted by the physician who ticked it -- so it satisfies the
# label-is-printed rule. What it does NOT satisfy on its own is the sweep,
# because the SAME phrasing appears in the policy itself, where a clause uses a
# rate as an EXAMPLE about a hypothetical person:
#
#     보험가입 전 한 팔의 손목관절에 심한 장해(지급률 20%)가 있었던 피보험자가...
#
# Reading that as this claimant's rate would be a fabrication of exactly the
# kind P1 exists to prevent.
#
# The two are separated by the publisher's own SPELLING, which is why the
# pattern is anchored on 율 and not `[율률]`. Swept across all 56,540 processed
# pages (2026-08-26): 134 `지급율` occurrences, every one in a certificate
# context, and 12 `지급률` occurrences, every one in a policy clause. Zero
# crossover in either direction. Widening this to `[율률]` re-admits all 12
# policy hits, so the narrow spelling is load-bearing rather than incidental.
#
# Ordered AFTER the McBride pattern in `_rate`: where a form prints both, the
# 노동능력상실율 line is the assessment the physician made, while the 장해분류표
# item is the policy category it was mapped onto.
_DISABILITY_RATE_PAYOUT = re.compile(
    r"[（(]\s*지\s*급\s*율\s*(?P<value>\d{1,3}(?:\.\d+)?)\s*%\s*[)）]"
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


# --- 확진구분 (진단서) -------------------------------------------------
# The form prints BOTH options and marks one. Which glyph means "checked"
# varies by publisher, so the two sets are enumerated from the corpus rather
# than guessed -- surveyed across all 29 processed 진단서 (2026-08-26):
#   checked:   ◉ (13)  ☑ (9)  ✔ (2)  [√] / [ √ ] (2)
#   unchecked: ○ (14)  ☐ (2)  □ (1)  [ ] / [  ] (4)
# Reading only the checked glyph is what makes this a lookup: `○ 임상적 추정`
# beside `◉ 최 종 진 단` states 최종진단, and a pattern that ignored the mark
# would report whichever label happened to come first.
#
# 최 종 진 단 prints with per-character spacing on several publishers' forms,
# so every label allows internal whitespace.
_CHECKED = r"[◉☑✔✓■●]|\[\s*[√✓vVxX]\s*\]"
_CERTAINTY = re.compile(
    r"(?:" + _CHECKED + r")\s*"
    r"(임\s*상\s*적\s*추\s*정|최\s*종\s*진\s*단)"
)

# --- 수술명 (수술기록지) -----------------------------------------------
# Two printed shapes, both from the corpus (24 processed 수술기록지):
#   `수술명 :` / `수술명:` / `수 술 명 :`  -- value follows on the same line
#   `수술명` alone                          -- value opens the NEXT line
# A THIRD shape is deliberately excluded: a 수술코드/수술명 TABLE HEADER, where
# the value sits in a body row under the header rather than after the label.
# Reading that with a label anchor would return the next header cell, so the
# header form is detected and refused -- the model reads those.
_SURGERY_NAME_INLINE = re.compile(
    r"^[|\s]*수\s*술\s*명\s*[:：]\s*(?P<value>[^\n|]+?)\s*\|?\s*$", re.MULTILINE)
_SURGERY_NAME_NEXTLINE = re.compile(
    r"^[|\s]*수\s*술\s*명\s*$\n(?P<value>[^\n|]+?)\s*$", re.MULTILINE)
# A header row names OTHER columns beside 수술명; a value line never does.
_SURGERY_TABLE_HEADER = re.compile(r"수술\s*코드|진단\s*코드")


# --- 장해상병명 (후유장해진단서) ---------------------------------------
# Surveyed across all 26 processed 후유장해진단서 (2026-08-26). Three printed
# shapes, all label-anchored:
#   `상병명 | S6280 : 손목...골절, 폐쇄성`       value in the next table cell
#   `상병명` then the value on the FOLLOWING line
#   `장해상병명 | 좌측 손목 원위요골 골절 | 부상 (발병)일 | ...`
# The third continues with ANOTHER label in the same row, so the value is cut
# at the next `|` rather than run to end of line.
#
# `상병명(상병명이 많을 때에는 ...)` is the form's own INSTRUCTION line, not a
# value, and is excluded: the parenthetical is what distinguishes it.
_DISABILITY_DIAGNOSIS_INLINE = re.compile(
    r"^[|\s]*(?:장해)?상\s*병\s*명\s*(?!\()[:：|]\s*(?P<value>[^\n|]{3,}?)\s*(?:\||$)",
    re.MULTILINE)
_DISABILITY_DIAGNOSIS_NEXTLINE = re.compile(
    r"^[|\s]*(?:장해)?상\s*병\s*명\s*(?!\()[:：|]?\s*$\n(?P<value>[^\n|]{3,}?)\s*$",
    re.MULTILINE)

# --- 수술전/후 진단명 (수술기록지) --------------------------------------
# The 수술기록지 prints both; the POST-operative name is the settled one (the
# pre-operative name is what was suspected going in), so it is read first and
# the pre-operative name is the fallback. 21 of 25 processed 수술기록지 carry
# them, in `수술후 진단명 :` and label-then-next-line shapes alike.
_POSTOP_DIAGNOSIS = re.compile(
    r"^[|\s]*수술\s*후\s*진단명\s*[:：|]?\s*(?P<value>[^\n|]{3,}?)\s*\|?\s*$"
    r"|^[|\s]*수술\s*후\s*진단명\s*$\n(?P<value2>[^\n|]{3,}?)\s*$", re.MULTILINE)

# --- 영구/한시 장해 유형 (후유장해진단서) --------------------------------
# Two printed shapes: the table cell `비고사항 (영구/한시) | 영구` that
# `_DISABILITY_DURATION` already anchors on, and a free-standing sentence
# `본 장해는 영구 장해임`. The sentence form is included because the publisher
# still prints 영구/한시 as the closed pair -- the writer picks one, not the
# wording.
_DISABILITY_TYPE = re.compile(
    r"비고\s*사항\s*\(\s*영구\s*/\s*한시\s*\)\s*[:|]\s*(?P<value>영구|한시)"
    r"|본\s*장해는\s*(?P<value2>영구|한시)\s*장해")


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


def _percent(text: str) -> int | float:
    """The printed percentage as a number, keeping the form's own precision.

    `13` stays an int rather than becoming `13.0`: the screening report renders
    the value with `str()`, so a float would print `13.0%` where the form
    printed `13%` -- a decimal the document never stated. Only a rate the form
    actually wrote with a decimal keeps one.
    """
    value = float(text)
    return int(value) if value.is_integer() and "." not in text else value


def _rate(pages):
    return _read_alternatives(
        pages, (_DISABILITY_RATE, _DISABILITY_RATE_PAYOUT), cast=_percent)


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


def _certainty(pages):
    """확진 or 임상적 추정, read from which box the form actually marks.

    `_match_on_pages` refuses the document when two DIFFERENT values are
    marked, which on this form means the publisher checked both boxes -- a
    real contradiction the model and the consistency stage should see, not
    something a rule may silently resolve by taking the first.
    """
    hit = _match_on_pages(_CERTAINTY, pages)
    if not hit:
        return None
    page_no, match = hit
    text = next(p["text"] for p in pages if p.get("page") == page_no)
    # Normalise the per-character spacing the form prints: `최 종 진 단`.
    value = re.sub(r"\s+", "", match.group(1))
    return _assert(value, page_no, _quote_for(match, text))


# WITHDRAWN 2026-08-26 -- kept for the survey in its docstring, no longer
# in RULES. The 수술명 label does not introduce a single-line value: of 44
# corpus occurrences only 4 (9%) carry one line, 38 (86%) carry two or more,
# and this reader took the first and dropped the rest. Every reading it
# published resolved to `conflict` against a fuller model reading (5 of 5:
# CASE_7015/7044/7046/9415/9418) -- on CASE_7044 the dropped line began with
# `ㄴ`, a continuation marker detailing the SAME operation, so the pipeline
# reported agreement as contradiction. Widening the capture does not fix it:
# the lines below the label are not uniformly surgery names (a diagnosis 16x,
# bare form labels 8x each), so capturing them all would invent a surgery out
# of a diagnosis. Telling those apart is judgement, not a label lookup.
def _surgery_name(pages):
    """The 수술명 line's value, from either printed shape.

    Returns None on a 수술코드/진단코드 table header, where the label names a
    COLUMN rather than introducing a value.
    """
    for pattern in (_SURGERY_NAME_INLINE, _SURGERY_NAME_NEXTLINE):
        hits: list[tuple[int, re.Match[str]]] = []
        for page in pages:
            text, page_no = page.get("text"), page.get("page")
            if not isinstance(text, str) or not isinstance(page_no, int):
                continue
            for match in pattern.finditer(text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.start())
                line = text[line_start:line_end if line_end > 0 else len(text)]
                if _SURGERY_TABLE_HEADER.search(line):
                    continue          # a column header, not a label
                hits.append((page_no, match))
        if not hits:
            continue
        values = {re.sub(r"\s+", " ", h[1].group("value")).strip() for h in hits}
        if len(values) > 1:
            return None               # two different 수술명 -- the model reads it
        page_no, match = hits[0]
        value = re.sub(r"\s+", " ", match.group("value")).strip()
        if not value:
            continue
        text = next(p["text"] for p in pages if p.get("page") == page_no)
        return _assert(value, page_no, _quote_for(match, text))
    return None


def _first_named_group(match: re.Match[str]) -> str | None:
    """The first non-empty capture among `value`/`value2`.

    Patterns that cover two printed shapes use a second alternative, so only
    one group is populated per match.
    """
    for name in ("value", "value2"):
        try:
            got = match.group(name)
        except (IndexError, re.error):
            # A pattern that declares only `value` has no `value2` group.
            continue
        if got and got.strip():
            return re.sub(r"\s+", " ", got).strip()
    return None


def _read_alternatives(pages, patterns, cast=None):
    """The first pattern that yields ONE consistent value across the document.

    Ordered: an earlier pattern is the more authoritative printed shape. Two
    different values from the same pattern make the read ambiguous and hand the
    field back to the model, exactly as `_match_on_pages` does.

    `cast` converts the captured text before it is asserted, for a field whose
    `value_shape` is a number rather than text. It runs AFTER the
    one-consistent-value check, so two spellings of the same number are still
    two values here -- the check is on what the form printed, not on what the
    figure means.
    """
    for pattern in patterns:
        hits = []
        for page in pages:
            text, page_no = page.get("text"), page.get("page")
            if not isinstance(text, str) or not isinstance(page_no, int):
                continue
            for match in pattern.finditer(text):
                value = _first_named_group(match)
                if value:
                    hits.append((page_no, match, value))
        if not hits:
            continue
        if len({h[2] for h in hits}) > 1:
            return None
        page_no, match, value = hits[0]
        text = next(p["text"] for p in pages if p.get("page") == page_no)
        if cast is not None:
            try:
                value = cast(value)
            except (TypeError, ValueError):
                # A capture the declared shape cannot represent is not repaired
                # into one; the field falls through to the model exactly as an
                # unmatched pattern does.
                continue
        return _assert(value, page_no, _quote_for(match, text))
    return None


def _disability_diagnosis(pages):
    return _read_alternatives(
        pages, (_DISABILITY_DIAGNOSIS_INLINE, _DISABILITY_DIAGNOSIS_NEXTLINE))


def _postop_diagnosis(pages):
    return _read_alternatives(pages, (_POSTOP_DIAGNOSIS,))


def _disability_type(pages):
    return _read_alternatives(pages, (_DISABILITY_TYPE,))


# field_id -> reader. Keyed by field so the driver can subtract exactly what a
# rule settled from what it still has to ask the model about.
RULES = {
    "documented_disability_rate": _rate,
    "documented_disability_duration": _duration,
    "documented_disability_standard": _standard,
    "diagnostic_certainty": _certainty,
    "disability_related_diagnosis": _disability_diagnosis,
    "primary_diagnosis": _postop_diagnosis,
    "disability_type": _disability_type,
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
