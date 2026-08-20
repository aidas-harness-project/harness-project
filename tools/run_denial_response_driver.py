"""Initial insurer-decision extraction driver (non-dispatch pilot).

Work is grouped by original source file and contiguous physical pages before
document type filtering. This keeps a split insurer response as one provider
work unit. Policy matching is a later enrichment checkpoint: this command
never invents a policy match from insurer text alone.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

import _cross_contract
import driver_runtime
import driver_schema
import trace as trace_mod
from llm_providers import ProviderExecutionError, add_provider_args, build_provider, parse_provider_config

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"
STAGE, UNIT = "denial_response", "initial_extraction"
CONTRACT, SCHEMA = "denial_reason_result.json", "denial_reason_result.schema.json"
# v0.4 (2026-08-20): the prompt now states the closed vocabularies the
# transport schema drops. Bumped rather than edited in place because
# `receipt_matches` reuses a stored candidate only when the prompt
# version matches -- a candidate produced under v0.3 was answered
# without ever being told decision_type's allowed values.
VERSION = "denial_response_initial_driver.v0.4"
TEXT_DISPOSITIONS = frozenset({"automated_text_pipeline", "text_only_no_normalization"})


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


def insurer_bundles(manifest: Mapping[str, Any]) -> list[list[dict]]:
    """Group contiguous source-file components that contain insurer response text."""
    by_source: dict[str, list[dict]] = {}
    for doc in manifest.get("documents", []):
        if not isinstance(doc, dict) or doc.get("downstream_disposition") not in TEXT_DISPOSITIONS:
            continue
        name = doc.get("source_file_name") or doc.get("file_name")
        start, end = doc.get("source_page_start"), doc.get("source_page_end")
        if isinstance(name, str) and isinstance(start, int) and isinstance(end, int):
            by_source.setdefault(name, []).append(doc)
    groups: list[list[dict]] = []
    for docs in by_source.values():
        docs.sort(key=lambda item: (item["source_page_start"], item["source_page_end"], item["document_id"]))
        component: list[dict] = []
        component_end = 0
        for doc in docs:
            if component and doc["source_page_start"] > component_end + 1:
                if any(item.get("document_type") == "insurer_response" for item in component):
                    groups.append(component)
                component, component_end = [], 0
            component.append(doc)
            component_end = max(component_end, doc["source_page_end"])
        if component and any(item.get("document_type") == "insurer_response" for item in component):
            groups.append(component)
    return sorted(groups, key=lambda group: (
        str(group[0].get("source_file_name") or group[0].get("file_name")),
        group[0]["source_page_start"],
    ))


def _body_schema() -> dict:
    """Return the authoritative, fully materialized local candidate schema."""
    public = driver_schema.load_materialized_schema(SCHEMA)
    all_of = public.get("allOf", [])
    props = all_of[1].get("properties", {}) if len(all_of) > 1 else {}
    if not isinstance(props.get("denial_reasons"), dict):
        raise RuntimeError("denial result schema has no denial_reasons definition")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Denial response bundle extraction v0.1",
        "type": "object", "additionalProperties": False,
        "properties": {
            "status": {"enum": ["success", "partial"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "review_required": {"type": "boolean"},
            "reviewer_role": {"enum": ["손해사정사", "의사", "법률전문가"]},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "denial_reasons": props["denial_reasons"],
            "accepted_coverages": props["accepted_coverages"],
        },
        "required": ["status", "confidence", "review_required", "warnings", "denial_reasons", "accepted_coverages"],
        "allOf": [{
            "if": {"properties": {"review_required": {"const": True}}, "required": ["review_required"]},
            "then": {"required": ["reviewer_role"]},
        }],
    }


def _transport_schema() -> dict:
    """Return Claude CLI's bounded outer structured-output contract.

    Claude accepts `--json-schema` only inline on argv. The fully materialized
    public denial schema is larger than Windows' command-line limit, so it
    cannot be passed to the provider. `_body_schema()` remains the
    authoritative local gate inside `structured_with_one_correction` before a
    candidate can be persisted or a DAO contract can be written.
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
            # The full nested item shapes are enforced locally. Keeping these
            # collection members permissive is intentional: duplicating the
            # public schema here reintroduces the Windows argv failure.
            "denial_reasons": {"type": "array", "items": {"type": "object"}},
            "accepted_coverages": {"type": "array", "items": {"type": "object"}},
        },
        "required": [
            "status", "confidence", "review_required", "warnings",
            "denial_reasons", "accepted_coverages",
        ],
        # Anthropic's native tool-input schemas reject a top-level allOf.
        # The equivalent conditional remains in the prompt below and, more
        # importantly, in _body_schema(), which is validated locally before
        # any candidate or governed contract can be written.
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


