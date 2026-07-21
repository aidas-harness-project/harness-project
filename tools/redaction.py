"""Redaction abstraction for document-pipeline checkpoint 2.

`redact_document.py` talks only to a `Redactor`, never to a provider or a
prompt directly. That seam lets a future dedicated de-identification model
(e.g. an OpenMed NER pipeline -- open-decisions.md #1) drop in as a new
`Redactor` subclass without touching the tool.

Design: span substitution, not page rewriting.
------------------------------------------------
The LLM's only job is to IDENTIFY PII values (copy each verbatim from the page
and tag its category). The redacted text is then built DETERMINISTICALLY by
`apply_redaction_spans`: it takes the original source text and replaces only the
identified PII strings with placeholders. Because the output is the source with
spans swapped -- never the model's own prose -- omission and fabrication of
non-PII content are structurally impossible (the failure class that contaminated
the OCR layer, known-gaps 11/14/15).

That pivot flips the dominant failure mode to UNDER-redaction (a missed PII
value). Two deterministic guards address it:
  * `apply_redaction_spans` reports `unmatched_spans` -- PII the model named but
    that is not present verbatim in the source (a misread/reformat that leaves
    the real value in the text, or model noise).
  * `scan_residual_pii` regex-scans the finished output for STRUCTURED PII
    (phone / RRN / email / vehicle plate / long digit run) that survived.
Per the checkpoint-2 privacy policy, either signal is treated as a possible
leak and HARD-FAILS the document (raise, write nothing) -- it is not written to
the processed layer for later review. Spans that cannot be safely auto-redacted
(too short, implausibly frequent, or glued to an institution suffix where a
blind replace-all would corrupt a kept entity name) are LEFT IN PLACE and
flagged for human review rather than hard-failed. Note this bucket can hold both
over-redaction risk and residual under-redaction (a real value left in the
text), so a review flag is not a safety guarantee -- a human must resolve it.

Residual limitation: `scan_residual_pii` only catches *structured* PII. An
unstructured value the model misses (a bare name/address with no format) is not
caught here -- only a real NER redactor closes that fully (open-decisions.md #1).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


PROMPT_VERSION = "pii_redaction_v0.3"

# category -> placeholder. Unknown categories collapse to [OTHER_PII].
CATEGORY_TO_PLACEHOLDER = {
    "person_name": "[PERSON_NAME]",
    "resident_registration_number": "[RESIDENT_REGISTRATION_NUMBER]",
    "phone_number": "[PHONE_NUMBER]",
    "address": "[ADDRESS]",
    "email": "[EMAIL]",
    "account_number": "[ACCOUNT_NUMBER]",
    "vehicle_number": "[VEHICLE_NUMBER]",
    "other_pii": "[OTHER_PII]",
}
_DEFAULT_PLACEHOLDER = "[OTHER_PII]"

# A single PII value appearing more than this many times on one page is
# implausible -- far more likely a short common substring the model mis-tagged,
# whose blind replace-all would corrupt kept text. Such spans are not redacted;
# they are flagged for review instead (over-redaction guard).
_HIGH_OCCURRENCE_FLAG = 20

# Redaction scope, settled after CASE_012/CASE_021 (document-pipeline.md).
# Carried in the prompt so the model actually sees it.
REDACTION_SCOPE = (
    "PII = every natural person's name regardless of capacity (claimant, "
    "patient, physician, adjuster, insurer staff, corporate signatories such as "
    "a 대표이사), resident registration numbers, phone/fax numbers, street "
    "addresses, email addresses, account numbers, and vehicle registration "
    "numbers -- INCLUDING published corporate contact info (complaint-desk "
    "hotlines, office addresses). NOT PII: corporate/institutional entity names "
    "(insurer names, hospital names), dates, monetary amounts, diagnoses, and "
    "policy clauses -- never list those."
)

PROMPT_TEMPLATE = """Identify every piece of personally identifying information (PII) in the page text below.

