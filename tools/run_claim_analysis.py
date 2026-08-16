"""Claim-analysis full-stage driver (v3: the 2-call spine).

All four checkpoints in TWO provider calls, per the P2-(c) assessment in
`plans/driverization/claim-analysis-v2.md` SS6: M1 = CP1 grouped extraction +
policy page selection in one response (claim bundle + article index in), M2 =
CP2 coverage judgment + CP3 case type + CP4 requirement matching as three
shelled sections of one response (verified fields + fetched policy pages in),
plus the deterministic medical-variable publication attempt after the CP3
section publishes. The four public contracts stay byte-compatible with the
agent path -- the merge changes call structure, never contract shape.

Why two calls, and why the model matters: the 5-call v2 spine measured ~944s
because a cold structured call costs its model's full latency every time
(arm E). The 2026-08-16 four-model bench showed claude-opus-5 doing MORE
extraction than the CLI default in ~62% of its wall (146/149s vs 227/237s,
n=2), an advantage that exists only in the big-single-call regime -- the
opus-5 AGENT run (arm F) was slower than the default's, because an agent
loop's cost is turns, not model speed. Two big calls on the model that wins
big calls is the first (c) configuration whose projection lands under the
agent's 528s; pass `--model claude-opus-5` to run it that way.

P4 grain: each merged call gets exactly one correction; a section failure in
M2 re-generates the whole call. P9 resume is per PUBLISHED CONTRACT (four
receipts), with M1's page selection persisted as its own candidate so a
resumed M2 never re-pays M1.

Governance: this driver never moves a run-state marker (T13 -- the
orchestrator owns `update-run-state`/`finalize-stage`), refuses to run unless
the orchestrator has opened a `claim_analysis` attempt, reads case data only
through DAO commands, and verifies every cited quote against the served page
text before any candidate or contract is persisted. Semantic judgment stays in
the model call; Python transports, verifies, and writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

import _cross_contract
import driver_runtime
import driver_schema
import trace as trace_mod
from llm_providers import ProviderExecutionError, add_provider_args, build_provider, parse_provider_config

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"
STAGE, UNIT = "claim_analysis", "cp1_field_extraction"
UNIT_CP2, UNIT_CP3, UNIT_CP4 = "cp2_coverage", "cp3_case_type", "cp4_requirements"
CONTRACT, SCHEMA = "extracted_claim_fields.json", "extracted_claim_fields.schema.json"
CONTRACT_CP2, SCHEMA_CP2 = "coverage_result.json", "coverage_result.schema.json"
CONTRACT_CP3, SCHEMA_CP3 = "case_type_result.json", "case_type_result.schema.json"
CONTRACT_CP4, SCHEMA_CP4 = "requirement_matching_result.json", "requirement_matching_result.schema.json"
VERSION = "claim_analysis_driver.v0.3.1_2call"
# Span/candidate unit ids for the two merged calls. Receipts stay per public
# contract (UNIT/UNIT_CP2/UNIT_CP3/UNIT_CP4) so resume grain is unchanged.
UNIT_M1, UNIT_M2 = "m1_extract_select", "m2_judge_type_requirements"
# Selection breadth: pages the judge/requirements calls will be served. Wide
# on purpose -- a missed governing clause is a wrong analysis, and pages are
# cheap inside one read (arm B's lesson: cost is calls, not characters).
MAX_SELECTED_PAGES = 60
TEXT_DISPOSITIONS = frozenset({"automated_text_pipeline", "text_only_no_normalization"})
# CP1 reads the claim-side documents whole. Policy bundles are checkpoint 2's
# input and are read there by the page; handing 200k+ characters of 약관 to a
# field-extraction call is the arm A shape this driver exists to remove.
EXCLUDED_TYPES = frozenset({"insurance_policy"})

# The named slots of extracted_claim_fields v0.2, listed in the prompt so the
# model uses typed fields instead of smuggling facts through warnings (the
# CASE_021 failure the v0.2 schema bump exists to prevent).
#
# These are the slots downstream consumes BY NAME, so they are non-substitutable
# -- a 2026-08-16 bench run recorded the coverage item as `policy_type` and lost
# `claim_item` entirely.
NAMED_FIELDS = (
    "diagnosis_name", "kcd_code", "accident_date", "onset_date", "surgery_name",
    "hospital_name", "treatment_period", "admission_period", "imaging_date",
    "diagnosis_date", "claim_received_date", "policy_contract_date",
    "claim_item", "disposition", "insurers",
)

# The exhaustive extraction checklist. Measured 2026-08-16: naming every slot
# the high-recall arms found, and requiring an explicit account of each, moved
# sonnet-5 from 31 to 43 of 46 semantic slots on CASE_034 and 32 to 42 on
# CASE_036's M1 -- the single largest recall lever found, and independent of
# model choice. Grouped by where the facts live so the model walks the case the
# way the documents are organized.
EXTRACTION_CHECKLIST = """\
A. 사고 (사고관련 서류/법률질의회신서/손해사정서)
   accident_date, accident_time, accident_location, accident_cause
   (사고경위: 무엇이 어떻게 일어났는지 서술), victim_position_at_accident

B. 진단·상병 (진단서/후유장해진단서/의무기록)
   diagnosis_name, kcd_code, diagnosis_name_official (KCD 상병명 원문),
   diagnosis_date, onset_date, injured_body_part, clinical_presentation,
   diagnosis_certainty (최종/추정)

C. 치료·수술 (수술기록지/의무기록/진료비내역서)
   surgery_name, surgery_date, anesthesia_method, implant_materials,
   hospital_name, treatment_department, admission_period,
   outpatient_visit_dates, expected_treatment_duration, prior_hospital_treatment

D. 영상·검사 (영상판독지/검사기록)
   imaging_date, imaging_modality, imaging_findings, examination_methods

E. 장해 (후유장해진단서)
   disability_rate (노동능력상실률 %), disability_evaluation_item
   (맥브라이드 항목번호 등), disability_evaluation_method,
   disability_permanence (영구/한시), disability_diagnosis_date,
   residual_disability_content (잔존 증상·ROM 측정치), rom_measurement_date,
   preexisting_condition_causation (기왕증 기여도)

F. 책임·과실 -- 양측 의견을 반드시 각각 별도 필드로
   liability_opinion_claimant_side, liability_opinion_insurer_side,
   victim_fault_opinion_claimant_side, victim_fault_opinion_insurer_side,
   legal_basis (근거 법조문), consolation_money_standard (위자료 산정 기준)

G. 계약·담보 (보험증권/사고접수서류)
   insurers, policy_contract_date, policy_type, policy_number, claim_item,
   coverage_limit (보상한도액), deductible (자기부담금), claim_received_date,
   loss_adjuster_firm

H. 금액 -- 기간별로 각각 분리
   inpatient_expense_total, outpatient_expense_total (진료기간이 다르면
   기간별로 별개 필드로 분리하고 필드명에 기간을 표시),
   offered_treatment_cost (보험자가 안내·제시한 금액), disposition
