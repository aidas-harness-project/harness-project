"""Screening Report assembler for the selective Claim Analysis lane.

Deterministic: it reads `claim_analysis_result.json` and
`evidence_validation_result.json` (plus the conflict ledger's deferred list)
and assembles `screening_report.json`. No provider call, no inference beyond
restating what those contracts already established with evidence.

The operational report carries exactly what a 손사/의사 needs to triage:

* core medical facts (read from case documents with exact citations --
  this lane claims no canonical projection authority),
* the four case types with 해당 / 불확실 / 비해당 and each verdict's direct basis,
* 접수 여부 per type,
* the per-type required-document checklist with 보유/미확인 status,
* whether a 후유장해진단서 / 신체감정서 already exists,
* verified material conflicts,
* what the records could not establish.

**Excluded on purpose.** Reading logs, per-field stop reasons, counter-evidence
notes, provider/token/wall telemetry, and conflict *candidates* that consistency
check withdrew. Those live in `claim_analysis_trace.json` and
`evidence_validation_result.json` -- development records, not a practitioner's
briefing. A screening report that shows its own search path buries the finding
under the process.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"

STAGE = "screening_report"
COMPONENT = "screening-report"
CONTRACT = "screening_report.json"
SCHEMA = "screening_report_selective.schema.json"
VERSION = "screening_report_selective.v0.1"
TEMPLATE = "screening_report_selective"
ASSEMBLY = ROOT / "tools" / "document_assembly.py"

# Korean labels: this is a deliverable read by Korean-speaking professionals,
# which is the documented exception to the English-only rule.
STATUS_LABEL = {
    "applicable": "해당",
    "uncertain": "불확실",
    "not_applicable": "비해당",
}
from medical_document_routing import KIND_LABEL_KO

CASE_TYPE_LABEL = {
    "personal_insurance": "개인보험",
    "traffic_accident": "교통사고",
    "industrial_accident": "산재/근재",
    "liability": "배상책임",
}
FILING_LABEL = {"filed": "접수", "not_filed": "미접수", "unknown": "확인 불가"}
CHECKLIST_LABEL = {
    "available": "보유",
    "missing": "미확인",
    "ambiguous": "미확인(분류 불가)",
    "not_applicable": "해당 없음",
}
DOCUMENT_KIND_LABEL = KIND_LABEL_KO

# The forms whose prior existence a screening reader asks about first.
DISABILITY_DOCUMENT_KIND = "disability_assessment"


def _dao_json(args: list[str], *, allow_missing: bool = False) -> dict | None:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode:
        if allow_missing and "NOT_FOUND:" in (proc.stdout or ""):
            return None
        raise RuntimeError((proc.stdout or proc.stderr or "DAO command failed").strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DAO command returned non-JSON output: {args[0]}") from exc


def _temp_json(value: Any) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", encoding="utf-8", delete=False)
    try:
        json.dump(value, handle, ensure_ascii=False)
    finally:
        handle.close()
    return Path(handle.name)


def _selected_value(field: Mapping[str, Any]) -> Any:
    selected = set(field.get("selected_observation_ids") or [])
    for observation in field.get("observations") or []:
        if observation.get("observation_id") in selected:
            return observation.get("value")
    return None


def withdrawn_candidate_ids(consistency: Mapping[str, Any]) -> set[str]:
    """Conflict candidates consistency_check judged NOT to be contradictions.

    A `consistent` check with no ledger entry is a withdrawal: the reviewing
    agent looked at both readings and found they state the same fact in
    different words. Nothing writes that judgement back into
    `claim_analysis_result.json` -- stage 5 is that contract's only writer --
    so without this the field stays `conflict` forever and the report prints
    확인 불가 for a value both sources actually agree on. Measured on CASE_049:
    `primary_diagnosis` rendered 확인 불가 two lines above `진단코드: S6280`,
    read from the same sentence of the same page.
    """
    return {
        check["conflict_candidate_id"]
        for check in consistency.get("checks") or []
        if check.get("result") == "consistent"
        and check.get("conflict_id") is None
        and check.get("conflict_candidate_id")
    }


def resolve_withdrawn_conflicts(
    facts: Mapping[str, Mapping[str, Any]],
    consistency: Mapping[str, Any],
) -> dict[str, dict]:
    """Facts with every withdrawn `conflict` field re-read as asserted.

    Returns a NEW mapping; the upstream contract on disk is never modified,
    which is the point -- the judgement lives in consistency_check's own
    contract and is applied at read time by each consumer.

    The elected reading is the FIRST observation, which is the highest-priority
    source the read ladder reached. That is a presentation choice, not a
    finding: the agent withdrew the candidate precisely because the readings do
    not disagree on fact, so no source is being declared the winner over
    another. A field whose candidates were not all withdrawn stays `conflict`.
    """
    withdrawn = withdrawn_candidate_ids(consistency)
    if not withdrawn:
        return dict(facts)

    resolved: dict[str, dict] = {}
    for field_id, field in facts.items():
        candidates = list(field.get("conflict_candidate_ids") or [])
        if (field.get("resolution_status") != "conflict"
                or not candidates
                or not all(c in withdrawn for c in candidates)):
            resolved[field_id] = dict(field)
            continue
        observations = field.get("observations") or []
        if not observations:
            resolved[field_id] = dict(field)
            continue
        elected = observations[0].get("observation_id")
        promoted = dict(field)
        promoted["resolution_status"] = "asserted"
        promoted["selected_observation_ids"] = [elected] if elected else []
        promoted["conflict_resolution"] = {
            "source": "consistency_check",
            "outcome": "withdrawn",
            "conflict_candidate_ids": candidates,
            "note": ("consistency_check judged these readings to state the same "
                     "fact; the highest-priority reading is shown"),
        }
        resolved[field_id] = promoted
    return resolved


def _first_value(facts: Mapping[str, Mapping[str, Any]], field_id: str) -> Any:
    field = facts.get(field_id)
    if field is None or field.get("resolution_status") != "asserted":
        return None
    return _selected_value(field)


def _selected_evidence(field: Mapping[str, Any]) -> list[dict]:
    """The exact citations behind the observation this field actually elected.

    Only the SELECTED observation's references: an `asserted` field elects
    exactly one reading, and the discarded ones are not evidence for the value
    that was published. Trimmed to the three fields the narrative needs --
    `start_char`/`end_char` stay upstream, where the DAO re-verifies them.
    """
    selected = set(field.get("selected_observation_ids") or [])
    references: list[dict] = []
    for observation in field.get("observations") or []:
        if observation.get("observation_id") not in selected:
            continue
        for reference in observation.get("evidence_references") or []:
            if not reference.get("document_id") or not reference.get("quote"):
                continue
            references.append({
                "document_id": reference["document_id"],
                "page": reference.get("page", 1),
                "quote": reference["quote"],
            })
    return references


def fact_evidence(
    facts: Mapping[str, Mapping[str, Any]],
    summary_fields: Mapping[str, str],
) -> dict:
    """Upstream evidence for each case-summary value that HAS one.

    Keyed by the summary field so the narrative can cite per value rather than
    per section: a section printing four facts of which two are grounded must
    carry those two citations and invent nothing for the others.

    A field with no asserted value contributes no key at all. That absence is
    what the narrative renders as "확인 불가" -- it is never filled with a
    citation borrowed from a neighbouring fact, which is exactly the
    fabrication P1 exists to prevent.
    """
    evidence: dict[str, list[dict]] = {}
    for summary_key, field_id in summary_fields.items():
        field = facts.get(field_id)
        if field is None or field.get("resolution_status") != "asserted":
            continue
        references = _selected_evidence(field)
        if references:
            evidence[summary_key] = references
    return evidence


# The case-summary values the narrative prints, and the claim fact each is read
# from. One place, so the value and its citation cannot come from different
# fields.
SUMMARY_FACT_FIELDS = {
    "accident_date": "accident_date",
    "main_diagnosis": "primary_diagnosis",
    "kcd_code": "diagnosis_code",
    "treatment_period": "treatment_period",
}


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, Mapping):
        # A `period`-shaped value is {"start": ..., "end": ...}. Three fields
        # carry that shape (treatment_period, admission_period,
        # disability_treatment_duration) but only the first was unpacked by
        # its own call site, so the other two fell through to `str()` and put
        # a Python dict repr into a Korean deliverable -- CASE_049 and
        # CASE_053 both rendered `입원기간: {'start': '2023-12-04', 'end':
        # '2023-12-07'}`. Formatting the shape here fixes every consumer at
        # once instead of one call site at a time.
        start, end = value.get("start"), value.get("end")
        if start and end:
            return f"{start} ~ {end}"
        if start:
            return f"{start} ~"
        if end:
            return f"~ {end}"
        # Not a period: render the pairs readably rather than as a repr.
        return ", ".join(f"{k}: {v}" for k, v in value.items() if v is not None) or None
    return str(value)


def case_type_section(assessments: Sequence[Mapping[str, Any]]) -> list[dict]:
    """One row per case type: verdict, its direct basis, and filing status.

    The basis is the assessment's own `reason` plus its exact evidence -- never
    a re-derivation here. A type with no evidence shows none rather than
    borrowing another type's.
    """
    rows = []
    for assessment in assessments:
        case_type = assessment["case_type"]
        rows.append({
            "case_type": case_type,
            "case_type_label": CASE_TYPE_LABEL.get(case_type, case_type),
            "status": assessment["status"],
            "status_label": STATUS_LABEL.get(
                assessment["status"], assessment["status"]),
            "basis": assessment["reason"],
            "evidence_references": [
                {
                    "document_id": reference["document_id"],
                    "page": reference["page"],
                    "quote": reference["quote"],
                }
                for reference in assessment.get("evidence_references") or []
            ],
            "filing_status": assessment.get("filing_status", "unknown"),
            "filing_status_label": FILING_LABEL.get(
                assessment.get("filing_status", "unknown"), "확인 불가"),
            # Only ever what an upstream source recorded. While no stage
            # produces a filing declaration, this is empty on every case and
            # the status stays 확인 불가 -- which is the honest reading, not a
            # gap to fill by inferring filing from how the accident happened.
            "filing_evidence_references": [
                {"document_id": reference["document_id"],
                 "page": reference.get("page", 1),
                 "quote": reference["quote"]}
                for reference in assessment.get("filing_evidence_references") or []
                if reference.get("document_id") and reference.get("quote")
            ],
        })
    return rows


def checklist_section(checklist: Sequence[Mapping[str, Any]]) -> list[dict]:
    rows = []
    for item in checklist:
        kind = item["document_kind"]
        rows.append({
            "document_kind": kind,
            "document_label": DOCUMENT_KIND_LABEL.get(kind, kind),
            "status": item["status"],
            "status_label": CHECKLIST_LABEL.get(item["status"], item["status"]),
            "required_for_case_types": [
                CASE_TYPE_LABEL.get(name, name)
                for name in item.get("required_for_case_types") or []
            ],
            "document_ids": list(item.get("document_ids") or []),
        })
    return rows


def existing_disability_documents(
    checklist: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Mapping[str, Any]],
) -> dict:
    """Whether a 후유장해진단서/신체감정서 is already in the pack.

    Reported as presence, not as an opinion on the assessment's contents: the
    rating itself is a D-grade judgment this pipeline does not make.
    """
    entry = next(
        (item for item in checklist
         if item.get("document_kind") == DISABILITY_DOCUMENT_KIND),
        None,
    )
    document_ids = list(entry.get("document_ids") or []) if entry else []
    recorded = facts.get("existing_disability_assessment")
    return {
        "present": bool(document_ids),
        "document_ids": document_ids,
        "status_label": "보유" if document_ids else "미확인",
        "recorded_in_claim_analysis": (
            recorded is not None
            and recorded.get("resolution_status") == "asserted"
        ),
        # Only when claim analysis actually read the form and asserted
        # something from it. Presence in the checklist is a fact about the
        # pack, not a quotable statement, so a case that merely HAS the
        # document carries no citation here.
        "evidence_references": (
            _selected_evidence(recorded)
            if recorded is not None
            and recorded.get("resolution_status") == "asserted"
            else []
        ),
    }


def _label_ko(row, key):
    """The reader-facing name of a config row, Korean first.

    `label` is the English identifier the code and changelog use; `label_ko`
    is what the report prints. Falling back through both means an untranslated
    config revision still renders a name rather than a bare field id.
    """
    return row.get("label_ko") or row.get("label") or row.get(key)


def unconfirmed_section(
    facts: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
) -> list[dict]:
    """Fields the records could not establish.

    Only fields that were actually searched (an A or B wave field) and came
    back `unknown`. A `not_applicable` field was never routable in this case
    and is not a gap in the records; a deferred field was never asked.
    """
    searched = {
        row["field_id"]: row for row in config.get("fields") or []
        if row.get("extraction_wave") in {"A", "B"}
    }
    rows = []
    for field_id, field in facts.items():
        if field_id not in searched:
            continue
        if field.get("resolution_status") != "unavailable":
            continue
        rows.append({
            "field_id": field_id,
            "label": _label_ko(searched[field_id], "field_id"),
            # The STRUCTURED cause travels with the sentence. Until 2026-08-21
            # only `reason` was carried, so a machine-readable consumer saw one
            # undifferentiated bucket: on CASE_700 all 25 unavailable fields
            # published `not_mentioned` while the free text held three
            # different causes. The distinction decides what a reviewer does --
            # request records, or note that a route never opened.
            "unavailable_reason": field.get("unavailable_reason"),
            "gap_kind": UNAVAILABLE_KIND.get(
                field.get("unavailable_reason"), "records_gap"),
            "reason": field.get(
                "resolution_reason", "자료에서 확인되지 않음"),
        })
    return sorted(rows, key=lambda row: row["field_id"])


# What a reader is meant to DO about each cause, which is the distinction the
# enum exists to carry. `records_gap` is the only one a document request can
# close; the others say the pipeline did not look, and why.
UNAVAILABLE_KIND = {
    "not_mentioned": "records_gap",
    "source_document_missing": "records_gap",
    "unreadable": "records_gap",
    "route_not_activated": "not_searched",
    "not_scheduled": "not_searched",
    "outside_poc_scope": "out_of_scope",
    "conflict_unresolved": "disputed",
}
UNAVAILABLE_KIND_LABEL = {
    "records_gap": "자료 미비",
    "not_searched": "탐색 미실행",
    "out_of_scope": "PoC 범위 밖",
    "disputed": "자료 간 불일치",
}


LINK_STATUS_LABEL = {
    "matched": "조항 확인",
    "candidate": "후보 조항",
    "not_found": "조항 미확인",
}
REQUIREMENT_STATUS_LABEL = {
    "supported": "자료 있음",
    "contradicted": "자료와 상충",
    "unknown": "자료 없음",
    "uncertain": "불확실",
    "conflict": "충돌 검토 필요",
}


def policy_links_section(links: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The clauses claim analysis linked, restated for a reader.

    Copied, not recomputed. `matched` keeps its exact `clause_ref` so the
    narrative can cite the clause text; `candidate` and `not_found` keep the
    reason they are uncertain, because a reviewer needs to know whether the
    policy was searched and came back ambiguous or was never there at all.

    `conflict_candidate_ids` travel unchanged: a requirement resting on a
    disputed fact must still point at that dispute after the hop, or the
    screening report would present a clean requirement over a disagreement the
    case actually recorded.
    """
    rows: list[dict] = []
    for link in links:
        status = link.get("clause_link_status", "not_found")
        row = {
            "coverage_id": link.get("coverage_id", ""),
            "coverage_name": link.get("coverage_name", ""),
            "clause_link_status": status,
            "clause_link_status_label": LINK_STATUS_LABEL.get(status, status),
            "uncertainty_reason": link.get("uncertainty_reason"),
            "requirements": [],
        }
        reference = link.get("clause_ref")
        if status == "matched" and isinstance(reference, dict):
            row["clause_ref"] = {
                "document_id": reference["document_id"],
                "page": reference.get("page", 1),
                "quote": reference["quote"],
            }
        for requirement in link.get("requirements") or []:
            requirement_status = requirement.get("evidence_status", "unknown")
            row["requirements"].append({
                "requirement_id": requirement.get("requirement_id", ""),
                "requirement_text": requirement.get("requirement_text", ""),
                "evidence_status": requirement_status,
                "evidence_status_label": REQUIREMENT_STATUS_LABEL.get(
                    requirement_status, requirement_status),
                "conflict_candidate_ids": list(
                    requirement.get("conflict_candidate_ids") or []),
                "reason": requirement.get("reason", ""),
                "evidence_references": [
                    {"document_id": item["document_id"],
                     "page": item.get("page", 1), "quote": item["quote"]}
                    for item in requirement.get("evidence_references") or []
                    if item.get("document_id") and item.get("quote")
                ],
            })
        rows.append(row)
    return rows


