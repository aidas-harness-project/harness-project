"""Selective Claim Analysis driver (routing config v0.1).

This is the gated replacement spine for `run_claim_analysis.py`'s
read-everything CP1. It reads documents **per field, in priority order, and
stops at the first trusted value**, instead of handing every claim-side
document to one extraction call.

What it owns:

* the wave loop (A, then B) over `claim_analysis_selection`'s plans,
* one provider call per (field-group, document) read -- never one per case,
* the stop rule and the single extra comparison a critical field may buy,
* assembling `claim_analysis_result.json` and `claim_analysis_trace.json`.

What it deliberately does NOT own:

* **Medical authority.** `medical_variables.json` stays the sole authority for
  medical facts. Every medical-domain field here is published with
  `authority: medical_variables_projection` and binds to the revision digest;
  this driver never invents a medical fact outside that revision.
* **Conflict adjudication.** A disagreement becomes a `conflict_candidate` with
  both observations preserved and no canonical value. It is NOT written to the
  P6 ledger here -- `consistency_check` verifies candidates and owns ledger
  entry creation. Writing them here is what would make `claim_analysis`
  conflict-gate itself (it is in `dao.CONFLICT_GATED_STAGES`).
* **Run-state.** T13: the orchestrator owns `update-run-state`/`finalize-stage`.
* **Eligibility and amounts.** Out of scope per the routing config.

Governance: case data is read only through DAO subcommands, every citation is
verified against served page text before anything is persisted, and the DAO
re-verifies exact ranges and the medical revision at write time.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import claim_analysis_case_types as case_types_mod
import claim_analysis_selection as selection
import medical_document_routing as routing

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"

STAGE = "claim_analysis"
COMPONENT = "claim-analysis"
RESULT_CONTRACT = "claim_analysis_result.json"
RESULT_SCHEMA = "claim_analysis_result.schema.json"
TRACE_CONTRACT = "claim_analysis_trace.json"
TRACE_SCHEMA = "claim_analysis_trace.schema.json"
VERSION = "claim_analysis_selective.v0.1"

# Domains whose facts are medical and therefore projections of the canonical
# medical revision rather than claim-native findings. `event_timeline` is the
# claim-native one: accident circumstances are a claim fact, not a clinical
# variable.
MEDICAL_DOMAINS = frozenset({
    "diagnosis", "diagnosis_basis", "treatment", "clinical_course_outcome",
    "prior_history_influences", "complications_new_problems", "disability",
})


# --------------------------------------------------------------- DAO edge --

def _dao_json(args: list[str], *, allow_missing: bool = False) -> dict | None:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        if allow_missing and "NOT_FOUND:" in (proc.stdout or ""):
            return None
        raise RuntimeError((proc.stdout or proc.stderr or "DAO command failed").strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DAO command returned non-JSON output: {args[0]}") from exc


def _dao_write(args: list[str]) -> None:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        raise RuntimeError((proc.stdout or proc.stderr or "DAO write failed").strip())


def _temp_json(value: Mapping[str, Any]) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", encoding="utf-8", delete=False)
    try:
        json.dump(value, handle, ensure_ascii=False)
    finally:
        handle.close()
    return Path(handle.name)


# ------------------------------------------------------- document intake --

def classified_documents(manifest: Mapping[str, Any]) -> list[selection.DocumentRef]:
    """Stage 2's fine-grained classifications, as the planner's document view.

    A document with no `medical_classification` block is NOT assumed
    non-medical: the block's own schema says its absence means the classifier
    has not run. Such a document carries `kind=None` and is therefore never
    routed -- it cannot silently stand in for a form kind it was never typed as.
    """
    documents: list[selection.DocumentRef] = []
    for entry in manifest.get("documents") or []:
        if not isinstance(entry, dict):
            continue
        doc_id = entry.get("document_id")
        if not isinstance(doc_id, str):
            continue
        block = entry.get("medical_classification")
        if not isinstance(block, dict):
            documents.append(selection.DocumentRef(doc_id, None))
            continue
        status = block.get("status")
        documents.append(selection.DocumentRef(
            document_id=doc_id,
            kind=block.get("kind") if status in {
                "deterministic_title", "llm_classified"
            } else None,
            ambiguous=status == "ambiguous",
        ))
    return sorted(documents, key=lambda ref: ref.document_id)


def _page_text_index(bundle: Mapping[str, Any]) -> dict[tuple[str, int], str]:
    return {
        (document["document_id"], page["page"]): page["text"]
        for document in bundle.get("documents") or []
        for page in document.get("pages") or []
    }


# --------------------------------------------------- evidence verification --

def locate_exact(text: str, quote: str) -> tuple[int, int] | None:
    """The exact character range of `quote` in `text`, if it occurs exactly once.

    Ambiguity is refused rather than resolved: a quote occurring twice cannot
    say which occurrence it means, and `strict_evidence_reference` exists
    precisely because quote equality is not a binding relation. Returning None
    makes the caller drop the observation instead of guessing an offset the DAO
    would later have to catch.
    """
    if not quote:
        return None
    first = text.find(quote)
    if first < 0:
        return None
    if text.find(quote, first + 1) >= 0:
        return None
    return first, first + len(quote)


def build_reference(
    document_id: str,
    page: int,
    quote: str,
    page_text: str,
) -> dict | None:
    located = locate_exact(page_text, quote)
    if located is None:
        return None
    start, end = located
    return {
        "document_id": document_id,
        "page": page,
        "quote": quote,
        "start_char": start,
        "end_char": end,
    }


# ------------------------------------------------------------ extraction --

class FieldExtractionOutcome:
    """One field's resolved state after its read ladder ran."""

    __slots__ = ("field_id", "domain_code", "grade", "status", "observations",
                 "canonical_ids", "stop_reason", "reason", "documents_read",
                 "comparisons")

    def __init__(self, *, field_id: str, domain_code: str, grade: str) -> None:
        self.field_id = field_id
        self.domain_code = domain_code
        self.grade = grade
        self.status = "unknown"
        self.observations: list[dict] = []
        self.canonical_ids: list[str] = []
        self.stop_reason = "sources_exhausted"
        self.reason = "no source in the routed priority order stated this field"
        self.documents_read = 0
        self.comparisons = 0


