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

Silence is a first-class answer here: `found: false` with no value is what a
document that does not mention a field is supposed to produce, and the caller
turns that into `unavailable`/`not_mentioned` rather than a guess.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

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
                    "found": {"type": "boolean"},
                    "value": {},
                    "page": {"type": ["integer", "null"], "minimum": 1},
                    "quote": {"type": ["string", "null"]},
                    "complete": {"type": "boolean"},
                    "unambiguous": {"type": "boolean"},
                },
                "required": ["found"],
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

1. A field is found ONLY if this document states it. If the document does not
   mention it, return {{"found": false}}. Silence is a correct answer and is
   recorded as such -- do not guess, and do not carry a value over from your
   general knowledge.
2. Every found field needs `page` and `quote`. The quote must appear on that
   page CHARACTER FOR CHARACTER. Do not normalise spacing, fix a typo, expand
   an abbreviation, or translate. If you cannot reproduce it exactly, return
   {{"found": false}}.
3. Never infer the accident narrative from clinical content. A diagnosis, an
   injured body part, or an operation name does NOT establish how the injury
   happened. Accident circumstances come only from a record that states them:
   {", ".join(ACCIDENT_SOURCE_ORDER)}.
4. `complete` is false when the document states only part of the value.
   `unambiguous` is false when the text could support more than one reading.
   Both default to true; set them false rather than picking one reading.
5. A statement that something is NOT present ("골절 소견 없음") is not a value
   for that field. Return {{"found": false}} and let the caller handle it -- do
   not encode the absence as a value.

Return one JSON object keyed by field id, and nothing else.

Document text:
{body}
"""


def parse_result(
    structured: Mapping[str, Any] | None,
    field_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict]:
    """Normalise one document read into {field_id: {value, page, quote, ...}}.

    Anything that does not carry a value, a page, and a quote is dropped
    outright: the caller verifies quotes against the served page text and a
    partial answer cannot be verified. Dropping is safe -- the field simply
    stays unresolved -- while keeping it would put an uncitable value into a
    contract whose whole basis is citation.
    """
    if not structured:
        return {}
    known = {row["field_id"] for row in field_rows}
    parsed: dict[str, dict] = {}
    for field_id, payload in structured.items():
        if field_id not in known or not isinstance(payload, dict):
            continue
        if not payload.get("found"):
            continue
        value, page, quote = (payload.get("value"), payload.get("page"),
                              payload.get("quote"))
        if value is None or not isinstance(page, int) or not isinstance(quote, str):
            continue
        if not quote.strip():
            continue
        parsed[field_id] = {
            "value": value,
            "page": page,
            "quote": quote,
            "complete": bool(payload.get("complete", True)),
            "unambiguous": bool(payload.get("unambiguous", True)),
        }
    return parsed


def make_reader(provider, pages_by_document: Mapping[str, Sequence[Mapping[str, Any]]]):
    """An `extract(document_id, kind, field_rows)` bound to a real provider.

    Returned as a closure so the driver's ordering logic stays testable with a
    plain function in place of a provider.
    """

    def extract(document_id: str, kind: str | None,
                field_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
        pages = pages_by_document.get(document_id) or []
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
