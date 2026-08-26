"""Phase 2: does the insurer's stated reason hold up against this case's evidence?

The only converted stage with no deterministic half to build on -- `consistency_check`
had prepare/register, `screening_report` had its assembler, `critic` had four DAO
checks. Here the driver is the whole thing: input assembly, one bounded call per
denial reason, evidence verification, publication, then the rebuttal pass.

Two checkpoints, in the order the contracts define them:

1. `denial_validation_result.json` -- one verdict per denial reason, plus a
   verification record per policy match the reason cites.
2. `rebuttal_points.json` -- one point per reason that came back `not_supported`
   or `partially_supported`. A reason whose position HELD UP produces no
   rebuttal, which is why this is a second pass over a filtered set rather than
   another field on the first.

What the driver owns rather than the model:

* **`retrieved_chunk_ids`** -- the schema calls it "every chunk id retrieval
  surfaced for this reason, not just what got cited". That is a record of what
  the driver put in front of the model. Asking the model to report it would be
  asking it to describe its own input.
* **Evidence verification.** Every cited quote goes through
  `dao.py verify-evidence-references`, which checks it appears verbatim on the
  page cited. A quote the model produced from memory fails there rather than
  reaching the contract.
* **Ids.** `RB-N` is assigned in order.

P3 applies to both outputs: these are inferences about someone else's reasoning,
so the explanation and the rebuttal argument are hedged, and `review_required`
is a floor this driver raises but never lowers.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import driver_runtime  # noqa: E402
import stage_models  # noqa: E402
import trace as trace_mod  # noqa: E402
from llm_providers import (ProviderConfigError, add_provider_args,  # noqa: E402
                           build_provider, parse_provider_config)

DAO = ROOT / "tools" / "dao.py"
STAGE = "denial_validation"
COMPONENT = "denial-validation"
CONTRACT = "denial_validation_result.json"
SCHEMA = "denial_validation_result.schema.json"
REBUTTAL_CONTRACT = "rebuttal_points.json"
REBUTTAL_SCHEMA = "rebuttal_points.schema.json"
VALIDATION_PROMPT_VERSION = "denial_validation_v0.1"
REBUTTAL_PROMPT_VERSION = "denial_rebuttal_v0.1"

VERDICTS = ["supported", "not_supported", "partially_supported", "insufficient_evidence"]
REBUTTABLE = ("not_supported", "partially_supported")
VERIFICATION_STATUSES = ["verified", "invalid", "unverifiable"]
REVIEWER_ROLES = ["손해사정사", "의사", "법률전문가"]

_EVIDENCE_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document_id": {"type": "string"},
        "page": {"type": "integer"},
        "quote": {"type": "string"},
    },
    "required": ["document_id", "page", "quote"],
}


def validation_schema(policy_match_ids: Sequence[str]) -> dict:
    """The transport shape for one reason's verdict.

    `policy_match_validations` is keyed to the matches THIS reason actually
    carries: a verification record for a match the reason does not cite is a
    verdict about nothing, and one missing for a match it does cite would leave
    an unverified clause looking like a trusted basis.

    `match_source` is deliberately NOT offered. Its schema says it is "carried
    forward from this match's match_source in denial_reason_result.json", and
    the reason it is required at all is that `verified` otherwise reads as "the
    insurer's policy basis checks out" when it means only "the clause exists
    where the match claims it does". Whether the INSURER cited a clause or the
    harness inferred it is a fact about the upstream contract, so the driver
    copies it and the model is never asked.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {"enum": VERDICTS},
            "verdict_explanation": {"type": "string"},
            "confidence": {"type": "number"},
            "review_required": {"type": "boolean"},
            "reviewer_role": {"enum": REVIEWER_ROLES},
            "evidence_references": {"type": "array", "items": _EVIDENCE_ITEM},
            "policy_match_validations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "policy_match_id": {"enum": list(policy_match_ids)}
                        if policy_match_ids else {"type": "string"},
                        "verification_status": {"enum": VERIFICATION_STATUSES},
                        "verification_explanation": {"type": "string"},
                        "review_required": {"type": "boolean"},
                        "reviewer_role": {"enum": REVIEWER_ROLES},
                        "evidence_references": {"type": "array", "items": _EVIDENCE_ITEM},
                    },
                    "required": ["policy_match_id", "verification_status",
                                 "verification_explanation", "review_required"],
                },
            },
        },
        "required": ["verdict", "verdict_explanation", "confidence",
                     "review_required", "evidence_references",
                     "policy_match_validations"],
    }


