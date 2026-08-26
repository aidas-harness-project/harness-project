"""The draft report's authoring contract, built one block at a time.

The deliverable is authored as `loss_adjustment_report.json` -- the canonical
structured contract at 33,727 bytes, nine required top-level properties -- which
`document_assembly.sections_from_structured_report` then renders into the Korean
narrative. Nothing produced that contract except the `draft-report` subagent.

**Why this is split into many small calls rather than one.** Sending the whole
schema means 33.7KB of contract on every attempt, and the response would be an
entire report against a 16,000-token output cap that refuses a truncated answer
outright. The contract is already eight fixed sections plus four independent
blocks, so each is requested on its own with a transport schema of its own size.
A section that fails is one section to correct, not the report.

**The evidence registry is built by the driver, and that is the load-bearing
decision.** Statements cite `evidence_refs` as `E<N>` ids INTO that registry, so
a model can only cite evidence the driver put there -- it cannot invent a
document, a page or a quote, because it never writes one. That is a stronger
guarantee than checking quotes afterwards, and it is why authoring is safe to
drive at all.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
import _validation

PROMPT_VERSION = "draft_authoring_v0.1"

SECTION_KEYS = ("summary", "assignment_contract", "facts", "governing_basis",
                "analysis", "calculation_summary", "conclusion", "evidence_index")

SECTION_BRIEF = {
    "summary": "요약 -- what this case is and what was assessed.",
    "assignment_contract": "위임/계약 관계 -- who assigned this and under which contract.",
    "facts": "사실관계 -- what happened, from the records only.",
    "governing_basis": "적용 기준 -- the policy clauses, law or medical criteria applied.",
    "analysis": "판단 -- the reasoning that connects the facts to the basis.",
    "calculation_summary": "산정 -- how any amount was arrived at.",
    "conclusion": "결론 -- the assessment reached.",
    "evidence_index": "증거 목록 -- what the assessment rests on.",
}

SUPPORT_TYPES = ["direct", "derived", "professional_judgment"]
CONFIDENCES = ["high", "medium", "low"]
STATUSES = ["included", "not_applicable", "deferred"]

_STATEMENT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "support_type": {"enum": SUPPORT_TYPES},
        "confidence": {"enum": CONFIDENCES},
        "uncertainty_note": {"type": "string"},
        "human_review_required": {"type": "boolean"},
    },
    "required": ["text", "evidence_refs", "support_type", "confidence",
                 "human_review_required"],
}

SECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "heading": {"type": "string"},
        "status": {"enum": STATUSES},
        "rationale": {"type": "string"},
        "statements": {"type": "array", "items": _STATEMENT},
    },
    "required": ["heading", "status", "rationale", "statements"],
}

SECTION_INSTRUCTIONS = """\
You are drafting ONE section of a Korean 손해사정서 (loss-adjustment report).
The document goes to Korean-speaking professionals; write the section body in
Korean, in the register a working adjuster uses.

Every sentence you write is a `statement`, and every statement cites evidence
by id from the registry below. You cannot cite anything that is not in that
registry: there is no way to name a document, a page or a quote here, only an
id. If a sentence needs support the registry does not contain, do not write the
sentence.

For each statement:

* `support_type` -- `direct` when the record says it, `derived` when you worked
  it out from records, `professional_judgment` when it is your assessment.
* `confidence` and, where anything is unsettled, `uncertainty_note`.
* `human_review_required: true` on anything a professional must confirm before
  this goes out.

Set `status`:

* `included` -- you are writing this section; it needs at least one statement.
* `not_applicable` -- this section does not apply to this case. Say why in
  `rationale`.
* `deferred` -- it applies but cannot be written from what is here. Say what is
  missing in `rationale`.

