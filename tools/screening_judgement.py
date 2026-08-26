"""The screening report's judgement half, as a provider call.

`run_screening_report.py` derives everything mechanical -- the case summary, the
type verdicts, the checklist, the unconfirmed items, the insurer's position, and
each verified conflict's `professional_summary` copied verbatim from the ledger.
What it cannot derive is which questions matter, who can answer them, and how
serious a deferred conflict is. Those are readings of the case, and until now
they arrived only from the `screening-report` subagent.

They usually did not arrive at all. `screening_report_judgement.schema.json`
records the history: the helper had always read the file with
`allow_missing=True` but no schema existed for it, so `write-contract` refused
it by name and **no run in this repository had ever supplied one** -- every
selective report fell back to the helper's floor values while reporting success.
This module is the second producer of that contract, so the judgement can be
made without a subagent.

Two things the model is never asked for, on the same principle as the
consistency judge's `candidate_digest`:

* **`issue_id` / `point_id`.** The driver assigns `ISSUE_N` / `RP-N` in order.
  They are bookkeeping, not judgement, and a model that renumbers them breaks
  the binding an expert review attaches its answers to.
* **A conflict id it was not given.** `conflict_severity` may only key conflicts
  the driver passed in, because a severity for a conflict nobody deferred is a
  judgement about nothing.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

PROMPT_VERSION = "screening_judgement_v0.1"

REVIEWER_ROLES = ["손해사정사", "의사", "법률전문가"]
SEVERITIES = ["low", "medium", "high"]


def output_schema(conflict_ids: Sequence[str]) -> dict:
    """The transport shape, narrowed to this case's deferred conflicts.

    `conflict_severity` is built from the ids actually deferred rather than
    from a pattern, so a severity for a conflict nobody deferred cannot be
    returned at all. With nothing deferred the property is omitted entirely --
    an empty object is the correct answer and the schema says so by having
    nowhere to put anything else.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "key_issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "related_documents": {
                            "type": "array", "items": {"type": "string"}},
                        "review_required": {"type": "boolean"},
                        "reviewer_role": {"enum": REVIEWER_ROLES},
                    },
                    "required": ["title", "description"],
                },
            },
            "review_points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "point": {"type": "string"},
                        "reviewer_role": {"enum": REVIEWER_ROLES},
                        "priority": {"enum": SEVERITIES},
                        "answerable": {"type": "boolean"},
                        "source_refs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "contract": {"type": "string"},
                                    "element_id": {"type": "string"},
                                },
                                "required": ["contract"],
                            },
                        },
                    },
                    "required": ["point", "reviewer_role", "priority"],
                },
            },
        },
        "required": ["key_issues", "review_points"],
    }
    if conflict_ids:
        schema["properties"]["conflict_severity"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                conflict_id: {"enum": SEVERITIES} for conflict_id in conflict_ids
            },
        }
        schema["required"] = [*schema["required"], "conflict_severity"]
    return schema


INSTRUCTIONS = """\
You are preparing the internal triage document a Korean loss adjuster (손해사정사)
or doctor (의사) reads FIRST, before the claimant's report is drafted. Everything
factual has already been assembled from this case's contracts. Your job is the
three things that cannot be derived from them.

1. `key_issues` -- the issues a reviewer must actually decide on this case.
   Not a restatement of what the contracts say; the questions they leave open.
   `description` says what is at stake and what would settle it.

2. `review_points` -- one per question a named role can answer. Route each by
   WHO CAN ANSWER IT, not by which contract raised it: whether a McBride rating
   is medically sound is a 의사 question no matter where the flag came from.
   Set `answerable: false` only for a point raised over material that is simply
   gone, where answering returns nothing -- and prefer raising the downstream
   CONSEQUENCE as an answerable point instead of "a field is null".
   `source_refs` names the upstream contract each point aggregates; leave it
   empty for a point you raise from your own reading.

3. `conflict_severity` -- how serious each DEFERRED conflict is, keyed by its
   id. Judge severity only; the neutral summary of each conflict was written by
   consistency-check with the evidence in hand and is used verbatim. Do not
   restate it and do not take a side.

Rules:

* Use only the material below. Do not introduce a fact no contract states, and
  do not cite a document that does not appear here.
* Write `title`, `description` and `point` in Korean: they are read by Korean
  professionals and are spliced into the report as written.
* Make no feasibility, difficulty, or payout judgement. This document routes
  questions; it does not answer them.
* Do not number anything. Ids are assigned after you answer.
"""


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "(none stated)"
    return str(value)