"""


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _dao_json(args: list[str], *, allow_missing: bool = False) -> dict | None:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        if allow_missing and "NOT_FOUND:" in (proc.stdout or ""):
            return None
        raise RuntimeError((proc.stdout or proc.stderr or "DAO command failed").strip())
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DAO command returned non-JSON output: {args[0]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"DAO command returned a non-object: {args[0]}")
    return data


def _dao_write(args: list[str]) -> None:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        raise RuntimeError((proc.stdout or proc.stderr or "DAO write failed").strip())


def _temp_json(value: Mapping[str, Any]) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False)
    try:
        json.dump(value, handle, ensure_ascii=False)
    finally:
        handle.close()
    return Path(handle.name)


def cp1_documents(manifest: Mapping[str, Any]) -> list[dict]:
    """The claim-side text documents CP1 extracts from, in stable order.

    The disposition filter keeps this on processed, chunk-ready text and --
    critically -- excludes a retained `superseded_bundle`, whose pages are all
    also a child's pages (the 2026-08-06 double-counting finding). An untyped
    entry is a bundle parent or an unprocessed source, never CP1 input.
    """
    selected = [
        doc for doc in manifest.get("documents", [])
        if isinstance(doc, dict)
        and doc.get("downstream_disposition") in TEXT_DISPOSITIONS
        and isinstance(doc.get("document_type"), str)
        and doc.get("document_type") not in EXCLUDED_TYPES
    ]
    return sorted(selected, key=lambda doc: doc["document_id"])


def _body_schema() -> dict:
    """The authoritative local schema for the model's CP1 response body.

    `fields` is taken from the materialized public contract schema, so the
    driver cannot drift from what `write-contract` will enforce; the public
    schema's $defs ride along because the property subtree references them by
    fragment ($ref: #/$defs/value_field), and a fragment resolves against the
    root of whatever schema object the validator is given -- this one.
    """
    public = driver_schema.load_materialized_schema(SCHEMA)
    all_of = public.get("allOf", [])
    props = all_of[1].get("properties", {}) if len(all_of) > 1 else {}
    if not isinstance(props.get("fields"), dict):
        raise RuntimeError("extracted_claim_fields schema has no fields definition")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Claim-analysis CP1 grouped extraction v0.1",
        "type": "object", "additionalProperties": False,
        "properties": {
            "status": {"enum": ["success", "partial"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "review_required": {"type": "boolean"},
            "reviewer_role": {"enum": ["손해사정사", "의사", "법률전문가"]},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "fields": props["fields"],
        },
        "required": ["status", "confidence", "review_required", "warnings", "fields"],
        "allOf": [{
            "if": {"properties": {"review_required": {"const": True}}, "required": ["review_required"]},
            "then": {"required": ["reviewer_role"]},
        }],
        "$defs": public.get("$defs", {}),
    }


def _transport_schema() -> dict:
    """Claude CLI's bounded outer structured-output contract.

    Same split as the denial driver: `--json-schema` is inline argv, so the
    materialized public schema does not fit Windows' command-line limit and
    the nested field shapes stay permissive here. `_body_schema()` remains the
    authoritative local gate before anything is persisted, and no top-level
    allOf/anyOf/oneOf (Anthropic's native tool-input schemas reject them).
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"enum": ["success", "partial"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "review_required": {"type": "boolean"},
            "reviewer_role": {"enum": ["손해사정사", "의사", "법률전문가"]},
            "warnings": {"type": "array", "items": {"type": "string"}},
            # additionalProperties as OBJECT is load-bearing: the arm E run of
            # 2026-08-16 died on CP1 twice because the model put a scalar
            # `confidence: 0.72` inside `fields`, which a bare {"type":
            # "object"} accepts natively and the local gate then refuses --
            # burning the P4 correction on a shape the transport schema could
            # have prevented at generation time.
            # Members are pinned to the three keys `field_common` requires plus
            # the payload keys each shape carries. Measured 2026-08-16: with a
            # bare {"type": "object"} sonnet-5 omitted `review_required` on 38
            # of 57 fields; requiring ONLY the three common keys then cost
            # `value` on 56 of 56, because an enumeration that lists just the
            # metadata reads as the complete member spec. Declaring the payload
            # keys alongside them is what closes both. `value` stays untyped
            # (it legitimately carries strings, numbers and null) while
            # `normalized_value` is string-only, matching the public schema.
            "fields": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "value": {"type": ["string", "number", "boolean", "null"]},
                        "normalized_value": {"type": ["string", "null"]},
                        "start_date": {"type": ["string", "null"]},
                        "end_date": {"type": ["string", "null"]},
                        "days": {"type": ["integer", "null"]},
                        "is_primary": {"type": "boolean"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "review_required": {"type": "boolean"},
                        "reviewer_role": {"enum": ["손해사정사", "의사", "법률전문가"]},
                        "evidence_references": {
                            "type": "array", "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "document_id": {"type": "string"},
                                    "page": {"type": "integer", "minimum": 1},
                                    "quote": {"type": "string", "minLength": 1},
                                },
                                "required": ["document_id", "page", "quote"],
                            },
                        },
                    },
                    "required": ["confidence", "evidence_references", "review_required"],
                },
            },
        },
        "required": ["status", "confidence", "review_required", "warnings", "fields"],
    }


