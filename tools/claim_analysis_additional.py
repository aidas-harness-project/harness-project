"""Stage 3-a: the type-conditional extraction round.

Runs AFTER case-type assessment, and re-opens fields the common A/B pass left
`unavailable` -- reading the non-medical documents no medical route ranks.

Why this exists as a round rather than as more routes
-----------------------------------------------------
A `source_route` ranks `medical_document_kind`s, and the routing schema keeps
medical and administrative routes disjoint on purpose: adding a 법률의견서 to
`medical_document_kind` would contaminate Stage 2's classification contract,
which types medical records. So a field routed to `accident_context` can never
reach a legal opinion however plainly the opinion answers it.

Measured on CASE_053 (2026-08-21). The case turned on whether a facility defect
caused the fall. `facility_defect_or_third_party_responsibility` resolved
`unavailable / sources_exhausted / not_mentioned` -- an honest report, since no
진료기록 discusses stair de-icing -- while the SAME case held two 법률의견서
arguing the question directly and reaching opposite conclusions (DOC_006, 10p,
claimant side: 공작물 하자 under 민법 제758조; DOC_008, 14p, insurer side: 강설
is a 자연현상, no 배상책임). `liability` therefore came back `uncertain` with
`triggered_field_ids: []`.

Three properties this round holds, each load-bearing
---------------------------------------------------
* **`unavailable`-only.** A field the common pass already answered is never
  re-opened. A 진료기록 that stated the mechanism keeps its reading, and a
  liability case whose medical records happen to cover everything pays nothing
  here. This also means a legal opinion never overwrites a clinical fact.
* **`in_play`, not `applicable`.** The trigger accepts every verdict except
  `not_applicable`, so `uncertain` counts. Requiring a decided verdict would be
  circular on exactly the cases this exists for: the type is uncertain BECAUSE
  its deciding field is unavailable.
* **The source axis is honest.** An observation from here carries
  `source_document_type` (the coarse manifest type) and NOT
  `source_document_kind`. The retired industrial branch defaulted to
  `other_medical`, which would have labelled a lawyer's letter a medical
  record; the result schema now refuses both axes at once.

This module is deliberately free of I/O and of provider calls: it decides WHAT
to read and WHICH fields are eligible, and the driver supplies the reader.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import claim_analysis_selection as selection


# The verdict that closes a round. Every other verdict -- including `uncertain`
# -- leaves the type in play. See the module docstring for why.
CLOSED_VERDICTS = frozenset({"not_applicable"})

# A field is eligible for a second attempt only if the common pass reached the
# end of its sources without a value. `conflict` is NOT re-opened: two sources
# already disagree, and adding a third reading is adjudication, which belongs
# to consistency_check rather than to an extraction round.
RETRYABLE_STATUSES = frozenset({"unavailable"})


def case_types_in_play(
    assessments: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Case types whose additional round may run, in stable order."""
    return sorted({
        row["case_type"] for row in assessments or []
        if row.get("case_type")
        and row.get("status") not in CLOSED_VERDICTS
    })


def rounds_for(
    config: Mapping[str, Any],
    assessments: Sequence[Mapping[str, Any]],
) -> list[tuple[str, Mapping[str, Any]]]:
    """(case_type, round) pairs that are live for this case.

    A round declaring no fields contributes nothing and is dropped here rather
    than later, so the caller never sees an entry that cannot produce a read.
    """
    declared = config.get("additional_fields_by_case_type") or {}
    live = []
    for case_type in case_types_in_play(assessments):
        row = declared.get(case_type)
        if not row or not row.get("field_ids") or not row.get("sources"):
            continue
        live.append((case_type, row))
    return live