def build_prompt(
    *,
    claim_analysis: Mapping[str, Any],
    consistency: Mapping[str, Any],
    denial: Mapping[str, Any] | None,
    deferred_conflicts: Sequence[Mapping[str, Any]],
) -> str:
    """One prompt over the frozen snapshot the driver already read.

    Deliberately built from the upstream CONTRACTS rather than from case
    documents: the stage's own inputs are those contracts, and re-opening pages
    here would let the screening report introduce a fact no contract states.
    """
    lines: list[str] = [INSTRUCTIONS, "", "## Case type assessment"]
    assessment = claim_analysis.get("case_type_assessment") or {}
    for key, value in sorted(assessment.items()):
        if isinstance(value, (str, int, float, bool)) or value is None:
            lines.append(f"- {key}: {_fmt(value)}")

    lines.append("")
    lines.append("## Claim facts")
    for field in claim_analysis.get("claim_facts") or []:
        state = field.get("field_state") or field.get("presence")
        lines.append(
            f"- {field.get('field_id')}: state={_fmt(state)}, "
            f"value={_fmt(field.get('value'))}")
        reason = field.get("unavailable_reason") or field.get("reason")
        if reason:
            lines.append(f"    unavailable_reason: {reason}")

    checks = consistency.get("checks") or []
    lines.append("")
    lines.append(f"## Consistency checks ({len(checks)})")
    for check in checks:
        lines.append(
            f"- {check.get('check_id')}: {_fmt(check.get('result'))} -- "
            f"{_fmt(check.get('description') or check.get('note'))}")

    lines.append("")
    lines.append("## Insurer position")
    if not denial:
        lines.append("- no denial contract for this case")
    else:
        for reason in denial.get("denial_reasons") or []:
            lines.append(
                f"- {reason.get('reason_id')}: "
                f"{_fmt(reason.get('category') or reason.get('reason_type'))} -- "
                f"{_fmt(reason.get('stated_reason') or reason.get('summary'))}")

    lines.append("")
    lines.append(f"## Deferred conflicts ({len(deferred_conflicts)})")
    if not deferred_conflicts:
        lines.append("- none deferred; return an empty conflict_severity")
    for conflict in deferred_conflicts:
        lines.append(
            f"- {conflict.get('conflict_id')}: "
            f"{_fmt(conflict.get('professional_summary'))}")
    return "\n".join(lines)


def bind_judgement(
    raw: Mapping[str, Any],
    *,
    conflict_ids: Sequence[str],
) -> dict:
    """Assign the ids, check what the schema cannot, or refuse.

    Raises ValueError so the caller's single P4 correction applies, rather than
    writing a judgement that `write-contract` would reject afterwards.
    """
    problems: list[str] = []

    issues = raw.get("key_issues")
    points = raw.get("review_points")
    if not isinstance(issues, list) or not issues:
        problems.append("key_issues: at least one issue is required -- a report "
                        "with none carries no agent reading at all")
    if not isinstance(points, list) or not points:
        problems.append("review_points: at least one point is required -- this "
                        "array is P3's aggregation point for the whole phase")

    key_issues = []
    for index, issue in enumerate(issues or [], start=1):
        if not isinstance(issue, Mapping):
            problems.append(f"key_issues[{index}]: not an object")
            continue
        # Ids are assigned here, never taken from the response: an expert
        # review binds its answers to them. The assignment goes AFTER the
        # spread deliberately -- with it before, a response that supplied its
        # own `issue_id` overrode the driver's, which is the opposite of the
        # property this is here to hold. The transport schema also forbids the
        # field; both, because a schema is a pre-check and this is the gate.
        entry = {**{k: v for k, v in issue.items() if v is not None},
                 "issue_id": f"ISSUE_{index}"}
        key_issues.append(entry)

    review_points = []
    for index, point in enumerate(points or [], start=1):
        if not isinstance(point, Mapping):
            problems.append(f"review_points[{index}]: not an object")
            continue
        entry = {**{k: v for k, v in point.items() if v is not None},
                 "point_id": f"RP-{index}"}
        review_points.append(entry)

    severity = raw.get("conflict_severity") or {}
    if not isinstance(severity, Mapping):
        problems.append("conflict_severity: must be an object keyed by conflict id")
        severity = {}
    unknown = sorted(set(severity) - set(conflict_ids))
    if unknown:
        problems.append(
            "conflict_severity names conflicts that were not deferred: "
            + ", ".join(unknown))
    ungraded = sorted(set(conflict_ids) - set(severity))
    if ungraded:
        problems.append(
            "conflict_severity is missing: " + ", ".join(ungraded)
            + " -- every deferred conflict needs a severity")

    if problems:
        raise ValueError("\n  - ".join(["judgement refused:", *problems]))
    return {
        "key_issues": key_issues,
        "review_points": review_points,
        "conflict_severity": dict(severity),
    }


def build_contract(
    *,
    case_id: str,
    run_id: str,
    judgement: Mapping[str, Any],
    model_name: str,
    created_at: str,
) -> dict:
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": "screening-report",
        "status": "success",
        "created_at": created_at,
        "model_info": {"model_name": model_name, "prompt_version": PROMPT_VERSION},
        "review_required": True,
        "warnings": [],
        "source_grounded": True,
        "reviewer_role": "손해사정사",
        "key_issues": list(judgement["key_issues"]),
        "review_points": list(judgement["review_points"]),
    }
    if judgement.get("conflict_severity"):
        contract["conflict_severity"] = dict(judgement["conflict_severity"])
    return contract