def _refs(value: Any) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_references":
                if not isinstance(child, list) or any(not isinstance(item, dict) for item in child):
                    raise RuntimeError("evidence_references must be an array of objects")
                found.extend(child)
            else:
                found.extend(_refs(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_refs(child))
    return found


def _ref_key(ref: Mapping[str, Any]) -> tuple:
    return tuple(ref.get(name) for name in ("document_id", "page", "quote", "start_char", "end_char"))


def _bind(value: Any, verified: Mapping[tuple, Mapping[str, Any]]) -> Any:
    if isinstance(value, list):
        return [_bind(item, verified) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: ([dict(verified[_ref_key(ref)]) for ref in child] if key == "evidence_references"
                  else _bind(child, verified)) for key, child in value.items()}


def _prompt(bundle: list[dict]) -> str:
    rendered = "\n\n".join(
        f"## {doc['document_id']} ({doc.get('document_type')})\n" + "\n".join(
            f"[page {page['page']}]\n{page['text']}" for page in doc["pages"])
        for doc in bundle)
    named = ", ".join(NAMED_FIELDS)
    return f"""Extract the structured core claim facts from these validated, redacted claim documents.
이 작업은 한 번의 호출로 완결되어야 합니다. 아래 체크리스트의 모든 슬롯을 빠짐없이 훑고, 각
슬롯마다 둘 중 하나를 반드시 수행하십시오: (1) 값을 추출해 해당 필드로 기록한다, 또는 (2) 문서에
없거나 마스킹되어 추출할 수 없다면 그 사유를 `warnings` 에 슬롯 이름과 함께 한 줄로 적는다
("accident_date: 마스킹됨" 형식). 슬롯을 조용히 건너뛰는 것은 실패입니다.

{EXTRACTION_CHECKLIST}
체크리스트에 없는 사실도 발견하면 같은 형태로 서술적 snake_case 이름을 붙여 추가하십시오.

Return only the supplied JSON Schema. Every entry under `fields` uses exactly one of three shapes:
single value {{value, normalized_value?}}, date {{value: YYYY-MM-DD|null}}, or period
{{start_date, end_date|null, days|null}} -- each with confidence, evidence_references, and
review_required (reviewer_role when true; 의사 for medical-judgment fields). Use these named slots
when the fact exists: {named}. Additional facts get descriptive snake_case field names in the same
three shapes. EVERY member of `fields` must be an OBJECT in one of those three shapes -- never a
bare string or number, and never top-level keys like status/confidence/review_required repeated
inside `fields` (those belong at the top level only). `warnings` is for actual warnings only,
never facts that lack a slot.

필수 키 규칙: `fields` 의 모든 항목은 confidence, evidence_references, review_required 세 키를
빠짐없이 포함합니다 (확실한 값도 review_required 를 false 로 명시). normalized_value 는 반드시
문자열입니다 -- 금액 500,000,000 원은 "500000000" 으로 적고 숫자를 그대로 넣지 마십시오.

표 인용 규칙: 본문에 ┌─┬┐│└┘ 괘선으로 그려진 표(진료비 세부산정내역 등)가 있으면, 여러 칸의
값을 가로로 이어붙여 "2023-12-05 | G6404 | 수관절 4매 | 10,675" 같은 문자열을 만들지 마십시오 --
그런 문자열은 본문에 연속해서 존재하지 않아 인용 검증에서 거부됩니다. 표에서 인용할 때는 한 줄
안에 실제로 연속해 나타나는 짧은 구간(예: 항목명 하나, 금액 하나)만 인용하고 나머지 정보는
value 에 담으십시오. 짧고 정확한 인용이 길고 부정확한 인용보다 낫습니다.

Evidence discipline: every field cites at least one evidence reference with document_id, page, and
an EXACT quote from the supplied text. A fact that is redacted or absent records value null with
review_required true, citing the page where it would appear. For a PERIOD slot
(treatment_period, admission_period, or any *_period field) whose start date is redacted or
unknown: OMIT the field entirely and state the known partial information (e.g. a stated duration
like "약 8주") in `warnings` -- the period shape requires a real YYYY-MM-DD start_date, and the
single-value shape is not valid for named period slots. Never substitute a value and never
derive one by combining documents; note such a possible derivation in `warnings` instead. When two
documents disagree on the same field, record BOTH values (the extra one under a descriptive field
name), set is_primary only where a document-character basis exists, and mark review_required --
never drop either value. If any field is medical-judgment-bearing, top-level review_required is
true with reviewer_role 의사 unless a non-medical role is clearly more specific.

DOCUMENT TEXT:
{rendered}
"""


def grouped_candidate_id(docs: list[dict]) -> str:
    """Stable unit id from the document set, not its position -- adding an
    unrelated document to the case must not silently re-point the stored
    result at different source text (it changes the set, hence the id)."""
    joined = "|".join(sorted(doc["document_id"] for doc in docs))
    return "grouped_" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


_REQUIRED_MEMBER_KEYS = ("confidence", "evidence_references", "review_required")
_MEMBER_STRING_KEYS = ("value", "normalized_value")
_MEMBER_DATE_KEYS = ("start_date", "end_date")


def normalize_cp1_shape(value: Mapping[str, Any]) -> tuple[dict, list[str]]:
    """Repair the SHAPE defects a model reliably makes, before the gate runs.

    Every repair here is deterministic and content-preserving: inject the
    default for an omitted required flag, coerce a scalar to the type the
    public schema declares, unwrap a member the model double-wrapped inside
    `value`, and drop a member that carries no payload or no evidence at all.
    Measured 2026-08-16: this turns four separate schema-failing sonnet-5
    outputs into schema-PASS with zero slot-recall loss, no extra model call.

    What it deliberately does NOT touch is `quote`. A wrong citation is not a
    format defect, and "repairing" one would automate fabrication -- the thing
    P1 exists to catch. Quotes pass through unchanged and still fail
    verification when they are wrong.

    Returns the repaired copy and a log, so a run can report what it changed
    rather than silently laundering the model's output.
    """
    log: list[str] = []
    out = dict(value)
    fields = out.get("fields")
    if not isinstance(fields, dict):
        return out, log

    repaired: dict[str, Any] = {}
    for name, member in fields.items():
        if not isinstance(member, dict):
            # A bare scalar carries no shape to repair; kept so the schema
            # names it, for the same reason as the two cases below.
            log.append(f"keep {name} (non-object): the gate must refuse it")
            repaired[name] = member
            continue
        member = dict(member)

        inner = member.get("value")
        if isinstance(inner, dict) and ("value" in inner or "start_date" in inner):
            for key, sub in inner.items():
                if key not in member or key == "value":
                    member[key] = sub
            log.append(f"unwrap {name}: value contained a nested member object")

        # Same rule as the uncited case below: a member with no payload key
        # carries no fact this layer can restore, but discarding it hides the
        # failure. It stays, and the schema refuses it.
        is_period = any(key in member for key in _MEMBER_DATE_KEYS)
        if not is_period and "value" not in member:
            log.append(f"keep {name} without payload: the gate must refuse it")
            repaired[name] = member
            continue

        for key in (_MEMBER_DATE_KEYS if is_period else _MEMBER_STRING_KEYS):
            if key in member and isinstance(member[key], (int, float, bool)):
                member[key] = str(member[key])
                log.append(f"coerce {name}.{key} to string")
        if is_period and isinstance(member.get("days"), str):
            try:
                member["days"] = int(member["days"])
                log.append(f"coerce {name}.days to int")
            except ValueError:
                pass

        if "review_required" not in member:
            member["review_required"] = False
            log.append(f"default {name}.review_required=false")
        if "confidence" not in member:
            member["confidence"] = 0.5
            log.append(f"default {name}.confidence=0.5")

        # An uncited fact is NOT repaired and NOT dropped. Dropping it would
        # convert a loud refusal into silent data loss -- the model extracted
        # something and the driver would discard it with no record -- while
        # synthesizing a citation would be fabrication. Leaving it in place
        # lets the schema refuse it, which is what earns the P4 correction and
        # a chance to cite the fact properly.
        refs = member.get("evidence_references")
        if not isinstance(refs, list) or not refs:
            log.append(f"keep {name} uncited: the gate must refuse it, not this layer")
            repaired[name] = member
            continue
        # Per-member `reviewer_role` is left alone for the same reason as the
        # top-level one: 의사 vs 손해사정사 is the substantive half of the
        # review flag, and a default would silently mis-route a medical field.
        repaired[name] = member

    out["fields"] = repaired
    # Top-level `reviewer_role` is deliberately NOT defaulted. WHICH expert a
    # case needs is a judgment -- 의사 for a medical-judgment field, not the
    # generic 손해사정사 -- so supplying one would answer a substantive
    # question with a placeholder and route a medical review to the wrong
    # role. The gate refuses it and the correction round asks the model, which
    # is the only party that read the material.
    return out, log


def _validate_cp1_output(value: Mapping[str, Any], bundle: list[dict],
                         schema: Mapping[str, Any]) -> dict:
    allowed = {doc["document_id"] for doc in bundle}
    # Shape repair first, so the one P4 correction is spent on a real defect
    # (a wrong citation) instead of on a missing boolean the driver can supply
    # itself. The repaired body is what the rest of this function validates and
    # what the caller persists -- see `normalize_cp1_shape` for the strict
    # limits on what it will touch.
    value, repairs = normalize_cp1_shape(value)
    if repairs:
        print(f"cp1 shape repairs ({len(repairs)}): {'; '.join(repairs[:8])}"
              + (" ..." if len(repairs) > 8 else ""), file=sys.stderr)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    page_text_by_key = {
        (doc["document_id"], page["page"]): page["text"]
        for doc in bundle
        for page in doc.get("pages", [])
    }
    for ref in _refs(value):
        doc_id = ref["document_id"]
        page = ref["page"]
        if doc_id not in allowed:
            raise ValueError("CP1 output cited a document outside its input bundle")
        page_text = page_text_by_key.get((doc_id, page))
        if page_text is None:
            raise ValueError(f"{doc_id}: page {page} is not present in the CP1 input text")
        quote = ref["quote"]
        start, end = ref.get("start_char"), ref.get("end_char")
        if (start is None) != (end is None):
            raise ValueError(f"{doc_id}: start_char and end_char must be supplied together")
        if start is not None:
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or start >= end:
                raise ValueError(f"{doc_id}: invalid start_char/end_char range")
            if end > len(page_text) or page_text[start:end] != quote:
                raise ValueError(f"{doc_id}: quote does not exactly match the claimed page range")
        elif not _cross_contract.quote_matches_page(quote, page_text):
            # The DAO's exact gate, applied before candidate persistence so a
            # bad citation gets P4's one model correction instead of failing
            # only at final publication -- including the page-pair fallback,
            # or this would refuse citations the DAO would then accept.
            if not _cross_contract.quote_spans_page_pair(
                    quote, page_text, page_text_by_key.get((doc_id, page + 1))):
                raise ValueError(f"{doc_id}: quote is not present on page {page}")
    return dict(value)


def _extract(provider, bundle: list[dict], schema: Mapping[str, Any],
             transport_schema: Mapping[str, Any], version: str,
             case_id: str, run_id: str) -> dict:
    driver_runtime.maybe_interrupt(STAGE, grouped_candidate_id(bundle))
    with driver_runtime.driver_span(case_id, run_id, "provider_wait", unit_id=UNIT, items=1):
        return driver_runtime.structured_with_one_correction(
            provider=provider, prompt=_prompt(bundle), prompt_version=version,
            output_schema=transport_schema,
            validate=lambda value: _validate_cp1_output(value, bundle, schema))


# ---------------------------------------------------------------- CP2-4 --

_SCALAR_SHELL_PROPS = {
    "status": {"enum": ["success", "partial"]},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "review_required": {"type": "boolean"},
    "reviewer_role": {"enum": ["손해사정사", "의사", "법률전문가"]},
    "warnings": {"type": "array", "items": {"type": "string"}},
}
_REVIEWER_CONDITIONAL = {
    "if": {"properties": {"review_required": {"const": True}}, "required": ["review_required"]},
    "then": {"required": ["reviewer_role"]},
}


def _local_schema(schema_name: str, body_props: dict, body_required: list[str]) -> dict:
    """Authoritative local schema for one checkpoint's response body.

    The unit's substantive shapes come from the materialized public contract
    schema, so the driver cannot drift from what `write-contract` enforces;
    the public $defs ride along for the same fragment-resolution reason as
    CP1's `_body_schema`.
    """
    public = driver_schema.load_materialized_schema(schema_name)
    all_of = public.get("allOf", [])
    props = all_of[1].get("properties", {}) if len(all_of) > 1 else {}
    resolved = {}
    for name in body_props:
        if name not in props:
            raise RuntimeError(f"{schema_name} has no {name} definition")
        resolved[name] = props[name]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "properties": {**_SCALAR_SHELL_PROPS, **resolved},
        "required": ["status", "confidence", "review_required", "warnings", *body_required],
        "allOf": [_REVIEWER_CONDITIONAL],
        "$defs": public.get("$defs", {}),
    }