Never state an outcome as certain. This is an assessment, not a decision: write
what the records support and hedge what they do not.
"""


def evidence_registry(
    claim_analysis: Mapping[str, Any],
    *,
    document_kinds: Mapping[str, str] | None = None,
) -> list[dict]:
    """Every citable source, numbered, built from the upstream contract.

    The model never writes a document id, a page or a quote -- it writes an
    `E<N>` that must already exist here. So the registry is the whole
    citation-integrity story for this stage: what the driver does not put in it
    cannot be cited, correctly or otherwise.

    Deduplicated on (document_id, page, quote) so one source cited by several
    fields gets one id, which is what the renderer's tag numbering expects.
    """
    kinds = dict(document_kinds or {})
    seen: dict[tuple, str] = {}
    registry: list[dict] = []
    for field in claim_analysis.get("claim_facts") or []:
        for observation in field.get("observations") or []:
            for reference in observation.get("evidence_references") or []:
                document_id = reference.get("document_id")
                page = reference.get("page")
                quote = reference.get("quote")
                if not (document_id and quote):
                    continue
                key = (document_id, page, quote)
                if key in seen:
                    continue
                evidence_id = f"E{len(registry) + 1}"
                seen[key] = evidence_id
                entry = {
                    "evidence_id": evidence_id,
                    "source_kind": kinds.get(document_id, "other"),
                    "title": observation.get("source_document_kind") or document_id,
                    "locator": f"{document_id} p.{page}" if page else document_id,
                    "document_id": document_id,
                    "quote": quote,
                    "available": True,
                }
                if page:
                    entry["page"] = page
                registry.append(entry)
    return registry


def render_registry(registry: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(
        f"- {entry['evidence_id']}: [{entry['source_kind']}] {entry['title']} "
        f"({entry['locator']}) -- \"{entry['quote']}\""
        for entry in registry)


def build_section_prompt(
    *, section_key: str, registry: Sequence[Mapping[str, Any]],
    case_summary: str, prior_sections: Mapping[str, Any] | None = None,
) -> str:
    lines = [SECTION_INSTRUCTIONS, "", f"## The section to write: {section_key}",
             f"({SECTION_BRIEF[section_key]})", "", "## This case", "",
             case_summary, "", "## Evidence registry -- the only citable ids", "",
             render_registry(registry)]
    if prior_sections:
        lines += ["", "## Sections already written (do not repeat them)", ""]
        for key, section in prior_sections.items():
            statements = section.get("statements") or []
            lines.append(f"- {key} ({section.get('status')}): "
                         f"{len(statements)} statement(s)")
            for statement in statements:
                lines.append(f"    {statement.get('text')}")
    return "\n".join(lines)


def bind_section(
    raw: Mapping[str, Any], *, section_key: str,
    evidence_ids: Sequence[str],
) -> dict:
    """Check what the transport schema cannot, or refuse.

    The two rules the schema states conditionally and this enforces directly:
    an `included` section needs at least one statement, and a section that is
    NOT included needs a rationale saying why. Plus the one the schema cannot
    state at all -- that every cited id exists in the registry this driver
    built.
    """
    problems: list[str] = []
    status = raw.get("status")
    statements = raw.get("statements") or []
    if status == "included" and not statements:
        problems.append("an included section needs at least one statement")
    if status in ("not_applicable", "deferred") and not (raw.get("rationale") or "").strip():
        problems.append(f"a {status} section must say why in rationale")

    known = set(evidence_ids)
    for index, statement in enumerate(statements, start=1):
        refs = statement.get("evidence_refs") or []
        if not refs:
            problems.append(f"statement {index}: every statement cites evidence")
        unknown = sorted(set(refs) - known)
        if unknown:
            problems.append(
                f"statement {index}: cites evidence that is not in the registry: "
                + ", ".join(unknown))
        if len(set(refs)) != len(refs):
            problems.append(f"statement {index}: duplicate evidence_refs")
    if problems:
        raise ValueError(
            "\n  - ".join([f"section {section_key} refused:", *problems]))
    return {
        "heading": raw.get("heading"),
        "status": status,
        "rationale": raw.get("rationale", ""),
        "statements": [
            {k: v for k, v in statement.items() if v is not None}
            for statement in statements
        ],
    }


REVIEW_GATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "evidence": {"enum": ["passed", "review_required", "blocked"]},
        "calculation": {"enum": ["passed", "review_required", "blocked"]},
        "medical": {"enum": ["not_applicable", "passed", "review_required", "blocked"]},
        "legal": {"enum": ["not_applicable", "passed", "review_required", "blocked"]},
        "finalization": {"enum": ["draft", "review_required"]},
    },
    "required": ["evidence", "calculation", "medical", "legal", "finalization"],
}

FINAL_ASSESSMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "outcome": {"enum": ["payable", "partially_payable", "not_payable",
                             "undetermined", "human_review_required"]},
        # money-or-null in the contract: an assessment that reaches no figure
        # says so with null rather than with a zero that reads as "nothing is
        # payable".
        "amount": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "integer"},
                           "currency": {"enum": ["KRW"]}},
            "required": ["value", "currency"],
        },
        "amount_not_determined": {"type": "boolean"},
        # A supportedStatement in the contract, not a string: even the closing
        # summary cites its evidence.
        "reasoning_summary": _STATEMENT,
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "reservations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "amount_not_determined", "reasoning_summary",
                 "evidence_refs", "reservations"],
}

FINAL_INSTRUCTIONS = """\
State the assessment this report reaches, and the gates a human must clear
before it goes out.

