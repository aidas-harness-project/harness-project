"""Consistency Check helper: prepare work items, then register agent verdicts.

The judgement -- is this pair of readings a real contradiction? -- belongs to
the `consistency-check` agent. This module is the deterministic half on either
side of that judgement:

    prepare   read claim_analysis_result.json -> consistency_check_workitems.json
              (both readings, their exact evidence, whether the field is
              decision-bearing. No verdict field: a prepared answer would make
              the decision by suggestion.)

    <agent>   confirmed / not_material / withdrawn, plus a neutral
              professional_summary on every confirmed one.

    register  verify the verdicts bind to what was prepared, register the
              confirmed ones in the P6 ledger as `pending`, and write the
              stage's audit contract.

Why the split rather than one deterministic driver: whether "우측" and "좌측"
on two forms are a genuine contradiction or a transcription artefact is a
reading of the documents, not a string comparison. What IS mechanical -- that
both sides are evidenced, that a verdict matches the candidate it was written
against, that nothing reaches the ledger twice -- lives here.

What this module never does:

* **Decide.** With no verdicts file, `register` registers nothing. It cannot
  reach a verdict on its own, which is the point.
* **Dispose.** Every entry is created `pending`. `resolved`, `false_positive`,
  and `deferred_to_report` are human calls under P6. A confirmed verdict says
  the conflict is real, not what should happen about it.
* **Re-read the case.** Source documents and page chunks are not opened here;
  everything comes from the upstream contract (Notion 3-10).
* **Move run state.** T13: the orchestrator owns update-run-state/finalize-stage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import claim_analysis_contracts as contracts

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"

STAGE = "consistency_check"
COMPONENT = "consistency-check"
CONTRACT = "evidence_validation_result.json"
SCHEMA = "evidence_validation_result.schema.json"
WORKITEMS_CONTRACT = "consistency_check_workitems.json"
WORKITEMS_SCHEMA = "consistency_check_workitems.schema.json"
VERDICTS_CONTRACT = "consistency_check_verdicts.json"
VERSION = "consistency_check_helper.v0.1"

# Phrases that elect a winner. This is a DELIBERATELY LIMITED denylist, not a
# semantic neutrality check: it catches the blunt forms of picking a side and
# nothing subtler. A summary that passes has not been proven neutral -- it has
# only avoided these patterns. The agent and the human reviewer remain
# responsible for the rest, and the failure message says which pattern matched
# so the author can see what to change.
ASSERTIVE_PATTERNS = (
    "이 맞다", "이 맞습니다", "가 맞다", "가 맞습니다",
    "로 확정", "으로 확정", "확정된다", "확정됩니다",
    "이 정확", "가 정확", "정확하다", "정확합니다",
    "틀렸다", "틀립니다", "오기이다", "오기입니다",
    "채택한다", "채택합니다", "우선한다", "우선합니다",
)


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


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest_of(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ----------------------------------------------------------------- prepare --

def critical_field_ids(config: Mapping[str, Any]) -> set[str]:
    """Fields whose disagreement can change a determination.

    Read from the routing config rather than hard-coded, so the operational
    definition of "decision-bearing" lives in one reviewable place.
    """
    return {
        row["field_id"] for row in config.get("fields") or []
        if row.get("critical_conflict_field")
    }


def candidate_digest(
    candidate: Mapping[str, Any],
    readings: Sequence[Mapping[str, Any]],
) -> str:
    """Digest over the candidate AND every reading it cites.

    Covering the readings is what makes the binding meaningful: a candidate
    whose id and field are unchanged but whose underlying values were re-read
    is a different disagreement, and a verdict written about the old one must
    not silently apply to it.
    """
    return digest_of({
        "conflict_candidate_id": candidate.get("conflict_candidate_id"),
        "field_id": candidate.get("field_id"),
        "observation_ids": list(candidate.get("observation_ids") or []),
        "readings": [
            {
                "observation_id": row.get("observation_id"),
                "value_state": row.get("value_state"),
                "value": row.get("value"),
                "evidence_references": row.get("evidence_references") or [],
            }
            for row in readings
        ],
    })


def build_work_items(
    result: Mapping[str, Any],
    config: Mapping[str, Any],
) -> list[dict]:
    """One item per conflict candidate. Facts only -- no verdict."""
    critical = critical_field_ids(config)
    labels = {row["field_id"]: row.get("label", row["field_id"])
              for row in config.get("fields") or []}
    fields = {row["field_id"]: row for row in result.get("claim_facts") or []}

    items: list[dict] = []
    for candidate in result.get("conflict_candidates") or []:
        field_id = candidate.get("field_id")
        field = fields.get(field_id) or {}
        by_id = {row["observation_id"]: row
                 for row in field.get("observations") or []}
        readings = []
        for observation_id in candidate.get("observation_ids") or []:
            observation = by_id.get(observation_id)
            if observation is None:
                continue
            readings.append({
                "observation_id": observation_id,
                "value_state": observation.get("value_state"),
                "value": _stringify(observation.get("value")) or None,
                "source_document_kind": observation.get("source_document_kind"),
                "evidence_references": [
                    dict(reference)
                    for reference in observation.get("evidence_references") or []
                ],
            })
        if not readings:
            # Nothing to judge: the candidate cites no surviving reading. Emitted
            # with an empty ladder it would invite a verdict about nothing, so it
            # is skipped and reported as unprepared by `register`.
            continue
        items.append({
            "conflict_candidate_id": candidate.get("conflict_candidate_id"),
            "candidate_digest": candidate_digest(candidate, readings),
            "field_id": field_id,
            "field_label": labels.get(field_id, field_id),
            "decision_bearing": field_id in critical,
            "readings": readings,
        })
    return items


def build_workitems_contract(
    *,
    case_id: str,
    run_id: str,
    work_items: Sequence[Mapping[str, Any]],
    claim_analysis_sha256: str,
) -> dict:
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": COMPONENT,
        "status": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_info": {"model_name": "deterministic", "prompt_version": VERSION},
        "review_required": bool(work_items),
        "warnings": [],
        "source_grounded": True,
        "schema_version": "consistency_check_workitems.v0.1",
        "claim_analysis_sha256": claim_analysis_sha256,
        "work_items": [dict(item) for item in work_items],
    }
    if work_items:
        contract["reviewer_role"] = "손해사정사"
    return contract


# ---------------------------------------------------------------- register --

def _structured_tokens(text: str) -> list[str]:
    """Dates and amounts appearing in the summary.

    Restricted to structured forms on purpose: a date or a figure asserts a
    specific fact that must come from the record. Free prose is not checked --
    claiming to verify it would overstate what this does.
    """
    tokens: list[str] = []
    tokens += re.findall(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", text)
    tokens += re.findall(r"[0-9][0-9,]*\s*원", text)
    return tokens


def summary_problems(
    summary: str,
    item: Mapping[str, Any],
) -> list[str]:
    """The deterministic checks a professional_summary must pass.

    Scope, stated plainly: this verifies that the summary MENTIONS both values
    and invents no unsourced date or amount, and that it avoids a short list of
    phrases that pick a winner. It does NOT verify that the prose is neutral,
    accurate, or complete -- those are readings, not string operations. A clean
    result here means "no mechanical defect found", never "verified neutral".
    """
    problems: list[str] = []

    asserted = [row for row in item.get("readings") or []
                if row.get("value_state") == "asserted"]
    for row in asserted:
        value = (row.get("value") or "").strip()
        if value and value not in summary:
            problems.append(
                f"the summary does not state the value {value!r} from "
                f"{row.get('observation_id')}; a reader cannot see what disagrees"
            )

    quoted = " ".join(
        reference.get("quote", "")
        for row in item.get("readings") or []
        for reference in row.get("evidence_references") or []
    )
    grounded = quoted + " " + " ".join(
        (row.get("value") or "") for row in item.get("readings") or [])
    for token in _structured_tokens(summary):
        if token not in grounded:
            problems.append(
                f"the summary states {token!r}, which appears in no cited "
                "evidence for this candidate"
            )

    for pattern in ASSERTIVE_PATTERNS:
        if pattern in summary:
            problems.append(
                f"the summary uses {pattern!r}, which elects one reading; "
                "state both and say what needs checking instead "
                "(limited pattern check, not a neutrality guarantee)"
            )
    return problems


def validate_verdicts(
    verdicts: Sequence[Mapping[str, Any]],
    work_items: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Bind every verdict to a prepared item of the same content."""
    errors: list[str] = []
    by_id = {item["conflict_candidate_id"]: item for item in work_items}
    seen: set[str] = set()

    for verdict in verdicts:
        candidate_id = verdict.get("conflict_candidate_id")
        if candidate_id in seen:
            errors.append(f"{candidate_id}: judged more than once")
            continue
        seen.add(candidate_id)

        item = by_id.get(candidate_id)
        if item is None:
            errors.append(
                f"{candidate_id}: no prepared work item -- a verdict must be "
                "written against what prepare produced"
            )
            continue
        if verdict.get("candidate_digest") != item["candidate_digest"]:
            errors.append(
                f"{candidate_id}: the verdict was written against a different "
                "version of this candidate (digest mismatch); re-run prepare "
                "and judge the current readings"
            )
            continue
        if verdict.get("outcome") != "confirmed":
            continue

        asserted = [row for row in item["readings"]
                    if row.get("value_state") == "asserted"]
        if len(asserted) < 2:
            errors.append(
                f"{candidate_id}: confirmed, but fewer than two readings assert "
                "a value -- silence is not a contradiction"
            )
            continue
        unevidenced = [row["observation_id"] for row in asserted
                       if not row.get("evidence_references")]
        if unevidenced:
            errors.append(
                f"{candidate_id}: confirmed, but {', '.join(unevidenced)} "
                "carries no exact evidence; an ungrounded reading cannot "
                "establish a disagreement"
            )
            continue
        summary = verdict.get("professional_summary") or ""
        for problem in summary_problems(summary, item):
            errors.append(f"{candidate_id}: {problem}")
    return errors