def _body_schema_cp2() -> dict:
    return _local_schema(SCHEMA_CP2, {"coverages": True}, ["coverages"])


def _body_schema_cp3() -> dict:
    names = ["case_type", "coverage_basis", "loss_type", "is_claim_case",
             "case_type_source", "secondary_case_types", "candidate_types",
             "template_id", "report_profile", "evidence_references"]
    return _local_schema(SCHEMA_CP3, {name: True for name in names},
                         ["case_type", "coverage_basis", "loss_type", "case_type_source",
                          "template_id", "report_profile", "evidence_references"])


def _body_schema_cp4() -> dict:
    return _local_schema(SCHEMA_CP4, {"coverage_requirements": True}, ["coverage_requirements"])


def _transport_shell(extra: dict, required: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {**_SCALAR_SHELL_PROPS, **extra},
        "required": ["status", "confidence", "review_required", "warnings", *required],
    }


def _transport_schema_select() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "pages": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"document_id": {"type": "string"},
                               "page": {"type": "integer", "minimum": 1}},
                "required": ["document_id", "page"]}},
            "rationale": {"type": "string"},
        },
        "required": ["pages"],
    }


def _transport_schema_cp2() -> dict:
    return _transport_shell({"coverages": {"type": "array", "items": {"type": "object"}}},
                            ["coverages"])


def _transport_schema_cp3() -> dict:
    # The classification enums are pinned NATIVELY, not left as bare strings:
    # arm G's M2 burned its P4 correction (an entire ~400s re-call) because the
    # model put a descriptive free-string into secondary_case_types, which the
    # loose transport accepted and the local enum gate then refused -- the
    # fourth instance of the same generation-time-shape failure (CP1's
    # scalar-in-fields, report_profile's wrong keys, the selection ceiling).
    # Values mirror case_type_result.schema.json's closed enums; the local
    # body-schema gate remains authoritative if they ever drift.
    _CASE_TYPES = ["후유장해", "진단·수술비", "실손", "배상책임", "기타"]
    return _transport_shell({
        "case_type": {"enum": _CASE_TYPES},
        "coverage_basis": {"enum": ["배상책임", "개인보험", "자동차보험", None]},
        "loss_type": {"enum": ["후유장해", "진단·수술비", "실손", None]},
        "is_claim_case": {"type": "boolean"},
        "case_type_source": {"enum": ["adjuster_input", "inferred"]},
        "secondary_case_types": {"type": "array", "items": {"enum": _CASE_TYPES}},
        "candidate_types": {"type": "array", "items": {"type": "object"}},
        "template_id": {"type": ["string", "null"]},
        # Fully keyed, additionalProperties false: the second arm E run died
        # here twice with the RIGHT semantics under the WRONG key names
        # (`mechanism` for claim_mechanism, `depth` for mode, plus extra
        # rationale/template_id members) -- the same generation-time shape
        # failure as CP1's scalar-in-fields, fixed the same way: the native
        # transport schema carries the exact keys so the model cannot spend
        # the P4 correction on spelling.
        "report_profile": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "format_contract_version": {"type": "string"},
                "family": {"type": "string"},
                "claim_mechanism": {"type": "string"},
                "mode": {"type": "string"},
                "support_status": {"type": "string"},
            },
            "required": ["format_contract_version", "family", "claim_mechanism",
                         "mode", "support_status"],
        },
        "evidence_references": {"type": "array", "items": {"type": "object"}},
    }, ["case_type", "case_type_source", "template_id", "report_profile",
        "evidence_references"])


def _transport_schema_cp4() -> dict:
    """CP4's item shape is pinned, unlike cp2/cp3 whose keys were already keyed.

    Measured 2026-08-16: with `items: {"type": "object"}` the model emitted six
    FLAT requirement items with no coverage key at all, and the failure was
    read (wrongly) as a semantic limit -- attributing a requirement to a
    coverage looked like something only a human could settle. Pinning the group
    shape, everything else held constant, produced correct per-coverage
    grouping twice with the `standardized_coverage_name` join onto
    coverage_section valid both times. The model could always do it; the schema
    never asked.
    """
    group = {
        "type": "object",
        "properties": {
            "coverage_name": {"type": "string"},
            "standardized_coverage_name": {"type": "string"},
            "requirements": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement_id": {"type": "string"},
                        "requirement_text": {"type": "string"},
                        "status": {"enum": ["met", "not_met", "uncertain"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "review_required": {"type": "boolean"},
                    },
                    "required": ["requirement_id", "requirement_text", "status"],
                },
            },
        },
        "required": ["standardized_coverage_name", "requirements"],
    }
    return _transport_shell({"coverage_requirements": {"type": "array", "items": group}},
                            ["coverage_requirements"])


def _norm(text: str) -> str:
    """The citation-comparison normalizer, shared by M2's grounding fast path
    and the `known` quote set it checks against.

    Uses the healing form so CP2-4 accept the same citations CP1 does: the
    Korean mid-word line wraps that blocked the 2026-08-16 CASE_038 runs are a
    rendering artifact, and a policy clause quoted across one is as legitimate
    as a claim fact quoted across one. Both sides of every comparison go
    through this single function, so the fast path and the stored set cannot
    disagree about what a quote is.
    """
    return _cross_contract.normalize_quote_for_pages(text)


def _known_quote_set(*contracts: Mapping[str, Any]) -> set[tuple]:
    """(document_id, page, normalized quote) for every reference already
    verified in an earlier checkpoint's contract. A later call that sees no
    new source text may only cite these."""
    known: set[tuple] = set()
    for contract in contracts:
        for ref in _refs(contract):
            known.add((ref.get("document_id"), ref.get("page"), _norm(ref.get("quote", ""))))
    return known


