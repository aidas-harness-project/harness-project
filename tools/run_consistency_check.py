"""Consistency Check driver: verify Claim Analysis's conflict candidates.

Claim Analysis records a disagreement as a `conflict_candidate` and stops. It
does not write the P6 ledger, because `claim_analysis` sits in
`dao.CONFLICT_GATED_STAGES` -- a stage that raised its own pending entry could
never finalize. `consistency_check` is deliberately NOT in that set (it is the
stage that raises conflicts), so ledger creation belongs here.

What this stage does:

1. Read `claim_analysis_result.json`'s candidates.
2. Keep only candidates on **decision-changing** fields -- the routing config's
   `critical_conflict_field` set. A disagreement on a field that cannot change
   a determination is recorded as `not_material` and does not become a ledger
   entry; raising it would spend a human's attention for nothing.
3. Confirm each surviving candidate is a **real contradiction**: two asserted
   values that actually differ, each with its own exact evidence. A missing
   mention, a silence, or one side lacking evidence is `withdrawn`, never a
   conflict.
4. Register the confirmed ones -- and only those -- through
   `dao.py add-conflict-entry` with `verdict: pending`.

What it deliberately does NOT do:

* **Choose a disposition.** Every entry is created `pending`. `resolved`,
  `false_positive`, and `deferred_to_report` are human calls under P6; this
  driver never auto-defers, which would turn a real disagreement into a
  bookkeeping step.
* **Re-verify every field.** The "second independent source" check is scoped to
  critical fields by design; applying it everywhere is the read-amplification
  the selective design exists to remove.
* **Expose development reasoning.** `evidence_validation_result.json` keeps the
  audit trail; the operational report gets findings, not debug commentary.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import claim_analysis_selection as selection

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"

STAGE = "consistency_check"
COMPONENT = "consistency-check"
CONTRACT = "evidence_validation_result.json"
SCHEMA = "evidence_validation_result.schema.json"
VERSION = "consistency_check_driver.v0.1"


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


def _dao_write(args: list[str]) -> str:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        raise RuntimeError((proc.stdout or proc.stderr or "DAO write failed").strip())
    return proc.stdout or ""


def _temp_json(value: Any) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", encoding="utf-8", delete=False)
    try:
        json.dump(value, handle, ensure_ascii=False)
    finally:
        handle.close()
    return Path(handle.name)


def critical_field_ids(config: Mapping[str, Any]) -> set[str]:
    """Fields whose disagreement can change a determination.

    Read from the routing config rather than hard-coded here, so the operational
    definition of "important" lives in one reviewable place. It covers exactly
    the decision-bearing facts: accident date and circumstances, primary
    diagnosis, site and laterality, surgery name and date, and the disability
    values.
    """
    return {
        row["field_id"] for row in config.get("fields") or []
        if row.get("critical_conflict_field")
    }


def _observations_by_id(field: Mapping[str, Any]) -> dict[str, dict]:
    return {
        observation["observation_id"]: observation
        for observation in field.get("observations") or []
    }


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def classify_candidate(
    candidate: Mapping[str, Any],
    field: Mapping[str, Any] | None,
    critical_ids: set[str],
) -> tuple[str, str, list[dict]]:
    """Verdict for one candidate: (outcome, reason, ledger sources).

    `outcome` is one of:
      * `confirmed`     -- a real, material contradiction; becomes a P6 entry.
      * `not_material`  -- real, but on a field that cannot change a decision.
      * `withdrawn`     -- not a contradiction after inspection.
    """
    field_id = candidate.get("field_id")
    if field is None:
        return "withdrawn", (
            "the candidate names a field that carries no claim fact"
        ), []

    observations = _observations_by_id(field)
    referenced = [
        observations.get(observation_id)
        for observation_id in candidate.get("observation_ids") or []
    ]
    present = [row for row in referenced if isinstance(row, dict)]
    if len(present) != len(referenced) or len(present) < 2:
        return "withdrawn", (
            "fewer than two of the cited observations survive in the field"
        ), []

    asserted = [row for row in present if row.get("value_state") == "asserted"]
    if len(asserted) < 2:
        # Silence on one side is not a disagreement -- the routing config's
        # `missing_mention_is_conflict: false`, enforced here rather than
        # assumed.
        return "withdrawn", (
            "only one source asserts a value; a missing mention is not a "
            "contradiction"
        ), []

    values = {_stringify(row.get("value")) for row in asserted}
    if len(values) < 2:
        return "withdrawn", (
            "the cited sources agree once their values are compared directly"
        ), []

    for row in asserted:
        if not row.get("evidence_references"):
            return "withdrawn", (
                "a cited reading carries no exact source evidence, so the "
                "disagreement cannot be established"
            ), []

    if field_id not in critical_ids:
        return "not_material", (
            "the sources differ, but this field does not by itself change a "
            "coverage or type determination"
        ), []

    sources: list[dict] = []
    for row in asserted:
        reference = (row.get("evidence_references") or [])[0]
        sources.append({
            "document_id": reference["document_id"],
            "page": reference["page"],
            "value": _stringify(row.get("value")),
            "quote": reference["quote"],
        })
    return "confirmed", (
        "two independent sources state materially different values for a "
        "decision-bearing field"
    ), sources


def verify_candidates(
    result: Mapping[str, Any],
    config: Mapping[str, Any],
) -> list[dict]:
    """Classify every candidate. Pure: no DAO writes, no ledger side effects."""
    critical_ids = critical_field_ids(config)
    fields = {row["field_id"]: row for row in result.get("claim_facts") or []}
    verdicts: list[dict] = []
    for candidate in result.get("conflict_candidates") or []:
        field = fields.get(candidate.get("field_id"))
        outcome, reason, sources = classify_candidate(candidate, field, critical_ids)
        verdicts.append({
            "conflict_candidate_id": candidate.get("conflict_candidate_id"),
            "field_id": candidate.get("field_id"),
            "outcome": outcome,
            "reason": reason,
            "sources": sources,
        })
    return verdicts


def register_confirmed(
    case_id: str,
    run_id: str,
    held_by: str,
    verdicts: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Create one `pending` P6 entry per confirmed conflict.

    Returns {conflict_candidate_id: conflict_id}. Every entry is created
    `pending`: choosing among the three dispositions is a human decision, and
    auto-`deferred_to_report` in particular would silently take on the screening
    report's carry obligation without anyone deciding it should.
    """
    registered: dict[str, str] = {}
    for verdict in verdicts:
        if verdict.get("outcome") != "confirmed":
            continue
        sources_file = _temp_json(verdict["sources"])
        try:
            output = _dao_write([
                "add-conflict-entry", case_id,
                "--stage", STAGE,
                "--topic", verdict["field_id"],
                "--sources-file", str(sources_file),
                "--held-by", held_by, "--run-id", run_id,
                "--operation-id", str(uuid.uuid4()),
            ])
        finally:
            sources_file.unlink(missing_ok=True)
        conflict_id = None
        for token in (output or "").replace(":", " ").split():
            if token.startswith("CONFLICT_"):
                conflict_id = token.strip(".,")
                break
        if conflict_id is None:
            raise RuntimeError(
                "add-conflict-entry did not report a conflict id for "
                f"{verdict['conflict_candidate_id']}: {output.strip()[:200]}"
            )
        registered[verdict["conflict_candidate_id"]] = conflict_id
    return registered


