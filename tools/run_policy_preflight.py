"""Prepare policy UID verification before a policy-stage attempt opens.

This is deliberately not a policy interpretation driver.  It only performs the
already-authorized DAO sequence ``record-source-digest`` then
``enable-canonical-uids`` for in-scope, noncanonical policy documents.  The
orchestrator still owns all stage-attempt markers and dispatches the policy
agent only after this command succeeds.
"""
from __future__ import annotations

import argparse
import json
import sys

import dao

TEXT_PROCESSED = frozenset({"automated_text_pipeline", "text_only_no_normalization"})

def _require_success(result: dict, operation: str, doc_id: str) -> None:
    """Fail closed using a DAO helper result, without parsing CLI output."""
    if not result["success"]:
        detail = "\n".join(result.get("messages", ()))
        raise RuntimeError(f"{operation} failed for {doc_id}: {detail}")


def _in_scope_doc_ids(manifest: dict, revision_index: dict) -> tuple[list[str], list[str]]:
    schemes = {item.get("document_id"): item.get("uid_scheme", "unregistered")
               for item in revision_index.get("documents", [])}
    all_policy = sorted(
        doc["document_id"] for doc in manifest.get("documents", [])
        if doc.get("document_type") == "insurance_policy"
        and doc.get("downstream_disposition") in TEXT_PROCESSED
    )
    return ([doc_id for doc_id in all_policy if schemes.get(doc_id) != "canonical_v1"],
            [doc_id for doc_id in all_policy if schemes.get(doc_id) == "canonical_v1"])


def run(case_id: str, held_by: str, run_id: str) -> dict:
    # Serialize the *whole* preflight against attempt-open, rather than merely
    # checking state before issuing independent DAO writes.  update-run-state
    # takes this same lock, so another orchestrator cannot open the policy
    # attempt between the guard below and a UID promotion that would invalidate
    # its policy snapshot.
    target = dao.run_state_path(case_id)
    existing = dao.acquire_lock_blocking(
        target, held_by, run_id, "policy UID preflight before attempt open"
    )
    if existing is not None:
        raise RuntimeError("BLOCKED: run-state lock is held; policy UID preflight "
                           "cannot establish an attempt-safe boundary")
    try:
        state = dao.read_contract_data(case_id, "_run_state.json") or {}
        policy = next((stage for stage in state.get("stages", [])
                       if stage.get("stage_name") == "policy_clause_processing"), None)
        if policy and policy.get("status") == "in_progress":
            raise RuntimeError("BLOCKED: policy_clause_processing is already in_progress; "
                               "UID preflight must finish before its attempt opens")

        manifest = dao.read_contract_data(case_id, "document_manifest.json")
        if manifest is None:
            raise RuntimeError("BLOCKED: document_manifest.json does not exist")
        revision_index = dao.load_revision_index(case_id)
        selected, already_canonical = _in_scope_doc_ids(manifest, revision_index)
        if not selected:
            return {"status": "noop", "promoted_document_ids": [],
                    "already_canonical_document_ids": already_canonical,
                    "policy_stage_status": (policy or {}).get("status", "absent")}

        for doc_id in selected:
            _require_success(
                dao.record_source_digest(case_id, doc_id, held_by, run_id),
                "record-source-digest", doc_id)
            _require_success(
                dao.enable_canonical_uids(
                    case_id, doc_id, held_by, run_id,
                    run_state_lock_already_held=True),
                "enable-canonical-uids", doc_id)

        return {"status": "promoted", "promoted_document_ids": selected,
                "already_canonical_document_ids": already_canonical,
                "policy_stage_status": (policy or {}).get("status", "absent")}
    finally:
        dao.release_lock(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args.case_id, args.held_by, args.run_id), ensure_ascii=False))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
