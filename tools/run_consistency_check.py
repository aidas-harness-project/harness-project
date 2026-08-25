"""Consistency Check helper: prepare work items, then register agent verdicts.

The judgement -- is this pair of readings a real contradiction? -- belongs to
the `consistency-check` agent. This module is the deterministic half on either
side of that judgement:

    prepare   read claim_analysis_result.json -> consistency_check_workitems.json
              (both readings, their exact evidence, whether the field is
              decision-bearing. No verdict field: a prepared answer would make
              the decision by suggestion.)

    judge     the same decision as a bounded provider call, so the stage can
              run without a subagent. Optional: `register` still accepts a
              verdicts contract written any other way, and the
              `consistency-check` agent remains a valid producer of one.
              (confirmed / not_material / withdrawn, plus a neutral
              professional_summary on every confirmed one.)

    register  verify the verdicts bind to what was prepared, register the
              confirmed ones in the P6 ledger as `pending`, and write the
              stage's audit contract.

Why the split rather than one deterministic driver: whether "우측" and "좌측"
on two forms are a genuine contradiction or a transcription artefact is a
reading of the documents, not a string comparison. What IS mechanical -- that
both sides are evidenced, that a verdict matches the candidate it was written
against, that nothing reaches the ledger twice -- lives here.

What this module never does:

* **Decide, in `register`.** With no verdicts file, `register` registers
  nothing. It cannot reach a verdict on its own, which is the point. `judge`
  is a SEPARATE step for exactly that reason: producing the verdicts and
  admitting them to the ledger stay two commands, so the deterministic
  admission checks still run against whatever wrote them.
* **Dispose.** Every entry is created `pending`. `resolved`, `false_positive`,
  and `deferred_to_report` are human calls under P6. A confirmed verdict says
  the conflict is real, not what should happen about it.
* **Re-read the case.** Source documents and page chunks are not opened here;
  everything comes from the upstream contract (Notion 3-10).
* **Move run state.** T13: the orchestrator owns update-run-state/finalize-stage.
"""
from __future__ import annotations

import argparse
import functools
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
import driver_runtime
from llm_providers import (ProviderConfigError, add_provider_args, build_provider,
                           parse_provider_config)

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
JUDGE_PROMPT_VERSION = "consistency_check_judge_v0.1"

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


# ------------------------------------------------------------------- judge --

# The model supplies a VERDICT per candidate id and nothing else. It is never
# asked for `candidate_digest`: the driver copies that from the prepared item,
# because a digest is a fact about what was prepared, not a judgement, and a
# model that mistypes one turns a binding check into a mismatch error. Same
# reason the schema below closes `additionalProperties` -- a verdict about a
# candidate nobody prepared has nothing to bind to.
JUDGE_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "conflict_candidate_id": {"type": "string"},
                    "outcome": {"enum": ["confirmed", "not_material", "withdrawn"]},
                    "reason": {"type": "string"},
                    "professional_summary": {"type": "string"},
                },
                "required": ["conflict_candidate_id", "outcome", "reason"],
            },
        },
    },
    "required": ["verdicts"],
}

JUDGE_INSTRUCTIONS = """\
You are reviewing pairs of readings taken from ONE insurance claim case's own
documents. For each candidate below, decide whether the readings genuinely
contradict each other.

Answer for every candidate id, exactly once each, using one outcome:

* confirmed    -- the sources genuinely contradict each other on something that
                  can change a determination.
* not_material -- a real difference that changes no decision.
* withdrawn    -- not a contradiction on inspection: one side is silent, the
                  difference is formatting, or one reading is merely more
                  detailed than the other.

Rules:

* Judge ONLY from the readings given. Do not infer a value neither reading
  states, and do not use knowledge about how such cases usually go.
* `reason` states what you compared and why the outcome follows. One or two
  sentences.
* On a `confirmed` verdict, also write `professional_summary` IN KOREAN for a
  손해사정사/의사: what each record says and what needs checking. It must NOT
  elect a side -- not by asserting one reading is correct, and not by
  presenting one as the default or as the more likely. This sentence is carried
  verbatim into the screening report, so write it for that reader.
* Do not write `professional_summary` on a verdict that is not `confirmed`.
* A field marked decision_bearing changes a determination if it is wrong; that
  raises the cost of a mistake, it does not make a contradiction more likely.
"""


def build_judge_prompt(work_items: Sequence[Mapping[str, Any]]) -> str:
    """One prompt covering every prepared candidate.

    Carries the readings the DAO already put in the work items and nothing
    else: no source documents are re-opened here, exactly as `prepare` does not
    open them. That keeps the prompt bounded and keeps this step unable to
    introduce a fact the upstream contract does not contain.
    """
    blocks: list[str] = []
    for item in work_items:
        lines = [
            f"### {item['conflict_candidate_id']}",
            f"field: {item.get('field_label') or item.get('field_id')} "
            f"(decision_bearing: {str(bool(item.get('decision_bearing'))).lower()})",
        ]
        for reading in item.get("readings") or []:
            value = reading.get("value")
            lines.append(
                f"- reading {reading.get('observation_id')}: "
                f"state={reading.get('value_state')}, "
                f"value={value if value is not None else '(none stated)'}, "
                f"source_kind={reading.get('source_document_kind')}"
            )
            for reference in reading.get("evidence_references") or []:
                quote = reference.get("quote")
                lines.append(
                    f"    evidence: {reference.get('document_id')} "
                    f"p.{reference.get('page')}"
                    + (f" -- \"{quote}\"" if quote else "")
                )
        blocks.append("\n".join(lines))
    return JUDGE_INSTRUCTIONS + "\n\n" + "\n\n".join(blocks)


