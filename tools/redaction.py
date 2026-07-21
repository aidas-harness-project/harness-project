"""Redaction abstraction for document-pipeline checkpoint 2.

`redact_document.py` talks only to a `Redactor`, never to a provider or a
prompt directly. That seam lets a future dedicated de-identification model
(e.g. an OpenMed NER pipeline -- open-decisions.md #1) drop in as a new
`Redactor` subclass without touching the tool: an NER redactor takes raw text
and returns entity spans, which is a different shape from an LLM that takes a
rendered prompt and returns rewritten text, but both produce a
`RedactionOutcome`.

Today the only implementation is `LlmRedactor`, which wraps any
`llm_providers` provider (`codex-cli` by default in dev). It owns the prompt
template and the JSON parsing that used to live in the tool.

Every outcome is checked for fidelity before the tool trusts it: an LLM asked
to rewrite a whole page can silently drop, reorder, or fabricate non-PII text
(the same failure class that contaminated the OCR layer -- known-gaps 11/14/15),
and unlike checkpoint 1 there is no second reader to catch it. `verify_fidelity`
enforces that everything BETWEEN the placeholders is a verbatim, in-order slice
of the source; any drift is surfaced as a warning and forces human review
rather than being trusted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


PROMPT_VERSION = "pii_redaction_v0.2"

# The eight stable placeholders the model may emit. Kept in sync with the prompt.
PLACEHOLDERS = (
    "[PERSON_NAME]", "[RESIDENT_REGISTRATION_NUMBER]", "[PHONE_NUMBER]",
    "[ADDRESS]", "[EMAIL]", "[ACCOUNT_NUMBER]", "[VEHICLE_NUMBER]", "[OTHER_PII]",
)
_PLACEHOLDER_RE = re.compile("|".join(re.escape(p) for p in PLACEHOLDERS))

# Redaction scope, settled after CASE_012/CASE_021 redacted the same content
# differently (document-pipeline.md). Carried in the prompt itself so the model
# actually sees it -- the agent spec's copy never reached the LLM before.
REDACTION_SCOPE = (
    "Redact every natural person's name regardless of capacity -- claimant, "
    "patient, physician, adjuster, insurer staff, and corporate signatories "
    "such as a 대표이사 (a CEO's name in an official document is still a natural "
    "person's name). Redact all phone/fax numbers, street addresses, email "
    "addresses, resident registration numbers, account numbers, vehicle "
    "registration numbers, and policy/certificate/license numbers, INCLUDING "
    "published corporate contact info (complaint-desk hotlines, published office "
    "addresses). Do NOT redact corporate or institutional entity names "
    "themselves (insurer names, hospital names) -- downstream stages key on "
    "them. Do NOT redact dates, monetary amounts, diagnoses, or policy clauses."
)

PROMPT_TEMPLATE = """Redact personally identifying information from the validated OCR text below.

{scope}

Replace each detected value with one of these stable placeholders:
[PERSON_NAME], [RESIDENT_REGISTRATION_NUMBER], [PHONE_NUMBER], [ADDRESS],
[EMAIL], [ACCOUNT_NUMBER], [VEHICLE_NUMBER], or [OTHER_PII].

Do not summarize, translate, correct, reorder, or omit non-PII content. Preserve
all line breaks and all non-PII text EXACTLY, character for character -- only the
PII values themselves may change (into a placeholder). Do not treat a value as
safe merely because it looks like a sample, alias, or pseudonym. A value is
already redacted only when it is one of the bracketed placeholders listed above.

For example, "홍길동 / 010-1234-5678 / 골절" must become
"[PERSON_NAME] / [PHONE_NUMBER] / 골절" with items_redacted=2.

Reply with ONLY one JSON object in this exact shape:
{{"redacted_text":"...", "items_redacted":0,
  "categories":["person_name"]}}