def _item_key(item: Mapping[str, Any]) -> tuple:
    return (*min((_ref_key(ref) for ref in _refs(item)), default=("", 0, "", None, None)),
            str(item.get("decided_coverage") or item.get("coverage_name") or ""),
            str(item.get("raw_reason_text") or ""))


def _assign_ids(body: dict) -> dict:
    copied = json.loads(json.dumps(body, ensure_ascii=False))
    copied["denial_reasons"].sort(key=_item_key)
    copied["accepted_coverages"].sort(key=_item_key)
    next_basis = 1
    for number, reason in enumerate(copied["denial_reasons"], 1):
        if not isinstance(reason.get("decided_coverage"), str) or not reason["decided_coverage"].strip():
            raise RuntimeError("each new denial reason must name decided_coverage")
        if reason.get("policy_matches"):
            raise RuntimeError("policy matches require the later policy-enrichment checkpoint")
        reason["reason_id"] = f"DR_{number}"
        for category in ("contractual_basis", "medical_or_factual_basis", "calculation_basis"):
            for basis in reason["grounds"][category]:
                basis["basis_id"] = f"BASIS_{next_basis}"
                next_basis += 1
    for number, accepted in enumerate(copied["accepted_coverages"], 1):
        if accepted.get("policy_matches"):
            raise RuntimeError("policy matches require the later policy-enrichment checkpoint")
        accepted["accepted_coverage_id"] = f"AC_{number}"
    return copied