def _check_ref_grounding(value: Any, served: Mapping[tuple, str],
                         known: set[tuple], *, clause_keys: tuple = (),
                         dao_verify=None) -> None:
    """Every reference must quote a SERVED page, reuse a verified quote, or --
    when `dao_verify` is supplied -- survive the DAO's own verification.

    The local rules are the fast path; the DAO is the authority. The third arm
    E run showed why the fallback matters: CP4 cited a real, verbatim
    condition sentence on a page outside the local fast path and the strict
    local rule burned the P4 correction refusing a citation the DAO's write
    gate would have accepted. Clause refs never fall back to `known` -- a
    claim-document quote is not a clause address -- but they may verify
    through the DAO like any real citation.
    """
    unresolved: list[tuple[dict, str]] = []

    def check_ref(ref: Mapping[str, Any], *, clause: bool) -> None:
        doc_id, page, quote = ref.get("document_id"), ref.get("page"), ref.get("quote", "")
        page_text = served.get((doc_id, page))
        if page_text is not None and (
                _norm(quote) in _norm(page_text)
                or _cross_contract.quote_spans_page_pair(
                    quote, page_text, served.get((doc_id, page + 1)))):
            return
        if not clause and (doc_id, page, _norm(quote)) in known:
            return
        kind = "clause reference" if clause else "evidence reference"
        unresolved.append((dict(ref), f"{doc_id} p{page}: {kind} quote "
                                      f"{quote[:60]!r} is neither on a served "
                                      "page nor a previously verified quote"))

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        for key, child in node.items():
            if key == "evidence_references" and isinstance(child, list):
                for ref in child:
                    check_ref(ref, clause=False)
            elif key in clause_keys and isinstance(child, dict):
                check_ref(child, clause=True)
            else:
                walk(child)

    walk(value)
    if not unresolved:
        return
    if dao_verify is None:
        raise ValueError("; ".join(message for _, message in unresolved))
    try:
        dao_verify([ref for ref, _ in unresolved])
    except RuntimeError as exc:
        raise ValueError(
            f"{'; '.join(message for _, message in unresolved)} -- and the DAO "
            f"could not verify them either: {str(exc)[:300]}") from exc


def _make_dao_verifier(case_id: str, run_id: str):
    """Batch-verify refs against the processed text through the DAO. Raises
    RuntimeError when any reference fails, mirroring the write gate."""
    def verify(refs: list[dict]) -> None:
        ref_file = _temp_json({"references": refs})
        try:
            checked = _dao_json(["verify-evidence-references", case_id,
                                 "--references-file", str(ref_file),
                                 "--run-id", run_id])
        finally:
            ref_file.unlink(missing_ok=True)
        verified = checked.get("verified_references", [])
        if len(verified) != len(refs):
            raise RuntimeError("DAO verified fewer references than submitted")
    return verify


def _structured_call(provider, prompt: str, transport: Mapping[str, Any],
                     validate, version: str, case_id: str, run_id: str,
                     unit_id: str) -> dict:
    driver_runtime.maybe_interrupt(STAGE, unit_id)
    with driver_runtime.driver_span(case_id, run_id, "provider_wait", unit_id=unit_id, items=1):
        return driver_runtime.structured_with_one_correction(
            provider=provider, prompt=prompt, prompt_version=version,
            output_schema=transport, validate=validate)


def _dao_json_loose(args: list[str]) -> dict:
    """A DAO command whose output carries a leading human NOTE before the JSON
    object (policy-snapshot does). Parse from the first brace."""
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        raise RuntimeError((proc.stdout or proc.stderr or "DAO command failed").strip())
    out = proc.stdout
    start = out.find("{")
    if start < 0:
        raise RuntimeError(f"DAO command returned no JSON object: {args[0]}")
    return json.loads(out[start:])


def _fetch_snapshot(case_id: str, run_id: str, cited_doc_ids: list[str]) -> dict:
    args = ["policy-snapshot", case_id]
    for doc_id in sorted(set(cited_doc_ids)):
        args += ["--document-id", doc_id]
    args += ["--run-id", run_id]
    return _dao_json_loose(args)


def _compact_fields(cp1: Mapping[str, Any]) -> str:
    """CP1's fields with their evidence quotes, compact, for later prompts."""
    return json.dumps(cp1.get("fields", {}), ensure_ascii=False)


def _index_listing(index: Mapping[str, Any]) -> tuple[str, dict[str, set[int]]]:
    lines: list[str] = []
    pages_by_doc: dict[str, set[int]] = {}
    for doc in index.get("documents", []):
        doc_id = doc.get("document_id")
        pages = pages_by_doc.setdefault(doc_id, set())
        for clause in doc.get("clauses", []):
            pages.add(clause["page"])
            lines.append(f"{doc_id} p{clause['page']} [{clause.get('policy_name')}] "
                         f"{clause.get('article')}({clause.get('heading')})")
        for table in doc.get("tables", []):
            pages.add(table["page"])
            lines.append(f"{doc_id} p{table['page']} [table] {table.get('header')}")
    return "\n".join(lines), pages_by_doc


def _m1_prompt(bundle: list[dict], listing: str) -> str:
    """CP1 extraction plus policy page selection, one response (call M1)."""
    return _prompt(bundle) + f"""
ADDITIONALLY, in the SAME response, fill the `selected_pages` field: select which policy pages
should be read for this claim's coverage analysis, based on the facts you extracted above. Below
is the complete article index of the case's policy documents (document, page, owning 약관, article
number and heading). Return the pages likely to contain: the coverages this claim could trigger,
their 보상하는 손해 definitions, their 면책/exclusion articles, payout conditions and limits, and
any 특별약관 relevant to the claim's facts. ALWAYS include the 보통약관's 보상하는 손해 and 면책
articles. Be inclusive -- a missed governing clause is a wrong analysis; when unsure, include the
page, and include adjacent listed pages when an article likely continues. Select only pages that
appear in the index listing. At most {MAX_SELECTED_PAGES} pages.

POLICY ARTICLE INDEX:
{listing}
"""


def _transport_schema_m1() -> dict:
    schema = _transport_schema()
    schema["properties"]["selected_pages"] = {
        "type": "array",
        "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"document_id": {"type": "string"},
                           "page": {"type": "integer", "minimum": 1}},
            "required": ["document_id", "page"],
        },
    }
    schema["required"] = [*schema["required"], "selected_pages"]
    return schema


def _clamp_selection(items: list, index_pages: Mapping[str, set]) -> dict:
    """The selection is a READ PLAN, not evidence, so out-of-range pages are
    dropped rather than refused: the first arm E run burned its P4 correction
    on `DOC_010 p36` -- a legitimate CONTINUATION page of the p35 article that
    the index (which lists article START pages) does not name. Any page up to
    a document's highest indexed page is guaranteed readable and admits
    continuations; only an empty result is an error."""
    ceilings = {doc_id: max(pages) for doc_id, pages in index_pages.items() if pages}
    pages, seen, dropped = [], set(), []
    for item in items:
        key = (item["document_id"], item["page"])
        if key in seen:
            continue
        seen.add(key)
        if not 1 <= item["page"] <= ceilings.get(item["document_id"], 0):
            dropped.append(f"{item['document_id']}p{item['page']}")
            continue
        pages.append({"document_id": item["document_id"], "page": item["page"]})
    if not pages:
        raise ValueError("selection returned no readable pages")
    return {"pages": pages[:MAX_SELECTED_PAGES],
            **({"dropped": dropped} if dropped else {})}


def _validate_m1(value: Mapping[str, Any], bundle: list[dict],
                 schema: Mapping[str, Any], index_pages: Mapping[str, set]) -> dict:
    """Split-and-validate M1: the CP1 portion through the authoritative CP1
    gate (schema + verbatim quote verification), the selection clamped."""
    remainder = dict(value)
    selection = remainder.pop("selected_pages", None)
    if not isinstance(selection, list):
        raise ValueError("selected_pages must be an array of {document_id, page}")
    return {"cp1": _validate_cp1_output(remainder, bundle, schema),
            "selection": _clamp_selection(selection, index_pages)}


def _render_pages(bundle: list[dict]) -> str:
    return "\n\n".join(
        f"## {doc['document_id']} pages\n" + "\n".join(
            f"[page {page['page']}]\n{page['text']}" for page in doc["pages"])
        for doc in bundle)