--- Validated page text ---
{text}
"""


@dataclass
class RedactionOutcome:
    redacted_text: str
    items_redacted: int
    categories: list[str]
    provider_metadata: dict[str, Any] | None = None
    # Non-empty when fidelity verification found drift (dropped/reordered/
    # fabricated non-PII text, or a placeholder-count mismatch). A non-empty
    # list forces human review -- the tool never trusts a drifting redaction.
    fidelity_warnings: list[str] = field(default_factory=list)
    # Reserved for a future span-based (NER) redactor; unused by LlmRedactor.
    spans: list[dict[str, Any]] | None = None


class Redactor(Protocol):
    method: str

    def redact_page(self, text: str) -> RedactionOutcome: ...


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def verify_fidelity(source_text: str, redacted_text: str, items_redacted: int) -> list[str]:
    """Confirm the redaction only substituted placeholders for PII and left
    everything else intact, in order. Returns a list of drift warnings (empty
    == clean).

    The check: split the redacted text on placeholder tokens; each literal chunk
    between placeholders must appear in the source, at or after the previous
    chunk's position (whitespace-normalized). A summarized, reordered, corrected,
    or fabricated page breaks this -- its between-placeholder text is no longer a
    verbatim in-order slice of the source. Also cross-checks the self-reported
    items_redacted against the actual placeholder count.

    Scope and limitation: this catches the dangerous direction -- text appearing
    in the redacted output that was NOT in the source (fabrication, reordering,
    rewriting), the same class that contaminated the OCR layer. It does NOT catch
    silent omission of non-PII content that sat between two placeholders, because
    the PII spans it was allowed to remove are unknown; a chunk that legitimately
    replaced PII is indistinguishable from one that also swallowed adjacent
    non-PII text. Omission is the less dangerous failure (lost info, not injected
    false info) and is left to human review of the redacted_text itself."""
    warnings: list[str] = []

    placeholder_count = len(_PLACEHOLDER_RE.findall(redacted_text))
    if placeholder_count != items_redacted:
        warnings.append(
            f"items_redacted={items_redacted} does not match the "
            f"{placeholder_count} placeholder(s) actually present in the redacted text"
        )

    haystack = _normalize_ws(source_text)
    cursor = 0
    for chunk in _PLACEHOLDER_RE.split(redacted_text):
        needle = _normalize_ws(chunk)
        if not needle:
            continue
        found = haystack.find(needle, cursor)
        if found == -1:
            preview = needle[:40] + ("..." if len(needle) > 40 else "")
            warnings.append(
                "redacted text contains non-PII content not found verbatim/in-order "
                f"in the source (possible drop, reorder, rewrite, or fabrication): {preview!r}"
            )
            # Keep scanning from where we were; report each drift chunk.
            continue
        cursor = found + len(needle)
    return warnings


class RedactionParseError(RuntimeError):
    """Raised when a redaction response cannot be parsed into the required shape."""


def parse_redaction_response(raw: str) -> dict:
    decoder = json.JSONDecoder()
    start = 0
    saw_brace = False
    while True:
        brace = raw.find("{", start)
        if brace == -1:
            msg = "redaction response was invalid JSON" if saw_brace else "redaction response was not JSON"
            raise RedactionParseError(f"{msg}: {raw!r}")
        saw_brace = True
        try:
            parsed, _ = decoder.raw_decode(raw[brace:])
            break
        except json.JSONDecodeError:
            start = brace + 1
            continue
    if not isinstance(parsed, dict):
        raise RedactionParseError(f"redaction response JSON must be an object: {raw!r}")
    if not isinstance(parsed.get("redacted_text"), str):
        raise RedactionParseError("redaction response omitted string redacted_text")
    if not isinstance(parsed.get("items_redacted"), int) or parsed["items_redacted"] < 0:
        raise RedactionParseError("redaction response items_redacted must be a non-negative integer")
    categories = parsed.get("categories", [])
    if not isinstance(categories, list) or any(not isinstance(item, str) for item in categories):
        raise RedactionParseError("redaction response categories must be an array of strings")
    parsed["categories"] = categories
    return parsed


class LlmRedactor:
    """Redactor backed by an llm_providers provider that rewrites the page."""

    method = "llm_redaction"

    def __init__(self, provider, *, scope: str = REDACTION_SCOPE):
        self.provider = provider
        self.scope = scope

    @property
    def label(self) -> str:
        return f"{self.method}:{self.provider.provider_name}:{self.provider.model_name}"

    def redact_page(self, text: str) -> RedactionOutcome:
        prompt = PROMPT_TEMPLATE.format(scope=self.scope, text=text)
        result = self.provider.redact_text(prompt, PROMPT_VERSION)
        parsed = parse_redaction_response(result.text)
        outcome = RedactionOutcome(
            redacted_text=parsed["redacted_text"],
            items_redacted=parsed["items_redacted"],
            categories=parsed["categories"],
            provider_metadata=result.metadata(),
        )
        outcome.fidelity_warnings = verify_fidelity(
            text, outcome.redacted_text, outcome.items_redacted
        )
        return outcome
