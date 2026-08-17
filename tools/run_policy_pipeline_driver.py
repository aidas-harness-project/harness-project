"""Safe policy-stage driver for the valid zero-normalization route.

Policy normalization is opt-in.  When every active policy document is
``text_only_no_normalization``, there is no semantic extraction to delegate to
an agent; this command records the exact manifest fingerprint and returns a
no-op result for the orchestrator to finalize through its existing DAO gate.
It intentionally blocks if a normalized policy document exists: canonical
boundary/table/clause generation is not approximated by this driver.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

import driver_runtime


ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"
STAGE, UNIT = "policy_clause_processing", "zero_normalization"
VERSION = "policy_zero_normalization_driver.v0.1"
TEXT_DISPOSITIONS = frozenset({"automated_text_pipeline", "text_only_no_normalization"})


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _dao_json(args: list[str], *, allow_missing: bool = False) -> dict[str, Any] | None:
    proc = subprocess.run([sys.executable, str(DAO), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        if allow_missing and "NOT_FOUND:" in (proc.stdout or ""):
            return None
        raise RuntimeError((proc.stdout or proc.stderr or "DAO command failed").strip())
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DAO command returned non-JSON output: {args[0]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"DAO command returned a non-object: {args[0]}")
    return parsed


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


def policy_documents(manifest: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    """Return (text-only policies, normalization-required policies)."""
    active = [doc for doc in manifest.get("documents", []) if isinstance(doc, dict)
              and doc.get("document_type") == "insurance_policy"
              and doc.get("downstream_disposition") in TEXT_DISPOSITIONS]
    text_only = sorted((doc for doc in active if doc["downstream_disposition"] == "text_only_no_normalization"),
                       key=lambda item: item["document_id"])
    normalized = sorted((doc for doc in active if doc["downstream_disposition"] == "automated_text_pipeline"),
                        key=lambda item: item["document_id"])
    return text_only, normalized


def _build_document_index(case_id: str, held_by: str, run_id: str) -> dict:
    """Refresh the derived clause/table index, reporting rather than raising.

    Failure here must not fail the stage. The index is advisory -- no contract
    references it, no gate reads it, and an agent without one falls back to
    the chunk scan it did before. Letting a table detector crash take down a
    policy stage that has no stake in the outcome would trade a real
    completion for an optional convenience.

    The reason it lives in this driver rather than in Stage 2: the index is
    about POLICY documents, this is the stage that owns them, and Stage 2 is
    already at 92% busy-union occupancy where the ~10s/document table scan
    would be pure addition. Here it lands in a stage measured at 6.9s whose
    only other work is recording a manifest fingerprint.
    """
    proc = subprocess.run(
        [sys.executable, str(DAO), "build-document-index", case_id,
         "--held-by", held_by, "--run-id", run_id],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        return {"status": "failed",
                "detail": (proc.stdout or proc.stderr or "").strip()[:200]}
    return {"status": "built", "detail": (proc.stdout or "").strip()[-120:]}


def run(*, case_id: str, held_by: str, run_id: str, prompt_version: str = VERSION) -> dict:
    state = _dao_json(["read-contract", case_id, "_run_state.json", "--run-id", run_id])
    if not any(item.get("stage_name") == STAGE and item.get("status") == "in_progress"
               for item in state.get("stages", [])):
        raise RuntimeError("BLOCKED: policy_clause_processing must be in_progress; the orchestrator owns attempt state")
    with driver_runtime.driver_span(case_id, run_id, "dao_content_read", unit_id=UNIT):
        manifest = _dao_json(["read-contract", case_id, "document_manifest.json", "--run-id", run_id])
        text_only, normalized = policy_documents(manifest)
    if not text_only and not normalized:
        raise RuntimeError("BLOCKED: no active text-processed insurance policy is available")
    if normalized:
        # Retired 2026-08-15, so this is now unreachable for any case intaken
        # after 2026-08-04 (`default_disposition` classifies every policy
        # document `text_only_no_normalization`). It still fires for the four
        # legacy cases whose manifests record the old value, and it must stay
        # a hard block rather than a silent pass: those documents were marked
        # as owing a clause contract, and quietly finalizing over that would
        # repeat CASE_112's `manual_override` -- a stage reading `passed` with
        # the obligation neither met nor withdrawn.
        ids = ", ".join(doc["document_id"] for doc in normalized)
        raise RuntimeError("BLOCKED: clause normalization is retired (2026-08-15) and no stage "
                           "produces a clause contract, but these documents are still recorded "
                           f"as owing one: {ids}. Re-classify them text_only_no_normalization "
                           "before re-running, rather than finalizing over the discrepancy.")
    # Build the navigation index before the receipt-reuse branch below, not
    # after. A resumed run takes that branch and returns early, so an index
    # built after it would be skipped on exactly the runs most likely to be
    # missing one -- and because the index is advisory, nothing downstream
    # would report its absence. Rebuilding is cheap and idempotent: it is
    # derived from the same processed text and registered PDFs the receipt
    # already fingerprints.
    index_summary = _build_document_index(case_id, held_by, run_id)

    digests = {"document_manifest": _digest(manifest)}
    receipt = _dao_json(["read-driver-receipt", case_id, "--stage", STAGE,
                         "--unit-id", UNIT, "--run-id", run_id], allow_missing=True)
    if receipt and driver_runtime.receipt_matches(
        receipt, input_digests=digests, prompt_version=prompt_version,
        response_schema_version=VERSION, provider_name="deterministic", model_name="none",
    ):
        return {"status": "reused", "text_only_policy_count": len(text_only),
                "document_index": index_summary}
    with driver_runtime.driver_span(case_id, run_id, "input_snapshot", unit_id=UNIT, items=len(text_only)):
        receipt_data = driver_runtime.make_receipt(
            case_id=case_id, run_id=run_id, stage=STAGE, unit_id=UNIT,
            input_digests=digests, prompt_version=prompt_version,
            response_schema_version=VERSION, provider_name="deterministic", model_name="none",
            completed_contracts=[],
        )
    receipt_file = _temp_json(receipt_data)
    try:
        with driver_runtime.driver_span(case_id, run_id, "dao_publish", unit_id=UNIT):
            _dao_write(["write-driver-receipt", case_id, "--stage", STAGE, "--unit-id", UNIT,
                        "--data-file", str(receipt_file), "--held-by", held_by, "--run-id", run_id])
    finally:
        receipt_file.unlink(missing_ok=True)
    return {"status": "noop", "text_only_policy_count": len(text_only),
            "document_index": index_summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--prompt-version", default=VERSION)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(case_id=args.case_id, held_by=args.held_by, run_id=args.run_id,
                             prompt_version=args.prompt_version), ensure_ascii=False))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