def _m2_prompt(fields_json: str, pages_text: str, adjuster_case_type) -> str:
    """CP2 judgment + CP3 case type + CP4 requirement matching, one response
    (call M2). The three checkpoint instructions are carried verbatim from the
    v2 per-call prompts; only the data blocks are shared instead of repeated."""
    adjuster = json.dumps(adjuster_case_type, ensure_ascii=False)
    return f"""Perform the three dependent analysis checkpoints below in ONE response, over the claim facts and
policy pages supplied at the end. Return only the supplied JSON Schema: an object with exactly
`coverage_section`, `case_type_section`, and `requirements_section`. Each section carries its OWN
status, confidence, review_required (reviewer_role when true), warnings, and its checkpoint's
fields -- judge each section's review need on its own material.

CHECKPOINT A (`coverage_section`): identify the coverages this claim could trigger. `coverages`
is an array; a claim can trigger more than one. Per coverage: `coverage_name` (the policy's own
name), `standardized_coverage_name` (snake_case English), `applicable` (whether its conditions
are actually met by the claim's facts, not merely mentioned -- if a condition is genuinely
unresolved on this record, record your best-supported reading, state the unresolved condition in
`warnings`, and set review_required with the right role), `matched_clause_ref` ({{document_id,
page, quote}} with an EXACT quote from a policy page shown below -- null ONLY if no clause was
found at all), `confidence`, `evidence_references`, and `review_required`.

CHECKPOINT B (`case_type_section`): classify this claim's case type and report profile from the
claim facts and the coverages you identified in coverage_section. `adjuster_case_type` from
intake is: {adjuster}. If it is non-null, copy its axes verbatim, set case_type_source
"adjuster_input", and record axis_cross_check per your independent view. If null, infer:
`coverage_basis` one of 배상책임/개인보험/자동차보험, `loss_type` one of 후유장해/진단·수술비/실손,
legacy `case_type` the closest single value, and case_type_source "inferred". Set
`report_profile` from the actual contractual mechanism:
third-party automobile bodily injury -> automobile_compensation/statutory_or_policy_auto_compensation/compact/supported, template 자동차보험_대인배상_간이형;
first-party automobile self-injury -> automobile_self_injury/automobile_policy_benefit/full/provisional, template 자동차보험_자기신체사고형 (review_required true, 손해사정사);
first-party personal-accident disability -> personal_accident_benefit/personal_accident_policy_benefit/full/supported, template 개인보험_후유장해형;
third-party liability damages -> liability_damages/insured_liability (or mutual_or_cooperative_liability as evidenced)/full/supported, template 배상책임_후유장해형;
disease/diagnosis-triggered benefit -> disease_benefit/disease_policy_benefit/full/supported, template 진단수술비형.
report_profile also carries format_contract_version "loss_adjustment_report.v1". For 실손, an
unconfirmed primary type, or no supported registry contract: family/mechanism other_review_required,
support_status unsupported, template_id null, review_required true. The profile records the
mechanism under which the claim was brought, never a verdict on whether it succeeds. Include
`is_claim_case`, `candidate_types` (ranked, with confidence), `secondary_case_types` (only
genuine ones), and `evidence_references`.

CHECKPOINT C (`requirements_section`): match each identified coverage's payout requirements
against the claim's facts. `coverage_requirements` groups requirements per coverage, joining
EXACTLY on the `standardized_coverage_name` values from coverage_section -- never invent a new
name. Per requirement: `requirement_id` (provisional REQ-N; the driver renumbers),
`requirement_text`, `clause_ref` ({{document_id, page, quote}} quoting the SPECIFIC condition
sentence from a policy page shown below), `status` met/not_met/uncertain, `confidence`,
`evidence_references`, `review_required`. met/not_met require at least one evidence reference;
uncertain may have none ONLY when evidence is genuinely absent -- a redaction-blanked fact that
blocks a check is uncertain with the blockage stated in the requirement_text or warnings. Include
exclusion/면책 requirements, time-limit conditions, and any interaction between coverages you can
ground in the shown pages.

Evidence discipline for ALL sections: every evidence reference must quote a policy page shown
below or reuse an exact quote from the claim facts (same document, page, and quote);
case_type_section may additionally reuse quotes from coverage_section's own evidence. Do not cite
anything else.

CLAIM FACTS (with their verified evidence quotes):
{fields_json}

POLICY PAGES:
{pages_text}
"""


def _transport_schema_m2() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "coverage_section": _transport_schema_cp2(),
            "case_type_section": _transport_schema_cp3(),
            "requirements_section": _transport_schema_cp4(),
        },
        "required": ["coverage_section", "case_type_section", "requirements_section"],
    }


def _validate_m2(value: Mapping[str, Any], served: Mapping[tuple, str],
                 known_cp1: set, schemas: tuple, dao_verify) -> dict:
    """Each section through its own authoritative body schema and grounding
    gate, in dependency order, inside M2's single P4 correction budget."""
    schema_cp2, schema_cp3, schema_cp4 = schemas
    cov = value["coverage_section"]
    ct = value["case_type_section"]
    req = value["requirements_section"]
    Draft202012Validator(schema_cp2, format_checker=FormatChecker()).validate(cov)
    _check_ref_grounding(cov["coverages"], served, known_cp1,
                         clause_keys=("matched_clause_ref",), dao_verify=dao_verify)
    known3 = known_cp1 | {(r.get("document_id"), r.get("page"), _norm(r.get("quote", "")))
                          for r in _refs(cov)}
    Draft202012Validator(schema_cp3, format_checker=FormatChecker()).validate(ct)
    _check_ref_grounding(ct, served, known3, dao_verify=dao_verify)
    names = {c.get("standardized_coverage_name") for c in cov["coverages"]}
    Draft202012Validator(schema_cp4, format_checker=FormatChecker()).validate(req)
    for group in req["coverage_requirements"]:
        if group.get("standardized_coverage_name") not in names:
            raise ValueError(
                f"coverage_requirements names unknown coverage "
                f"{group.get('standardized_coverage_name')!r}; join exactly on "
                f"coverage_section's standardized_coverage_name values")
    _check_ref_grounding(req["coverage_requirements"], served, known3,
                         clause_keys=("clause_ref",), dao_verify=dao_verify)
    return {"coverage_section": dict(cov), "case_type_section": dict(ct),
            "requirements_section": dict(req)}


def _renumber_requirements(body: dict) -> dict:
    copied = json.loads(json.dumps(body, ensure_ascii=False))
    number = 1
    for group in copied["coverage_requirements"]:
        for requirement in group.get("requirements", []):
            requirement["requirement_id"] = f"REQ-{number}"
            number += 1
    return copied


def _medical_candidate(case_id: str, run_id: str, case_type: str) -> dict:
    """The deterministic publication candidate. Structures evidence only; the
    empty collections are honest -- no clinical inference is made here, and
    under the deferred configuration (`variable_kinds: []`) no variable could
    be enabled anyway. Schema-validity is pinned by test so a refusal is
    always the CONFIG refusal, never a malformed candidate."""
    return {
        "case_id": case_id, "run_id": run_id, "component": "claim-analysis",
        "status": "success", "case_type": case_type,
        "schema_version": "medical_variables.v0.1",
        "config_version": "medical_structuring.v0.1",
        "source_coverage": [], "domains": [], "medical_issues": [],
        "variables": [], "contradiction_groups": [],
        "importance_assignments": [], "quantity_summaries": [],
        "timeline_observation_ids": [],
    }


_DEFERRED_REFUSALS = ("disabled or lacks approval", "is not enabled by medical configuration")