def ledger_sources(item: Mapping[str, Any]) -> list[dict]:
    sources = []
    for row in item["readings"]:
        if row.get("value_state") != "asserted":
            continue
        reference = (row.get("evidence_references") or [])[0]
        sources.append({
            "document_id": reference["document_id"],
            "page": reference["page"],
            "value": row.get("value") or "",
            "quote": reference["quote"],
        })
    return sources


def operation_id_for(
    case_id: str, run_id: str, candidate_id: str, digest: str
) -> str:
    """A deterministic id covering the candidate AND its content.

    Deterministic so a retried register is the same operation and the DAO
    returns the entry it already made rather than minting a second one. It
    carries the digest as well as the id because a candidate whose readings
    changed is a different disagreement and deserves its own entry.

    `run_id` is deliberately NOT part of the id, though it stays in the
    signature so callers are unchanged. Including it made every re-run produce
    a fresh id, so the DAO saw a new operation and minted a duplicate entry for
    a disagreement already recorded -- the exact opposite of what the paragraph
    above promises. Measured 2026-08-26: duplicates on CASE_7015/7034/7061/
    7071/7077, and CASE_7077 ended holding CONFLICT_1 `deferred_to_report` and
    CONFLICT_2 `pending` for the same field from the same two sources, the
    pending one blocking `screening_report`.

    Dropping it is safe because the DAO compares the request envelope as well
    as the id, and `cmd_add_conflict_entry`'s payload is
    `{stage, topic, sources}` -- no run id in it. So a genuine re-run over the
    same evidence matches and returns the original entry, while a candidate
    whose readings changed still gets a new id through the digest.
    """
    return f"consistency_check:{case_id}:{candidate_id}:{digest}"