def build_report(
    *,
    case_id: str,
    run_id: str,
    claim_analysis: Mapping[str, Any],
    consistency: Mapping[str, Any],
    config: Mapping[str, Any],
    conflict_entries: Mapping[str, Mapping[str, Any]] | None = None,
    deferred_conflict_ids: Sequence[str] = (),
    report_path: str | None = None,
    denial_reasons: Mapping[str, Any] | None = None,
    agent_judgement: Mapping[str, Any] | None = None,
) -> dict:
    """Assemble `screening_report.json`.

    Two kinds of content, kept apart on purpose:

    * **Deterministic** -- restated from upstream contracts with their evidence:
      the case summary, the four type verdicts, the checklist, the unconfirmed
      items, the insurer's position, and each verified conflict's
      `professional_summary` COPIED VERBATIM from the ledger entry.
    * **Agent** (`agent_judgement`) -- the screening-report agent's `key_issues`,
      `review_points`, and per-conflict severity/placement. Nothing else: this
      lane makes no feasibility, difficulty, or payout judgement.

    The summary is copied rather than regenerated because it was written by
    whoever had the evidence in hand at the moment the conflict was confirmed.
    A downstream paraphrase of a disagreement is a second reading of it.
    """
    judgement = dict(agent_judgement or {})
    # Read-time application of consistency_check's judgement. The upstream
    # contract is not rewritten -- stage 5 remains its only writer -- so a
    # withdrawn conflict stops hiding a value both sources agreed on.
    facts = resolve_withdrawn_conflicts(
        {row["field_id"]: row for row in claim_analysis.get("claim_facts") or []},
        consistency,
    )
    assessments = claim_analysis.get("case_type_assessment") or []
    checklist = claim_analysis.get("required_document_checklist") or []
    entries = dict(conflict_entries or {})

    applicable = [
        CASE_TYPE_LABEL.get(row["case_type"], row["case_type"])
        for row in assessments if row["status"] == "applicable"
    ]
    uncertain = [
        CASE_TYPE_LABEL.get(row["case_type"], row["case_type"])
        for row in assessments if row["status"] == "uncertain"
    ]

    main_diagnosis = _as_text(_first_value(facts, "primary_diagnosis")) or "확인 불가"
    treatment_period = _first_value(facts, "treatment_period")
    period_block = None
    if isinstance(treatment_period, dict) and treatment_period.get("start"):
        period_block = {"start_date": treatment_period["start"]}
        if treatment_period.get("end"):
            period_block["end_date"] = treatment_period["end"]

    case_summary = {
        "case_type": " / ".join(applicable) if applicable else "확정된 유형 없음",
        "main_diagnosis": main_diagnosis,
        "kcd_code": _as_text(_first_value(facts, "diagnosis_code")),
        "accident_date": _as_text(_first_value(facts, "accident_date")),
        "claim_coverages": [],
        # Additive, and the point of this report: the four verdicts side by
        # side rather than one collapsed "case type" string.
        "case_type_assessment": case_type_section(assessments),
        # Per-value citations for the scalar facts above. The narrative reads
        # these to attach evidence to the exact values it prints, so a
        # displayed diagnosis is checkable and an unestablished one is visibly
        # unestablished rather than silently uncited.
        "fact_evidence": fact_evidence(facts, SUMMARY_FACT_FIELDS),
        "medical_authority": {
            "source": "source_document_extraction",
            "medical_projection_status": claim_analysis.get(
                "medical_projection_status", "not_configured"),
            "note": (
                "의료 사실은 사건 문서 원문에서 정확한 근거와 함께 추출한 값입니다. "
                "canonical 의료 체계의 권위를 주장하지 않습니다."
            ),
        },
    }
    if period_block:
        case_summary["treatment_period"] = period_block

    # Only conflicts consistency_check CONFIRMED reach the report. A withdrawn
    # or immaterial candidate is development detail, not a finding.
    severity_by_ref = dict(judgement.get("conflict_severity") or {})
    inconsistencies = []
    for check in consistency.get("checks") or []:
        if check.get("result") != "inconsistent":
            continue
        conflict_id = check.get("conflict_id")
        entry = entries.get(conflict_id, {})
        inconsistencies.append(_conflict_row(conflict_id, entry, severity_by_ref))

    # A deferred conflict is a carried obligation: finalize-stage refuses the
    # report unless every deferred id appears here by conflict_ref.
    carried = {row.get("conflict_ref") for row in inconsistencies}
    for conflict_id in deferred_conflict_ids:
        if conflict_id in carried:
            continue
        inconsistencies.append(
            _conflict_row(conflict_id, entries.get(conflict_id, {}), severity_by_ref))

    missing_documents = [
        {
            "document_type": row["document_kind"],
            "reason": (
                f"{', '.join(row['required_for_case_types'])} 유형에서 요구되는 "
                f"{row['document_label']}이(가) 확인되지 않았습니다."
            ),
        }
        for row in checklist_section(checklist)
        if row["status"] in {"missing", "ambiguous"}
    ]

    unconfirmed = unconfirmed_section(facts, config)
    # The agent's half. key_issues, review_points, and per-conflict severity
    # are readings of the case -- which questions matter and who can answer
    # them -- so they come from the screening-report agent, not from a rule
    # here. The fallbacks below state facts (a type is unconfirmed, N conflicts
    # were verified) and route them for review; they are a floor for a run with
    # no agent input, never a substitute for that judgement.
    key_issues = list(judgement.get("key_issues") or [])
    if not key_issues:
        if uncertain:
            key_issues.append({
                "issue_id": "ISSUE_1",
                "title": "사건유형 확정 필요",
                "description": (
                    f"{', '.join(uncertain)} 유형은 자료만으로 확정되지 않았습니다. "
                    "추가 자료 또는 담당자 확인이 필요합니다."
                ),
                "review_required": True,
                "reviewer_role": "손해사정사",
            })
        if inconsistencies:
            key_issues.append({
                "issue_id": f"ISSUE_{len(key_issues) + 1}",
                "title": "출처 간 불일치 확인 필요",
                "description": (
                    f"{len(inconsistencies)}건의 검증된 불일치가 확인되었습니다. "
                    "어느 값을 채택할지는 판단이 필요합니다."
                ),
                "review_required": True,
                "reviewer_role": "손해사정사",
            })

    review_points = list(judgement.get("review_points") or [])
    if not review_points:
        review_points = [{
            "point": issue["description"],
            "reviewer_role": issue.get("reviewer_role", "손해사정사"),
            "priority": "high",
        } for issue in key_issues]
    if not review_points:
        review_points = [{
            "point": "자동 판정 단계에서 추가 확인이 필요한 쟁점은 확인되지 않았습니다.",
            "reviewer_role": "손해사정사",
            "priority": "low",
        }]

    report = {
        "case_id": case_id,
        "run_id": run_id,
        "component": COMPONENT,
        "status": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_info": {"model_name": "deterministic", "prompt_version": VERSION},
        "review_required": True,
        "reviewer_role": "손해사정사",
        # The agent's own warnings, carried through rather than dropped.
        # `key_issues`, `review_points` and `conflict_severity` were read from
        # the judgement contract while this stayed hardcoded to [], so on
        # CASE_053 seven warnings -- the lane/case-scope mismatch, the
        # medical-only accident routing, the candidate-only clause links --
        # were written by the agent, validated, and then silently discarded.
        # A caller trusting this field saw a clean run.
        "warnings": list(judgement.get("warnings") or []),
        "source_grounded": True,
        "report_path": report_path or f"outputs/{case_id}/screening_report.md",
        "case_summary": case_summary,
        "insurer_position": insurer_position(denial_reasons),
        "key_issues": key_issues,
        "inconsistencies": inconsistencies,
        "missing_documents": missing_documents,
        "review_points": review_points,
        # Compatibility only. The selective contract does not require this
        # section, and the lane makes no feasibility/difficulty judgement -- so
        # the helper writes constants and the agent contributes nothing here.
        # `priority_review_points` is copied from the review points above, not
        # re-decided.
        "preliminary_assessment": {
            "feasibility": "not_assessed",
            "difficulty": "not_assessed",
            "priority_review_points": [
                point["point"] for point in review_points
            ],
            "rationale_evidence_references": [],
        },
        # Additive operational sections.
        # Carried through from claim analysis rather than re-derived. This
        # report links no clauses of its own: it shows the ones the analysis
        # already established, with their status and their uncertainty, and
        # adds no coverage or payout verdict on top.
        "policy_links": policy_links_section(
            claim_analysis.get("policy_links") or []),
        "required_document_checklist": checklist_section(checklist),
        "existing_disability_assessment": existing_disability_documents(
            checklist, facts),
        "established_facts": established_facts(facts, config),
        "unconfirmed_items": unconfirmed,
    }
    return report