def _values_disagree(left: Any, right: Any) -> bool:
    """Whether two extracted values are a real contradiction.

    A missing mention is never a conflict (`missing_mention_is_conflict:
    false`): only two *asserted* values that differ are. String comparison is
    whitespace-normalized so a line-wrap is not mistaken for a disagreement.
    """
    def normal(value: Any) -> Any:
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, list):
            return tuple(normal(item) for item in value)
        return value

    return normal(left) != normal(right)


def resolve_field(
    plan: selection.FieldPlan,
    field_row: Mapping[str, Any],
    config: Mapping[str, Any],
    extract,
    page_text: Mapping[tuple[str, int], str],
    observation_ids,
) -> FieldExtractionOutcome:
    """Walk one field's priority ladder and apply the stop rule.

    `extract(field_row, document_id, kind, rank)` returns either None (this
    document does not state the field) or a dict with `value`, `page`, `quote`.
    It is injected so the ordering logic is testable without a provider.
    """
    outcome = FieldExtractionOutcome(
        field_id=plan.field_id,
        domain_code=plan.domain_code,
        grade=field_row["medical_advisory_grade"],
    )
    if plan.skip_reason is not None:
        outcome.status = "not_applicable"
        outcome.stop_reason = "not_applicable"
        outcome.reason = plan.skip_reason
        return outcome

    budget = selection.comparison_budget(field_row, config)
    trusted: dict | None = None

    for step in plan.steps:
        for document_id, kind in zip(step.document_ids, step.document_kinds):
            if trusted is not None and outcome.comparisons >= budget:
                break
            found = extract(field_row, document_id, kind, step.priority_rank)
            outcome.documents_read += 1
            if found is None:
                continue
            text = page_text.get((document_id, found["page"]))
            if text is None:
                continue
            reference = build_reference(
                document_id, found["page"], found["quote"], text)
            if reference is None:
                # An unverifiable or ambiguous quote is dropped, never repaired.
                continue
            if not selection.is_trusted_value(
                from_priority_source=True,
                has_exact_quote=True,
                complete=bool(found.get("complete", True)),
                unambiguous=bool(found.get("unambiguous", True)),
            ):
                continue
            observation = {
                "observation_id": next(observation_ids),
                "value_state": "asserted",
                "value": found["value"],
                "source_document_kind": kind,
                "source_priority_rank": step.priority_rank,
                "extraction_wave": plan.wave,
                "evidence_references": [reference],
            }
            outcome.observations.append(observation)
            if trusted is None:
                trusted = observation
                if budget == 0:
                    break
            else:
                outcome.comparisons += 1
                if _values_disagree(trusted["value"], observation["value"]):
                    # Both readings are preserved and NEITHER becomes canonical.
                    outcome.status = "conflict"
                    outcome.stop_reason = "conflict_found"
                    outcome.canonical_ids = []
                    outcome.reason = (
                        "two independent priority sources state different "
                        "values for this field; both readings are preserved "
                        "for consistency_check to verify"
                    )
                    return outcome
                break
        if trusted is not None and outcome.comparisons >= budget:
            break

    if trusted is not None:
        outcome.status = "resolved"
        outcome.stop_reason = "trusted_value_found"
        outcome.canonical_ids = [trusted["observation_id"]]
        outcome.reason = "a trusted value was found in the highest available priority source"
    return outcome


