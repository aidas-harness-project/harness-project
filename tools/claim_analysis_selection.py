"""Deterministic selective-reading planner for Claim Analysis.

This module owns the *ordering* decisions the routing config expresses, and
nothing else. It never reads case data, never calls a provider, and never
writes a contract: it turns (routing config + the case's fine-grained document
classifications) into an ordered plan, and it evaluates the stop rule against
values another layer produced.

Why it is separate from the driver: the plan is the part that must be
inspectable and testable without a provider or a case on disk. The driver
transports and verifies; the ordering rules live here as pure functions.

Vocabulary, matching `claim_analysis_routing_v0.1.json`:

* **wave** -- `A` is the first pass (advisory-critical fields), `B` the second.
  `opportunistic` fields are never *searched* for; they are read only out of a
  document some other field already opened. `deferred` fields are not extracted
  at all.
* **priority group** -- one rung of a `source_routes[].priority_groups` ladder.
  Documents inside one rung are equally authoritative, so they are read
  together; a later rung is only consulted when the earlier rungs produced no
  trusted value.
* **trusted value** -- the four `extraction_policy.trusted_value_requirements`
  conditions. Only a caller that observed the actual extracted value can judge
  `complete`/`unambiguous`, so `is_trusted_value` takes those as inputs rather
  than inferring them.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Mapping, Sequence


# Cost documents are checked for presence and never opened for content. The set
# is duplicated in claim_analysis_contracts.COST_KINDS, which validates that the
# config keeps them presence-only; here it is the runtime read gate.
PRESENCE_ONLY_MODE = "presence_only"
CONTENT_MODE = "content"

# Waves that actively search. `opportunistic` deliberately absent: those fields
# ride along with a read some other field paid for.
SEARCHING_WAVES = ("A", "B")


@dataclass(frozen=True)
class DocumentRef:
    """One classified document as the planner sees it.

    `kind` is the fine-grained `medical_document_kind` published by Stage 2's
    `medical_classification` block. `None` means Stage 2 could not resolve one
    (ambiguous, not_medical, or the classifier never ran) -- such a document is
    never routed to a field, because routing is by form kind.
    """

    document_id: str
    kind: str | None
    ambiguous: bool = False


@dataclass(frozen=True)
class ReadStep:
    """One planned read: a field, a priority rung, and the documents on it."""

    field_id: str
    route_id: str
    wave: str
    priority_rank: int
    document_ids: tuple[str, ...]
    document_kinds: tuple[str, ...]


@dataclass
class FieldPlan:
    field_id: str
    domain_code: str
    wave: str
    grade: str
    critical: bool
    route_id: str | None
    steps: list[ReadStep] = dataclass_field(default_factory=list)
    # Why a field has no steps at all, when that is the case. Recorded so the
    # driver can state `not_applicable` with a reason instead of silence.
    skip_reason: str | None = None


def _by_field_id(config: Mapping[str, Any]) -> dict[str, dict]:
    return {row["field_id"]: row for row in config.get("fields") or []}


def _by_route_id(config: Mapping[str, Any]) -> dict[str, dict]:
    return {row["route_id"]: row for row in config.get("source_routes") or []}


def read_mode_by_kind(config: Mapping[str, Any]) -> dict[str, str]:
    return {
        row["kind"]: row["claim_analysis_read_mode"]
        for row in config.get("document_kinds") or []
    }


def presence_only_kinds(config: Mapping[str, Any]) -> set[str]:
    return {
        kind for kind, mode in read_mode_by_kind(config).items()
        if mode == PRESENCE_ONLY_MODE
    }


def active_fields(config: Mapping[str, Any], wave: str) -> list[dict]:
    """Configured fields this wave actively searches for, in config order.

    A `D` grade never reaches here (the config schema pins it to `deferred`),
    and a `C` grade only does when it carries a named operational override --
    which the config schema and the semantic validator both enforce, so this
    function does not re-litigate the grade rule; it filters on the wave the
    override already placed the field in.
    """
    if wave not in SEARCHING_WAVES:
        raise ValueError(f"{wave!r} is not a searching wave")
    return [
        row for row in config.get("fields") or []
        if row.get("extraction_wave") == wave
    ]


def opportunistic_fields(config: Mapping[str, Any]) -> list[dict]:
    return [
        row for row in config.get("fields") or []
        if row.get("extraction_wave") == "opportunistic"
    ]


def plan_field(
    field_row: Mapping[str, Any],
    config: Mapping[str, Any],
    documents: Sequence[DocumentRef],
) -> FieldPlan:
    """The ordered read ladder for one field against this case's documents.

    Each priority group becomes at most one `ReadStep`, holding every document
    of the kinds on that rung. A rung with no matching document in the case
    produces no step -- it is simply absent, not an empty read.
    """
    routes = _by_route_id(config)
    route_id = field_row.get("source_route_id")
    plan = FieldPlan(
        field_id=field_row["field_id"],
        domain_code=field_row["domain_code"],
        wave=field_row["extraction_wave"],
        grade=field_row["medical_advisory_grade"],
        critical=bool(field_row.get("critical_conflict_field")),
        route_id=route_id,
    )
    if route_id is None:
        plan.skip_reason = "the field is not routed to any document source"
        return plan
    route = routes.get(route_id)
    if route is None:
        raise ValueError(
            f"field {field_row['field_id']!r} names unknown route {route_id!r}"
        )

    blocked = presence_only_kinds(config)
    by_kind: dict[str, list[str]] = {}
    for document in documents:
        if document.kind is None or document.ambiguous:
            continue
        by_kind.setdefault(document.kind, []).append(document.document_id)

    rank = 0
    for group in route.get("priority_groups") or []:
        doc_ids: list[str] = []
        kinds: list[str] = []
        for kind in group:
            if kind in blocked:
                # Structurally unreachable while the config validator holds, but
                # a runtime gate too: a cost document is never opened for
                # content, whatever a future config edit says.
                continue
            for doc_id in by_kind.get(kind, []):
                if doc_id not in doc_ids:
                    doc_ids.append(doc_id)
                    kinds.append(kind)
        if not doc_ids:
            continue
        rank += 1
        plan.steps.append(ReadStep(
            field_id=plan.field_id,
            route_id=route_id,
            wave=plan.wave,
            priority_rank=rank,
            document_ids=tuple(doc_ids),
            document_kinds=tuple(kinds),
        ))
    if not plan.steps:
        plan.skip_reason = (
            "the case holds no document of any kind this field routes to"
        )
    return plan


def plan_wave(
    config: Mapping[str, Any],
    documents: Sequence[DocumentRef],
    wave: str,
) -> list[FieldPlan]:
    return [
        plan_field(row, config, documents)
        for row in active_fields(config, wave)
    ]


def is_trusted_value(
    *,
    from_priority_source: bool,
    has_exact_quote: bool,
    complete: bool,
    unambiguous: bool,
) -> bool:
    """The config's four trusted-value conditions, all required.

    Kept as an explicit four-argument predicate rather than a heuristic so a
    caller cannot accidentally satisfy it by supplying only confidence. The
    config lists exactly these four in `trusted_value_requirements`.
    """
    return bool(
        from_priority_source and has_exact_quote and complete and unambiguous
    )


def comparison_budget(field_row: Mapping[str, Any], config: Mapping[str, Any]) -> int:
    """How many extra independent sources this field may consult after a
    trusted value is already in hand.

    Only a `critical_conflict_field` gets one; everything else stops dead at the
    first trusted value. The limit itself comes from the config
    (`critical_field_additional_comparison_limit`), never from a constant here.
    """
    if not field_row.get("critical_conflict_field"):
        return 0
    return int(
        (config.get("extraction_policy") or {})
        .get("critical_field_additional_comparison_limit", 0)
    )


def presence_checklist(
    config: Mapping[str, Any],
    documents: Sequence[DocumentRef],
    case_types: Iterable[str],
) -> list[dict]:
    """Required-document status per kind, for the named case types.

    Presence only. A cost document's status is decided by whether a document of
    that kind exists, never by reading one. `ambiguous` is reported distinctly
    from `missing`: Stage 2 saw a document it could not type, which is a
    different thing for a reviewer than the document being absent.
    """
    required_map = config.get("required_documents_by_case_type") or {}
    wanted: dict[str, set[str]] = {}
    for case_type in case_types:
        for kind in required_map.get(case_type) or []:
            wanted.setdefault(kind, set()).add(case_type)

    resolved: dict[str, list[str]] = {}
    ambiguous_ids: list[str] = []
    for document in documents:
        if document.ambiguous:
            ambiguous_ids.append(document.document_id)
        elif document.kind is not None:
            resolved.setdefault(document.kind, []).append(document.document_id)

    items = []
    for kind in sorted(wanted):
        doc_ids = sorted(resolved.get(kind, []))
        if doc_ids:
            status, reason = "available", (
                f"{len(doc_ids)} document(s) of this kind are classified in the case"
            )
        elif ambiguous_ids:
            status, reason = "ambiguous", (
                "no document is classified as this kind, and the case holds "
                "document(s) Stage 2 could not type"
            )
        else:
            status, reason = "missing", (
                "no document of this kind is present in the case"
            )
        items.append({
            "document_kind": kind,
            "status": status,
            "required_for_case_types": sorted(wanted[kind]),
            "document_ids": doc_ids,
            "reason": reason,
        })
    return items
