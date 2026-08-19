"""The provider half of selective Claim Analysis: one document, many fields.

A single call per document asks for every field that document is on the ladder
for. That is the shape the read budget requires -- one provider call per
document per run -- and it is also the shape that matches how a document reads:
a 진단서 states the diagnosis, its date, its site, and the department in one
place, so asking for them separately pays four times for one page.

Two things this module refuses to do:

* **Infer.** The prompt forbids deriving the accident narrative from the
  diagnosis, the injured part, or the operation name. A fracture of the right
  radius does not say how it happened, and a model asked to fill an empty field
  will supply the most probable story rather than the recorded one.
* **Repair.** A quote that does not appear verbatim on the page it names is
  dropped by the caller, never corrected. The exact range is what makes the
  value checkable, so a quote that cannot be located has no standing.

Silence is a first-class answer here, and so is a stated absence -- they are
different answers. `not_mentioned` means the document said nothing and produces
no observation at all; `explicitly_absent` means the document said the thing is
NOT there, which is a finding and carries the sentence that states it. Folding
the second into the first (as an earlier revision did, by returning
`found: false` for both) makes a recorded negative finding indistinguishable
from a gap in the records.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

PROMPT_VERSION = "claim_analysis_selective_extraction.v0.1"

# The accident narrative comes from the record that states it, in this order.
# Listed in the prompt so a model reading a diagnosis certificate knows the
# 사고 경위 line there is a source, while the diagnosis name is not.
ACCIDENT_SOURCE_ORDER = (
    "접수 사고내용",
    "초진기록",
    "응급실기록",
    "사고 경위가 직접 적힌 진단서",
    "사고 경위가 직접 적힌 기타 기록",
)

_VALUE_SHAPE_HINT = {
    "date": "an ISO date (YYYY-MM-DD) exactly as the document states it",
    "text": "a short verbatim phrase from the document",
    "text_list": "a JSON array of short verbatim phrases",
    "boolean": "true or false",
    "enum": "one short token",
    "code": "the code exactly as printed",
    "number": "a number",
    "period": 'an object {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD or null"}',
}


def output_schema(field_rows: Sequence[Mapping[str, Any]]) -> dict:
    """The structured shape one document read must return."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            row["field_id"]: {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "presence": {
                        "enum": ["asserted", "explicitly_absent", "not_mentioned"],
                    },
                    "value": {},
                    "page": {"type": ["integer", "null"], "minimum": 1},
                    "quote": {"type": ["string", "null"]},
                    "reason": {"type": ["string", "null"]},
                    "complete": {"type": "boolean"},
                    "unambiguous": {"type": "boolean"},
                },
                "required": ["presence"],
            }
            for row in field_rows
        },
        "required": [row["field_id"] for row in field_rows],
    }


def build_prompt(
    *,
    document_id: str,
    document_kind: str | None,
    pages: Sequence[Mapping[str, Any]],
    field_rows: Sequence[Mapping[str, Any]],
) -> str:
    """One prompt covering every field this document is responsible for."""
    field_lines = []
    for row in field_rows:
        hint = _VALUE_SHAPE_HINT.get(row.get("value_shape"), "a short phrase")
        line = f'- "{row["field_id"]}" ({row.get("label", row["field_id"])}): {hint}'
        if row.get("notes"):
            line += f"\n    NOTE: {row['notes']}"
        field_lines.append(line)

    body = "\n\n".join(
        f"<<<PAGE page={page['page']}>>>\n{page['text']}" for page in pages
    )

    return f"""You are reading ONE document from a Korean insurance claim file and
extracting only the fields listed below.

Document: {document_id} (form kind: {document_kind or "unknown"})

Extract exactly these fields:
{chr(10).join(field_lines)}

Rules, all of which matter more than filling the fields in:

1. Every field gets a `presence` verdict, and the three are different facts:
   - "asserted"          this document states a value for the field.
   - "explicitly_absent" this document states the thing is NOT present --
                         "골절 소견 없음", "특이소견 없음", "수술 시행하지 않음".
                         This is a FINDING, not a blank.
   - "not_mentioned"     this document simply does not discuss the field.
   Do not guess, and do not carry a value over from general knowledge.
2. "asserted" needs `value`, `page`, and `quote`. "explicitly_absent" needs
   `page` and `quote` too -- the quote is the sentence stating the absence --
   plus a short `reason`; it carries NO `value`. "not_mentioned" carries none
   of them: there is nothing to cite when a document says nothing.
3. Every quote must appear on the page you name CHARACTER FOR CHARACTER. Do not
   normalise spacing, fix a typo, expand an abbreviation, or translate. If you
   cannot reproduce it exactly, downgrade to "not_mentioned" rather than
   supplying an approximate quote.
4. Never infer the accident narrative from clinical content. A diagnosis, an
   injured body part, or an operation name does NOT establish how the injury
   happened. Accident circumstances come only from a record that states them:
   접수 사고내용, 초진기록, 응급실기록, 사고 경위가 직접 적힌 진단서, 사고 경위가 직접 적힌 기타 기록.
5. `complete` is false when the document states only part of the value.
   `unambiguous` is false when the text could support more than one reading.
   Both default to true; set them false rather than picking one reading.
6. A stated absence is NOT the boolean false. For a yes/no field, "수술을
   시행하지 않았다" is `explicitly_absent` with that sentence quoted -- not
   `asserted` with `value: false`. The distinction is what lets a reader tell a
   recorded negative finding from a value someone computed.

Return one JSON object keyed by field id, and nothing else.

Document text:
{body}
"""


