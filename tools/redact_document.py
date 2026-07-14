"""Document-pipeline checkpoint 2: redact validated page text via a provider.

All case data access goes through tools/dao.py. The default provider is the
offline ``local-llm`` adapter; it requires a preloaded Ollama model on a
loopback-only daemon and never falls back to an external provider.

Usage:
    python tools/redact_document.py CASE_ID DOC_ID \
        --held-by document-pipeline --run-id RUN_ID \
        --provider local-llm --model MODEL
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from llm_providers import (
    ProviderConfig,
    ProviderConfigError,
    ProviderExecutionError,
    SUPPORTED_PROVIDERS,
    build_provider,
)


ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"
PROMPT_VERSION = "pii_redaction_v0.1"
PROMPT_TEMPLATE = """Redact personally identifying information from the validated OCR text below.

Replace each detected value with one of these stable placeholders:
[PERSON_NAME], [RESIDENT_REGISTRATION_NUMBER], [PHONE_NUMBER], [ADDRESS],
[EMAIL], [ACCOUNT_NUMBER], [VEHICLE_NUMBER], or [OTHER_PII].

Do not summarize, translate, correct, reorder, or omit non-PII content. Preserve
all line breaks and all medical, insurance, date, amount, diagnosis, and policy
content exactly unless the value itself is PII. Do not treat a value as safe
merely because it looks like a sample, alias, or pseudonym. A value is already
redacted only when it is one of the bracketed placeholders listed above.

For example, "홍길동 / 010-1234-5678 / 골절" must become
"[PERSON_NAME] / [PHONE_NUMBER] / 골절" with items_redacted=2.

Reply with ONLY one JSON object in this exact shape:
{{"redacted_text":"...", "items_redacted":0,
  "categories":["person_name"]}}

--- Validated page text ---
{text}
"""


def _dao(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(DAO), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"dao.py {' '.join(args[:2])} failed: {detail}")
    return result.stdout


def _parse_redaction(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ProviderExecutionError(f"redaction response was not JSON: {raw!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ProviderExecutionError(f"redaction response was invalid JSON: {raw!r}") from exc
    if not isinstance(parsed.get("redacted_text"), str):
        raise ProviderExecutionError("redaction response omitted string redacted_text")
    if not isinstance(parsed.get("items_redacted"), int) or parsed["items_redacted"] < 0:
        raise ProviderExecutionError("redaction response items_redacted must be a non-negative integer")
    categories = parsed.get("categories", [])
    if not isinstance(categories, list) or any(not isinstance(item, str) for item in categories):
        raise ProviderExecutionError("redaction response categories must be an array of strings")
    parsed["categories"] = categories
    return parsed


def redact_document(case_id: str, doc_id: str, held_by: str, run_id: str, provider) -> dict:
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
    for page in ocr_result.get("pages", []):
        page_number = page["page"]
        text = _dao("read-page-text", case_id, doc_id, str(page_number))
        prompt = PROMPT_TEMPLATE.format(text=text)
        result = provider.redact_text(prompt, PROMPT_VERSION)
        parsed = _parse_redaction(result.text)
        redacted_pages.append(f"<<<PAGE page={page_number}>>>\n{parsed['redacted_text']}")
        total_items += parsed["items_redacted"]
        categories.update(parsed["categories"])
        provider_metadata = result.metadata()

    if not redacted_pages:
        raise RuntimeError(f"checkpoint 2 blocked: {doc_id} has no validated pages")

    redacted_text = "\n".join(redacted_pages) + "\n"
    redacted_path = f"data/processed/{case_id}/{doc_id}/redacted_text.md"
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": "document-pipeline",
        "status": "success",
        "model_info": {
            "model_name": f"{provider.provider_name}:{provider.model_name}",
            "prompt_version": PROMPT_VERSION,
        },
        "method": "local_llm_offline" if provider.provider_name == "local-llm" else provider.provider_name,
        "document_id": doc_id,
        "redacted_text_path": redacted_path,
        "items_redacted": total_items,
        "categories": sorted(categories),
        "review_required": False,
        "warnings": [],
    }
    if provider_metadata:
        contract["warnings"] = [
            "Provider execution metadata is recorded in model_info; source text was accessed only through dao.py."
        ]

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
        "provider": f"{provider.provider_name}:{provider.model_name}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("doc_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS,
                        default=os.environ.get("HARNESS_REDACTION_PROVIDER", "local-llm"))
    parser.add_argument("--model", default=os.environ.get("HARNESS_REDACTION_MODEL"))
    args = parser.parse_args()

    try:
        provider = build_provider(ProviderConfig(args.provider, args.model), env=os.environ, root=ROOT)
        result = redact_document(args.case_id, args.doc_id, args.held_by, args.run_id, provider)
    except (ProviderConfigError, ProviderExecutionError, RuntimeError) as exc:
        sys.exit(f"error: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