def established_facts(
    facts: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
) -> list[dict]:
    """Every asserted fact with its value and citations, grouped by domain.

    The counterpart to `unconfirmed_items`: that lists what the records could
    not establish, this lists what they DID -- so the two together account for
    every field the lane searched. Before this existed the report showed only
    the four fields `SUMMARY_FACT_FIELDS` names, and the rest of what claim
    analysis had resolved never reached the reader (CASE_489: 19 asserted, 2
    printed).

    Labels come from the routing config, which already names every domain and
    field, so this introduces no second vocabulary to keep in sync.
    """
    domain_labels = {
        domain["code"]: _label_ko(domain, "code")
        for domain in config.get("domains") or []
    }
    field_labels = {
        row["field_id"]: _label_ko(row, "field_id")
        for row in config.get("fields") or []
    }
    order = list(domain_labels)
    grouped: dict[str, list[dict]] = {}
    for field_id, field in facts.items():
        if field.get("resolution_status") != "asserted":
            continue
        value = _as_text(_first_value({field_id: field}, field_id))
        if value in (None, ""):
            continue
        grouped.setdefault(field.get("domain_code") or "", []).append({
            "field_id": field_id,
            "field_label": field_labels.get(field_id, field_id),
            "value_text": value,
            "evidence_references": _selected_evidence(field),
        })
    return [
        {
            "domain_code": code,
            "domain_label": domain_labels.get(code, code),
            "facts": rows,
        }
        for code, rows in sorted(
            grouped.items(),
            key=lambda item: order.index(item[0]) if item[0] in order else len(order))
    ]