`outcome` is an ASSESSMENT, not a decision. Use `undetermined` when the records
do not settle it and `human_review_required` when a professional must decide.
`reservations` lists, in Korean, what is not settled -- an empty list asserts
that nothing is, which is rarely true of a draft.

The gates say what still needs checking: `review_required` wherever a
professional must look, `blocked` where the draft cannot proceed without it,
`not_applicable` only where the discipline genuinely does not arise. Do not
report `passed` on a gate you did not verify.
"""


# ------------------------------------------------------- reasoning issues --

ISSUE_KINDS = ["coverage", "causation", "diagnosis_definition_match", "exclusion",
               "liability", "comparative_negligence", "disability", "income_basis",
               "damages", "other"]
DISPOSITIONS = ["supported", "not_supported", "partially_supported",
                "undetermined", "human_review_required"]
OUTCOME_EFFECTS = ["supports_payment", "reduces_payment", "denies_payment", "none"]
RULE_TYPES = ["law", "policy", "precedent", "medical_criteria",
              "calculation_standard", "other"]

_RULE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rule_type": {"enum": RULE_TYPES},
        "citation": {"type": "string"},
        "rule_text": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["rule_type", "citation", "rule_text", "evidence_refs"],
}

ISSUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "issue_kind": {"enum": ISSUE_KINDS},
                    "question": {"type": "string"},
                    "facts": {"type": "array", "items": _STATEMENT},
                    "rules": {"type": "array", "items": _RULE},
                    "application_steps": {"type": "array", "items": _STATEMENT},
                    "counterevidence": {"type": "array", "items": _STATEMENT},
                    "alternative_interpretations": {
                        "type": "array", "items": {"type": "string"}},
                    "unresolved_items": {"type": "array", "items": {"type": "string"}},
                    "finding": _STATEMENT,
                    "disposition": {"enum": DISPOSITIONS},
                    "outcome_effect": {"enum": OUTCOME_EFFECTS},
                },
                "required": ["issue_kind", "question", "facts", "rules",
                             "application_steps", "counterevidence",
                             "alternative_interpretations", "unresolved_items",
                             "finding", "disposition", "outcome_effect"],
            },
        },
    },
    "required": ["issues"],
}

ISSUE_INSTRUCTIONS = """\
Set out the analytical issues this case turns on -- the questions whose answers
decide the assessment. One entry per issue.