def _attempt_medical_publication(case_id: str, run_id: str, held_by: str,
                                 case_type: str) -> dict:
    candidate_file = _temp_json(_medical_candidate(case_id, run_id, case_type))
    try:
        proc = subprocess.run(
            [sys.executable, str(DAO), "write-medical-variables", case_id,
             str(candidate_file), "--held-by", held_by, "--run-id", run_id],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    finally:
        candidate_file.unlink(missing_ok=True)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return {"published": True}
    if any(marker in output for marker in _DEFERRED_REFUSALS):
        # The approved deferred state (P11): report, do not fabricate a way
        # through. The stage gate is `check-medical-reviews-clear`, which the
        # orchestrator and the DAO's finalize both consult.
        return {"published": False, "deferred_config_refusal": True,
                "detail": output.strip()[:500]}
    raise RuntimeError(f"medical-variable publication failed: {output.strip()[:500]}")


def _unit_receipt_reuse(case_id: str, run_id: str, unit_id: str, contract: str,
                        digests: Mapping[str, str], prompt_version: str,
                        provider) -> dict | None:
    receipt = _dao_json(["read-driver-receipt", case_id, "--stage", STAGE,
                         "--unit-id", unit_id, "--run-id", run_id], allow_missing=True)
    if receipt and driver_runtime.receipt_matches(
            receipt, input_digests=digests, prompt_version=prompt_version,
            response_schema_version=VERSION, provider_name=provider.provider_name,
            model_name=provider.model_name):
        return _dao_json(["read-contract", case_id, contract, "--run-id", run_id],
                         allow_missing=True)
    return None


def _write_unit(case_id: str, run_id: str, held_by: str, unit_id: str,
                contract_name: str, schema_name: str, contract: Mapping[str, Any],
                digests: Mapping[str, str], prompt_version: str, provider) -> None:
    data_file = _temp_json(contract)
    try:
        with driver_runtime.driver_span(case_id, run_id, "dao_publish", unit_id=unit_id):
            _dao_write(["write-contract", case_id, contract_name,
                        "--data-file", str(data_file), "--schema-name", schema_name,
                        "--held-by", held_by, "--run-id", run_id, "--stage", STAGE])
    finally:
        data_file.unlink(missing_ok=True)
    receipt_file = _temp_json(driver_runtime.make_receipt(
        case_id=case_id, run_id=run_id, stage=STAGE, unit_id=unit_id,
        input_digests=digests, prompt_version=prompt_version,
        response_schema_version=VERSION, provider_name=provider.provider_name,
        model_name=provider.model_name, completed_contracts=[contract_name]))
    try:
        _dao_write(["write-driver-receipt", case_id, "--stage", STAGE,
                    "--unit-id", unit_id, "--data-file", str(receipt_file),
                    "--held-by", held_by, "--run-id", run_id])
    finally:
        receipt_file.unlink(missing_ok=True)


def _envelope(case_id: str, run_id: str, body: Mapping[str, Any], provider,
              prompt_version: str, extra: Mapping[str, Any]) -> dict:
    contract = {"case_id": case_id, "run_id": run_id, "component": "claim-analysis",
                "status": body["status"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model_info": {"model_name": provider.model_name,
                               "prompt_version": prompt_version},
                "confidence": body["confidence"],
                "review_required": body["review_required"],
                "warnings": body["warnings"], "source_grounded": True}
    if body["review_required"]:
        contract["reviewer_role"] = body["reviewer_role"]
    contract.update(extra)
    return contract


def run(*, case_id: str, held_by: str, run_id: str, provider,
        prompt_version: str = VERSION) -> dict:
    state = _dao_json(["read-contract", case_id, "_run_state.json", "--run-id", run_id])
    if not any(item.get("stage_name") == STAGE and item.get("status") == "in_progress"
               for item in state.get("stages", [])):
        raise RuntimeError(
            "BLOCKED: claim_analysis must be in_progress; the orchestrator owns attempt state")
    schema = _body_schema()
    with driver_runtime.driver_span(case_id, run_id, "dao_content_read", unit_id=UNIT_M1):
        manifest = _dao_json(["read-contract", case_id, "document_manifest.json", "--run-id", run_id])
        selected = cp1_documents(manifest)
        if not selected:
            raise RuntimeError("BLOCKED: no processed non-policy claim document is available for CP1")
        doc_ids = [doc["document_id"] for doc in selected]
        raw = _dao_json(["read-redacted-text-bundle", case_id,
                         *[item for doc_id in doc_ids for item in ("--doc-id", doc_id)],
                         "--run-id", run_id])
        by_id = {doc["document_id"]: doc for doc in raw.get("documents", [])}
        missing = [doc_id for doc_id in doc_ids if doc_id not in by_id]
        if missing:
            raise RuntimeError("BLOCKED: redacted text missing for " + ", ".join(missing))
        bundle = [by_id[doc_id] for doc_id in doc_ids]
        index = _dao_json(["read-document-index", case_id, "--run-id", run_id],
                          allow_missing=True)
        if index is None:
            raise RuntimeError("BLOCKED: _document_index.json is required for M1's page "
                               "selection -- run the policy driver first")
    listing, index_pages = _index_listing(index)
    with driver_runtime.driver_span(case_id, run_id, "input_snapshot", unit_id=UNIT_M1, items=len(doc_ids)):
        digests = {"manifest_entries": _digest(selected), "document_index": _digest(index)}
        digests.update({f"redacted:{doc_id}": by_id[doc_id]["redacted_text_sha256"]
                        for doc_id in doc_ids})
    summary: dict = {"units": {}}

    # -------------------------------------------------- M1 (CP1 + select) --
    unit_start = time.monotonic()
    candidate_id = grouped_candidate_id(selected)
    stored_m1 = (_dao_json(["read-driver-candidates", case_id, "--stage", STAGE,
                            "--unit-id", UNIT_M1, "--run-id", run_id],
                           allow_missing=True) or {}).get("candidates", {})

    def _reuse_m1(stored):
        existing = stored.get(candidate_id)
        if isinstance(existing, dict) and driver_runtime.candidate_matches(
                existing, input_digests=digests, prompt_version=prompt_version,
                response_schema_version=VERSION, provider_name=provider.provider_name,
                model_name=provider.model_name):
            result = existing["result"]
            return {"cp1": _validate_cp1_output(result["cp1"], bundle, schema),
                    "selection": _clamp_selection(result["selection"]["pages"], index_pages)}
        return None

    selection = None
    cp1_contract = _unit_receipt_reuse(case_id, run_id, UNIT, CONTRACT, digests,
                                       prompt_version, provider)
    if cp1_contract is not None:
        reused = _reuse_m1(stored_m1)
        if reused is not None:
            selection = reused["selection"]
        summary["units"][UNIT] = {"status": "reused", "seconds": round(time.monotonic() - unit_start, 1)}
    else:
        m1 = _reuse_m1(stored_m1)
        if m1 is None:
            driver_runtime.maybe_interrupt(STAGE, candidate_id)
            with driver_runtime.driver_span(case_id, run_id, "provider_wait", unit_id=UNIT_M1, items=1):
                m1 = driver_runtime.structured_with_one_correction(
                    provider=provider, prompt=_m1_prompt(bundle, listing),
                    prompt_version=prompt_version, output_schema=_transport_schema_m1(),
                    validate=lambda value: _validate_m1(value, bundle, schema, index_pages))
            _persist_candidate(case_id, run_id, held_by, UNIT_M1, candidate_id,
                               digests, prompt_version, provider, m1)
        selection = m1["selection"]
        body = _verify_and_bind(case_id, run_id, UNIT, m1["cp1"])
        cp1_contract = _envelope(case_id, run_id, body, provider, prompt_version,
                                 {"fields": body["fields"],
                                  **({"evidence_references": _refs(body)} if _refs(body) else {})})
        _write_unit(case_id, run_id, held_by, UNIT, CONTRACT, SCHEMA, cp1_contract,
                    digests, prompt_version, provider)
        summary["units"][UNIT] = {"status": "published", "seconds": round(time.monotonic() - unit_start, 1)}

    # --------------------------------------------- M2 (judge + type + req) --
    unit_start = time.monotonic()
    fields_json = _compact_fields(cp1_contract)
    adjuster_case_type = manifest.get("adjuster_case_type")
    digests_m2 = {"cp1_contract": _digest(cp1_contract),
                  "document_index": _digest(index),
                  "adjuster_case_type": _digest(adjuster_case_type)}
    cp2_contract = _unit_receipt_reuse(case_id, run_id, UNIT_CP2, CONTRACT_CP2,
                                       digests_m2, prompt_version, provider)
    cp3_contract = _unit_receipt_reuse(case_id, run_id, UNIT_CP3, CONTRACT_CP3,
                                       digests_m2, prompt_version, provider)
    cp4_contract = _unit_receipt_reuse(case_id, run_id, UNIT_CP4, CONTRACT_CP4,
                                       digests_m2, prompt_version, provider)
    cp3_reused = cp3_contract is not None
    if cp2_contract is not None and cp3_contract is not None and cp4_contract is not None:
        for unit_id in (UNIT_CP2, UNIT_CP3, UNIT_CP4):
            summary["units"][unit_id] = {"status": "reused", "seconds": 0.0}
    else:
        if selection is None:
            raise RuntimeError(
                "BLOCKED: checkpoint 1 is published but M1's page selection candidate is "
                "missing or stale, so M2 has no read plan; clear the stage's driver "
                "receipts/candidates to re-run M1")
        by_doc = {}
        for item in selection["pages"]:
            by_doc.setdefault(item["document_id"], []).append(item["page"])
        args = ["read-redacted-text-bundle", case_id]
        for doc_id in sorted(by_doc):
            page_list = ",".join(str(p) for p in sorted(set(by_doc[doc_id])))
            args += ["--doc-id", doc_id, "--pages", f"{doc_id}={page_list}"]
        args += ["--run-id", run_id]
        with driver_runtime.driver_span(case_id, run_id, "dao_content_read", unit_id=UNIT_M2):
            raw_pages = _dao_json(args)
        served_bundle = raw_pages.get("documents", [])
        served = {(doc["document_id"], page["page"]): page["text"]
                  for doc in served_bundle for page in doc.get("pages", [])}
        known1 = _known_quote_set(cp1_contract)
        dao_verify = _make_dao_verifier(case_id, run_id)
        schemas = (_body_schema_cp2(), _body_schema_cp3(), _body_schema_cp4())
        stored_m2 = (_dao_json(["read-driver-candidates", case_id, "--stage", STAGE,
                                "--unit-id", UNIT_M2, "--run-id", run_id],
                               allow_missing=True) or {}).get("candidates", {})
        existing = stored_m2.get("m2")
        if isinstance(existing, dict) and driver_runtime.candidate_matches(
                existing, input_digests=digests_m2, prompt_version=prompt_version,
                response_schema_version=VERSION, provider_name=provider.provider_name,
                model_name=provider.model_name):
            sections = _validate_m2(existing["result"], served, known1, schemas, dao_verify)
        else:
            driver_runtime.maybe_interrupt(STAGE, "m2")
            with driver_runtime.driver_span(case_id, run_id, "provider_wait", unit_id=UNIT_M2, items=1):
                sections = driver_runtime.structured_with_one_correction(
                    provider=provider,
                    prompt=_m2_prompt(fields_json, _render_pages(served_bundle), adjuster_case_type),
                    prompt_version=prompt_version, output_schema=_transport_schema_m2(),
                    validate=lambda value: _validate_m2(value, served, known1, schemas, dao_verify))
            _persist_candidate(case_id, run_id, held_by, UNIT_M2, "m2",
                               digests_m2, prompt_version, provider, sections)

        if cp2_contract is None:
            body = _verify_and_bind(case_id, run_id, UNIT_CP2, sections["coverage_section"])
            cited = [c["matched_clause_ref"]["document_id"] for c in body["coverages"]
                     if isinstance(c.get("matched_clause_ref"), dict)]
            extra = {"coverages": body["coverages"]}
            if cited:
                extra["upstream_policy_snapshot"] = _fetch_snapshot(case_id, run_id, cited)
            cp2_contract = _envelope(case_id, run_id, body, provider, prompt_version, extra)
            _write_unit(case_id, run_id, held_by, UNIT_CP2, CONTRACT_CP2, SCHEMA_CP2,
                        cp2_contract, digests_m2, prompt_version, provider)
            summary["units"][UNIT_CP2] = {"status": "published", "seconds": round(time.monotonic() - unit_start, 1)}
        else:
            summary["units"][UNIT_CP2] = {"status": "reused", "seconds": 0.0}

        if cp3_contract is None:
            body = _verify_and_bind(case_id, run_id, UNIT_CP3, sections["case_type_section"])
            extra = {name: body[name] for name in
                     ("case_type", "coverage_basis", "loss_type", "is_claim_case",
                      "case_type_source", "secondary_case_types", "candidate_types",
                      "template_id", "report_profile", "evidence_references")
                     if name in body}
            cp3_contract = _envelope(case_id, run_id, body, provider, prompt_version, extra)
            _write_unit(case_id, run_id, held_by, UNIT_CP3, CONTRACT_CP3, SCHEMA_CP3,
                        cp3_contract, digests_m2, prompt_version, provider)
            summary["units"][UNIT_CP3] = {"status": "published", "seconds": round(time.monotonic() - unit_start, 1)}
        else:
            summary["units"][UNIT_CP3] = {"status": "reused", "seconds": 0.0}

        if cp4_contract is None:
            body = _renumber_requirements(sections["requirements_section"])
            body = _verify_and_bind(case_id, run_id, UNIT_CP4, body)
            cited = [r["clause_ref"]["document_id"]
                     for group in body["coverage_requirements"]
                     for r in group.get("requirements", [])
                     if isinstance(r.get("clause_ref"), dict)]
            extra = {"coverage_requirements": body["coverage_requirements"]}
            if cited:
                extra["upstream_policy_snapshot"] = _fetch_snapshot(case_id, run_id, cited)
            cp4_contract = _envelope(case_id, run_id, body, provider, prompt_version, extra)
            _write_unit(case_id, run_id, held_by, UNIT_CP4, CONTRACT_CP4, SCHEMA_CP4,
                        cp4_contract, digests_m2, prompt_version, provider)
            summary["units"][UNIT_CP4] = {"status": "published", "seconds": round(time.monotonic() - unit_start, 1)}
        else:
            summary["units"][UNIT_CP4] = {"status": "reused", "seconds": 0.0}

    # ------------------------------------------- medical publication --
    if cp3_reused:
        summary["medical_publication"] = {"skipped": "cp3 unit reused"}
    else:
        summary["medical_publication"] = _attempt_medical_publication(
            case_id, run_id, held_by, cp3_contract["case_type"])

    summary["status"] = "complete"
    summary["document_count"] = len(doc_ids)
    return summary


def _persist_candidate(case_id: str, run_id: str, held_by: str, unit_id: str,
                       candidate_id: str, digests: Mapping[str, str],
                       prompt_version: str, provider, result: Mapping[str, Any]) -> None:
    payload = driver_runtime.make_candidate(
        case_id=case_id, run_id=run_id, stage=STAGE, unit_id=unit_id,
        candidate_id=candidate_id, input_digests=digests,
        prompt_version=prompt_version, response_schema_version=VERSION,
        provider_name=provider.provider_name, model_name=provider.model_name,
        result=result)
    candidate_file = _temp_json(payload)
    try:
        _dao_write(["write-driver-candidate", case_id, "--stage", STAGE,
                    "--unit-id", unit_id, "--candidate-id", candidate_id,
                    "--data-file", str(candidate_file), "--held-by", held_by,
                    "--run-id", run_id])
    finally:
        candidate_file.unlink(missing_ok=True)


def _verify_and_bind(case_id: str, run_id: str, unit_id: str, body: dict) -> dict:
    refs = _refs(body)
    if not refs:
        return body
    ref_file = _temp_json({"references": refs})
    try:
        with driver_runtime.driver_span(case_id, run_id, "evidence_verify",
                                        unit_id=unit_id, items=len(refs)):
            checked = _dao_json(["verify-evidence-references", case_id,
                                 "--references-file", str(ref_file), "--run-id", run_id])
    finally:
        ref_file.unlink(missing_ok=True)
    verified = checked.get("verified_references", [])
    if len(verified) != len(refs):
        raise RuntimeError("DAO returned an incomplete evidence verification result")
    return _bind(body, {_ref_key(ref): ref for ref in verified})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--prompt-version", default=VERSION)
    add_provider_args(parser)
    args = parser.parse_args(argv)
    trace_mod.configure_from_args(args)
    try:
        provider = build_provider(parse_provider_config(args))
        print(json.dumps(run(case_id=args.case_id, held_by=args.held_by, run_id=args.run_id,
                             provider=provider, prompt_version=args.prompt_version),
                         ensure_ascii=False))
    except (RuntimeError, ValueError, ProviderExecutionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