def _conflict_row(
    conflict_id: str | None,
    entry: Mapping[str, Any],
    severity_by_ref: Mapping[str, str],
) -> dict:
    """One inconsistency row, carrying the ledger's own wording.

    `professional_summary` is reproduced exactly. When an entry predates that
    field there is nothing to carry, so the row shows the disagreeing values
    and their sources and says where that leaves it -- a summary invented here
    would be this stage's reading of a disagreement it did not examine.
    """
    sources = entry.get("sources") or []
    summary = entry.get("professional_summary")
    if summary:
        description = summary
        summary_source = "consistency_check_professional_summary"
    else:
        described = " / ".join(
            f"{source.get('document_id')} p.{source.get('page')}: {source.get('value')}"
            for source in sources
        )
        description = (
            f"출처 간 값이 다릅니다. {described}".strip()
            if described else "출처 간 값이 다릅니다."
        )
        summary_source = "legacy_entry_without_summary"
    row = {
        "field": entry.get("field_or_topic") or (conflict_id or ""),
        "description": description,
        "severity": severity_by_ref.get(conflict_id, "high"),
        "summary_source": summary_source,
        "related_documents": [
            source["document_id"] for source in sources
            if source.get("document_id")
        ],
        "source_values": [
            {
                "document_id": source.get("document_id"),
                "page": source.get("page"),
                "value": source.get("value"),
                "quote": source.get("quote"),
            }
            for source in sources
        ],
    }
    if conflict_id:
        row["conflict_ref"] = conflict_id
    return row