def _prompt(bundle: list[dict]) -> str:
    rendered = "\n\n".join(
        f"## {doc['document_id']} ({doc.get('document_type')})\n" + "\n".join(
            f"[page {page['page']}]\n{page['text']}" for page in doc["pages"])
        for doc in bundle)
    return f"""Extract the insurer's stated decisions from this one complete response bundle.
Return only the supplied JSON Schema. Separate every denial/reduction and every accepted coverage.
Use only exact quotes from this bundle. Do not infer missing amounts. Provide ranked Top-3 R-code
candidates. There is no policy text in this checkpoint: set every policy_matches array to [].
IDs are provisional -- the driver reassigns the final deterministic ones -- but they are still
pattern-checked before that happens, so they must already have the right SHAPE. Number them from 1
in order of appearance and use exactly these forms:
  reason_id            "DR_1", "DR_2", ...       (never "REASON_1")
  basis_id             "BASIS_1", "BASIS_2", ... numbered across ALL grounds arrays, not per array
  accepted_coverage_id "AC_1", "AC_2", ...
document_id in every evidence reference is the real DOC_NNN id from the bundle heading above the
text you quote, never invented.

For each denial_reasons item include: reason_id, decided_coverage, decision_type, payment_status,
taxonomy_code, candidate_codes, raw_reason_text, insurer_claim_summary, grounds with all three
basis arrays, amounts, requested_documents, policy_matches, confidence, evidence_references, and
review_required. For each accepted_coverages item include: accepted_coverage_id, coverage_name,
payment_status, accepted_amount, insurer_stated_basis, policy_matches, confidence,
evidence_references, and review_required. Every evidence reference needs document_id, page, and an
exact quote.

candidate_codes is an array of 1 to 3 objects, and each object has EXACTLY these two keys:
  taxonomy_code -- the R-code string, e.g. "R07"
  confidence    -- a number from 0 to 1
No other key is permitted in a candidate_codes entry: rank, code, rationale, label and reason are
all rejected. Rank is expressed by array ORDER, not by a field. The list is ranked best-first, its
first entry's taxonomy_code MUST equal this reason's taxonomy_code, the codes must be distinct, and
confidence must never increase as the list goes on. For example:
  "taxonomy_code": "R07",
  "candidate_codes": [
    {{"taxonomy_code": "R07", "confidence": 0.82}},
    {{"taxonomy_code": "R12", "confidence": 0.11}}
  ]

grounds is an object with EXACTLY these three keys, and no others -- policy_basis, legal_basis and
factual_basis are rejected:
  contractual_basis        -- grounds drawn from the policy/contract terms
  medical_or_factual_basis -- grounds drawn from medical findings or the facts of the accident,
                              INCLUDING statute and case-law reasoning about liability
  calculation_basis        -- grounds about how an amount was computed
Each of the three is an array (use [] when the insurer stated nothing in that category). Each array
ENTRY is an object, never a bare string, with these keys:
  basis_id          -- see the ID shapes above
  text              -- the ground itself, as an exact quote from the bundle
  basis_source      -- exactly one of: insurer_stated, agent_inferred
  evidence_references -- array of {{document_id, page, quote}}
  confidence        -- a number from 0 to 1
  review_required   -- true or false; when true, also give reviewer_role
For example:
  "grounds": {{
    "contractual_basis": [],
    "medical_or_factual_basis": [
      {{"basis_id": "BASIS_1", "text": "<exact quote>", "basis_source": "insurer_stated",
        "evidence_references": [{{"document_id": "DOC_007", "page": 3, "quote": "<exact quote>"}}],
        "confidence": 0.9, "review_required": false}}
    ],
    "calculation_basis": []
  }}

amounts is an object with EXACTLY these five keys, ALL of them always present. Write null for any
figure the insurer did not state -- never omit the key, never leave the object empty, and never
infer or compute a number that is not written in the bundle:
  claimed_amount, payable_amount, denied_amount, reduction_amount  -- numbers >= 0, or null
  reduction_rate                                                   -- a number from 0 to 1, or null
For example, when the insurer denies without naming any figure:
  "amounts": {{"claimed_amount": null, "payable_amount": null, "denied_amount": null,
               "reduction_amount": null, "reduction_rate": null}}

requested_documents is an array of document-type codes (use [] when none were requested). Each
entry is exactly one of these literal codes, not a Korean document name:
  insurance_certificate, insurance_policy, application_form, diagnosis_certificate,
  medical_record, imaging_report, receipt, insurer_response, legal_opinion,
  legal_reference, other

Each accepted_coverages entry needs accepted_coverage_id, coverage_name, payment_status,
accepted_amount (a number, or null when the insurer accepts without naming a figure),
confidence, evidence_references and review_required; insurer_stated_basis is a string or null.

policy_matches is [] everywhere in this checkpoint, as stated above, so it needs no inner shape.

The complete response is validated against the full local contract before publication.
If review_required is true at the response level, reviewer_role is required and must be one of
손해사정사, 의사, or 법률전문가.

CLOSED VOCABULARIES. These fields take one of a fixed set of codes. Write the code exactly as
written here -- they are literal identifiers, not labels to translate. The source bundle is
Korean and these codes are not; that is expected, and a Korean rendering of one is invalid:
  decision_type must be one of: denial, reduction
  payment_status must be one of: unpaid, partially_paid, paid, unknown
  reviewer_role, where required, must be one of: 손해사정사, 의사, 법률전문가
decision_type applies to denial_reasons items only. payment_status applies to items in both
denial_reasons and accepted_coverages. taxonomy_code is an R-code (R01-R21, or R99 when none
applies), also written verbatim.

THE R-CODE DETERMINES decision_type. Each code is intrinsically about reducing a payment or about
refusing one, so the pair must agree or the response is rejected:
  reduction codes -- R01 R02 R03 R06 R07 R10 R11 R13 R16 R17 R18 R19 R20 R21
                     these REQUIRE decision_type "reduction"
  denial codes    -- R04 R05 R08 R09
                     these REQUIRE decision_type "denial"
  R12 and R14     -- either decision_type, but review_required MUST be true and reviewer_role
                     must be given
  R99             -- either decision_type; use it only when no R01-R21 code fits
Choose the code that matches what the insurer actually did. If the insurer refused payment
outright, pick a denial code (or R99) -- do not pick a reduction code and then label it a denial.

BUNDLE TEXT:
{rendered}
"""