REBUTTAL_SCHEMA_SHAPE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rebuttal_argument": {"type": "string"},
        "confidence": {"type": "number"},
        "review_required": {"type": "boolean"},
        "reviewer_role": {"enum": REVIEWER_ROLES},
        "evidence_references": {"type": "array", "items": _EVIDENCE_ITEM},
    },
    "required": ["rebuttal_argument", "confidence", "review_required",
                 "evidence_references"],
}

VALIDATION_INSTRUCTIONS = """\
An insurer has denied or reduced part of a Korean insurance claim. You are
judging ONE of its stated reasons against the evidence in this case's own
records. You are not deciding the claim and you are not writing to the insurer.

Return one verdict:

* supported            -- the records bear the insurer's reason out.
* not_supported        -- the records CONTRADICT it.
* partially_supported  -- it holds for part of what it covers and not the rest.
* insufficient_evidence -- the records settle it neither way. This is NOT
                          not_supported: absence of evidence is not evidence to
                          the contrary, and this verdict says so.

Also verify each policy match the reason cites: `verified` when the cited
clause is where it is said to be and says what it is said to say, `invalid`
when the reference is wrong or does not support the match, `unverifiable` when
the source material needed is not here. Neither of the last two may be treated
as a trusted policy basis afterwards.

Rules:

* Cite evidence for every claim you make. `document_id`, `page` and `quote`
  must be exact -- each quote is checked against the page it names, verbatim,
  and a quote that is not there fails the whole answer.
* `evidence_references` may be empty ONLY for `insufficient_evidence`.
* Write `verdict_explanation` in Korean and HEDGE it: this is an inference
  about someone else's reasoning, not a restatement of a record.
* Set `review_required: true` whenever a professional needs to look at this,
  and route `reviewer_role` by who can actually answer it.
* Use only the material below. Do not reason about what a document you cannot
  see probably says.
"""

REBUTTAL_INSTRUCTIONS = """\
A denial reason has been found not to hold up. Write the rebuttal point a
Korean loss adjuster would put to the insurer.

Rules:

* HEDGE. The deliverable may not claim an outcome -- write that there is room
  to dispute the position ("분쟁 대응 여지가 있다"), never that the claim wins.
* Every rebuttal cites evidence: at least one exact `document_id` / `page` /
  `quote`, checked verbatim against the page it names.
* Argue from the records in front of you, not from what usually happens in
  such cases.
* Write `rebuttal_argument` in Korean; it is read by Korean professionals and
  is assembled into the deliverable as written.
"""


def _dao_json(args: list[str], *, allow_missing: bool = False) -> dict | None:
    proc = subprocess.run(
        [sys.executable, str(DAO), *args], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "").strip()
    if proc.returncode:
        if allow_missing and "NOT_FOUND:" in out:
            return None
        raise RuntimeError((out or proc.stderr or "").strip())
    return json.loads(out) if out else None


def _temp_json(value: Any) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    return Path(handle.name)


def _dao_write(args: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, str(DAO), *args], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise RuntimeError((proc.stdout or proc.stderr or "").strip())


def chunk_ids_for(chunks: Mapping[str, Any], document_ids: Sequence[str]) -> list[str]:
    """Every chunk this driver put in front of the model for one reason.

    The schema wants what retrieval SURFACED, not what got cited, so this is
    computed from the input bundle rather than reported by the model -- asking
    the model would be asking it to describe its own prompt.
    """
    wanted = set(document_ids)
    ids: list[str] = []
    for chunk in chunks.get("chunks") or []:
        if not wanted or chunk.get("document_id") in wanted:
            chunk_id = chunk.get("chunk_id")
            if chunk_id:
                ids.append(chunk_id)
    return ids