def eligible_field_ids(
    config: Mapping[str, Any],
    assessments: Sequence[Mapping[str, Any]],
    outcomes_by_field: Mapping[str, Any],
) -> dict[str, list[str]]:
    """field_id -> the case types asking for it, for fields worth re-opening.

    The union across types is deliberate: a case that is both 산재 and 배상
    re-opens the union of both rounds' fields, and one field wanted by two
    types is read once, not twice.
    """
    wanted: dict[str, list[str]] = {}
    for case_type, row in rounds_for(config, assessments):
        for field_id in row["field_ids"]:
            outcome = outcomes_by_field.get(field_id)
            if outcome is None:
                # The common pass produces an outcome for every configured
                # field, so absence means the field is not in this run at all.
                continue
            if getattr(outcome, "status", None) not in RETRYABLE_STATUSES:
                continue
            wanted.setdefault(field_id, []).append(case_type)
    return {field_id: sorted(types) for field_id, types in wanted.items()}


def source_types_for(
    config: Mapping[str, Any],
    assessments: Sequence[Mapping[str, Any]],
    field_id: str,
) -> list[str]:
    """The coarse document types to try for one field, in priority order.

    Order comes from the declaring round's `sources`. Where two live types both
    claim the field, the first type's order leads and the second only appends
    what the first did not name -- so a source is never read twice and the
    stronger-evidence source (a 법률의견서 over a covering letter) keeps its
    lead.
    """
    ordered: list[str] = []
    for _case_type, row in rounds_for(config, assessments):
        if field_id not in row["field_ids"]:
            continue
        for source_type in row["sources"]:
            if source_type not in ordered:
                ordered.append(source_type)
    return ordered


def documents_for_source(
    documents: Sequence[selection.DocumentRef],
    source_type: str,
) -> list[str]:
    """Document ids of one coarse type, in stable id order.

    Several documents can share a type -- CASE_053 held two `legal_opinion`s --
    and both are returned. Reading both is what lets a critical field spend its
    comparison budget on genuinely independent sources and surface a real
    disagreement instead of taking whichever happened to be first.
    """
    return sorted(
        ref.document_id for ref in documents
        if getattr(ref, "document_type", None) == source_type
    )


def read_plan(
    config: Mapping[str, Any],
    assessments: Sequence[Mapping[str, Any]],
    outcomes_by_field: Mapping[str, Any],
    documents: Sequence[selection.DocumentRef],
) -> list[dict[str, Any]]:
    """The whole round as an ordered list of (document, fields) reads.

    Batched the same way the common pass batches: every eligible field wanting
    the same document shares one provider call. Documents are ordered by the
    highest-priority source that names them, so a 법률의견서 is read before an
    insurer letter even when both answer the same field.
    """
    wanted = eligible_field_ids(config, assessments, outcomes_by_field)
    if not wanted:
        return []

    # (document_id -> {"rank": best rank, "fields": [...], "source_type": str})
    plan: dict[str, dict[str, Any]] = {}
    for field_id, case_types in sorted(wanted.items()):
        for rank, source_type in enumerate(
                source_types_for(config, assessments, field_id), start=1):
            for document_id in documents_for_source(documents, source_type):
                entry = plan.setdefault(document_id, {
                    "document_id": document_id,
                    "source_type": source_type,
                    "priority_rank": rank,
                    "field_ids": [],
                    "field_ranks": {},
                    "case_types": set(),
                })
                # A document's own rank is the BEST rank any field gives it,
                # which is what orders the reads. Each field also keeps its own
                # rank for that document: an insurer letter is rank 1 for the
                # 산재 filing fields (they name no other source) and rank 2 for
                # a liability field that ranks the 법률의견서 first, and the
                # observation must record the rank for the field it answers,
                # not the document's headline rank.
                if rank < entry["priority_rank"]:
                    entry["priority_rank"] = rank
                    entry["source_type"] = source_type
                if field_id not in entry["field_ids"]:
                    entry["field_ids"].append(field_id)
                entry["field_ranks"][field_id] = rank
                entry["case_types"].update(case_types)

    ordered = sorted(
        plan.values(), key=lambda e: (e["priority_rank"], e["document_id"]))
    for entry in ordered:
        entry["field_ids"] = sorted(entry["field_ids"])
        entry["case_types"] = sorted(entry["case_types"])
    return ordered