Each issue is worked, not asserted:

* `facts` -- what the records establish, each citing evidence.
* `rules` -- the policy clause, statute or medical criterion applied, each with
  its citation and the evidence it comes from.
* `application_steps` -- how the rule meets the facts, step by step.
* `counterevidence` -- what cuts the other way. An issue with nothing against
  it is rare; leaving this empty asserts there is nothing, so leave it empty
  only when that is true.
* `alternative_interpretations` and `unresolved_items` -- in Korean.
* `finding` -- the conclusion, itself a cited statement.
* `disposition` -- `undetermined` when the records do not settle it,
  `human_review_required` when a professional must decide.

Every statement cites `E<N>` ids from the registry and nothing else: you cannot
write a document, a page or a quote here. Write the Korean text a working
adjuster would write, and never state an outcome as certain.
"""


def bind_issues(raw, *, evidence_ids) -> list[dict]:
    """Number the issues and check every citation, or refuse.

    `issue_id` is assigned here (`I1`, `I2`, ...) for the same reason every
    other id in this pipeline is: it is bookkeeping the rest of the contract
    refers to, not a judgement.
    """
    known = set(evidence_ids)
    problems: list[str] = []
    issues = raw.get("issues") or []
    if not issues:
        problems.append("at least one issue is required -- a report that turns "
                        "on nothing has nothing to assess")

    def check(statement, where):
        refs = statement.get("evidence_refs") or []
        if not refs:
            problems.append(f"{where}: cites no evidence")
        unknown = sorted(set(refs) - known)
        if unknown:
            problems.append(f"{where}: cites {', '.join(unknown)}, which is not "
                            "in the registry")

    out = []
    for index, issue in enumerate(issues, start=1):
        issue_id = f"I{index}"
        for group in ("facts", "application_steps", "counterevidence"):
            for position, statement in enumerate(issue.get(group) or [], start=1):
                check(statement, f"{issue_id}.{group}[{position}]")
        for position, rule in enumerate(issue.get("rules") or [], start=1):
            check(rule, f"{issue_id}.rules[{position}]")
        if issue.get("finding"):
            check(issue["finding"], f"{issue_id}.finding")
        for group in ("facts", "rules", "application_steps"):
            if not (issue.get(group) or []):
                problems.append(f"{issue_id}.{group}: at least one entry is required")
        out.append({**{k: v for k, v in issue.items() if v is not None},
                    "issue_id": issue_id})
    if problems:
        raise ValueError("\n  - ".join(["reasoning issues refused:", *problems]))
    return out


# ------------------------------------------------------------ calculations --
#
# These are authored, and the contract is why. A calculation's `inputs` require
# at least one entry, and a `literal` input REQUIRES `evidence_refs` with at
# least one id. So the schema does not permit a calculation that rests on
# nothing -- a placeholder saying "not computed" would have to cite evidence as
# the basis of an amount nobody worked out, which is a worse lie than the gap it
# was papering over.
#
# `claim_analysis_routing_v0.1.json` still puts amount calculation out of scope
# for this PoC. That is expressed the way the contract provides for: `result`
# null and `status: human_review_required`, on a calculation whose inputs are
# the figures the records actually state.
CALCULATION_CATEGORIES = ["benefit_amount", "consolation", "treatment_cost",
                          "future_treatment_cost", "lost_income",
                          "lost_earning_capacity", "nursing_cost",
                          "other_damages", "comparative_fault", "total_damages",
                          "other"]

CALCULATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "calculations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "category": {"enum": CALCULATION_CATEGORIES},
                    "label": {"type": "string"},
                    "operation": {"enum": ["identity", "sum", "subtract",
                                           "multiply", "divide"]},
                    "inputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                                "unit": {"enum": ["KRW", "ratio"]},
                                "evidence_refs": {"type": "array",
                                                  "items": {"type": "string"}},
                            },
                            "required": ["label", "value", "unit", "evidence_refs"],
                        },
                    },
                    "result": {"type": "integer"},
                    "result_not_determined": {"type": "boolean"},
                    "rounding_mode": {"enum": ["none", "truncate", "half_up"]},
                    "rounding_unit": {"type": "integer"},
                    "status": {"enum": ["complete", "provisional",
                                        "human_review_required"]},
                },
                "required": ["category", "label", "operation", "inputs",
                             "result_not_determined", "rounding_mode",
                             "rounding_unit", "status"],
            },
        },
    },
    "required": ["calculations"],
}

CALCULATION_INSTRUCTIONS = """\
List the amounts this assessment turns on, as calculations.