def build_validation_prompt(
    *, reason: Mapping[str, Any], evidence_text: str,
    policy_matches: Sequence[Mapping[str, Any]],
) -> str:
    lines = [VALIDATION_INSTRUCTIONS, "", "## The insurer's reason", ""]
    lines.append(f"- reason_id: {reason.get('reason_id')}")
    lines.append(f"- decision: {reason.get('decision_type')} / "
                 f"{reason.get('payment_status')}")
    lines.append(f"- taxonomy: {reason.get('taxonomy_code')}")
    lines.append(f"- as stated: {reason.get('raw_reason_text')}")
    lines.append(f"- insurer's summary: {reason.get('insurer_claim_summary')}")
    for ground in reason.get("grounds") or []:
        lines.append(f"- ground: {ground}")

    lines += ["", "## Policy matches this reason cites", ""]
    if not policy_matches:
        lines.append("- none; return an empty policy_match_validations")
    for match in policy_matches:
        lines.append(
            f"- {match.get('policy_match_id')}: {match.get('clause_id') or ''} "
            f"{match.get('clause_title') or ''} -- {match.get('clause_text') or ''}")

    lines += ["", "## The case's records", "", evidence_text]
    return "\n".join(lines)


def build_rebuttal_prompt(
    *, reason: Mapping[str, Any], validation: Mapping[str, Any], evidence_text: str,
) -> str:
    lines = [REBUTTAL_INSTRUCTIONS, "", "## The reason being rebutted", ""]
    lines.append(f"- reason_id: {reason.get('reason_id')}")
    lines.append(f"- as stated: {reason.get('raw_reason_text')}")
    lines.append(f"- verdict reached: {validation.get('verdict')}")
    lines.append(f"- why: {validation.get('verdict_explanation')}")
    lines += ["", "## The case's records", "", evidence_text]
    return "\n".join(lines)


def bind_validation(
    raw: Mapping[str, Any], *, reason_id: str,
    policy_matches: Sequence[Mapping[str, Any]],
    retrieved_chunk_ids: Sequence[str],
) -> dict:
    policy_match_ids = [m.get("policy_match_id") for m in policy_matches
                        if m.get("policy_match_id")]
    source_by_id = {m.get("policy_match_id"): m.get("match_source")
                    for m in policy_matches}
    problems: list[str] = []
    verdict = raw.get("verdict")
    refs = raw.get("evidence_references") or []
    if verdict != "insufficient_evidence" and not refs:
        # The schema allows an empty list only there; anywhere else a verdict
        # with no evidence is an assertion, which is what P1 forbids.
        problems.append(
            f"evidence_references: empty is allowed only for "
            f"insufficient_evidence, not for {verdict!r}")

    validations = raw.get("policy_match_validations") or []
    seen = [v.get("policy_match_id") for v in validations]
    missing = sorted(set(policy_match_ids) - set(seen))
    unknown = sorted(set(seen) - set(policy_match_ids))
    if missing:
        problems.append(
            "policy_match_validations is missing: " + ", ".join(missing)
            + " -- an unverified clause must not look like a trusted basis")
    if unknown:
        problems.append(
            "policy_match_validations names matches this reason does not cite: "
            + ", ".join(unknown))
    for entry in validations:
        if entry.get("verification_status") == "verified" and not (
                entry.get("evidence_references") or []):
            problems.append(
                f"{entry.get('policy_match_id')}: a verified match needs at "
                "least one evidence reference")
        if source_by_id.get(entry.get("policy_match_id")) is None:
            problems.append(
                f"{entry.get('policy_match_id')}: the upstream contract records "
                "no match_source, so this verification cannot say whether the "
                "insurer cited the clause or the harness inferred it")
    if problems:
        raise ValueError("\n  - ".join(["validation refused:", *problems]))

    bound_matches = []
    for entry in validations:
        bound_matches.append({
            **{k: v for k, v in entry.items() if v is not None},
            # Copied from denial_reason_result.json, never asked for.
            "match_source": source_by_id[entry["policy_match_id"]],
            "evidence_references": [dict(r) for r in
                                    entry.get("evidence_references") or []],
            "review_required": bool(entry.get("review_required")),
        })

    out = {
        "reason_id": reason_id,
        "verdict": verdict,
        "verdict_explanation": raw.get("verdict_explanation"),
        # Recorded by the driver, not reported by the model.
        "retrieved_chunk_ids": list(retrieved_chunk_ids),
        "policy_match_validations": bound_matches,
        "evidence_references": [dict(r) for r in refs],
        "confidence": raw.get("confidence"),
        "review_required": bool(raw.get("review_required")),
    }
    if raw.get("reviewer_role"):
        out["reviewer_role"] = raw["reviewer_role"]
    return out


