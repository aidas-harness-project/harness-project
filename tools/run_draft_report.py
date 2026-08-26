"""Author the deliverable's structured contract, one block at a time.

`draft-report` was the last stage with no code path at all. It writes
`loss_adjustment_report_{version}.json` -- the canonical authoring contract --
which `document_assembly.py` then renders into the Korean narrative and its
evidence sidecar. That rendering was already deterministic; producing the
contract was not.

The driver:

1. Builds the **evidence registry** from `claim_analysis_result.json`. Every
   statement cites `E<N>` ids into it, so the model can only cite evidence the
   driver put there -- it never writes a document id, a page or a quote.
2. Requests the **eight sections one at a time**, each with its own small
   transport schema, passing the sections already written so the report does
   not repeat itself. One call per section rather than one call for the report:
   the whole contract is 33.7KB of schema and a full report against a 16,000
   token cap, and a truncated response is refused outright.
3. Requests **final_assessment** and **review_gates**.
4. Assembles, validates against the real contract, publishes through the DAO.

Amount calculation is out of scope for this PoC, and the way that is expressed
is the contract's own: `result` null with `status: human_review_required` on a
calculation whose inputs are the figures the records actually state. A
placeholder saying "not computed" is not expressible -- a literal input REQUIRES
evidence, so a stub would have to cite a basis for an amount nobody worked out.

v2 reads v1 plus `rebuttal_points.json` and re-authors. It never overwrites a v1
artifact.
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

import draft_authoring  # noqa: E402
import driver_runtime  # noqa: E402
import stage_models  # noqa: E402
import trace as trace_mod  # noqa: E402
from llm_providers import (ProviderConfigError, add_provider_args,  # noqa: E402
                           build_provider, parse_provider_config)

DAO = ROOT / "tools" / "dao.py"
COMPONENT = "draft-report"
SCHEMA = "loss_adjustment_report.schema.json"


def _dao_json(args: list[str], *, allow_missing: bool = False) -> dict | None:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    out = (proc.stdout or "").strip()
    if proc.returncode:
        if allow_missing and "NOT_FOUND:" in out:
            return None
        raise RuntimeError((out or proc.stderr or "").strip())
    return json.loads(out) if out else None


def _temp_json(value: Any) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
    with handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    return Path(handle.name)


def case_summary(claim_analysis: Mapping[str, Any],
                 rebuttals: Mapping[str, Any] | None = None) -> str:
    """What the model is told about the case, from the upstream contract only."""
    lines: list[str] = ["### Case type assessment"]
    for key, value in sorted((claim_analysis.get("case_type_assessment") or {}).items()):
        if isinstance(value, (str, int, float, bool)) or value is None:
            lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("### Claim facts")
    for field in claim_analysis.get("claim_facts") or []:
        state = field.get("field_state") or field.get("presence")
        lines.append(f"- {field.get('field_id')}: {state} = {field.get('value')}")
    if rebuttals:
        lines += ["", "### Rebuttal points carried into this version"]
        for point in rebuttals.get("rebuttal_points") or []:
            lines.append(f"- {point.get('point_id')} ({point.get('verdict')}): "
                         f"{point.get('rebuttal_argument')}")
    return "\n".join(lines)


def author(*, case_id: str, version: str, provider, claim_analysis: Mapping[str, Any],
           rebuttals: Mapping[str, Any] | None, document_profile: Mapping[str, Any],
           case_reference: Mapping[str, Any]) -> dict:
    family = document_profile.get("family")
    requirements = draft_authoring.render_requirements(family) if family else ""
    registry = draft_authoring.evidence_registry(claim_analysis)
    if not registry:
        raise RuntimeError(
            "BLOCKED: claim_analysis_result.json carries no evidence references, "
            "so nothing in this report could be cited. A deliverable whose every "
            "sentence must trace to evidence cannot be authored from a case with "
            "none.")
    evidence_ids = [entry["evidence_id"] for entry in registry]
    summary = case_summary(claim_analysis, rebuttals)

    sections: dict[str, Any] = {}
    for key in draft_authoring.SECTION_KEYS:
        raw = driver_runtime.structured_with_one_correction(
            provider=provider,
            prompt=draft_authoring.build_section_prompt(
                section_key=key, registry=registry, case_summary=summary,
                prior_sections=dict(sections)),
            prompt_version=draft_authoring.PROMPT_VERSION,
            output_schema=draft_authoring.SECTION_SCHEMA,
            validate=lambda raw, k=key, ids=evidence_ids:
                draft_authoring.bind_section(raw, section_key=k, evidence_ids=ids),
        )
        sections[key] = raw

    issues = driver_runtime.structured_with_one_correction(
        provider=provider,
        prompt=draft_authoring.ISSUE_INSTRUCTIONS + "\n\n" + requirements
        + "\n\n" + summary
        + "\n\n## Evidence registry -- the only citable ids\n\n"
        + draft_authoring.render_registry(registry),
        prompt_version=draft_authoring.PROMPT_VERSION,
        output_schema=draft_authoring.ISSUE_SCHEMA,
        validate=lambda raw, ids=evidence_ids:
            draft_authoring.bind_issues(raw, evidence_ids=ids),
    )

    calculations = driver_runtime.structured_with_one_correction(
        provider=provider,
        prompt=draft_authoring.CALCULATION_INSTRUCTIONS + "\n\n" + requirements
        + "\n\n" + summary
        + "\n\n## Evidence registry -- the only citable ids\n\n"
        + draft_authoring.render_registry(registry),
        prompt_version=draft_authoring.PROMPT_VERSION,
        output_schema=draft_authoring.CALCULATION_SCHEMA,
        validate=lambda raw, ids=evidence_ids:
            draft_authoring.bind_calculations(raw, evidence_ids=ids),
    )
    if not calculations:
        raise RuntimeError(
            "BLOCKED: the records state no amounts, so no calculation could be "
            "written -- and the contract requires at least one. A report with "
            "an invented calculation is worse than a blocked stage.")

    final = driver_runtime.structured_with_one_correction(
        provider=provider,
        prompt=draft_authoring.FINAL_INSTRUCTIONS + "\n\n" + summary
        + "\n\n## Evidence registry\n\n"
        + draft_authoring.render_registry(registry),
        prompt_version=draft_authoring.PROMPT_VERSION,
        output_schema=draft_authoring.FINAL_ASSESSMENT_SCHEMA,
        validate=lambda raw, ids=evidence_ids:
            draft_authoring.bind_final_assessment(raw, evidence_ids=ids),
    )
    # A gate may not read not_applicable while the report is still asking a
    # professional questions, so what the document actually contains decides
    # what the gates may say.
    judgment_open = any(
        statement.get("support_type") == "professional_judgment"
        or statement.get("human_review_required")
        for section in sections.values()
        for statement in section.get("statements") or []
    ) or any(issue.get("unresolved_items") for issue in issues)
    gates = driver_runtime.structured_with_one_correction(
        provider=provider,
        prompt=draft_authoring.FINAL_INSTRUCTIONS + "\n\n" + summary,
        prompt_version=draft_authoring.PROMPT_VERSION,
        output_schema=draft_authoring.REVIEW_GATE_SCHEMA,
        validate=lambda raw, open_=judgment_open: (
            draft_authoring.check_gates(raw, judgment_open=open_) or dict(raw)),
    )

    return {
        "schema_version": "loss_adjustment_report.v1",
        "document_profile": dict(document_profile),
        "case_reference": dict(case_reference),
        "evidence_registry": registry,
        "sections": sections,
        "reasoning_issues": issues,
        "calculations": calculations,
        "final_assessment": final,
        "review_gates": gates,
    }


def run(*, case_id: str, version: str, run_id: str, held_by: str, provider,
        document_profile: Mapping[str, Any],
        case_reference: Mapping[str, Any]) -> dict:
    filename = f"loss_adjustment_report_{version}.json"
    if _dao_json(["read-contract", case_id, filename, "--run-id", run_id],
                 allow_missing=True) is not None:
        raise RuntimeError(
            f"BLOCKED: {filename} already exists for this run. A version's "
            "artifacts are never overwritten.")

    claim_analysis = _dao_json(["read-contract", case_id,
                                "claim_analysis_result.json", "--run-id", run_id])
    rebuttals = None
    if version == "v2":
        rebuttals = _dao_json(["read-contract", case_id, "rebuttal_points.json",
                               "--run-id", run_id], allow_missing=True)
        if rebuttals is None:
            raise RuntimeError(
                "BLOCKED: v2 exists to carry Phase 2's rebuttal points; without "
                "rebuttal_points.json it would be v1 written twice.")

    document = author(case_id=case_id, version=version, provider=provider,
                      claim_analysis=claim_analysis, rebuttals=rebuttals,
                      document_profile=document_profile,
                      case_reference=case_reference)

    data_file = _temp_json(document)
    try:
        proc = subprocess.run(
            [sys.executable, str(DAO), "write-contract", case_id, filename,
             "--data-file", str(data_file), "--schema-name", SCHEMA,
             "--held-by", held_by, "--run-id", run_id,
             "--stage", f"draft_report_{version}"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        if proc.returncode:
            raise RuntimeError((proc.stdout or proc.stderr or "").strip())
    finally:
        data_file.unlink(missing_ok=True)

    deferred = [k for k, s in document["sections"].items()
                if s.get("status") != "included"]
    return {
        "structured_report": filename,
        "evidence_sources": len(document["evidence_registry"]),
        "sections_deferred": deferred,
        "outcome": document["final_assessment"]["outcome"],
        "reasoning_issues": len(document["reasoning_issues"]),
        "calculations": len(document["calculations"]),
        "amount_determined": document["final_assessment"]["amount"] is not None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case_id")
    parser.add_argument("version", choices=["v1", "v2"])
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile-file", required=True,
                        help="JSON with document_profile and case_reference. "
                             "Required rather than inferred: family, "
                             "claim_mechanism and title decide which template "
                             "renders the deliverable, and guessing them "
                             "produces a document shape no adjuster writes.")
    add_provider_args(parser)
    args = parser.parse_args(argv)
    trace_mod.configure_from_args(args)

    profile = json.loads(Path(args.profile_file).read_text(encoding="utf-8"))
    stage = f"draft_report_{args.version}"
    args.provider, args.model = stage_models.resolve(
        stage, "authoring", provider=args.provider, model=args.model)
    try:
        provider = build_provider(parse_provider_config(args))
    except ProviderConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    try:
        print(json.dumps(run(case_id=args.case_id, version=args.version,
                             run_id=args.run_id, held_by=args.held_by,
                             provider=provider,
                             document_profile=profile["document_profile"],
                             case_reference=profile["case_reference"]),
                         ensure_ascii=False, sort_keys=True))
    except (RuntimeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