{scope}

Do NOT rewrite, translate, summarize, reformat, or redact the text yourself.
Your ONLY job is to list each PII value, copied EXACTLY (character for
character) as it appears in the text, with its category. If a value appears
multiple times, list it once. If there is no PII, return an empty list.

Categories: person_name, resident_registration_number, phone_number, address,
email, account_number, vehicle_number, other_pii.

Reply with ONLY one JSON object in this exact shape:
{{"pii_items": [{{"text": "홍길동", "category": "person_name"}},
  {{"text": "010-1234-5678", "category": "phone_number"}}]}}

--- Page text ---
{text}
"""


# ---------------------------------------------------------------------------
# S3: residual structured-PII scan (deterministic)
# ---------------------------------------------------------------------------
# Separators between number groups: hyphen, dot, whitespace (incl. newline, and
# the full-width variants that _normalize_widths folds first). `+` (one-or-more)
# so a value wrapped across a line ("010-1234-\n5678") or double-spaced still
# matches -- the earlier single-separator form let those slip (fleet finding).
_SEP = r"[-.\s]+"
# The fixed set of Hangul syllables used in the middle of a Korean vehicle plate
# (passenger + common commercial/special). Deliberately NOT all of 가-힣.
_PLATE_SYLLABLES = (
    "가나다라마거너더러머버서어저고노도로모보소오조구누두루무부수우주바사아자하허호배육해공국합"
)
# Lookarounds require a non-digit boundary so these do not fire inside longer
# numbers. Calibrated NOT to match dates (4-2-2, last group only 2 digits),
# comma-grouped amounts, KCD codes, or clause refs -- proven in
# tests/test_redaction.py. Separators accepted as -, ., whitespace (incl.
# newline) in any run, plus optional ()-wrapped area codes, so multi-separator /
# line-wrapped RRN & phone cannot slip past (fleet finding).
_RESIDUAL_PII_PATTERNS = {
    "resident_registration_number": re.compile(rf"(?<!\d)\d{{6}}(?:{_SEP})?\d{{7}}(?!\d)"),
    "phone_number_separated": re.compile(rf"(?<!\d)\(?\d{{2,3}}\)?{_SEP}\d{{3,4}}{_SEP}\d{{4}}(?!\d)"),
    # Contiguous phone: any 10-11 digit run beginning 0 (mobile 01x… and landline
    # 02x…), subsumes the old 01x-only pattern and catches "0212345678".
    "phone_number_contiguous": re.compile(r"(?<!\d)0\d{9,10}(?!\d)"),
    # account/long numbers written with dashes: 3+ / 2+ / 4+ groups. The final
    # group requires >=4 digits, which a date (…-NN) never has, so dates don't hit.
    "account_number_dashed": re.compile(r"(?<!\d)\d{3,}-\d{2,}-\d{4,}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.IGNORECASE),
    # Korean plate middle syllable is from a FIXED set -- restricting to it (vs
    # any 가-힣) avoids false-positives on counter phrases like "12개 1234" (개 is
    # not a plate syllable). Only a preceding DIGIT is excluded (not Hangul), so
    # a region-prefixed plate "서울12가3456" is caught.
    "vehicle_number": re.compile(
        rf"(?<!\d)\d{{2,3}}[{_PLATE_SYLLABLES}](?:{_SEP})?\d{{4}}(?!\d)"
    ),
    "long_digit_run": re.compile(r"(?<!\d)\d{11,}(?!\d)"),
}

# Full-width digits ０-９ and full-width separators to their ASCII equivalents,
# so a value typed in full-width (common in Korean IME output) can't evade the
# ASCII-oriented patterns above.
_WIDTH_FOLD = {ord(fw): ord(ascii_) for fw, ascii_ in zip("０１２３４５６７８９－．", "0123456789-.")}


def _normalize_widths(text: str) -> str:
    return text.translate(_WIDTH_FOLD)


def scan_residual_pii(redacted_text: str) -> list[dict[str, str]]:
    """Regex-scan finished redacted text for structured PII that survived.

    Returns a list of {kind, sample} hits (empty == clean). Only structured
    formats are detectable this way; unstructured PII (bare names) is out of
    scope. A non-empty result is treated as a possible leak by the caller."""
    scanned = _normalize_widths(redacted_text)
    hits: list[dict[str, str]] = []
    for kind, pattern in _RESIDUAL_PII_PATTERNS.items():
        for m in pattern.finditer(scanned):
            hits.append({"kind": kind, "sample": m.group(0)})
    return hits


# ---------------------------------------------------------------------------
# S1: deterministic span substitution
# ---------------------------------------------------------------------------
# Hangul institution suffixes: when a name-like PII span is glued immediately
# before one of these, replacing it would corrupt a KEPT entity name the pipeline
# depends on (e.g. a hospital 김영수병원 named after a person 김영수). Those
# occurrences are left in place and flagged for review instead of blindly
# replaced. Particles (은/는/이/가/…) are NOT here, so a normal name+particle
# ("홍길동은") still redacts cleanly.
_INSTITUTION_SUFFIXES = (
    "병원", "의원", "약국", "의료원", "한의원", "치과", "센터", "보험", "화재",
    "은행", "증권", "대학교", "대학", "학교", "회사", "그룹", "상사", "공사",
    "재단", "협회", "조합", "지점", "연구소", "클리닉", "법인",
)
# Categories where a value can be a substring of kept text (names). Structured
# categories (phone/RRN/…) are implausible substrings, so they replace-all.
_NAME_CATEGORIES = {"person_name", "other_pii"}


@dataclass
class RedactionApplication:
    redacted_text: str
    items_redacted: int
    categories: list[str]
    # PII the model named but NOT present verbatim anywhere in the SOURCE (not
    # merely consumed by an overlapping longer span): a misread/reformat that
    # leaves the real value in the text, or model noise. Treated as a leak.
    unmatched_spans: list[dict[str, str]] = field(default_factory=list)
    # Spans left un-redacted and flagged for human review -- NOT necessarily
    # privacy-safe: this bucket holds both over-redaction risk (a <=1-char or
    # implausibly-frequent span not blindly replaced) AND under-redaction (a real
    # value left in place, e.g. a name glued to an institution suffix, or a
    # 1-char surname). A human must resolve these; the residual text may still
    # contain the value.
    ambiguous_spans: list[dict[str, str]] = field(default_factory=list)
    applied_spans: list[dict[str, Any]] = field(default_factory=list)


def _replace_span(working: str, text: str, placeholder: str, category: str):
    """Replace occurrences of `text` with `placeholder`.

    For name-like categories, an occurrence glued (no space/punctuation) into a
    Hangul run that ends in an institution suffix is LEFT in place -- replacing
    it would corrupt a kept entity name (김영수병원, and compounds like
    김영수의료재단 where the suffix isn't immediately adjacent). A name followed by
    a particle or a SPACED institution word (홍길동은, 김영수 병원) still redacts,
    because the intervening space breaks the Hangul-only run. Returns
    (new_working, replaced_count, glued_count)."""
    if category not in _NAME_CATEGORIES:
        count = working.count(text)
        return working.replace(text, placeholder), count, 0
    suffix_alt = "|".join(re.escape(s) for s in _INSTITUTION_SUFFIXES)
    # name directly followed by up to a short Hangul run ending in a suffix
    inst = rf"(?=[가-힣]{{0,10}}(?:{suffix_alt}))"
    glued = len(re.findall(re.escape(text) + inst, working))
    new_working, replaced = re.subn(re.escape(text) + rf"(?![가-힣]{{0,10}}(?:{suffix_alt}))", placeholder, working)
    return new_working, replaced, glued


def apply_redaction_spans(source_text: str, pii_items: list[dict[str, str]]) -> RedactionApplication:
    """Build redacted text by replacing only the identified PII strings in the
    SOURCE with their category placeholders -- deterministic, so all non-PII
    content is preserved by construction.

    Longest spans are replaced first so a longer PII value (e.g. a full address)
    is substituted before a shorter one that may sit inside it; placeholders
    contain no Korean/PII characters so they never re-match. Exact (verbatim)
    matching only -- a near-miss becomes an unmatched span, never a silent drop."""
    # Dedup by text (first category wins), drop empties.
    seen: set[str] = set()
    items: list[dict[str, str]] = []
    for it in pii_items:
        text = it.get("text") or ""
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        items.append({"text": text, "category": it.get("category") or "other_pii"})

    # Longest first.
    items.sort(key=lambda it: len(it["text"]), reverse=True)

    working = source_text
    total = 0
    categories: set[str] = set()
    unmatched: list[dict[str, str]] = []
    ambiguous: list[dict[str, str]] = []
    applied: list[dict[str, Any]] = []

    for it in items:
        text, category = it["text"], it["category"]
        if len(text.strip()) <= 1:
            ambiguous.append({"text": text, "category": category, "reason": "span too short (<=1 char)"})
            continue
        # Leak decision keys on the ORIGINAL source, not the mutated working text.
        # A value present in the source but with 0 occurrences left in `working`
        # was already covered by an overlapping longer span -- it is redacted, not
        # leaked, so it must NOT be misclassified as unmatched (which would
        # hard-fail a fully-redacted document).
        if source_text.count(text) == 0:
            unmatched.append({"text": text, "category": category})
            continue
        work_count = working.count(text)
        if work_count == 0:
            continue  # already redacted via an overlapping longer span
        if work_count > _HIGH_OCCURRENCE_FLAG:
            ambiguous.append({"text": text, "category": category,
                              "reason": f"{work_count} occurrences (implausible; left un-redacted for review)"})
            continue
        placeholder = CATEGORY_TO_PLACEHOLDER.get(category, _DEFAULT_PLACEHOLDER)
        working, replaced, glued = _replace_span(working, text, placeholder, category)
        if replaced:
            total += replaced
            categories.add(category)
            applied.append({"text": text, "category": category, "occurrences": replaced})
        if glued:
            ambiguous.append({"text": text, "category": category,
                              "reason": f"{glued} occurrence(s) left un-redacted -- glued to an "
                                        "institution suffix (would corrupt a kept entity name)"})

    return RedactionApplication(
        redacted_text=working,
        items_redacted=total,
        categories=sorted(categories),
        unmatched_spans=unmatched,
        ambiguous_spans=ambiguous,
        applied_spans=applied,
    )


_PLACEHOLDER_RE = re.compile("|".join(re.escape(p) for p in CATEGORY_TO_PLACEHOLDER.values()))


def is_built_from_source(redacted_text: str, source_text: str) -> bool:
    """Defense-in-depth invariant: with placeholders removed, the redacted text
    must be an in-order sequence of source slices. Always true for output of
    apply_redaction_spans; a violation means a substitution bug, not model
    error. (Whitespace-normalized, since we never alter whitespace ourselves.)"""
    hay = re.sub(r"\s+", " ", source_text).strip()
    cursor = 0
    for chunk in _PLACEHOLDER_RE.split(redacted_text):
        needle = re.sub(r"\s+", " ", chunk).strip()
        if not needle:
            continue
        found = hay.find(needle, cursor)
        if found == -1:
            return False
        cursor = found + len(needle)
    return True


# ---------------------------------------------------------------------------
# S2: span-based LLM contract (parse)
# ---------------------------------------------------------------------------
class RedactionParseError(RuntimeError):
    """Raised when a redaction response cannot be parsed into the required shape."""


class RedactionLeakError(RuntimeError):
    """Raised when redaction output shows a possible PII leak (residual
    structured PII, or a model-named value not found verbatim in the source).
    Per the checkpoint-2 policy this hard-fails the document -- the redacted text
    is never written to the processed layer."""


_ALLOWED_CATEGORIES = set(CATEGORY_TO_PLACEHOLDER)


def parse_pii_items(raw: str) -> list[dict[str, str]]:
    """Extract the {pii_items:[{text,category}]} object from a model reply.

    Unknown categories are coerced to other_pii rather than rejected (the value
    is still redacted); shape violations fail closed."""
    decoder = json.JSONDecoder()
    start = 0
    saw_brace = False
    parsed = None
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
    if not isinstance(parsed, dict) or not isinstance(parsed.get("pii_items"), list):
        raise RedactionParseError(f"redaction response must be an object with a pii_items array: {raw!r}")
    items: list[dict[str, str]] = []
    for entry in parsed["pii_items"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
            raise RedactionParseError(f"each pii_item must be an object with a string text: {entry!r}")
        category = entry.get("category")
        if not isinstance(category, str) or category not in _ALLOWED_CATEGORIES:
            category = "other_pii"
        items.append({"text": entry["text"], "category": category})
    return items


@dataclass
class RedactionOutcome:
    redacted_text: str
    items_redacted: int
    categories: list[str]
    provider_metadata: dict[str, Any] | None = None
    # Review-forcing, non-leak flags (over-redaction risk). A leak hard-fails
    # before an outcome is ever produced, so this never carries a leak.
    review_warnings: list[str] = field(default_factory=list)
    spans: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# S4: the redactor
# ---------------------------------------------------------------------------
class Redactor(Protocol):
    method: str

    def redact_page(self, text: str) -> RedactionOutcome: ...


class LlmRedactor:
    """Redactor backed by an llm_providers provider. The provider IDENTIFIES PII
    spans; substitution and all safety checks are deterministic and local."""

    method = "llm_span_redaction"

    def __init__(self, provider, *, scope: str = REDACTION_SCOPE):
        self.provider = provider
        self.scope = scope

    @property
    def label(self) -> str:
        return f"{self.method}:{self.provider.provider_name}:{self.provider.model_name}"

    def redact_page(self, text: str) -> RedactionOutcome:
        prompt = PROMPT_TEMPLATE.format(scope=self.scope, text=text)
        result = self.provider.redact_text(prompt, PROMPT_VERSION)
        items = parse_pii_items(result.text)
        app = apply_redaction_spans(text, items)

        # Hard-fail on any possible leak (checkpoint-2 privacy policy): a
        # structured PII pattern surviving in the output, or PII the model named
        # that is not present verbatim in the source (real value may remain).
        residual = scan_residual_pii(app.redacted_text)
        if residual or app.unmatched_spans:
            reasons = []
            if residual:
                reasons.append("residual structured PII in output: "
                               + ", ".join(f"{h['kind']}={h['sample']!r}" for h in residual))
            if app.unmatched_spans:
                reasons.append("model-named PII not found verbatim in source: "
                               + ", ".join(repr(s["text"]) for s in app.unmatched_spans))
            raise RedactionLeakError("; ".join(reasons))

        # Defense-in-depth: my own output must be source-with-spans-swapped.
        if not is_built_from_source(app.redacted_text, text):
            raise RedactionLeakError(
                "internal invariant failed: redacted text is not an in-order slice of the source"
            )

        review_warnings = [
            f"span not redacted ({s['reason']}): {s['text']!r}" for s in app.ambiguous_spans
        ]
        return RedactionOutcome(
            redacted_text=app.redacted_text,
            items_redacted=app.items_redacted,
            categories=app.categories,
            provider_metadata=result.metadata(),
            review_warnings=review_warnings,
            spans=app.applied_spans,
        )