def _observation_id_sequence(start: int = 1):
    counter = start
    while True:
        yield f"CAO_{counter:04d}"
        counter += 1


def _candidate_id_sequence(start: int = 1):
    counter = start
    while True:
        yield f"CAC_{counter:04d}"
        counter += 1


def field_result(
    outcome: FieldExtractionOutcome,
    *,
    priority_grade: str,
    authority: str,
    conflict_candidate_ids: Sequence[str] = (),
) -> dict:
    result = {
        "field_id": outcome.field_id,
        "domain_code": outcome.domain_code,
        "priority_grade": priority_grade,
        "authority": authority,
        "resolution_status": outcome.status,
        "canonical_observation_ids": list(outcome.canonical_ids),
        "observations": list(outcome.observations),
        "conflict_candidate_ids": list(conflict_candidate_ids),
        "stop_reason": outcome.stop_reason,
    }
    if outcome.status != "resolved":
        result["resolution_reason"] = outcome.reason
    if outcome.status in {"unknown", "not_applicable"}:
        # The schema requires every retained observation to match the field's
        # own unresolved state, so an asserted reading cannot hide under an
        # "unknown" verdict.
        state = outcome.status
        result["observations"] = [{
            "observation_id": observation["observation_id"],
            "value_state": state,
            "reason": outcome.reason,
            "extraction_wave": observation["extraction_wave"],
            "evidence_references": [],
        } for observation in outcome.observations]
    return result


def authority_for(domain_code: str) -> str:
    return (
        "medical_variables_projection" if domain_code in MEDICAL_DOMAINS
        else "claim_analysis_native"
    )


def priority_grade_for(field_row: Mapping[str, Any]) -> str:
    """The published grade, clamped to the schema's A/B axis.

    A `C` field only reaches extraction through a named operational override,
    and it rides the B wave when it does; publishing it as `B` states that
    honestly rather than inventing a `C` the downstream contract has no slot
    for.
    """
    grade = field_row["medical_advisory_grade"]
    return "A" if grade == "A" else "B"


# ------------------------------------------------------------- assembly --

def build_result(
    *,
    case_id: str,
    run_id: str,
    outcomes: Sequence[FieldExtractionOutcome],
    config: Mapping[str, Any],
    documents: Sequence[selection.DocumentRef],
    medical_revision: Mapping[str, Any],
    policy_links: Sequence[Mapping[str, Any]] = (),
    filing_status_by_type: Mapping[str, str] | None = None,
    model_name: str = "deterministic",
    prompt_version: str = VERSION,
) -> dict:
    by_field = {row["field_id"]: row for row in config.get("fields") or []}
    candidate_ids = _candidate_id_sequence()

    claim_facts: list[dict] = []
    conflict_candidates: list[dict] = []
    for outcome in outcomes:
        field_row = by_field[outcome.field_id]
        attached: list[str] = []
        if outcome.status == "conflict":
            candidate_id = next(candidate_ids)
            attached.append(candidate_id)
            conflict_candidates.append({
                "conflict_candidate_id": candidate_id,
                "field_id": outcome.field_id,
                "observation_ids": [
                    observation["observation_id"]
                    for observation in outcome.observations
                ],
                "reason": outcome.reason,
                "consistency_status": "pending_consistency_check",
            })
        claim_facts.append(field_result(
            outcome,
            priority_grade=priority_grade_for(field_row),
            authority=authority_for(outcome.domain_code),
            conflict_candidate_ids=attached,
        ))

    assessments = case_types_mod.assess_case_types(
        claim_facts, filing_status_by_type=filing_status_by_type)
    checklist = selection.presence_checklist(
        config, documents, case_types_mod.selected_case_types(assessments))

    warnings: list[str] = []
    if conflict_candidates:
        warnings.append(
            f"{len(conflict_candidates)} conflict candidate(s) recorded for "
            "consistency_check to verify; no ledger entry is created here"
        )

    result = {
        "case_id": case_id,
        "run_id": run_id,
        "component": COMPONENT,
        "status": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_info": {"model_name": model_name, "prompt_version": prompt_version},
        # `confidence` is deliberately OMITTED, not set to a number. Field-level
        # trust here is the four-condition stop rule, which is a decision rather
        # than a score; publishing a synthetic contract-level confidence would
        # be a fabricated measurement (P1).
        "review_required": bool(conflict_candidates),
        "warnings": warnings,
        "source_grounded": True,
        "schema_version": "claim_analysis_result.v0.1",
        "config_version": config["config_version"],
        "medical_revision": dict(medical_revision),
        "claim_facts": claim_facts,
        "case_type_assessment": assessments,
        "policy_links": list(policy_links),
        "required_document_checklist": checklist,
        "conflict_candidates": conflict_candidates,
    }
    if result["review_required"]:
        # Only meaningful alongside review_required; the conflict candidates are
        # a 손해사정사's call to verify, which is consistency_check's input.
        result["reviewer_role"] = "손해사정사"
    return result