def bundle_candidate_id(bundle: list[dict]) -> str:
    """Stable per-bundle unit id.

    Derived from the bundle's own document set rather than its position, so
    reordering or adding an unrelated bundle cannot silently re-point one
    unit's stored result at different source text.
    """
    joined = "|".join(sorted(doc["document_id"] for doc in bundle))
    return "bundle_" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _bundle_digests(manifest_entries: list[dict], bundle: list[dict]) -> dict[str, str]:
    """Only the digests this one bundle consumes, so a sibling edit cannot invalidate it.

    Covers the bundle's OWN member manifest entries in their grouped order --
    not the whole manifest, which would change whenever any unrelated document
    is added or re-typed and would re-call the provider for bundles whose own
    input never moved. Order is part of the digest because grouping decides
    which pages the prompt concatenates, so a reordering is a real input change.
    """
    digests = {"manifest_entries": _digest(manifest_entries)}
    digests.update({f"redacted:{doc['document_id']}": doc["redacted_text_sha256"]
                    for doc in bundle})
    return digests


def _validate_bundle_output(value: Mapping[str, Any], bundle: list[dict],
                            schema: Mapping[str, Any]) -> dict:
    allowed = {doc["document_id"] for doc in bundle}
    Draft202012Validator(schema).validate(value)
    page_text_by_key = {
        (doc["document_id"], page["page"]): page["text"]
        for doc in bundle
        for page in doc.get("pages", [])
    }
    page_corrections: list[str] = []
    for ref in _refs(value):
        doc_id = ref["document_id"]
        page = ref["page"]
        if doc_id not in allowed:
            raise ValueError("insurer-bundle output cited a document outside its bundle")
        page_text = page_text_by_key.get((doc_id, page))
        if page_text is None:
            raise ValueError(f"{doc_id}: page {page} is not present in insurer bundle text")
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
            # Reproduces the DAO's exact gate before candidate persistence --
            # page-pair fallback included, or this would refuse citations the
            # DAO would accept. A bad citation therefore gets P4's one model
            # correction rather than becoming an orphaned candidate that only
            # fails at final publication.
            if not _cross_contract.quote_spans_page_pair(
                    quote, page_text, page_text_by_key.get((doc_id, page + 1))):
                doc_pages = {p: t for (d, p), t in page_text_by_key.items()
                             if d == doc_id}
                # Same rule as the claim driver: a quote found on exactly one
                # other page is a knowably-wrong page number, corrected and
                # recorded; anything else refuses.
                resolved = _cross_contract.resolve_cited_page(quote, doc_pages, page)
                if resolved is None:
                    hint = _cross_contract.locate_quote_hint(quote, doc_pages, page)
                    raise ValueError(
                        f"{doc_id}: quote is not present on page {page}{hint}")
                ref["page"] = resolved
                page_corrections.append(f"{doc_id}: evidence page {page} -> {resolved}")
    if page_corrections:
        warnings = list(value.get("warnings") or [])
        warnings.extend(f"citation page corrected -- {item}" for item in page_corrections)
        value = {**value, "warnings": warnings}
        print(f"denial citation page corrections ({len(page_corrections)}): "
              + "; ".join(page_corrections[:6]), file=sys.stderr)
    return dict(value)


def _extract(provider, bundle: list[dict], schema: Mapping[str, Any],
             transport_schema: Mapping[str, Any], version: str,
             case_id: str, run_id: str) -> dict:
    driver_runtime.maybe_interrupt(STAGE, bundle_candidate_id(bundle))
    with driver_runtime.driver_span(case_id, run_id, "provider_wait", unit_id=UNIT, items=1):
        return driver_runtime.structured_with_one_correction(
            provider=provider, prompt=_prompt(bundle), prompt_version=version,
            output_schema=transport_schema,
            validate=lambda value: _validate_bundle_output(value, bundle, schema))