def parse_result(
    structured: Mapping[str, Any] | None,
    field_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict]:
    """Normalise one document read into {field_id: {presence, ...}}.

    Three outcomes survive, and they are not interchangeable:

    * `asserted` -- a value with a citable page and quote.
    * `explicitly_absent` -- no value, but the sentence stating the absence is
      cited. The caller records it as a real observation; it does not satisfy
      the field's search, because a later source may still assert a value.
    * dropped -- `not_mentioned`, or a verdict missing what its own presence
      requires. Silence produces nothing at all, and the caller turns the
      absence of any reading into `unavailable`/`not_mentioned`.

    Dropping the malformed cases is safe -- the field stays unresolved -- while
    keeping them would put an uncitable claim into a contract whose entire
    basis is citation.
    """
    if not structured:
        return {}
    known = {row["field_id"] for row in field_rows}
    parsed: dict[str, dict] = {}
    for field_id, payload in structured.items():
        if field_id not in known or not isinstance(payload, dict):
            continue
        presence = payload.get("presence")
        # Back-compatible with the older boolean shape, so an in-flight
        # provider result is read rather than silently discarded.
        if presence is None and "found" in payload:
            presence = "asserted" if payload.get("found") else "not_mentioned"
        if presence not in {"asserted", "explicitly_absent"}:
            continue

        page, quote = payload.get("page"), payload.get("quote")
        if not isinstance(page, int) or not isinstance(quote, str) or not quote.strip():
            continue

        if presence == "explicitly_absent":
            parsed[field_id] = {
                "presence": "explicitly_absent",
                "page": page,
                "quote": quote,
                "reason": (payload.get("reason")
                           or "the source states this is not present"),
            }
            continue

        value = payload.get("value")
        if value is None:
            continue
        parsed[field_id] = {
            "presence": "asserted",
            "value": value,
            "page": page,
            "quote": quote,
            "complete": bool(payload.get("complete", True)),
            "unambiguous": bool(payload.get("unambiguous", True)),
        }
    return parsed


def make_reader(
    provider,
    pages_by_document: (
        Mapping[str, Sequence[Mapping[str, Any]]]
        | Callable[[str], Sequence[Mapping[str, Any]]]
    ),
):
    """An `extract(document_id, kind, field_rows)` bound to a real provider.

    Returned as a closure so the driver's ordering logic stays testable with a
    plain function in place of a provider.
    """

    def extract(document_id: str, kind: str | None,
                field_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
        pages = (
            pages_by_document(document_id)
            if callable(pages_by_document)
            else pages_by_document.get(document_id)
        ) or []
        if not pages or not field_rows:
            return {}
        prompt = build_prompt(document_id=document_id, document_kind=kind,
                              pages=pages, field_rows=field_rows)
        result = provider.analyze_text_structured(
            prompt, PROMPT_VERSION, output_schema(field_rows))
        structured = result.structured_output
        if structured is None and result.text:
            try:
                structured = json.loads(result.text)
            except json.JSONDecodeError:
                structured = None
        return parse_result(structured, field_rows)

    return extract