def build_trace(
    *,
    case_id: str,
    run_id: str,
    config: Mapping[str, Any],
    outcomes: Sequence[FieldExtractionOutcome],
    document_dispositions: Sequence[Mapping[str, Any]],
    provider_calls: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    wall_time_seconds: float | None = None,
) -> dict:
    """Development/performance trace, kept out of the authority contract."""
    presence_only = sum(
        1 for row in document_dispositions
        if row.get("disposition") == "presence_only"
    )
    read = sum(
        1 for row in document_dispositions if row.get("disposition") == "read"
    )
    return {
        "case_id": case_id,
        "run_id": run_id,
        "schema_version": "claim_analysis_trace.v0.1",
        "config_version": config["config_version"],
        "documents": list(document_dispositions),
        "field_stops": [{
            "field_id": outcome.field_id,
            "stop_reason": outcome.stop_reason,
            "documents_read": outcome.documents_read,
            "additional_comparisons": outcome.comparisons,
        } for outcome in outcomes],
        "metrics": {
            "documents_considered": len(document_dispositions),
            "documents_read": read,
            "documents_presence_only": presence_only,
            "a_stop_fields": sum(
                1 for outcome in outcomes
                if outcome.grade == "A" and outcome.status == "resolved"
            ),
            "b_fallback_fields": sum(
                1 for outcome in outcomes
                if outcome.grade != "A" and outcome.status == "resolved"
            ),
            "provider_calls": provider_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "wall_time_seconds": wall_time_seconds,
        },
    }


def publish(
    *,
    case_id: str,
    run_id: str,
    held_by: str,
    result: Mapping[str, Any],
    trace: Mapping[str, Any],
) -> None:
    """Write both contracts through the DAO, result first.

    The result carries the exact-evidence and medical-revision gates, so it is
    written first: a trace describing reads whose result was refused would be a
    record of work that produced no governed output.
    """
    for contract_name, schema_name, data in (
        (RESULT_CONTRACT, RESULT_SCHEMA, result),
        (TRACE_CONTRACT, TRACE_SCHEMA, trace),
    ):
        data_file = _temp_json(data)
        try:
            _dao_write([
                "write-contract", case_id, contract_name,
                "--data-file", str(data_file), "--schema-name", schema_name,
                "--held-by", held_by, "--run-id", run_id, "--stage", STAGE,
            ])
        finally:
            data_file.unlink(missing_ok=True)


def require_open_attempt(case_id: str, run_id: str) -> None:
    """T13: the orchestrator owns run-state; this driver only checks it."""
    state = _dao_json(
        ["read-contract", case_id, "_run_state.json", "--run-id", run_id])
    if not any(
        item.get("stage_name") == STAGE and item.get("status") == "in_progress"
        for item in (state or {}).get("stages", [])
    ):
        raise RuntimeError(
            "BLOCKED: claim_analysis must be in_progress; the orchestrator owns "
            "attempt state")


def require_enabled(config: Mapping[str, Any]) -> None:
    if not routing.routing_enabled(config):
        raise RuntimeError(
            "BLOCKED: selective claim analysis is disabled "
            "(behavior_enabled=false). Activation requires a recorded "
            "`activation` block in the routing config.")