def _publish_candidate(*, case_id: str, run_id: str, held_by: str, provider,
                       prompt_version: str, candidate_id: str,
                       digests: Mapping[str, str], result: Mapping[str, Any]) -> None:
    """Persist one completed bundle so a restart never re-calls it."""
    payload = driver_runtime.make_candidate(
        case_id=case_id, run_id=run_id, stage=STAGE, unit_id=UNIT,
        candidate_id=candidate_id, input_digests=digests,
        prompt_version=prompt_version, response_schema_version=VERSION,
        provider_name=provider.provider_name, model_name=provider.model_name,
        result=result)
    candidate_file = _temp_json(payload)
    try:
        _dao_write(["write-driver-candidate", case_id, "--stage", STAGE,
                    "--unit-id", UNIT, "--candidate-id", candidate_id,
                    "--data-file", str(candidate_file), "--held-by", held_by,
                    "--run-id", run_id])
    finally:
        candidate_file.unlink(missing_ok=True)


def run(*, case_id: str, held_by: str, run_id: str, provider, workers: int,
        prompt_version: str = VERSION) -> dict:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    state = _dao_json(["read-contract", case_id, "_run_state.json", "--run-id", run_id])
    if not any(item.get("stage_name") == STAGE and item.get("status") == "in_progress" for item in state.get("stages", [])):
        raise RuntimeError("BLOCKED: denial_response must be in_progress; the orchestrator owns attempt state")
    schema = _body_schema()
    transport_schema = _transport_schema()
    with driver_runtime.driver_span(case_id, run_id, "dao_content_read", unit_id=UNIT, worker_count=workers):
        manifest = _dao_json(["read-contract", case_id, "document_manifest.json", "--run-id", run_id])
        groups = insurer_bundles(manifest)
        if not groups:
            raise RuntimeError("BLOCKED: no active insurer-response bundle is available")
        doc_ids = [doc["document_id"] for group in groups for doc in group]
        raw = _dao_json(["read-redacted-text-bundle", case_id,
                         *[item for doc_id in doc_ids for item in ("--doc-id", doc_id)], "--run-id", run_id])
        by_id = {doc["document_id"]: doc for doc in raw.get("documents", [])}
        bundles = [[by_id[doc["document_id"]] for doc in group] for group in groups]
    with driver_runtime.driver_span(case_id, run_id, "input_snapshot", unit_id=UNIT, items=len(doc_ids)):
        digests = {"document_manifest": _digest(manifest)}
        digests.update({f"redacted:{doc_id}": by_id[doc_id]["redacted_text_sha256"] for doc_id in doc_ids})
    receipt = _dao_json(["read-driver-receipt", case_id, "--stage", STAGE, "--unit-id", UNIT, "--run-id", run_id], allow_missing=True)
    if receipt and driver_runtime.receipt_matches(receipt, input_digests=digests, prompt_version=prompt_version,
        response_schema_version=VERSION, provider_name=provider.provider_name, model_name=provider.model_name):
        if _dao_json(["read-contract", case_id, CONTRACT, "--run-id", run_id], allow_missing=True) is not None:
            return {"status": "reused", "bundle_count": len(bundles), "contract": CONTRACT}
    # V3 resume boundary: each insurer bundle is its own unit, so an
    # interruption reruns the unfinished bundle and reuses the rest.
    stored = (_dao_json(["read-driver-candidates", case_id, "--stage", STAGE,
                         "--unit-id", UNIT, "--run-id", run_id],
                        allow_missing=True) or {}).get("candidates", {})
    candidate_ids = [bundle_candidate_id(bundle) for bundle in bundles]
    unit_digests = [_bundle_digests(group, bundle)
                    for group, bundle in zip(groups, bundles)]
    outputs: list[dict | None] = [None] * len(bundles)
    pending: list[int] = []
    for index, bundle in enumerate(bundles):
        existing = stored.get(candidate_ids[index])
        if isinstance(existing, dict) and driver_runtime.candidate_matches(
            existing, input_digests=unit_digests[index], prompt_version=prompt_version,
            response_schema_version=VERSION, provider_name=provider.provider_name,
            model_name=provider.model_name,
        ):
            outputs[index] = _validate_bundle_output(existing["result"], bundle, schema)
        else:
            pending.append(index)
    reused_count = len(bundles) - len(pending)

    failure: BaseException | None = None
    if pending:
        def extract_and_persist(index: int) -> dict:
            """Publish this bundle as soon as IT finishes, not when the pool drains.

            Persisting inside the worker is what makes the guarantee hold: a
            sibling's interruption cannot discard a bundle that already
            completed, and the artifact is on disk before the interrupted
            invocation returns.
            """
            result = _extract(provider, bundles[index], schema, transport_schema, prompt_version,
                              case_id, run_id)
            _publish_candidate(case_id=case_id, run_id=run_id, held_by=held_by,
                               provider=provider, prompt_version=prompt_version,
                               candidate_id=candidate_ids[index],
                               digests=unit_digests[index], result=result)
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {index: executor.submit(extract_and_persist, index)
                       for index in pending}
        for index, future in futures.items():
            try:
                outputs[index] = future.result()
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                failure = failure or exc
    if failure is not None:
        raise failure
    with driver_runtime.driver_span(case_id, run_id, "deterministic_merge", unit_id=UNIT, items=len(outputs)):
        body = {"status": "partial" if any(item["status"] == "partial" for item in outputs) else "success",
                "confidence": min(item["confidence"] for item in outputs),
                "review_required": any(item["review_required"] for item in outputs),
                "warnings": [warning for item in outputs for warning in item["warnings"]],
                "denial_reasons": [reason for item in outputs for reason in item["denial_reasons"]],
                "accepted_coverages": [coverage for item in outputs for coverage in item["accepted_coverages"]]}
        roles = sorted({item.get("reviewer_role") for item in outputs if item.get("review_required")})
        if body["review_required"]:
            body["reviewer_role"] = roles[0] if roles else "손해사정사"
        body = _assign_ids(body)
    refs = _refs(body)
    if refs:
        ref_file = _temp_json({"references": refs})
        try:
            with driver_runtime.driver_span(case_id, run_id, "evidence_verify", unit_id=UNIT, items=len(refs)):
                checked = _dao_json(["verify-evidence-references", case_id, "--references-file", str(ref_file), "--run-id", run_id])
        finally:
            ref_file.unlink(missing_ok=True)
        verified = checked.get("verified_references", [])
        if len(verified) != len(refs):
            raise RuntimeError("DAO returned an incomplete evidence verification result")
        body = _bind(body, {_ref_key(ref): ref for ref in verified})
    contract = {"case_id": case_id, "run_id": run_id, "component": "denial-response", "status": body["status"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model_info": {"model_name": provider.model_name, "prompt_version": prompt_version},
                "confidence": body["confidence"], "review_required": body["review_required"],
                "warnings": body["warnings"], "source_grounded": True,
                "denial_reasons": body["denial_reasons"], "accepted_coverages": body["accepted_coverages"]}
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
    return {"status": "published", "bundle_count": len(bundles),
            "reused_candidate_count": reused_count, "contract": CONTRACT}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--prompt-version", default=VERSION)
    add_provider_args(parser)
    args = parser.parse_args(argv)
    # Without this the driver's own spans, plus the dao and provider spans it
    # reaches, are discarded silently while the command still exits 0.
    trace_mod.configure_from_args(args)
    try:
        provider = build_provider(parse_provider_config(args))
        print(json.dumps(run(case_id=args.case_id, held_by=args.held_by, run_id=args.run_id, provider=provider,
                             workers=args.workers, prompt_version=args.prompt_version), ensure_ascii=False))
    except (RuntimeError, ValueError, ProviderExecutionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