def build_contract(
    *,
    case_id: str,
    run_id: str,
    verdicts: Sequence[Mapping[str, Any]],
    registered: Mapping[str, str],
) -> dict:
    """`evidence_validation_result.json` -- the stage's audit record.

    One check per candidate actually examined. `conflict_id` is set exactly for
    the confirmed ones and null otherwise, which is the schema's own rule.
    """
    checks = []
    for index, verdict in enumerate(verdicts, start=1):
        confirmed = verdict["outcome"] == "confirmed"
        # `values_compared` needs at least one attributed value even for a
        # withdrawn candidate, so a check always says what it looked at.
        compared = [{
            "document_id": source["document_id"],
            "page": source["page"],
            "quote": source["quote"],
        } for source in verdict.get("sources") or []]
        if not compared:
            compared = [{"quote": verdict["reason"]}]
        checks.append({
            "check_id": f"CHK-{index}",
            "topic": (
                f"{verdict['field_id']} ({verdict['conflict_candidate_id']}): "
                f"{verdict['outcome']}"
            ),
            "values_compared": compared,
            "result": "inconsistent" if confirmed else "consistent",
            "conflict_id": registered.get(verdict["conflict_candidate_id"]),
        })
    warnings: list[str] = []
    unregistered = [
        verdict["conflict_candidate_id"] for verdict in verdicts
        if verdict["outcome"] == "confirmed"
        and verdict["conflict_candidate_id"] not in registered
    ]
    if unregistered:
        warnings.append(
            "confirmed conflicts without a ledger entry: " + ", ".join(unregistered)
        )
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": COMPONENT,
        "status": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_info": {"model_name": "deterministic", "prompt_version": VERSION},
        "review_required": bool(registered),
        "warnings": warnings,
        "source_grounded": True,
        "checks": checks,
    }
    if contract["review_required"]:
        # A registered conflict is a 손해사정사's disposition call under P6 --
        # this stage records the finding, it never picks a verdict.
        contract["reviewer_role"] = "손해사정사"
    return contract


def require_open_attempt(case_id: str, run_id: str) -> None:
    state = _dao_json(
        ["read-contract", case_id, "_run_state.json", "--run-id", run_id])
    if not any(
        item.get("stage_name") == STAGE and item.get("status") == "in_progress"
        for item in (state or {}).get("stages", [])
    ):
        raise RuntimeError(
            "BLOCKED: consistency_check must be in_progress; the orchestrator "
            "owns attempt state")