def bind_rebuttal(
    raw: Mapping[str, Any], *, index: int, reason_id: str, verdict: str,
) -> dict:
    refs = raw.get("evidence_references") or []
    if not refs:
        raise ValueError(
            "rebuttal refused: every rebuttal point cites at least one piece of "
            "evidence -- an unevidenced argument is the shape P1 exists to stop")
    out = {
        "point_id": f"RB-{index}",
        "reason_id": reason_id,
        "verdict": verdict,
        "rebuttal_argument": raw.get("rebuttal_argument"),
        "evidence_references": [dict(r) for r in refs],
        "confidence": raw.get("confidence"),
        "review_required": bool(raw.get("review_required")),
    }
    if raw.get("reviewer_role"):
        out["reviewer_role"] = raw["reviewer_role"]
    return out


def verify_references(case_id: str, run_id: str, references: Sequence[Mapping[str, Any]]):
    """Every quote must appear verbatim on the page it names, or nothing is written."""
    if not references:
        return []
    ref_file = _temp_json({"references": [dict(r) for r in references]})
    try:
        checked = _dao_json(["verify-evidence-references", case_id,
                             "--references-file", str(ref_file), "--run-id", run_id])
    finally:
        ref_file.unlink(missing_ok=True)
    verified = (checked or {}).get("verified_references") or []
    if len(verified) != len(references):
        raise RuntimeError(
            "BLOCKED: a cited quote does not appear on the page it names; "
            "nothing was written")
    return verified


def build_contract(
    *, case_id: str, run_id: str, validations: Sequence[Mapping[str, Any]],
    model_name: str, prompt_version: str, source_denial_hash: str | None,
) -> dict:
    review_required = any(v["review_required"] for v in validations)
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": COMPONENT,
        "status": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_info": {"model_name": model_name, "prompt_version": prompt_version},
        "review_required": review_required,
        "warnings": [],
        "source_grounded": True,
        "validations": [dict(v) for v in validations],
    }
    if review_required:
        contract["reviewer_role"] = next(
            (v["reviewer_role"] for v in validations if v.get("reviewer_role")),
            "손해사정사")
    if source_denial_hash:
        contract["source_denial_contract_hash"] = source_denial_hash
    return contract


def build_rebuttal_contract(
    *, case_id: str, run_id: str, points: Sequence[Mapping[str, Any]],
    model_name: str,
) -> dict:
    review_required = any(p["review_required"] for p in points)
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": COMPONENT,
        "status": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_info": {"model_name": model_name,
                       "prompt_version": REBUTTAL_PROMPT_VERSION},
        "review_required": review_required,
        "warnings": [],
        "source_grounded": True,
        "rebuttal_points": [dict(p) for p in points],
    }
    if review_required:
        contract["reviewer_role"] = next(
            (p["reviewer_role"] for p in points if p.get("reviewer_role")),
            "손해사정사")
    return contract


