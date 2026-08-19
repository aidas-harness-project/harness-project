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
SCHEMA = "screening_report.schema.json"
VERSION = "screening_report_selective.v0.1"

# Korean labels: this is a deliverable read by Korean-speaking professionals,
# which is the documented exception to the English-only rule.
STATUS_LABEL = {
    "applicable": "해당",
    "uncertain": "불확실",
    "not_applicable": "비해당",
}
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
DOCUMENT_KIND_LABEL = {
    "diagnosis_certificate": "진단서",
    "initial_visit_record": "초진기록지",
    "outpatient_record": "외래기록",
    "progress_record": "경과기록지",
    "final_visit_record": "최종진료기록",
    "surgery_procedure_record": "수술기록지",
    "imaging_interpretation": "영상판독지",
    "major_test_result": "주요검사결과지",
    "admission_discharge_summary": "입퇴원요약",
    "emergency_record": "응급실기록",
    "disability_assessment": "후유장해진단서/신체감정서",
    "prescription_treatment_history": "처방·치료내역",
    "nursing_routine_record": "간호기록지",
    "medical_expense_receipt": "진료비 영수증",
    "medical_expense_itemization": "진료비 세부내역서",
    "pharmacy_payment_confirmation": "약제비 납입확인서",
    "other_medical": "기타 의무기록",
}

# The forms whose prior existence a screening reader asks about first.
DISABILITY_DOCUMENT_KIND = "disability_assessment"


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


def _first_value(facts: Mapping[str, Mapping[str, Any]], field_id: str) -> Any:
    field = facts.get(field_id)
    if field is None or field.get("resolution_status") != "asserted":
        return None
    return _selected_value(field)


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "예" if value else "아니오"
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
            and recorded.get("resolution_status") == "resolved"
        ),
    }


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
            "label": searched[field_id].get("label", field_id),
            "reason": field.get(
                "resolution_reason", "자료에서 확인되지 않음"),
        })
    return sorted(rows, key=lambda row: row["field_id"])


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
) -> dict:
    """Assemble `screening_report.json` from the two upstream contracts."""
    facts = {row["field_id"]: row for row in claim_analysis.get("claim_facts") or []}
    assessments = claim_analysis.get("case_type_assessment") or []
    checklist = claim_analysis.get("required_document_checklist") or []
    entries = dict(conflict_entries or {})

    supported = [
        CASE_TYPE_LABEL.get(row["case_type"], row["case_type"])
        for row in assessments if row["status"] == "supported"
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
        "case_type": " / ".join(supported) if supported else "확정된 유형 없음",
        "main_diagnosis": main_diagnosis,
        "kcd_code": _as_text(_first_value(facts, "diagnosis_code")),
        "accident_date": _as_text(_first_value(facts, "accident_date")),
        "claim_coverages": [],
        # Additive, and the point of this report: the four verdicts side by
        # side rather than one collapsed "case type" string.
        "case_type_assessment": case_type_section(assessments),
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
    inconsistencies = []
    for check in consistency.get("checks") or []:
        if check.get("result") != "inconsistent":
            continue
        conflict_id = check.get("conflict_id")
        entry = entries.get(conflict_id, {})
        sources = entry.get("sources") or []
        described = " / ".join(
            f"{source.get('document_id')}: {source.get('value')}"
            for source in sources
        )
        row = {
            "field": entry.get("field_or_topic") or check.get("topic", ""),
            "description": (
                f"출처 간 값이 다릅니다. {described}" if described
                else "출처 간 값이 다릅니다."
            ),
            "severity": "high",
            "related_documents": [
                source["document_id"] for source in sources
                if source.get("document_id")
            ],
        }
        if conflict_id:
            row["conflict_ref"] = conflict_id
        inconsistencies.append(row)

    # A deferred conflict is a carried obligation: finalize-stage refuses the
    # report unless every deferred id appears here by conflict_ref.
    carried = {row.get("conflict_ref") for row in inconsistencies}
    for conflict_id in deferred_conflict_ids:
        if conflict_id in carried:
            continue
        entry = entries.get(conflict_id, {})
        sources = entry.get("sources") or []
        inconsistencies.append({
            "field": entry.get("field_or_topic") or conflict_id,
            "description": (
                "판단이 필요한 출처 간 불일치입니다. "
                + " / ".join(
                    f"{source.get('document_id')}: {source.get('value')}"
                    for source in sources
                )
            ).strip(),
            "severity": "high",
            "conflict_ref": conflict_id,
            "related_documents": [
                source["document_id"] for source in sources
                if source.get("document_id")
            ],
        })

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
    key_issues = []
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
        "warnings": [],
        "source_grounded": True,
        "report_path": report_path or f"outputs/{case_id}/screening_report.md",
        "case_summary": case_summary,
        "insurer_position": {
            "has_denial": False,
            "has_reduction": False,
            "has_denial_or_reduction": False,
            "denial": {"reason_ids": [], "total_amount": None},
            "reduction": {"reason_ids": [], "total_amount": None},
        },
        "key_issues": key_issues,
        "inconsistencies": inconsistencies,
        "missing_documents": missing_documents,
        "review_points": review_points,
        "preliminary_assessment": {
            # P3: this is the judgment-heavy section, so it is hedged and
            # routed for review rather than asserted.
            "feasibility": "medium",
            "difficulty": "medium",
            "priority_review_points": [
                point["point"] for point in review_points
            ],
            "rationale_evidence_references": [],
        },
        # Additive operational sections.
        "required_document_checklist": checklist_section(checklist),
        "existing_disability_assessment": existing_disability_documents(
            checklist, facts),
        "unconfirmed_items": unconfirmed,
    }
    return report


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
