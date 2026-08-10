"""Document-pipeline checkpoint 2: redact validated page text via a Redactor.

All case data access goes through tools/dao.py. Redaction itself goes through
the `redaction.Redactor` abstraction (today: `LlmRedactor` over any configured
provider), so a future dedicated de-identification model can drop in without
changing this tool. Dev-phase default provider is `codex-cli`.

Redaction is span-substitution, not page rewriting: the model only IDENTIFIES
PII values and `redaction.py` deterministically replaces them in the source, so
non-PII content is preserved by construction (omission/fabrication impossible).
A possible PII leak -- structured PII surviving in the output, or a model-named
value not found verbatim in the source -- HARD-FAILS the document (RedactionLeakError,
nothing written), like a P8 disagreement. Over-redaction risk (a span left
un-redacted to avoid corrupting kept text) is privacy-safe and only sets
review_required. A redaction is never trusted silently.

Usage:
    python tools/redact_document.py CASE_ID DOC_ID \
        --held-by document-pipeline --run-id RUN_ID \
        --provider codex-cli --model MODEL
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import dao
# tools/trace.py, not the stdlib `trace` module.
import trace as trace_mod
from llm_providers import (
    ProviderConfig,
    ProviderConfigError,
    ProviderExecutionError,
    SUPPORTED_PROVIDERS,
    build_provider,
)
from redaction import (PROMPT_VERSION, LlmRedactor, NoPiiClassRedactor,
                       RedactionLeakError, RedactionParseError)


ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"
DEFAULT_REDACTION_PROVIDER = "codex-cli"


def _dao(*args: str, capability: str | None = None) -> str:
    """Run a DAO subcommand as a subprocess.

    Only the WRITE path still uses this. The per-page read moved in-process
    (see redact_document), which is where the cost was: a subprocess per page
    multiplied ~0.38s of interpreter startup by page count. The remaining
    calls are a fixed three per document, and they go through write-contract's
    schema + cross-contract validation, which is not worth re-implementing
    in-process to save a constant.

    `capability` is retained for callers that still need the subprocess form:
    it travels through the child environment rather than argv so it does not
    land in a process listing, and it is minted fresh per run so it cannot be
    replayed from a log. The in-process read passes the same token as an
    argument instead, which keeps it out of the environment entirely.
    """
    env = dict(os.environ)
    if capability is not None:
        env[dao.PAGE_TEXT_CAPABILITY_ENV] = capability
    # Instrumented to quantify B1's per-page tax: this spawns a fresh Python
    # interpreter for every page of every document, and a bare `import dao`
    # subprocess measures ~0.35s. Only the subcommand name is recorded -- the
    # rest of argv carries case/doc/page identifiers, and the capability secret
    # travels in env and must never reach a trace file.
    with trace_mod.span("subprocess.dao", category="subprocess",
                        argv0="dao.py",
                        subcommand=args[0] if args else None) as sp:
        result = subprocess.run(
            [sys.executable, str(DAO), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(ROOT), env=env,
        )
        sp.set(exit_code=result.returncode)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"dao.py {' '.join(args[:2])} failed: {detail}")
        return result.stdout


def redact_document(case_id: str, doc_id: str, held_by: str, run_id: str, redactor) -> dict:
    # read_contract_data is the DAO's own in-process contract read -- same
    # path resolution and traversal guard as the CLI, without paying an
    # interpreter start to get it.
    ocr_result = dao.read_contract_data(case_id, f"ocr_result_{doc_id}.json")
    if ocr_result is None:
        raise RuntimeError(f"checkpoint 2 blocked: no ocr_result for {doc_id}")
    if ocr_result.get("cross_validation_status") not in {"agreed", "disagreed_resolved"}:
        raise RuntimeError(
            f"checkpoint 2 blocked: {doc_id} cross_validation_status is "
            f"{ocr_result.get('cross_validation_status')!r}"
        )

    redacted_pages: list[str] = []
    total_items = 0
    categories: set[str] = set()
    provider_metadata = None
    review_warnings: list[str] = []

    # Scoped to this one document and revoked in the finally, so the window in
    # which pre-redaction text is obtainable at all is the page loop and
    # nothing more. An analysis agent shelling out to dao.py cannot produce
    # this, so --caller-stage alone stops being enough.
    capability, capability_path = dao._issue_page_text_capability(case_id, doc_id)
    try:
        for page in ocr_result.get("pages", []):
            page_number = page["page"]
            with trace_mod.span("redact.page", category="io", case_id=case_id,
                                doc_id=doc_id, page=page_number) as sp:
                # In-process, not a subprocess: this is the per-PAGE call, so
                # the ~0.38s of interpreter startup (measured) was multiplied
                # by page count -- 11.2% of redaction wall-clock on a real
                # 17-page document. Both DAO gates still run, in the same
                # order, and the capability now travels as an argument instead
                # of through the child environment, so it never appears in a
                # process listing. read_page_text_data raises on refusal, so a
                # denial cannot be mistaken for page content.
                text = dao.read_page_text_data(
                    case_id, doc_id, page_number,
                    caller_stage="document-pipeline", capability=capability)
                sp.set(bytes_in=len(text))
                # redact_page HARD-FAILS (RedactionLeakError) on any detected
                # possible PII leak -- that propagates out of this function and
                # nothing is written, blocking the document exactly like a P8
                # disagreement. Only a leak-free page returns an outcome.
                outcome = redactor.redact_page(text)
                sp.set(bytes_out=len(outcome.redacted_text))
                redacted_pages.append(f"<<<PAGE page={page_number}>>>\n{outcome.redacted_text}")
                total_items += outcome.items_redacted
                categories.update(outcome.categories)
                provider_metadata = outcome.provider_metadata
                for warning in outcome.review_warnings:
                    review_warnings.append(f"page {page_number}: {warning}")
    finally:
        dao.release_page_text_capability(capability_path)

    if not redacted_pages:
        raise RuntimeError(f"checkpoint 2 blocked: {doc_id} has no validated pages")

    # review_required is COMPUTED, never hardcoded. A leak already hard-failed
    # above (no write), so what remains here is over-redaction risk (a span left
    # un-redacted because replace-all was unsafe) -- privacy-safe but worth a
    # human look. A downstream agent may raise this floor, never lower it.
    review_required = bool(review_warnings)

    warnings: list[str] = list(review_warnings)
    if provider_metadata:
        warnings.append(
            "Provider execution metadata is recorded in model_info.provider_metadata; "
            "source text was accessed only through dao.py."
        )

    redacted_text = "\n".join(redacted_pages) + "\n"
    redacted_path = f"data/processed/{case_id}/{doc_id}/redacted_text.md"
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": "document-pipeline",
        "status": "success",
        "model_info": {
            "model_name": redactor.label,
            "prompt_version": PROMPT_VERSION,
            "provider_metadata": provider_metadata or {},
        },
        "method": redactor.method,
        "document_id": doc_id,
        "redacted_text_path": redacted_path,
        "items_redacted": total_items,
        "categories": sorted(categories),
        "review_required": review_required,
        "warnings": warnings,
    }
    if review_required:
        # Schema rule (common_component_output.schema.json): review_required
        # true requires reviewer_role set. Over-redaction risk is a masking-
        # completeness question, not a medical/legal judgment call -- routes
        # to 손해사정사, same role run_checkpoint1.py uses for its own
        # review_required case (a P8 disagreement).
        contract["reviewer_role"] = "손해사정사"
        contract["review_reason"] = (
            "Over-redaction risk: a span was left un-redacted because a safe "
            "replacement could not be made without risking corruption of "
            "surrounding kept text (privacy-safe direction, but needs a human "
            "check). See warnings for the specific page(s)/span(s)."
        )

    scratch_root = ROOT / "_redaction_scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{case_id}_{doc_id}_", dir=scratch_root) as temp_dir:
        temp = Path(temp_dir)
        text_file = temp / "redacted_text.md"
        contract_file = temp / "redaction_result.json"
        fields_file = temp / "manifest_fields.json"
        text_file.write_text(redacted_text, encoding="utf-8")
        contract_file.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
        fields_file.write_text(json.dumps({"redacted_text_path": redacted_path}), encoding="utf-8")

        _dao("write-redacted-text", case_id, doc_id, "--text-file", str(text_file),
             "--held-by", held_by, "--run-id", run_id)
        _dao("write-contract", case_id, f"redaction_result_{doc_id}.json",
             "--data-file", str(contract_file), "--schema-name", "redaction_result.schema.json",
             "--held-by", held_by, "--run-id", run_id)
        _dao("patch-manifest-document", case_id, doc_id, "--fields-file", str(fields_file),
             "--held-by", held_by, "--run-id", run_id)

    return {
        "status": "success",
        "case_id": case_id,
        "doc_id": doc_id,
        "pages": len(redacted_pages),
        "items_redacted": total_items,
        "review_required": review_required,
        "redactor": redactor.label,
    }


# Document classes whose text is published standard-form contract wording,
# identical for every policyholder, and therefore structurally free of claimant
# PII. Eligibility is decided from the manifest's classified document_type --
# never from a filename or an agent's assertion. See
# redaction.NoPiiClassRedactor for what the exemption does and does not give up
# (the deterministic residual-PII sweep still runs on every page and still
# hard-fails, so the claim is verified per page rather than trusted).
NO_PII_DOCUMENT_TYPES = frozenset({"insurance_policy"})


def _redactor_for(case_id: str, doc_id: str, provider_name: str, model: str | None):
    """Pick the redactor for this document: deterministic pass-through for a
    PII-free document class, otherwise the real LLM span redactor."""
    manifest = dao.read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        raise RuntimeError(f"no document_manifest.json for {case_id}")
    entry = next((d for d in manifest.get("documents", [])
                  if d.get("document_id") == doc_id), None)
    if entry and entry.get("document_type") in NO_PII_DOCUMENT_TYPES:
        print(f"{doc_id}: document_type={entry['document_type']} is a PII-free class -- "
              "deterministic pass-through, no redaction model called "
              "(residual-PII scan still enforced per page)", file=sys.stderr)
        return NoPiiClassRedactor()
    provider = build_provider(ProviderConfig(provider_name, model), env=os.environ, root=ROOT)
    return LlmRedactor(provider)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("doc_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS,
                        default=os.environ.get("HARNESS_REDACTION_PROVIDER", DEFAULT_REDACTION_PROVIDER))
    parser.add_argument("--model", default=os.environ.get("HARNESS_REDACTION_MODEL"))
    args = parser.parse_args()

    # Without this every span below is a no-op: trace.enabled() stays False
    # until a case/run is configured, so the redact.page and subprocess.dao
    # spans this tool raises are silently discarded. Found by running a real
    # 17-page redaction and getting an empty rollup for the very path the
    # runtime plan calls the pipeline's worst (B1) -- the instrumentation was
    # planted here but never switched on.
    trace_mod.configure(args.case_id, args.run_id)

    try:
        redactor = _redactor_for(args.case_id, args.doc_id, args.provider, args.model)
        result = redact_document(args.case_id, args.doc_id, args.held_by, args.run_id, redactor)
    except RedactionLeakError as exc:
        # Possible PII leak detected -- nothing was written. Block the document.
        sys.exit(f"REDACTION BLOCKED ({args.doc_id}): possible PII leak -- {exc}")
    except (ProviderConfigError, ProviderExecutionError, RedactionParseError, RuntimeError) as exc:
        sys.exit(f"error: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