def run(*, case_id: str, run_id: str, held_by: str, provider) -> dict:
    denial = _dao_json(["read-contract", case_id, "denial_reason_result.json",
                        "--run-id", run_id])
    reasons = denial.get("denial_reasons") or []
    if not reasons:
        raise RuntimeError(
            "BLOCKED: denial_reason_result.json states no denial reasons, so "
            "there is nothing to validate. A stage that validates nothing must "
            "not report success.")

    chunks = _dao_json(["read-contract", case_id, "page_chunks.json",
                        "--run-id", run_id], allow_missing=True) or {}
    manifest = _dao_json(["read-contract", case_id, "document_manifest.json",
                          "--run-id", run_id])
    document_ids = [d["document_id"] for d in manifest.get("documents") or []
                    if d.get("redacted_text_path")]
    bundle = _dao_json(["read-redacted-text-bundle", case_id,
                        *sum((["--doc-id", d] for d in document_ids), [])])
    evidence_text = "\n\n".join(
        f"### {doc.get('document_id')}\n{doc.get('redacted_text')}"
        for doc in (bundle or {}).get("documents") or [])

    import _cross_contract
    source_hash = _cross_contract.upstream_hash(denial)

    validations: list[dict] = []
    for reason in reasons:
        reason_id = reason.get("reason_id")
        matches = reason.get("policy_matches") or []
        match_ids = [m.get("policy_match_id") for m in matches
                     if m.get("policy_match_id")]
        retrieved = chunk_ids_for(chunks, document_ids)
        with driver_runtime.driver_span(case_id, run_id, "provider_wait",
                                        unit_id=reason_id, items=1):
            raw = driver_runtime.structured_with_one_correction(
                provider=provider,
                prompt=build_validation_prompt(
                    reason=reason, evidence_text=evidence_text,
                    policy_matches=matches),
                prompt_version=VALIDATION_PROMPT_VERSION,
                output_schema=validation_schema(match_ids),
                validate=lambda raw, rid=reason_id, ms=matches, chs=retrieved:
                    bind_validation(raw, reason_id=rid, policy_matches=ms,
                                    retrieved_chunk_ids=chs),
            )
        refs = list(raw["evidence_references"])
        for entry in raw["policy_match_validations"]:
            refs.extend(entry.get("evidence_references") or [])
        verify_references(case_id, run_id, refs)
        validations.append(raw)

    contract = build_contract(
        case_id=case_id, run_id=run_id, validations=validations,
        model_name=f"{provider.provider_name}:{provider.model_name}",
        prompt_version=VALIDATION_PROMPT_VERSION, source_denial_hash=source_hash)
    data_file = _temp_json(contract)
    try:
        _dao_write(["write-contract", case_id, CONTRACT, "--data-file",
                    str(data_file), "--schema-name", SCHEMA, "--held-by", held_by,
                    "--run-id", run_id, "--stage", STAGE])
    finally:
        data_file.unlink(missing_ok=True)

    # ---- checkpoint 2: only the reasons whose position did NOT hold up ------
    by_id = {r.get("reason_id"): r for r in reasons}
    points: list[dict] = []
    for validation in validations:
        if validation["verdict"] not in REBUTTABLE:
            continue
        reason = by_id[validation["reason_id"]]
        with driver_runtime.driver_span(case_id, run_id, "provider_wait",
                                        unit_id=f"rebuttal:{validation['reason_id']}",
                                        items=1):
            raw = driver_runtime.structured_with_one_correction(
                provider=provider,
                prompt=build_rebuttal_prompt(
                    reason=reason, validation=validation,
                    evidence_text=evidence_text),
                prompt_version=REBUTTAL_PROMPT_VERSION,
                output_schema=REBUTTAL_SCHEMA_SHAPE,
                validate=lambda raw, i=len(points) + 1,
                                rid=validation["reason_id"],
                                v=validation["verdict"]:
                    bind_rebuttal(raw, index=i, reason_id=rid, verdict=v),
            )
        verify_references(case_id, run_id, raw["evidence_references"])
        points.append(raw)

    if points:
        rebuttal = build_rebuttal_contract(
            case_id=case_id, run_id=run_id, points=points,
            model_name=f"{provider.provider_name}:{provider.model_name}")
        data_file = _temp_json(rebuttal)
        try:
            _dao_write(["write-contract", case_id, REBUTTAL_CONTRACT, "--data-file",
                        str(data_file), "--schema-name", REBUTTAL_SCHEMA,
                        "--held-by", held_by, "--run-id", run_id, "--stage", STAGE])
        finally:
            data_file.unlink(missing_ok=True)

    verdicts: dict[str, int] = {}
    for validation in validations:
        verdicts[validation["verdict"]] = verdicts.get(validation["verdict"], 0) + 1
    return {"validated": len(validations), "verdicts": verdicts,
            "rebuttal_points": len(points)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    add_provider_args(parser)
    args = parser.parse_args(argv)
    trace_mod.configure_from_args(args)

    args.provider, args.model = stage_models.resolve(
        STAGE, "review", provider=args.provider, model=args.model)
    try:
        provider = build_provider(parse_provider_config(args))
    except ProviderConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    try:
        print(json.dumps(run(case_id=args.case_id, run_id=args.run_id,
                             held_by=args.held_by, provider=provider),
                         ensure_ascii=False, sort_keys=True))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