def insurer_position(denial_reasons: Mapping[str, Any] | None) -> dict:
    """The insurer's own decision, preserved as three separate outcomes.

    Hard-coding false was wrong in the one direction that matters: a case where
    the insurer denied or reduced would have been summarized as though it had
    not, and the screening report is the first document a human reads. False is
    only correct when there is no insurer response contract to read.
    """
    if not denial_reasons:
        return {
            "has_denial": False,
            "has_reduction": False,
            "has_denial_or_reduction": False,
            "denial": {"reason_ids": [], "total_amount": None},
            "reduction": {"reason_ids": [], "total_amount": None},
        }

    reasons = denial_reasons.get("denial_reasons") or []
    denial_ids = [r["reason_id"] for r in reasons
                  if r.get("decision_type") == "denial"]
    reduction_ids = [r["reason_id"] for r in reasons
                     if r.get("decision_type") == "reduction"]
    accepted = denial_reasons.get("accepted_coverages") or []
    position = {
        "has_denial": bool(denial_ids),
        "has_reduction": bool(reduction_ids),
        "has_denial_or_reduction": bool(denial_ids or reduction_ids),
        "denial": {
            "reason_ids": denial_ids,
            "total_amount": _total_amount(reasons, "denial"),
            "evidence_references": _decision_evidence(reasons, "denial"),
        },
        "reduction": {
            "reason_ids": reduction_ids,
            "total_amount": _total_amount(reasons, "reduction"),
            "evidence_references": _decision_evidence(reasons, "reduction"),
        },
    }
    if accepted:
        # Silence about an accepted coverage reads as a total denial, which is
        # the misreading the upstream field exists to prevent.
        position["has_acceptance"] = True
        position["acceptance"] = {
            "accepted_coverage_ids": [
                row["accepted_coverage_id"] for row in accepted
            ],
            "evidence_references": _dedupe_references([
                reference
                for row in accepted
                for reference in row.get("evidence_references") or []
            ]),
        }
    return position


def _decision_evidence(
    reasons: Sequence[Mapping[str, Any]],
    decision_type: str,
) -> list[dict]:
    """What the insurer itself wrote, for the reasons of one decision type.

    Copied from the denial contract; nothing here reads a source document. A
    reason with no citation contributes none rather than borrowing the one
    next to it -- the reason code still shows, and the missing basis is
    visible as a missing citation.
    """
    return _dedupe_references([
        reference
        for reason in reasons if reason.get("decision_type") == decision_type
        for reference in reason.get("evidence_references") or []
    ])


def _total_amount(reasons: Sequence[Mapping[str, Any]], decision_type: str):
    """A total only when every contributing amount is stated.

    A partial sum presented as a total understates what the insurer withheld,
    and nothing downstream would show that a figure was incomplete.
    """
    amounts = [r.get("amount") for r in reasons
               if r.get("decision_type") == decision_type]
    if not amounts or any(amount is None for amount in amounts):
        return None
    return sum(amounts)


def require_open_attempt(case_id: str, run_id: str) -> None:
    state = _dao_json(
        ["read-contract", case_id, "_run_state.json", "--run-id", run_id])
    if not any(
        item.get("stage_name") == STAGE and item.get("status") == "in_progress"
        for item in (state or {}).get("stages", [])
    ):
        raise RuntimeError(
            "BLOCKED: screening_report must be in_progress; the orchestrator "
            "owns attempt state")


# ------------------------------------------------------ markdown sections --