Amount calculation is OUT OF SCOPE for this pipeline, so you are not being
asked to produce a payable figure. What you are asked for is the structure: the
figures the records actually state, each cited, and what would be done with
them.

* Every input carries the number AS THE RECORD STATES IT and cites the evidence
  it came from. Do not carry a figure no record states.
* Set `result_not_determined: true` and `status: human_review_required` unless
  the result follows arithmetically from inputs that are all stated in the
  records. Do not compute a total from figures you inferred.
* `operation` says what would combine the inputs; `identity` when there is one.

A calculation that rests on no cited figure cannot be written here -- if the
records state no amounts, return an empty list and the stage will say so.
"""


def bind_calculations(raw, *, evidence_ids) -> list[dict]:
    """Number them and expand the flat transport shape into the contract's.

    `rounding_rule` is an object in the contract and two flat fields on the
    wire, because a nested object inside an array item is where a structured
    response most often comes back subtly wrong for no benefit.
    """
    known = set(evidence_ids)
    problems: list[str] = []
    out = []
    for index, calculation in enumerate(raw.get("calculations") or [], start=1):
        calculation_id = f"C{index}"
        inputs = calculation.get("inputs") or []
        if not inputs:
            problems.append(f"{calculation_id}: a calculation needs at least one "
                            "cited input; the contract has no way to express one "
                            "that rests on nothing")
        for position, entry in enumerate(inputs, start=1):
            refs = entry.get("evidence_refs") or []
            unknown = sorted(set(refs) - known)
            if not refs:
                problems.append(f"{calculation_id}.inputs[{position}]: cites no evidence")
            if unknown:
                problems.append(
                    f"{calculation_id}.inputs[{position}]: cites "
                    f"{', '.join(unknown)}, which is not in the registry")
        determined = not calculation.get("result_not_determined", True)
        if determined and calculation.get("result") is None:
            problems.append(f"{calculation_id}: says the result is determined but "
                            "gives none")
        out.append({
            "calculation_id": calculation_id,
            "category": calculation.get("category"),
            "label": calculation.get("label"),
            "operation": calculation.get("operation"),
            "inputs": [{"label": entry.get("label"), "input_type": "literal",
                        "value": entry.get("value"), "unit": entry.get("unit"),
                        "evidence_refs": list(entry.get("evidence_refs") or [])}
                       for entry in inputs],
            "result": calculation.get("result") if determined else None,
            "currency": "KRW",
            "rounding_rule": {"mode": calculation.get("rounding_mode", "none"),
                              "unit": calculation.get("rounding_unit", 1)},
            "status": calculation.get("status"),
        })
    if problems:
        raise ValueError("\n  - ".join(["calculations refused:", *problems]))
    return out


def bind_final_assessment(raw, *, evidence_ids) -> dict:
    """Expand the flat amount into the contract's money-or-null."""
    known = set(evidence_ids)
    problems: list[str] = []
    refs = raw.get("evidence_refs") or []
    if not refs:
        problems.append("evidence_refs: the closing assessment cites its basis")
    unknown = sorted(set(refs) - known)
    if unknown:
        problems.append("evidence_refs: " + ", ".join(unknown)
                        + " is not in the registry")
    summary = raw.get("reasoning_summary") or {}
    summary_refs = summary.get("evidence_refs") or []
    if not summary_refs:
        problems.append("reasoning_summary: cites no evidence")
    if sorted(set(summary_refs) - known):
        problems.append("reasoning_summary cites evidence outside the registry")
    if problems:
        raise ValueError("\n  - ".join(["final assessment refused:", *problems]))
    out = {k: v for k, v in raw.items()
           if k not in ("amount", "amount_not_determined") and v is not None}
    # null, not zero: a zero amount reads as "nothing is payable", which is a
    # different statement from "no figure was reached".
    out["amount"] = None if raw.get("amount_not_determined", True) else raw["amount"]
    return out