def register_confirmed(
    case_id: str,
    run_id: str,
    held_by: str,
    verdicts: Sequence[Mapping[str, Any]],
    work_items: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Create one `pending` P6 entry per confirmed verdict."""
    by_id = {item["conflict_candidate_id"]: item for item in work_items}
    registered: dict[str, str] = {}
    for verdict in verdicts:
        if verdict.get("outcome") != "confirmed":
            continue
        candidate_id = verdict["conflict_candidate_id"]
        item = by_id[candidate_id]
        sources_file = _temp_json(ledger_sources(item))
        try:
            output = _dao_write([
                "add-conflict-entry", case_id,
                "--stage", STAGE,
                "--topic", item["field_id"],
                "--sources-file", str(sources_file),
                "--held-by", held_by, "--run-id", run_id,
                "--operation-id", operation_id_for(
                    case_id, run_id, candidate_id, item["candidate_digest"]),
                "--professional-summary", verdict["professional_summary"],
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
                f"{candidate_id}: {output.strip()[:200]}"
            )
        registered[candidate_id] = conflict_id
    return registered


def build_contract(
    *,
    case_id: str,
    run_id: str,
    verdicts: Sequence[Mapping[str, Any]],
    registered: Mapping[str, str],
    work_items: Sequence[Mapping[str, Any]] = (),
) -> dict:
    """`evidence_validation_result.json` -- the stage's audit record.

    Records the set-aside candidates too, with the agent's reason. That detail
    stays HERE: the screening report carries verified findings, and showing a
    professional the candidates that were ruled out buries the finding under
    the process.

    `work_items` supplies the readings that were actually compared. Until
    2026-08-20 this function did not receive them, so `values_compared` was
    filled with the verdict's own `reason` text and `topic` fell back to the
    candidate id -- schema-valid, and therefore silently wrong: the audit
    record named no source and quoted no competing value, which is the one
    thing a reader opens it for.
    """
    prepared = {item.get("conflict_candidate_id"): item for item in work_items}
    checks = []
    for index, verdict in enumerate(verdicts, start=1):
        candidate_id = verdict["conflict_candidate_id"]
        confirmed = verdict["outcome"] == "confirmed"
        item = prepared.get(candidate_id) or {}
        field_id = verdict.get("field_id") or item.get("field_id") or candidate_id
        label = item.get("field_label") or field_id

        # One entry per reading, each carrying its own document/page/quote --
        # the comparison itself, not a description of it.
        values_compared = []
        for reading in item.get("readings") or []:
            for reference in reading.get("evidence_references") or []:
                values_compared.append(dict(reference))
        if not values_compared:
            # minItems is 1, and a prepared item always carries readings. Fall
            # back only so a missing work item cannot fail the whole write,
            # and say plainly that the sources were not recoverable.
            values_compared = [{
                "quote": verdict.get("reason")
                or f"{candidate_id}: prepared readings unavailable",
            }]

        documents = sorted({
            reference.get("document_id") for reference in values_compared
            if reference.get("document_id")
        })
        scope = f" across {'/'.join(documents)}" if documents else ""
        checks.append({
            "check_id": f"CHK-{index}",
            # Published as their own fields, not only inside `topic`: stage 7
            # has to match a check back to the claim_facts field it settled,
            # and parsing a display string to do that is exactly the kind of
            # coupling that breaks the next time the wording changes.
            "conflict_candidate_id": candidate_id,
            "field_id": field_id,
            "topic": f"{label}{scope} ({candidate_id}): {verdict['outcome']}",
            "values_compared": values_compared,
            # THREE outcomes, three results. Collapsing `not_material` into
            # `consistent` threw away the distinction the agent is asked to
            # make: `withdrawn` means the readings state the same fact, while
            # `not_material` means they genuinely differ and the difference
            # changes no decision. Stage 7 promotes a `consistent` field's
            # first observation to a settled value, so a real dispute rendered
            # as established fact -- CASE_704 printed DOC_006's "the victim
            # walked normally" in section 4 while DOC_008 p4 said the accident
            # arose from the victim's carelessness, and the reader saw only
            # the first.
            "result": ("inconsistent" if confirmed
                       else "contested_not_decisive"
                       if verdict["outcome"] == "not_material"
                       else "consistent"),
            "conflict_id": registered.get(candidate_id),
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
        "model_info": {"model_name": "agent+deterministic", "prompt_version": VERSION},
        "review_required": bool(registered),
        "warnings": warnings,
        "source_grounded": True,
        "checks": checks,
    }
    if contract["review_required"]:
        contract["reviewer_role"] = "손해사정사"
    return contract


# --------------------------------------------------------------- lifecycle --

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


def run_prepare(*, case_id: str, run_id: str, held_by: str) -> dict:
    require_open_attempt(case_id, run_id)
    result = _dao_json(["read-contract", case_id, "claim_analysis_result.json",
                        "--run-id", run_id])
    config = contracts.load_default_routing_config()
    work_items = build_work_items(result, config)
    contract = build_workitems_contract(
        case_id=case_id, run_id=run_id, work_items=work_items,
        claim_analysis_sha256=digest_of(result),
    )
    data_file = _temp_json(contract)
    try:
        _dao_write([
            "write-contract", case_id, WORKITEMS_CONTRACT,
            "--data-file", str(data_file), "--schema-name", WORKITEMS_SCHEMA,
            "--held-by", held_by, "--run-id", run_id, "--stage", STAGE,
        ])
    finally:
        data_file.unlink(missing_ok=True)
    return {"work_items": len(work_items),
            "decision_bearing": sum(1 for i in work_items if i["decision_bearing"])}


def run_register(*, case_id: str, run_id: str, held_by: str) -> dict:
    require_open_attempt(case_id, run_id)
    prepared = _dao_json(["read-contract", case_id, WORKITEMS_CONTRACT,
                          "--run-id", run_id])
    verdict_doc = _dao_json(["read-contract", case_id, VERDICTS_CONTRACT,
                             "--run-id", run_id], allow_missing=True)
    if verdict_doc is None:
        raise RuntimeError(
            "BLOCKED: no consistency_check_verdicts.json. This helper does not "
            "judge candidates; the consistency-check agent must write its "
            "verdicts before they can be registered.")
    if verdict_doc.get("claim_analysis_sha256") != prepared.get("claim_analysis_sha256"):
        raise RuntimeError(
            "BLOCKED: the verdicts were written against a different "
            "claim_analysis_result than the prepared work items")

    work_items = prepared.get("work_items") or []
    verdicts = verdict_doc.get("verdicts") or []
    errors = validate_verdicts(verdicts, work_items)
    if errors:
        raise RuntimeError(
            "BLOCKED: consistency-check verdicts were refused:\n  - "
            + "\n  - ".join(errors))

    registered = register_confirmed(case_id, run_id, held_by, verdicts, work_items)
    contract = build_contract(case_id=case_id, run_id=run_id,
                              verdicts=verdicts, registered=registered,
                              work_items=work_items)
    data_file = _temp_json(contract)
    try:
        _dao_write([
            "write-contract", case_id, CONTRACT,
            "--data-file", str(data_file), "--schema-name", SCHEMA,
            "--held-by", held_by, "--run-id", run_id, "--stage", STAGE,
        ])
    finally:
        data_file.unlink(missing_ok=True)
    return {"judged": len(verdicts), "registered": len(registered),
            "conflict_ids": sorted(registered.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subcommand", choices=["prepare", "register"])
    parser.add_argument("case_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    runner = run_prepare if args.subcommand == "prepare" else run_register
    try:
        print(json.dumps(runner(case_id=args.case_id, run_id=args.run_id,
                                held_by=args.held_by),
                         ensure_ascii=False, sort_keys=True))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