def _dedupe_references(references: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Same citation once per section, in first-seen order.

    Two facts read off one sentence produce the same reference twice; the
    assembly tool would then emit two tags for one quote, which reads as two
    independent corroborations of a single source.
    """
    seen: set[tuple] = set()
    unique: list[dict] = []
    for reference in references:
        key = (reference.get("document_id"), reference.get("page"),
               reference.get("quote"))
        if key in seen or not key[0] or not key[2]:
            continue
        seen.add(key)
        unique.append({"document_id": key[0], "page": key[1], "quote": key[2]})
    return unique


def _mark(reference: Mapping[str, Any], collected: list[dict]) -> str:
    """Collect one reference and return the `{{E}}` it is owed, if any.

    The renderer requires a section's placeholder count to equal its reference
    count, and the section publishes `_dedupe_references(collected)` -- so a
    marker may only be emitted for a reference that SURVIVES deduplication.
    Emitting one per appended reference is wrong in the other direction: two
    requirements citing one sentence collapse to a single published reference
    and would leave an orphan placeholder.

    Both failures are silent at write time and fatal at render time, which is
    how section 8 shipped with zero markers for two references (CASE_700,
    2026-08-21) -- the first run to supply a judgement AND match a
    coverage-level 약관, so the branch had never executed with a real
    `clause_ref` before.
    """
    if not reference.get("document_id") or not reference.get("quote"):
        # `_dedupe_references` drops these, so a marker would be an orphan.
        return ""
    key = (reference.get("document_id"), reference.get("page"),
           reference.get("quote"))
    if any(key == (r.get("document_id"), r.get("page"), r.get("quote"))
           for r in collected):
        return ""
    collected.append(dict(reference))
    return "{{E}}"


def _bullet(label: str, value, *, cited: int = 0) -> str:
    """One fact line, carrying one `{{E}}` per citation that backs it.

    `document_assembly.render` substitutes `[E#]` into these placeholders and
    builds the sidecar from the same references, and refuses the section unless
    the counts match 1:1 -- the P1 mechanism that stops a tag and its citation
    drifting apart. Printing the value without a placeholder is therefore not a
    cosmetic omission: it made assembly reject the whole report on CASE_489
    ("0 {{E}} placeholders but 1 evidence_references").

    A line the records could not establish renders 확인 불가 and takes no
    placeholder -- it has nothing to cite, and must not borrow the citation of
    the fact printed next to it.
    """
    if value in (None, "", [], {}):
        return f"- {label}: 확인 불가"
    if isinstance(value, list):
        rendered = ", ".join(str(item) for item in value)
    else:
        rendered = str(value)
    return f"- {label}: {rendered}{' ' + '{{E}}' * cited if cited else ''}"


def _cited_bullets(rows) -> tuple[list[str], list[dict]]:
    """Bullet lines and their references, with the 1:1 pairing already right.

    `document_assembly.render` refuses a section whose `{{E}}` count differs
    from its `evidence_references` count -- the P1 mechanism that keeps a tag
    and its citation from drifting apart. Every section that prints cited facts
    has to satisfy it, and doing that by hand per section is what produced the
    same defect three times on CASE_489 (sections 1, 2 and 9, each surfacing
    only once the data that populates it arrived).

    Takes (label, value, references) and returns lines paired with the flat
    reference list. Deduplicated across the section: two facts read off one
    sentence cite it once, and the line that first introduces a citation is the
    one that carries its placeholder.
    """
    lines: list[str] = []
    collected: list[dict] = []
    for label, value, references in rows:
        fresh = [
            reference
            for reference in _dedupe_references(list(references or []))
            if reference not in collected
        ]
        # A line with no value states 확인 불가 and cites nothing -- it must not
        # borrow the citation of the fact printed beside it.
        if value in (None, "", [], {}):
            fresh = []
        lines.append(_bullet(label, value, cited=len(fresh)))
        collected.extend(fresh)
    return lines, collected


def markdown_sections(report: Mapping[str, Any]) -> list[dict]:
    """The nine sections of the selective template, as document_assembly input.

    Content only -- no `[E#]` tags. The tool generates those and the matching
    sidecar from the `evidence_references` supplied here, so a tag and its
    citation cannot drift apart; writing a tag by hand is what P1 forbids.

    Every section is rendered even when empty, because the template enforces
    exactly these nine in order, and because "이 항목은 확인되지 않았습니다" is
    itself information for a reviewer -- a silently omitted section is
    indistinguishable from one nobody looked at.
    """
    summary = report.get("case_summary") or {}
    evidence_by_fact = summary.get("fact_evidence") or {}
    sections: list[dict] = []

    # 1. accident and shared medical facts
    #
    # Values and citations are collected together, so the section carries the
    # evidence for exactly the values it printed. A value the records did not
    # establish renders as 확인 불가 and contributes no citation -- it never
    # borrows one from the fact printed next to it.
    period = summary.get("treatment_period") or {}
    printed = [
        ("사고일", "accident_date", summary.get("accident_date")),
        ("주요 진단명", "main_diagnosis", summary.get("main_diagnosis")),
        ("진단코드", "kcd_code", summary.get("kcd_code")),
        ("치료기간", "treatment_period",
         (f"{period.get('start_date')} ~ {period.get('end_date') or '미종결'}")
         if period else None),
    ]
    # Each line carries one placeholder per citation it contributes, in the
    # same order the references are collected below, so assembly's 1:1 pairing
    # binds each `[E#]` to the fact it was printed beside.
    # Deduplicated ACROSS the section, not per line: two facts read off one
    # sentence cite it once, or the report would read as two corroborations of
    # each other. The line that first introduces a citation carries its
    # placeholder; a later line repeating the same sentence carries none.
    printed_refs: list[list[dict]] = []
    seen: list[dict] = []
    for _label, key, value in printed:
        if value in (None, "", [], {}, "확인 불가"):
            printed_refs.append([])
            continue
        fresh = [
            reference
            for reference in _dedupe_references(list(evidence_by_fact.get(key) or []))
            if reference not in seen
        ]
        seen.extend(fresh)
        printed_refs.append(fresh)
    sections.append({
        "heading": "1. 사고와 공통 의료정보",
        "content": "\n".join(
            _bullet(label, value, cited=len(refs))
            for (label, _key, value), refs in zip(printed, printed_refs)),
        "evidence_references": [
            reference for refs in printed_refs for reference in refs
        ],
    })

    # 2. the four verdicts, side by side
    rows = summary.get("case_type_assessment") or []
    row_refs: list[list[dict]] = []
    seen_rows: list[dict] = []
    for row in rows:
        fresh = []
        for ref in row.get("evidence_references") or []:
            citation = {"document_id": ref["document_id"], "page": ref["page"],
                        "quote": ref["quote"]}
            if citation not in seen_rows and citation not in fresh:
                fresh.append(citation)
        seen_rows.extend(fresh)
        row_refs.append(fresh)
    sections.append({
        "heading": "2. 사건유형 병렬 판정",
        "content": "\n".join(
            f"- {row['case_type_label']}: {row['status_label']} — {row['basis']}"
            + ("{{E}}" * len(refs) if refs else "")
            for row, refs in zip(rows, row_refs)) or "- 판정 결과 없음",
        "evidence_references": [ref for refs in row_refs for ref in refs],
    })

    # 3. filing status per type
    #
    # Filing is an administrative fact somebody recorded, so it carries the
    # evidence of that record when one exists. A type whose status is 확인 불가
    # contributes none -- there was no record to cite, which is the finding.
    sections.append({
        "heading": "3. 유형별 추가정보와 접수 상태",
        "content": "\n".join(
            f"- {row['case_type_label']}: 접수 {row['filing_status_label']}"
            for row in rows) or "- 접수 정보 없음",
        "evidence_references": _dedupe_references([
            reference
            for row in rows if row.get("filing_status") in {"filed", "not_filed"}
            for reference in row.get("filing_evidence_references") or []
        ]),
    })

    # 4. which medical areas the records could establish
    #
    # The diagnosis printed here is the same value section 1 prints, so it
    # carries the same citation rather than a second reading of it. The
    # disability form's evidence is its presence in the pack, which the
    # checklist records by document id.
    unconfirmed = report.get("unconfirmed_items") or []
    disability = report.get("existing_disability_assessment") or {}
    established = report.get("established_facts") or []

    # Every fact claim analysis ASSERTED, grouped by its domain -- not a
    # whitelist of four slots. Measured on CASE_489: 19 fields resolved with
    # values (surgery name, admission period, disability type, injury site and
    # laterality, department, joint range of motion ...) and the report printed
    # two of them, because SUMMARY_FACT_FIELDS named only accident_date,
    # primary_diagnosis, diagnosis_code and treatment_period. Everything else
    # stayed in the contract, so the 손해사정사 reading this document could not
    # see what the pipeline had already established. Widening the whitelist
    # would lose the next field added; grouping by the domain each fact already
    # carries does not.
    lines: list[str] = []
    references: list[dict] = []
    for group in established:
        lines.append(f"- {group['domain_label']}")
        for row in group["facts"]:
            citations = row.get("evidence_references") or []
            lines.append(
                f"  - {row['field_label']}: {row['value_text']}"
                + ("{{E}}" * len(citations) if citations else ""))
            references.extend(citations)
    if not lines:
        lines.append(_bullet("확인된 핵심 의료정보", None))
    lines.append(_bullet("후유장해진단서/신체감정서", disability.get("status_label")))
    disability_refs = _dedupe_references(
        disability.get("evidence_references") or [])
    if disability_refs:
        lines[-1] += "{{E}}" * len(disability_refs)
        references.extend(disability_refs)
    lines.append(_bullet("미확인 항목 수", len(unconfirmed) if unconfirmed else None))

    sections.append({
        "heading": "4. 의료영역 확보 현황",
        "content": "\n".join(lines),
        "evidence_references": references,
    })

    # 5. required-document checklist
    checklist = report.get("required_document_checklist") or []
    sections.append({
        "heading": "5. 필요서류 체크리스트",
        "content": "\n".join(
            f"- {row['document_label']}: {row['status_label']}"
            f" ({', '.join(row['required_for_case_types'])})"
            for row in checklist) or "- 체크리스트 없음",
        "evidence_references": [],
    })

    # 6. verified conflicts, in consistency-check's own words
    inconsistencies = report.get("inconsistencies") or []
    conflict_evidence = [
        {"document_id": source["document_id"], "page": source.get("page", 1),
         "quote": source["quote"]}
        for row in inconsistencies
        for source in (row.get("source_values") or [])
        if source.get("document_id") and source.get("quote")
    ]
    sections.append({
        "heading": "6. 중요 충돌과 유형 판정 영향",
        "content": "\n".join(
            f"- [{row['field']}] {row['description']}"
            for row in inconsistencies) or "- 검증된 충돌 없음",
        "evidence_references": conflict_evidence,
    })

    # 7. what the records could not establish
    #
    # Grouped by what the reader can DO about it. A flat list read as one
    # undifferentiated "missing" pile: on CASE_700 all 25 entries carried
    # `not_mentioned`, mixing 21 genuine records gaps with 3 fields whose
    # conditional route never opened and 1 never scheduled a read. Only the
    # first group is closable by requesting documents; a reviewer chasing the
    # other four would find nothing to chase.
    grouped: dict[str, list[dict]] = {}
    for row in unconfirmed:
        grouped.setdefault(row.get("gap_kind") or "records_gap", []).append(row)
    unconfirmed_lines: list[str] = []
    for kind in ("records_gap", "disputed", "not_searched", "out_of_scope"):
        rows = grouped.get(kind)
        if not rows:
            continue
        if len(grouped) > 1:
            unconfirmed_lines.append(
                f"**{UNAVAILABLE_KIND_LABEL.get(kind, kind)}** ({len(rows)}건)")
        unconfirmed_lines.extend(
            f"- {row['label']}: {row['reason']}" for row in rows)
    sections.append({
        "heading": "7. 주요 미확인 항목",
        "content": "\n".join(unconfirmed_lines) or "- 미확인 항목 없음",
        "evidence_references": [],
    })

    # 8. linked clauses and requirement evidence status
    #
    # A matched link names its clause and cites the clause text; a candidate or
    # not_found link states why it is uncertain and cites nothing, because
    # naming a clause the processed text does not confirm is the fabricated
    # reference this lane refuses. The report adds no coverage verdict on top
    # of either -- which clause applies is the reviewer's question, and this
    # section exists to put the clause in front of them.
    links = report.get("policy_links") or []
    link_lines: list[str] = []
    link_references: list[dict] = []
    for link in links:
        label = link.get("clause_link_status_label", link.get(
            "clause_link_status", ""))
        line = f"- {link.get('coverage_name') or link.get('coverage_id')}: {label}"
        reference = link.get("clause_ref")
        if link.get("clause_link_status") == "matched" and isinstance(reference, dict):
            # Every appended reference needs its own `{{E}}` marker: the
            # renderer refuses a section whose placeholder count differs from
            # its reference count. This line appended the clause_ref without
            # one, and stayed invisible until CASE_700 became the first run to
            # reach it -- a matched clause_ref only arrives here when a
            # `screening_report_judgement.json` is supplied AND the linker
            # matched a clause, and until 2026-08-20 no coverage-level 약관
            # ever matched, so the branch never ran with a real reference.
            line += (f" — {reference['document_id']} p.{reference.get('page')} "
                     f"{reference['quote']}")
            line += _mark(reference, link_references)
            # A matched coverage may still carry a note, and until 2026-08-20
            # this was an `elif` that dropped it: a coverage spanning several
            # articles elects one into `clause_ref`, and the sentence naming
            # its remaining articles never reached the reader.
            if link.get("uncertainty_reason"):
                line += f"\n  - {link['uncertainty_reason']}"
        elif link.get("uncertainty_reason"):
            line += f" — {link['uncertainty_reason']}"
        for requirement in link.get("requirements") or []:
            status = requirement.get(
                "evidence_status_label", requirement.get("evidence_status", ""))
            line += f"\n  - {requirement.get('requirement_text', '')}: {status}"
            candidates = requirement.get("conflict_candidate_ids") or []
            if candidates:
                line += f" (충돌 후보 {', '.join(candidates)})"
            for evidence in requirement.get("evidence_references") or []:
                line += _mark(evidence, link_references)
        link_lines.append(line)
    sections.append({
        "heading": "8. 관련 약관과 요건 자료상태",
        "content": "\n".join(link_lines) or "- 연결된 약관 조항 없음",
        "evidence_references": _dedupe_references(link_references),
    })

    # 9. the insurer's own decision
    #
    # The grounds the insurer itself stated, cited to the response document.
    # Without them a reader sees reason codes and has no way to check what the
    # insurer actually wrote.
    position = report.get("insurer_position") or {}
    denial = position.get("denial") or {}
    reduction = position.get("reduction") or {}
    acceptance = position.get("acceptance") or {}
    lines, references = _cited_bullets([
        ("거절", ", ".join(denial.get("reason_ids") or []) or None,
         denial.get("evidence_references")),
        ("감액", ", ".join(reduction.get("reason_ids") or []) or None,
         reduction.get("evidence_references")),
        ("승인", ", ".join(acceptance.get("accepted_coverage_ids") or []) or None,
         acceptance.get("evidence_references")),
    ])
    sections.append({
        "heading": "9. 보험사 응답",
        "content": "\n".join(lines),
        "evidence_references": references,
    })
    return sections


def render_markdown(
    *, case_id: str, run_id: str, held_by: str, report: Mapping[str, Any],
) -> str:
    """Render the .md and its evidence sidecar through document_assembly.

    Called rather than reimplemented: that tool verifies every citation quote
    against the processed text and refuses the whole document if one does not
    resolve, then writes the file and the sidecar atomically under a lock. A
    hand-rolled renderer here would be a second, unverified way to produce the
    deliverable.
    """
    output_path = f"outputs/{case_id}/screening_report.md"
    spec = {"output_path": output_path,
            "sections": markdown_sections(report)}
    spec_file = _temp_json(spec)
    try:
        proc = subprocess.run(
            [sys.executable, str(ASSEMBLY),
             "--sections-file", str(spec_file),
             "--template", TEMPLATE,
             "--held-by", held_by, "--run-id", run_id],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
        if proc.returncode:
            raise RuntimeError(
                "document assembly refused the screening report:\n"
                + (proc.stdout or proc.stderr or "").strip())
    finally:
        spec_file.unlink(missing_ok=True)
    return output_path


def run(*, case_id: str, run_id: str, held_by: str) -> dict:
    """Assemble and publish the screening report.

    Gate order matters: P6 first. A pending conflict means the case has an
    unadjudicated disagreement, and a triage document written over one tells a
    professional the case is ready when it is not.
    """
    require_open_attempt(case_id, run_id)
    conflicts = _dao_json(["check-conflicts-clear", case_id])
    pending = (conflicts or {}).get("pending") or []
    if pending:
        raise RuntimeError(
            "BLOCKED: P6 -- the case has unresolved conflict ledger entries: "
            + ", ".join(pending))
    deferred = (conflicts or {}).get("deferred_to_report") or []

    claim_analysis = _dao_json(
        ["read-contract", case_id, "claim_analysis_result.json",
         "--run-id", run_id])
    consistency = _dao_json(
        ["read-contract", case_id, "evidence_validation_result.json",
         "--run-id", run_id], allow_missing=True) or {"checks": []}
    denial = _dao_json(
        ["read-contract", case_id, "denial_reason_result.json",
         "--run-id", run_id], allow_missing=True)
    judgement = _dao_json(
        ["read-contract", case_id, "screening_report_judgement.json",
         "--run-id", run_id], allow_missing=True)

    ledger = _dao_json(["read-conflict-ledger", case_id], allow_missing=True) or {}
    entries = {row["conflict_id"]: row for row in ledger.get("conflicts") or []}

    config = json.loads(
        (ROOT / "config" / "claim_analysis" /
         "claim_analysis_routing_v0.1.json").read_text(encoding="utf-8"))

    report = build_report(
        case_id=case_id, run_id=run_id, claim_analysis=claim_analysis,
        consistency=consistency, config=config, conflict_entries=entries,
        deferred_conflict_ids=deferred, denial_reasons=denial,
        agent_judgement=judgement,
    )
    if denial:
        # A derived contract must record which reason set it was built from;
        # the DAO refuses the write otherwise, and would refuse it again later
        # if that set changed underneath.
        import _cross_contract
        report["source_denial_contract_hash"] = _cross_contract.upstream_hash(denial)

    data_file = _temp_json(report)
    try:
        proc = subprocess.run(
            [sys.executable, str(DAO), "write-contract", case_id, CONTRACT,
             "--data-file", str(data_file), "--schema-name", SCHEMA,
             "--held-by", held_by, "--run-id", run_id, "--stage", STAGE],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
        if proc.returncode:
            raise RuntimeError((proc.stdout or proc.stderr or "").strip())
    finally:
        data_file.unlink(missing_ok=True)
    # JSON first, then the narrative rendered FROM it. The order matters: the
    # Markdown is a view of the contract that was already schema-validated and
    # persisted, so the two cannot describe different findings.
    markdown_path = render_markdown(
        case_id=case_id, run_id=run_id, held_by=held_by, report=report)

    return {
        "inconsistencies": len(report["inconsistencies"]),
        "deferred_carried": len(deferred),
        "has_denial": report["insurer_position"]["has_denial"],
        "report_path": markdown_path,
        "evidence_sidecar": markdown_path.replace(".md", ".evidence.json"),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(case_id=args.case_id, run_id=args.run_id,
                             held_by=args.held_by),
                         ensure_ascii=False, sort_keys=True))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
