"""Claim-analysis checkpoint 1 driver (v2 pilot, grouped-call variant).

Skeleton scope per `plans/driverization/claim-analysis-v2.md` step 1: input
bundle assembly + ONE grouped CP1 extraction call + local quote verification +
governed DAO write of `extracted_claim_fields.json`, resumable via driver
receipts/candidates. Checkpoints 2-4 and the medical publication arrive in
step 2; until then the stage is completed by the agent path and this command
exists for the arm E measurement only.

Why grouped: the CASE_601 v1 pilot ran CP1 as 16 per-document calls summing
647s at max concurrency 2. The whole non-policy payload of the measurement
case fits one prompt, and the four-arm 2026-08-16 measurement showed call
count, not payload, is what wall time follows. Per-document fan-out remains a
benchmarked variant in the plan, not this default.

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
CONTRACT, SCHEMA = "extracted_claim_fields.json", "extracted_claim_fields.schema.json"
VERSION = "claim_analysis_cp1_driver.v0.1"
TEXT_DISPOSITIONS = frozenset({"automated_text_pipeline", "text_only_no_normalization"})
# CP1 reads the claim-side documents whole. Policy bundles are checkpoint 2's
# input and are read there by the page; handing 200k+ characters of 약관 to a
# field-extraction call is the arm A shape this driver exists to remove.
EXCLUDED_TYPES = frozenset({"insurance_policy"})

# The named slots of extracted_claim_fields v0.2, listed in the prompt so the
# model uses typed fields instead of smuggling facts through warnings (the
# CASE_021 failure the v0.2 schema bump exists to prevent).
NAMED_FIELDS = (
    "diagnosis_name", "kcd_code", "accident_date", "onset_date", "surgery_name",
    "hospital_name", "treatment_period", "admission_period", "imaging_date",
    "diagnosis_date", "claim_received_date", "policy_contract_date",
    "claim_item", "disposition", "insurers",
)


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
            "fields": {"type": "object"},
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
Return only the supplied JSON Schema. Every entry under `fields` uses exactly one of three shapes:
single value {{value, normalized_value?}}, date {{value: YYYY-MM-DD|null}}, or period
{{start_date, end_date|null, days|null}} -- each with confidence, evidence_references, and
review_required (reviewer_role when true; 의사 for medical-judgment fields). Use these named slots
when the fact exists: {named}. Additional facts get descriptive snake_case field names in the same
three shapes. `warnings` is for actual warnings only, never facts that lack a slot.

Evidence discipline: every field cites at least one evidence reference with document_id, page, and
an EXACT quote from the supplied text. A fact that is redacted or absent records value null with
review_required true, citing the page where it would appear -- never substitute a value and never
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


def _validate_cp1_output(value: Mapping[str, Any], bundle: list[dict],
                         schema: Mapping[str, Any]) -> dict:
    allowed = {doc["document_id"] for doc in bundle}
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
        elif _cross_contract._normalize_ws(quote) not in _cross_contract._normalize_ws(page_text):
            # The DAO's exact whitespace-normalized substring gate, applied
            # before candidate persistence so a bad citation gets P4's one
            # model correction instead of failing only at final publication.
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


def run(*, case_id: str, held_by: str, run_id: str, provider,
        prompt_version: str = VERSION) -> dict:
    state = _dao_json(["read-contract", case_id, "_run_state.json", "--run-id", run_id])
    if not any(item.get("stage_name") == STAGE and item.get("status") == "in_progress"
               for item in state.get("stages", [])):
        raise RuntimeError(
            "BLOCKED: claim_analysis must be in_progress; the orchestrator owns attempt state")
    schema = _body_schema()
    transport_schema = _transport_schema()
    with driver_runtime.driver_span(case_id, run_id, "dao_content_read", unit_id=UNIT):
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
            raise RuntimeError(f"BLOCKED: redacted text missing for {', '.join(missing)}")
        bundle = [by_id[doc_id] for doc_id in doc_ids]
    with driver_runtime.driver_span(case_id, run_id, "input_snapshot", unit_id=UNIT, items=len(doc_ids)):
        digests = {"manifest_entries": _digest(selected)}
        digests.update({f"redacted:{doc_id}": by_id[doc_id]["redacted_text_sha256"]
                        for doc_id in doc_ids})
    receipt = _dao_json(["read-driver-receipt", case_id, "--stage", STAGE, "--unit-id", UNIT,
                         "--run-id", run_id], allow_missing=True)
    if receipt and driver_runtime.receipt_matches(
            receipt, input_digests=digests, prompt_version=prompt_version,
            response_schema_version=VERSION, provider_name=provider.provider_name,
            model_name=provider.model_name):
        if _dao_json(["read-contract", case_id, CONTRACT, "--run-id", run_id],
                     allow_missing=True) is not None:
            return {"status": "reused", "document_count": len(doc_ids), "contract": CONTRACT}
    candidate_id = grouped_candidate_id(selected)
    stored = (_dao_json(["read-driver-candidates", case_id, "--stage", STAGE,
                         "--unit-id", UNIT, "--run-id", run_id],
                        allow_missing=True) or {}).get("candidates", {})
    existing = stored.get(candidate_id)
    provider_called = False
    if isinstance(existing, dict) and driver_runtime.candidate_matches(
            existing, input_digests=digests, prompt_version=prompt_version,
            response_schema_version=VERSION, provider_name=provider.provider_name,
            model_name=provider.model_name):
        body = _validate_cp1_output(existing["result"], bundle, schema)
    else:
        body = _extract(provider, bundle, schema, transport_schema, prompt_version,
                        case_id, run_id)
        provider_called = True
        payload = driver_runtime.make_candidate(
            case_id=case_id, run_id=run_id, stage=STAGE, unit_id=UNIT,
            candidate_id=candidate_id, input_digests=digests,
            prompt_version=prompt_version, response_schema_version=VERSION,
            provider_name=provider.provider_name, model_name=provider.model_name,
            result=body)
        candidate_file = _temp_json(payload)
        try:
            _dao_write(["write-driver-candidate", case_id, "--stage", STAGE,
                        "--unit-id", UNIT, "--candidate-id", candidate_id,
                        "--data-file", str(candidate_file), "--held-by", held_by,
                        "--run-id", run_id])
        finally:
            candidate_file.unlink(missing_ok=True)
    refs = _refs(body)
    if refs:
        ref_file = _temp_json({"references": refs})
        try:
            with driver_runtime.driver_span(case_id, run_id, "evidence_verify",
                                            unit_id=UNIT, items=len(refs)):
                checked = _dao_json(["verify-evidence-references", case_id,
                                     "--references-file", str(ref_file), "--run-id", run_id])
        finally:
            ref_file.unlink(missing_ok=True)
        verified = checked.get("verified_references", [])
        if len(verified) != len(refs):
            raise RuntimeError("DAO returned an incomplete evidence verification result")
        body = _bind(body, {_ref_key(ref): ref for ref in verified})
    contract = {"case_id": case_id, "run_id": run_id, "component": "claim-analysis",
                "status": body["status"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model_info": {"model_name": provider.model_name, "prompt_version": prompt_version},
                "confidence": body["confidence"], "review_required": body["review_required"],
                "warnings": body["warnings"], "source_grounded": True,
                "fields": body["fields"]}
    if refs:
        contract["evidence_references"] = _refs(body)
    if body["review_required"]:
        contract["reviewer_role"] = body["reviewer_role"]
    data_file = _temp_json(contract)
    try:
        with driver_runtime.driver_span(case_id, run_id, "dao_publish", unit_id=UNIT):
            _dao_write([
                "write-contract", case_id, CONTRACT,
                "--data-file", str(data_file),
                "--schema-name", SCHEMA,
                "--held-by", held_by, "--run-id", run_id, "--stage", STAGE,
            ])
    finally:
        data_file.unlink(missing_ok=True)
    receipt_file = _temp_json(driver_runtime.make_receipt(
        case_id=case_id, run_id=run_id, stage=STAGE, unit_id=UNIT, input_digests=digests,
        prompt_version=prompt_version, response_schema_version=VERSION,
        provider_name=provider.provider_name, model_name=provider.model_name,
        completed_contracts=[CONTRACT]))
    try:
        _dao_write(["write-driver-receipt", case_id, "--stage", STAGE, "--unit-id", UNIT,
                    "--data-file", str(receipt_file), "--held-by", held_by, "--run-id", run_id])
    finally:
        receipt_file.unlink(missing_ok=True)
    return {"status": "published", "document_count": len(doc_ids),
            "provider_called": provider_called, "contract": CONTRACT}


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
