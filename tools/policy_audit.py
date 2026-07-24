"""Cross-contract checks for version-bound policy extraction audits."""
from __future__ import annotations

import re


AUDIT_SCHEMA = "policy_audit_result.schema.json"
_DOC_ID_RE = re.compile(r"_(DOC_\d+)\.json$")


def doc_id_from_filename(filename: str) -> str | None:
    match = _DOC_ID_RE.search(filename)
    return match.group(1) if match else None


def check_policy_audit(
        data: dict,
        filename: str,
        expected_hashes: dict[str, str | None],
        normalized: dict | None,
        inventory: dict | None,
        reference_table: dict | None) -> list[str]:
    errors = []
    target_doc = doc_id_from_filename(filename)
    if target_doc is None:
        errors.append(
            "audit filename must be policy_audit_result_<DOC_ID>.json")
    if data.get("source_document_id") != target_doc:
        errors.append("source_document_id does not match audit filename")
    for field, expected in expected_hashes.items():
        if data.get(field) != expected:
            errors.append(
                f"{field} is stale or incorrect: audit={data.get(field)!r}, "
                f"current={expected!r}")

    clause_uids = {
        clause.get("clause_uid")
        for clause in (normalized or {}).get("clauses", [])}
    condition_uids = {
        item.get("condition_uid")
        for clause in (normalized or {}).get("clauses", [])
        for bucket, items in clause.items()
        if isinstance(items, list)
        for item in items
        if isinstance(item, dict) and "condition_uid" in item}
    boundary_uids = {
        boundary.get("boundary_uid")
        for boundary in (inventory or {}).get("boundaries", [])}
    table_uids = {
        table.get("table_uid")
        for table in (reference_table or {}).get("tables", [])}

    # Deterministic defects are discovered from the bound artifacts themselves;
    # the audit author cannot make them disappear by submitting findings=[].
    boundary_to_clauses = {}
    for boundary in (inventory or {}).get("boundaries", []):
        boundary_uid = boundary.get("boundary_uid")
        for mapping in boundary.get("normalized_mappings") or []:
            clause_uid = mapping.get("clause_uid")
            if boundary_uid and clause_uid:
                boundary_to_clauses.setdefault(
                    boundary_uid, set()).add(clause_uid)
        if boundary.get("disposition") in (
                "review_required", "extraction_failed"):
            errors.append(
                f"deterministic audit: boundary {boundary_uid!r} remains "
                f"{boundary.get('disposition')}")

    for clause_index, clause in enumerate(
            (normalized or {}).get("clauses", [])):
        clause_uid = clause.get("clause_uid")
        if clause.get("review_required") is True:
            errors.append(
                f"deterministic audit: clause {clause_uid!r} remains "
                "review_required")
        for boundary_uid in clause.get("source_boundary_uids") or []:
            if boundary_uid not in boundary_uids:
                errors.append(
                    f"deterministic audit: clauses[{clause_index}] source "
                    f"boundary {boundary_uid!r} does not exist")
            elif clause_uid not in boundary_to_clauses.get(
                    boundary_uid, set()):
                errors.append(
                    f"deterministic audit: boundary {boundary_uid!r} does not "
                    f"map back to clause {clause_uid!r}")
        for bucket, items in clause.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and \
                        item.get("condition_uid") and \
                        item.get("review_required") is True:
                    errors.append(
                        "deterministic audit: condition "
                        f"{item.get('condition_uid')!r} in {bucket} remains "
                        "review_required")

    for table in (reference_table or {}).get("tables", []):
        if table.get("review_required") is True:
            errors.append(
                f"deterministic audit: table {table.get('table_uid')!r} "
                "remains review_required")
        for row in table.get("rows") or []:
            for cell in row.get("cells") or []:
                if cell.get("review_required") is True:
                    errors.append(
                        f"deterministic audit: cell "
                        f"{cell.get('cell_uid')!r} remains review_required")

    seen = set()
    for index, finding in enumerate(data.get("findings") or []):
        uid = finding.get("finding_uid")
        if uid in seen:
            errors.append(f"findings[{index}]: duplicate finding_uid {uid!r}")
        if uid:
            seen.add(uid)
        for field, available in (
            ("clause_uid", clause_uids),
            ("condition_uid", condition_uids),
            ("boundary_uid", boundary_uids),
            ("table_uid", table_uids),
        ):
            value = finding.get(field)
            if value is not None and value not in available:
                errors.append(
                    f"findings[{index}].{field} {value!r} does not resolve")
    return errors


def unresolved_findings(data: dict) -> list[str]:
    return [
        f"{finding.get('finding_uid')} [{finding.get('severity')}]: "
        f"{finding.get('description')}"
        for finding in data.get("findings", [])
        if finding.get("status") == "open"
    ]
