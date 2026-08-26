"""Critic: the deterministic floors, then one bounded reading, then the contract.

The stage was entirely a subagent. Four of the six fields in `critic_result`
were already mechanical -- `dao.py read-evidence-tags` produces the orphaned and
unused counts, `check-forbidden-expressions` the literal-phrase count,
`check-untagged-claims` the untagged-candidate count -- but nothing ran them
outside an agent's own tool calls, so the counts and the reading were produced
by the same pass and a run that skipped a check looked like one that passed it.

This driver separates them. It runs the four checks itself, hands their OUTPUT
to one bounded provider call along with the draft, and publishes the result.

Two judgements remain, and both stay with the model:

* `findings` -- what a reviewer notices. The checks produce CANDIDATES; a
  flagged line may legitimately owe no citation, so a non-zero count with no
  corresponding finding is a valid outcome.
* `passed` -- the schema is explicit that this is "not inferred from
  findings.length", so it "lets critic mark passed:false on a severity judgment
  call even alongside minor-only findings". A driver that computed it from the
  finding count would delete the judgement the field exists to carry.

**Blindness is structural here, not promised.** D1 says the critic never reads
`source-cases/` or `data/ground_truth/`. This driver builds its prompt from the
draft, its sidecar and the four check outputs, and opens nothing else -- so the
property holds by what the prompt contains rather than by an instruction the
model is asked to obey.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import driver_runtime  # noqa: E402
import stage_models  # noqa: E402
import trace as trace_mod  # noqa: E402
from llm_providers import (ProviderConfigError, add_provider_args,  # noqa: E402
                           build_provider, parse_provider_config)

DAO = ROOT / "tools" / "dao.py"
COMPONENT = "critic"
CONTRACT = "critic_result.json"
SCHEMA = "critic_result.schema.json"
PROMPT_VERSION = "critic_review_v0.1"

FINDING_TYPES = ["fabrication_unlinked_claim", "unhedged_inference",
                 "forbidden_expression"]
SEVERITIES = ["low", "medium", "high"]

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "passed": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "finding_type": {"enum": FINDING_TYPES},
                    "description": {"type": "string"},
                    "severity": {"enum": SEVERITIES},
                    "suggested_fix": {"type": "string"},
                    "heading": {"type": "string"},
                    "tag": {"type": "string"},
                },
                "required": ["finding_type", "description", "severity"],
            },
        },
    },
    "required": ["passed", "findings"],
}

INSTRUCTIONS = """\
You are the blind review pass on a Korean loss-adjustment draft report. Blind
means you have never seen the correct answer and are not being asked to guess
it: you judge the document against ITSELF and against the evidence it cites.

Three checks, and only these three:

* fabrication_unlinked_claim -- the document asserts something that no [E#]
  citation supports. The untagged-claim scan below lists CANDIDATE lines; a
  candidate is not a finding. A line that restates a heading, or that carries
  its support in an adjacent sentence, owes no citation of its own.
* unhedged_inference -- the document states an inference as established fact.
  An inference is legitimate; presenting it as a record is not.
* forbidden_expression -- language the deliverable may not use. The literal
  scan below is a FLOOR: it catches listed phrases only, and the implied cases
  are yours.

Then set `passed`. It is a judgement, not a count: mark `passed: false` when
what you found should stop this draft going to a human reviewer, even if every
finding is individually minor, and `passed: true` when the findings are worth
recording but do not.

Rules:

* Judge only from the draft, its citation sidecar, and the check output below.
  You have no access to the case's source documents and must not reason about
  what they "probably" say.
* Write `description` and `suggested_fix` in Korean: they are read by the
  people who will revise this draft.
* Quote or name the location precisely -- the heading, and the [E#] tag when
  one is involved.
* Do not number your findings. Ids are assigned after you answer.
"""


def _dao(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(DAO), *args], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def run_checks(doc_path: str, template: str) -> dict:
    """The four deterministic floors, run here rather than inside a model turn.

    A check that could not run is recorded as such and never as a zero: a count
    of zero says "the check ran and found nothing", which is exactly the claim
    a failed check must not make. `check-untagged-claims` is the sharp case --
    it refuses a template whose headings match nothing rather than returning
    clean, because a zero-line scan reported as clean is how CASE_909's
    rebuttal_points.md came back passing.
    """
    checks: dict[str, Any] = {}

    code, out = _dao(["read-evidence-tags", doc_path])
    try:
        tags = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"BLOCKED: read-evidence-tags did not return JSON: {out}")
    checks["evidence_tags"] = tags

    code, out = _dao(["check-forbidden-expressions", doc_path])
    if out.startswith(("NO_TEMPLATE", "NOT_FOUND")):
        checks["forbidden"] = {"ran": False, "reason": out}
    else:
        checks["forbidden"] = {"ran": True, **json.loads(out)}

    code, out = _dao(["check-untagged-claims", doc_path, "--template", template])
    if out.startswith(("NO_ANALYTICAL_SECTIONS", "NO_MATCHING_SECTIONS", "NOT_FOUND")):
        checks["untagged"] = {"ran": False, "reason": out}
    else:
        checks["untagged"] = {"ran": True, **json.loads(out)}

    return checks


def build_prompt(*, draft: str, sidecar: Mapping[str, Any], checks: Mapping[str, Any]) -> str:
    """Draft, citations, check output. Nothing else reaches the model."""
    lines = [INSTRUCTIONS, "", "## Deterministic check output", ""]

    tags = checks.get("evidence_tags") or {}
    lines.append(f"- orphaned [E#] tags (no sidecar entry): {tags.get('orphaned_tags')}")
    lines.append(f"- unused citations (sidecar entry never cited): {tags.get('unused_citations')}")

    forbidden = checks.get("forbidden") or {}
    if not forbidden.get("ran"):
        lines.append(f"- forbidden-phrase scan DID NOT RUN: {forbidden.get('reason')}")
    else:
        hits = forbidden.get("hits") or []
        lines.append(f"- forbidden literal phrases found: {len(hits)}")
        for hit in hits:
            lines.append(f"    line {hit.get('line')}: {hit.get('phrase')}")

    untagged = checks.get("untagged") or {}
    if not untagged.get("ran"):
        lines.append(f"- untagged-claim scan DID NOT RUN: {untagged.get('reason')}")
    else:
        candidates = untagged.get("findings") or untagged.get("candidates") or []
        lines.append(f"- untagged-claim CANDIDATES (not findings): {len(candidates)}")
        for candidate in candidates:
            lines.append(f"    {candidate}")

    lines += ["", "## Citation sidecar", ""]
    for citation in (sidecar.get("citations") or []):
        lines.append(
            f"- {citation.get('tag')}: {citation.get('document_id')} "
            f"p.{citation.get('page')} -- {citation.get('quote')}")

    lines += ["", "## The draft under review", "", draft]
    return "\n".join(lines)


def bind_result(raw: Mapping[str, Any]) -> dict:
    """Assign finding ids, or refuse.

    `passed` is taken exactly as given. It is the one field this driver must
    not compute: the schema says it is an explicit rollup precisely so a critic
    can fail a draft over severity rather than over a count.
    """
    if not isinstance(raw.get("passed"), bool):
        raise ValueError("passed: an explicit true/false is required")
    findings = []
    for index, finding in enumerate(raw.get("findings") or [], start=1):
        if not isinstance(finding, Mapping):
            raise ValueError(f"findings[{index}]: not an object")
        entry: dict[str, Any] = {
            "finding_id": f"CF-{index}",
            "finding_type": finding.get("finding_type"),
            "description": finding.get("description"),
            "severity": finding.get("severity"),
        }
        if finding.get("suggested_fix"):
            entry["suggested_fix"] = finding["suggested_fix"]
        location = {k: finding[k] for k in ("heading", "tag") if finding.get(k)}
        if location:
            entry["location"] = location
        findings.append(entry)
    return {"passed": raw["passed"], "findings": findings}


def build_contract(
    *, case_id: str, run_id: str, reviewed_document: str,
    checks: Mapping[str, Any], result: Mapping[str, Any], model_name: str,
) -> dict:
    tags = checks.get("evidence_tags") or {}
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": COMPONENT,
        "status": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_info": {"model_name": model_name, "prompt_version": PROMPT_VERSION},
        "review_required": not result["passed"],
        "warnings": [],
        "source_grounded": True,
        "reviewed_document": reviewed_document,
        "passed": result["passed"],
        "orphaned_tag_count": len(tags.get("orphaned_tags") or []),
        "unused_citation_count": len(tags.get("unused_citations") or []),
        "findings": list(result["findings"]),
    }
    if not result["passed"]:
        contract["reviewer_role"] = "손해사정사"
    # Recorded only when the check actually ran. The schema makes both counts
    # optional for exactly this reason, and a zero from a check that refused to
    # run would assert a clean scan nobody performed.
    forbidden = checks.get("forbidden") or {}
    if forbidden.get("ran"):
        contract["forbidden_literal_hit_count"] = len(forbidden.get("hits") or [])
    untagged = checks.get("untagged") or {}
    if untagged.get("ran"):
        contract["untagged_claim_candidate_count"] = len(
            untagged.get("findings") or untagged.get("candidates") or [])
    if not forbidden.get("ran") or not untagged.get("ran"):
        contract["warnings"] = [
            w for w in (
                None if forbidden.get("ran") else
                f"forbidden-expression scan did not run: {forbidden.get('reason')}",
                None if untagged.get("ran") else
                f"untagged-claim scan did not run: {untagged.get('reason')}",
            ) if w
        ]
    return contract


def run(*, case_id: str, version: str, template: str, run_id: str, held_by: str,
        provider) -> dict:
    doc_path = f"outputs/{case_id}/draft_report_{version}.md"
    absolute = ROOT / doc_path
    if not absolute.exists():
        raise RuntimeError(f"BLOCKED: no draft to review at {doc_path}")
    sidecar_path = absolute.with_suffix(".evidence.json")
    sidecar = (json.loads(sidecar_path.read_text(encoding="utf-8"))
               if sidecar_path.exists() else {"citations": []})

    checks = run_checks(doc_path, template)
    result = driver_runtime.structured_with_one_correction(
        provider=provider,
        prompt=build_prompt(draft=absolute.read_text(encoding="utf-8"),
                            sidecar=sidecar, checks=checks),
        prompt_version=PROMPT_VERSION,
        output_schema=OUTPUT_SCHEMA,
        validate=bind_result,
    )
    contract = build_contract(
        case_id=case_id, run_id=run_id, reviewed_document=doc_path,
        checks=checks, result=result,
        model_name=f"{provider.provider_name}:{provider.model_name}")

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(contract, handle, ensure_ascii=False, indent=2)
    data_file = Path(handle.name)
    try:
        filename = f"critic_result_{version}.json"
        code, out = _dao([
            "write-contract", case_id, filename, "--data-file", str(data_file),
            "--schema-name", SCHEMA, "--held-by", held_by, "--run-id", run_id,
            "--stage", f"critic_{version}"])
        if code:
            raise RuntimeError(out)
    finally:
        data_file.unlink(missing_ok=True)

    return {
        "reviewed_document": doc_path,
        "passed": contract["passed"],
        "findings": len(contract["findings"]),
        "checks_skipped": contract["warnings"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case_id")
    parser.add_argument("version", choices=["v1", "v2"])
    parser.add_argument("--template", required=True,
                        help="Registry key the draft was rendered from; scopes "
                             "the untagged-claim scan. Passing the wrong one "
                             "matches no section and is refused, not reported "
                             "clean.")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    add_provider_args(parser)
    args = parser.parse_args(argv)
    trace_mod.configure_from_args(args)

    stage = f"critic_{args.version}"
    args.provider, args.model = stage_models.resolve(
        stage, "review", provider=args.provider, model=args.model)
    try:
        provider = build_provider(parse_provider_config(args))
    except ProviderConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    try:
        print(json.dumps(run(case_id=args.case_id, version=args.version,
                             template=args.template, run_id=args.run_id,
                             held_by=args.held_by, provider=provider),
                         ensure_ascii=False, sort_keys=True))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
