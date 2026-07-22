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
from llm_providers import (
    ProviderConfig,
    ProviderConfigError,
    ProviderExecutionError,
    SUPPORTED_PROVIDERS,
    build_provider,
)
from redaction import PROMPT_VERSION, LlmRedactor, RedactionLeakError, RedactionParseError


ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"
DEFAULT_REDACTION_PROVIDER = "codex-cli"


def _dao(*args: str, capability: str | None = None) -> str:
    """Run a DAO subcommand.

    `capability` is checkpoint 2's per-run secret, passed only on the one call
    that needs pre-redaction page text. It goes through the environment rather
    than argv so it does not land in process listings, and it is minted fresh
    per run so it cannot be replayed from a log. Every other DAO call runs
    without it -- the capability is scoped to the reads that actually require
    it, not granted for the whole process.
    """
    env = dict(os.environ)
    if capability is not None:
        env[dao.PAGE_TEXT_CAPABILITY_ENV] = capability
    result = subprocess.run(
        [sys.executable, str(DAO), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(ROOT), env=env,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"dao.py {' '.join(args[:2])} failed: {detail}")
    return result.stdout


def redact_document(case_id: str, doc_id: str, held_by: str, run_id: str, redactor) -> dict:
    ocr_result = json.loads(_dao("read-contract", case_id, f"ocr_result_{doc_id}.json"))
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
            # --caller-stage is a hard DAO gate, not a label, and the
            # capability is what makes the claim verifiable rather than
            # self-asserted.
            text = _dao("read-page-text", case_id, doc_id, str(page_number),
                        "--caller-stage", "document-pipeline", capability=capability)
            # redact_page HARD-FAILS (RedactionLeakError) on any detected
            # possible PII leak -- that propagates out of this function and
            # nothing is written, blocking the document exactly like a P8
            # disagreement. Only a leak-free page returns an outcome.
            outcome = redactor.redact_page(text)
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

    try:
        provider = build_provider(ProviderConfig(args.provider, args.model), env=os.environ, root=ROOT)
        redactor = LlmRedactor(provider)
        result = redact_document(args.case_id, args.doc_id, args.held_by, args.run_id, redactor)
    except RedactionLeakError as exc:
        # Possible PII leak detected -- nothing was written. Block the document.
        sys.exit(f"REDACTION BLOCKED ({args.doc_id}): possible PII leak -- {exc}")
    except (ProviderConfigError, ProviderExecutionError, RedactionParseError, RuntimeError) as exc:
        sys.exit(f"error: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