# ------------------------------------------------- what the family requires --
#
# The canonical contract carries eleven document-level conditional rules: which
# reasoning-issue kinds and calculation categories a given `document_profile.
# family` must contain, and which review gates may not be closed while
# professional judgment or unresolved items remain open. They are real domain
# rules -- a 배상책임 report without a liability issue is not a report -- and a
# driver that ignored them would produce a document that fails validation at
# write time, after every call had been paid for.
#
# Read OUT OF THE SCHEMA rather than restated here. A copy would drift, and the
# schema is the thing `write-contract` actually enforces.


def _canonical() -> dict:
    return json.loads(_validation.LOSS_ADJUSTMENT_SCHEMA.read_text(encoding="utf-8"))


def _required_consts(node: Any, key: str, found: list[str]) -> None:
    if isinstance(node, Mapping):
        if "contains" in node and isinstance(node["contains"], Mapping):
            prop = (node["contains"].get("properties") or {}).get(key)
            if isinstance(prop, Mapping) and "const" in prop:
                found.append(prop["const"])
        for value in node.values():
            _required_consts(value, key, found)
    elif isinstance(node, list):
        for value in node:
            _required_consts(value, key, found)


def family_requirements(family: str, *, schema: Mapping[str, Any] | None = None) -> dict:
    """The issue kinds and calculation categories this family's report must carry."""
    document = dict(schema or _canonical())
    issue_kinds: list[str] = []
    categories: list[str] = []
    for rule in document.get("allOf") or []:
        condition = (((rule.get("if") or {}).get("properties") or {})
                     .get("document_profile") or {})
        wanted = ((condition.get("properties") or {}).get("family") or {}).get("const")
        if wanted != family:
            continue
        then = (rule.get("then") or {}).get("properties") or {}
        _required_consts(then.get("reasoning_issues"), "issue_kind", issue_kinds)
        _required_consts(then.get("calculations"), "category", categories)
    return {"issue_kinds": sorted(set(issue_kinds)),
            "calculation_categories": sorted(set(categories))}


def render_requirements(family: str) -> str:
    requirements = family_requirements(family)
    lines = [f"## What a {family} report must contain", ""]
    if requirements["issue_kinds"]:
        lines.append("- reasoning issues MUST include one of each kind: "
                     + ", ".join(requirements["issue_kinds"]))
    if requirements["calculation_categories"]:
        lines.append("- calculations MUST include one of each category: "
                     + ", ".join(requirements["calculation_categories"]))
    if len(lines) == 2:
        lines.append("- no family-specific requirement")
    return "\n".join(lines)


def check_gates(gates: Mapping[str, Any], *, judgment_open: bool) -> None:
    """The contract's gate rule, checked before the write rather than at it.

    A gate may not read `not_applicable` while professional judgment or
    unresolved items remain open -- that combination claims a discipline does
    not arise on a report that is still asking it questions.
    """
    if not judgment_open:
        return
    closed = [name for name in ("medical", "legal")
              if gates.get(name) == "not_applicable"]
    if closed:
        raise ValueError(
            "review gates refused: " + ", ".join(closed)
            + " cannot be not_applicable while professional judgment or "
              "unresolved items remain open")