def bind_verdicts(
    raw: Mapping[str, Any],
    work_items: Sequence[Mapping[str, Any]],
) -> list[dict]:
    """Attach each prepared candidate's digest, then run the admission checks.

    Raises ValueError on anything the register step would refuse anyway, so a
    bad response is corrected once (P4) rather than written and rejected later.
    """
    by_id = {item["conflict_candidate_id"]: item for item in work_items}
    verdicts: list[dict] = []
    seen: set[str] = set()
    problems: list[str] = []

    for entry in raw.get("verdicts") or []:
        candidate_id = entry.get("conflict_candidate_id")
        item = by_id.get(candidate_id)
        if item is None:
            problems.append(f"{candidate_id}: no such prepared candidate")
            continue
        if candidate_id in seen:
            problems.append(f"{candidate_id}: judged more than once")
            continue
        seen.add(candidate_id)
        verdict = {
            "conflict_candidate_id": candidate_id,
            # Copied, never taken from the response -- see JUDGE_OUTPUT_SCHEMA.
            "candidate_digest": item["candidate_digest"],
            "outcome": entry.get("outcome"),
            "reason": entry.get("reason"),
        }
        summary = entry.get("professional_summary")
        if summary:
            verdict["professional_summary"] = summary
        verdicts.append(verdict)

    missing = sorted(set(by_id) - seen)
    if missing:
        problems.append(
            "no verdict for: " + ", ".join(missing)
            + " -- every prepared candidate must be judged")
    problems.extend(validate_verdicts(verdicts, work_items))
    if problems:
        raise ValueError("\n  - ".join(["verdicts refused:", *problems]))
    return verdicts


def build_verdicts_contract(
    *,
    case_id: str,
    run_id: str,
    verdicts: Sequence[Mapping[str, Any]],
    claim_analysis_sha256: str,
    model_name: str,
) -> dict:
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": COMPONENT,
        "status": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_info": {"model_name": model_name,
                       "prompt_version": JUDGE_PROMPT_VERSION},
        "review_required": any(v["outcome"] == "confirmed" for v in verdicts),
        "warnings": [],
        "source_grounded": True,
        "schema_version": "consistency_check_verdicts.v0.1",
        "claim_analysis_sha256": claim_analysis_sha256,
        "verdicts": [dict(v) for v in verdicts],
    }
    if contract["review_required"]:
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
    """
    return f"consistency_check:{case_id}:{run_id}:{candidate_id}:{digest}"


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


def run_judge(*, case_id: str, run_id: str, held_by: str,
              provider=None, usage_out: list | None = None) -> dict:
    """Produce the verdicts contract with one bounded provider call.

    Does NOT register anything: `register` remains a separate command, so the
    deterministic admission checks still run against these verdicts exactly as
    they run against an agent's.
    """
    require_open_attempt(case_id, run_id)
    prepared = _dao_json(["read-contract", case_id, WORKITEMS_CONTRACT,
                          "--run-id", run_id])
    work_items = prepared.get("work_items") or []
    if not work_items:
        # Nothing was prepared, so there is nothing to judge. Writing an empty
        # verdicts contract would be a judgement about no candidates.
        return {"judged": 0, "skipped": "no prepared work items"}

    selected = provider or build_provider(parse_provider_config())
    verdicts = driver_runtime.structured_with_one_correction(
        provider=selected,
        prompt=build_judge_prompt(work_items),
        prompt_version=JUDGE_PROMPT_VERSION,
        output_schema=JUDGE_OUTPUT_SCHEMA,
        validate=lambda raw: bind_verdicts(raw, work_items),
        usage_out=usage_out,
    )
    contract = build_verdicts_contract(
        case_id=case_id, run_id=run_id, verdicts=verdicts,
        claim_analysis_sha256=prepared.get("claim_analysis_sha256"),
        model_name=f"{selected.provider_name}:{selected.model_name}",
    )
    data_file = _temp_json(contract)
    try:
        _dao_write([
            "write-contract", case_id, VERDICTS_CONTRACT,
            "--data-file", str(data_file),
            "--schema-name", "consistency_check_verdicts.schema.json",
            "--held-by", held_by, "--run-id", run_id, "--stage", STAGE,
        ])
    finally:
        data_file.unlink(missing_ok=True)
    counts: dict[str, int] = {}
    for verdict in verdicts:
        counts[verdict["outcome"]] = counts.get(verdict["outcome"], 0) + 1
    return {"judged": len(verdicts), "outcomes": counts}


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
    parser.add_argument("subcommand", choices=["prepare", "judge", "register"])
    parser.add_argument("case_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    add_provider_args(parser)
    args = parser.parse_args(argv)
    runners = {"prepare": run_prepare, "judge": run_judge, "register": run_register}
    runner = runners[args.subcommand]
    if args.subcommand == "judge":
        try:
            provider = build_provider(parse_provider_config(args))
        except ProviderConfigError as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 1
        runner = functools.partial(run_judge, provider=provider)
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
