"""Semantic contracts for medical structuring and compatibility projection.

JSON Schema owns object shape. This module owns cross-record identity, containment,
source resolvability, configuration, and deterministic index invariants that JSON
Schema cannot express. It performs no medical inference.
"""
from __future__ import annotations

from collections import Counter
import json
from typing import Iterable

from _validation import load_registry, validate_instance


def _duplicates(values: Iterable[str]) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _schema_errors(data: dict, schema_name: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(data, schema_name, schemas, registry)


def _timeline_key(observation: dict) -> tuple[str, str]:
    observed_at = observation["observed_at"]
    kind = observed_at["kind"]
    if kind == "point":
        return observed_at["value"], observation["observation_id"]
    if kind == "range":
        return observed_at["start"]["value"], observation["observation_id"]
    return "9999-99-99", observation["observation_id"]


def build_timeline_index(data: dict) -> list[str]:
    """Return observation IDs in stable chronological order without copying facts."""
    observations = [
        observation
        for variable in data.get("variables", [])
        for observation in variable.get("observations", [])
    ]
    return [observation["observation_id"] for observation in sorted(observations, key=_timeline_key)]


def validate_medical_variables(
    data: dict,
    *,
    manifest: dict,
    page_chunks: dict,
    conflict_ledger: dict,
    config: dict,
    canonical_case_type: str | None = None,
) -> list[str]:
    """Return deterministic, human-readable semantic validation errors."""
    errors = _schema_errors(data, "medical_variables.schema.json")
    errors.extend(f"config: {error}" for error in _schema_errors(config, "medical_structuring_config.schema.json"))
    if errors:
        return sorted(set(errors))

    case_id = data["case_id"]
    for label, owner in (("manifest", manifest), ("page_chunks", page_chunks), ("conflict_ledger", conflict_ledger)):
        if owner.get("case_id") != case_id:
            errors.append(f"{label} case_id {owner.get('case_id')!r} does not match {case_id}")

    if not config["behavior_enabled"] or config["approval"] is None:
        errors.append("medical structuring behavior is disabled or lacks approval metadata")
    if data["config_version"] != config["config_version"]:
        errors.append(
            f"config_version {data['config_version']!r} does not match loaded {config['config_version']!r}"
        )
    if data["status"] != "success":
        errors.append("medical variables status must be success for canonical publication")
    if data["case_type"] not in config["enabled_case_types"]:
        errors.append(
            f"case type {data['case_type']!r} is not enabled by medical configuration"
        )
    if canonical_case_type is None:
        errors.append("canonical case type dependency is unavailable")
    elif data["case_type"] != canonical_case_type:
        errors.append(
            "medical case_type does not match the canonical case classification"
        )

    configured_domains = {entry["code"]: entry for entry in config["domains"]}
    configured_kinds = {entry["code"]: entry for entry in config["variable_kinds"]}
    configured_units = set(config["units"])
    for duplicate in _duplicates(entry["code"] for entry in config["domains"]):
        errors.append(f"duplicate configured domain code {duplicate}")
    for duplicate in _duplicates(entry["code"] for entry in config["variable_kinds"]):
        errors.append(f"duplicate configured variable kind {duplicate}")
    for duplicate in _duplicates(config["units"]):
        errors.append(f"duplicate configured unit {duplicate}")
    for duplicate in _duplicates(entry["rule_id"] for entry in config["quantity_rules"]):
        errors.append(f"duplicate configured quantity rule {duplicate}")
    for rule in config["quantity_rules"]:
        if rule["variable_kind"] not in configured_kinds:
            errors.append(f"quantity rule {rule['rule_id']} uses an unconfigured variable kind")
        for unit in rule["allowed_units"]:
            if unit not in configured_units:
                errors.append(f"quantity rule {rule['rule_id']} uses unconfigured unit {unit!r}")

    domains = {entry["domain_id"]: entry for entry in data["domains"]}
    for duplicate in _duplicates(entry["domain_id"] for entry in data["domains"]):
        errors.append(f"duplicate domain_id {duplicate}")
    for domain in data["domains"]:
        configured = configured_domains.get(domain["domain_code"])
        if configured is None:
            errors.append(f"domain code {domain['domain_code']} is not configured")
        if domain["domain_id"] != f"MD_{domain['domain_code']}":
            errors.append(f"domain_id {domain['domain_id']} does not match domain_code {domain['domain_code']}")
        if domain["config_version"] != config["config_version"]:
            errors.append(f"domain {domain['domain_id']} uses a different config_version")

    variables = {entry["variable_id"]: entry for entry in data["variables"]}
    for duplicate in _duplicates(entry["variable_id"] for entry in data["variables"]):
        errors.append(f"duplicate variable_id {duplicate}")

    observation_rows: list[tuple[dict, dict]] = []
    for variable in data["variables"]:
        if variable["domain_id"] not in domains:
            errors.append(f"variable {variable['variable_id']} references unknown domain {variable['domain_id']}")
        configured_kind = configured_kinds.get(variable["variable_kind"])
        if configured_kind is None:
            errors.append(f"variable kind {variable['variable_kind']} is not configured")
        elif variable["domain_id"] in domains and configured_kind["domain_code"] != domains[variable["domain_id"]]["domain_code"]:
            errors.append(f"variable {variable['variable_id']} kind/domain configuration does not match")
        for related_id in variable["related_variable_ids"]:
            if related_id == variable["variable_id"]:
                errors.append(f"variable {variable['variable_id']} cannot relate to itself")
            elif related_id not in variables:
                errors.append(f"variable {variable['variable_id']} references unknown related variable {related_id}")
        for observation in variable["observations"]:
            value = observation.get("numeric_value") or observation.get("quantity_value")
            if value and value.get("unit_status") == "normalized" and value.get("unit_code") not in configured_units:
                errors.append(
                    f"observation {observation['observation_id']} uses unconfigured normalized unit {value.get('unit_code')!r}"
                )
        observation_rows.extend((variable, observation) for observation in variable["observations"])

    observations = {observation["observation_id"]: observation for _, observation in observation_rows}
    for duplicate in _duplicates(observation["observation_id"] for _, observation in observation_rows):
        errors.append(f"duplicate observation_id {duplicate}")

    manifest_docs = {entry["document_id"]: entry for entry in manifest.get("documents", [])}
    chunks = {entry["chunk_id"]: entry for entry in page_chunks.get("chunks", [])}
    locator_ids: list[str] = []
    for variable, observation in observation_rows:
        for locator in observation["evidence"]:
            locator_ids.append(locator["locator_id"])
            document_id = locator["document_id"]
            document = manifest_docs.get(document_id)
            if document is None:
                errors.append(f"locator {locator['locator_id']} references unknown document {document_id}")
                continue
            if document.get("downstream_disposition") == "expert_review_only":
                errors.append(f"locator {locator['locator_id']} consumes expert_review_only document {document_id}")
            pages = document.get("pages")
            if pages is None or locator["page"] > pages:
                errors.append(f"locator {locator['locator_id']} references unknown page {locator['page']} in {document_id}")
            chunk_id = locator.get("chunk_id")
            candidate_chunks = []
            if chunk_id:
                chunk = chunks.get(chunk_id)
                if chunk is None:
                    errors.append(f"locator {locator['locator_id']} references unknown chunk {chunk_id}")
                else:
                    candidate_chunks = [chunk]
            else:
                candidate_chunks = [
                    chunk for chunk in chunks.values()
                    if chunk["document_id"] == document_id
                    and chunk["page_start"] <= locator["page"] <= chunk["page_end"]
                ]
            if not candidate_chunks:
                errors.append(
                    f"locator {locator['locator_id']} does not resolve to a validated chunk"
                )
            for chunk in candidate_chunks:
                if chunk["document_id"] != document_id or not chunk["page_start"] <= locator["page"] <= chunk["page_end"]:
                    errors.append(f"locator {locator['locator_id']} page/document does not belong to chunk {chunk['chunk_id']}")
            if "quote" in locator and candidate_chunks and not any(locator["quote"] in chunk["text"] for chunk in candidate_chunks):
                errors.append(f"locator {locator['locator_id']} quote is not present in its validated chunk")
    for duplicate in _duplicates(locator_ids):
        errors.append(f"duplicate evidence locator_id {duplicate}")

    coverage_rows = data["source_coverage"]
    for duplicate in _duplicates(entry["document_id"] for entry in coverage_rows):
        errors.append(f"duplicate source coverage for {duplicate}")
    coverage = {entry["document_id"]: entry for entry in coverage_rows}
    if set(coverage) != set(manifest_docs):
        for missing in sorted(set(manifest_docs) - set(coverage)):
            errors.append(f"source coverage missing document {missing}")
        for unknown in sorted(set(coverage) - set(manifest_docs)):
            errors.append(f"source coverage references unknown document {unknown}")
    enabled_roles = set(config["enabled_document_roles"])
    for document_id, document in manifest_docs.items():
        entry = coverage.get(document_id)
        if entry is None:
            continue
        role = document.get("document_type") or document.get("pre_flagged_type") or "unknown"
        if entry["document_role"] != role:
            errors.append(f"source coverage role for {document_id} does not match manifest role {role}")
        if document.get("downstream_disposition") == "expert_review_only":
            expected = "human_only_not_consumed"
        elif role not in enabled_roles:
            expected = "unsupported_needs_configuration"
        elif document.get("ocr_status") != "completed" or document.get("cross_validation_status") not in {"agreed", "disagreed_resolved"}:
            expected = "blocked_upstream"
        else:
            expected = "consumed"
        if entry["coverage_status"] != expected:
            errors.append(
                f"source coverage {document_id} is {entry['coverage_status']}, expected {expected} from canonical inputs"
            )

    issues = {entry["issue_id"]: entry for entry in data["medical_issues"]}
    for duplicate in _duplicates(entry["issue_id"] for entry in data["medical_issues"]):
        errors.append(f"duplicate issue_id {duplicate}")
    issue_link_ids: list[str] = []
    for issue in data["medical_issues"]:
        if issue["config_version"] != config["config_version"]:
            errors.append(f"issue {issue['issue_id']} uses a different config_version")
        for link in issue["evidence_links"]:
            issue_link_ids.append(link["issue_evidence_link_id"])
            if link["observation_id"] not in observations:
                errors.append(f"issue {issue['issue_id']} references unknown observation {link['observation_id']}")
    for duplicate in _duplicates(issue_link_ids):
        errors.append(f"duplicate issue_evidence_link_id {duplicate}")

    importance_ids: list[str] = []
    for assignment in data["importance_assignments"]:
        importance_ids.append(assignment["importance_id"])
        if assignment["issue_id"] not in issues:
            errors.append(f"importance {assignment['importance_id']} references unknown issue {assignment['issue_id']}")
        if "variable_id" in assignment and assignment["variable_id"] not in variables:
            errors.append(f"importance {assignment['importance_id']} references unknown variable {assignment['variable_id']}")
        if "observation_id" in assignment and assignment["observation_id"] not in observations:
            errors.append(f"importance {assignment['importance_id']} references unknown observation {assignment['observation_id']}")
        if assignment["config_version"] != config["config_version"]:
            errors.append(f"importance {assignment['importance_id']} uses a different config_version")
    for duplicate in _duplicates(importance_ids):
        errors.append(f"duplicate importance_id {duplicate}")

    conflicts = {entry.get("conflict_id"): entry for entry in conflict_ledger.get("conflicts", [])}
    contradiction_ids: list[str] = []
    for group in data["contradiction_groups"]:
        contradiction_ids.append(group["contradiction_group_id"])
        for observation_id in group["observation_ids"]:
            observation = observations.get(observation_id)
            if observation is None:
                errors.append(f"contradiction {group['contradiction_group_id']} references unknown observation {observation_id}")
            elif observation["value_state"] != "asserted":
                errors.append(f"contradiction {group['contradiction_group_id']} includes non-asserted observation {observation_id}")
        if group["conflict_id"] not in conflicts:
            errors.append(f"contradiction {group['contradiction_group_id']} references unknown P6 conflict {group['conflict_id']}")
    for duplicate in _duplicates(contradiction_ids):
        errors.append(f"duplicate contradiction_group_id {duplicate}")

    quantity_ids: list[str] = []
    quantity_rules: dict[str, list[dict]] = {}
    for rule in config["quantity_rules"]:
        quantity_rules.setdefault(rule["variable_kind"], []).append(rule)
    consumed_docs = {document_id for document_id, entry in coverage.items() if entry["coverage_status"] == "consumed"}
    for summary in data["quantity_summaries"]:
        quantity_ids.append(summary["quantity_id"])
        if summary["variable_id"] not in variables:
            errors.append(f"quantity {summary['quantity_id']} references unknown variable {summary['variable_id']}")
        else:
            variable_kind = variables[summary["variable_id"]]["variable_kind"]
            matching_rules = [
                rule for rule in quantity_rules.get(variable_kind, [])
                if rule["counting_basis"] == summary["counting_basis"]
                and (
                    summary["unit_status"] != "normalized"
                    or summary.get("unit_code") in rule["allowed_units"]
                )
            ]
            if len(matching_rules) != 1:
                errors.append(
                    f"quantity {summary['quantity_id']} does not match exactly one approved counting rule"
                )
        source_docs: set[str] = set()
        source_observations: list[dict] = []
        for observation_id in summary["source_observation_ids"]:
            observation = observations.get(observation_id)
            if observation is None:
                errors.append(f"quantity {summary['quantity_id']} references unknown observation {observation_id}")
            else:
                source_observations.append(observation)
                source_docs.update(locator["document_id"] for locator in observation["evidence"])
        quantity_values = [
            observation.get("quantity_value")
            for observation in source_observations
        ]
        typed_quantity_values = [
            value for value in quantity_values if isinstance(value, dict)
        ]
        if source_observations and len(typed_quantity_values) == len(
            source_observations
        ):
            if any(
                value["counting_basis"] != summary["counting_basis"]
                or value["unit_status"] != summary["unit_status"]
                or value.get("unit_code") != summary.get("unit_code")
                or value.get("raw_unit") != summary.get("raw_unit")
                for value in typed_quantity_values
            ):
                errors.append(
                    f"quantity {summary['quantity_id']} source quantities do not match its counting basis/unit"
                )
            source_count = sum(
                value["count"] for value in typed_quantity_values
            )
            if summary["count"] != source_count:
                errors.append(
                    f"quantity {summary['quantity_id']} count {summary['count']} "
                    f"does not equal source observation count {source_count}"
                )
        if summary["coverage_status"] == "complete" and set(manifest_docs) - consumed_docs:
            errors.append(f"quantity {summary['quantity_id']} cannot claim complete coverage while case source coverage is incomplete")
        if summary["unit_status"] == "unmapped" and len(source_docs) > 1:
            errors.append(f"quantity {summary['quantity_id']} cannot combine unmapped units across sources")
    for duplicate in _duplicates(quantity_ids):
        errors.append(f"duplicate quantity_id {duplicate}")

    expected_timeline = build_timeline_index(data)
    if data["timeline_observation_ids"] != expected_timeline:
        errors.append("timeline_observation_ids is not the deterministic chronological observation index")

    return sorted(set(errors))


def project_legacy_medical_fields(data: dict, *, projection_config: dict) -> dict:
    """Build deterministic legacy fields from canonical observations.

    The projection configuration owns every selected field and compatibility
    confidence. Multiple distinct candidates fail closed; exact duplicate values
    merge provenance without creating a second medical truth source.
    """
    config_errors = _schema_errors(
        projection_config, "medical_projection_config.schema.json"
    )
    if config_errors:
        raise ValueError("invalid medical projection configuration: " + "; ".join(config_errors))
    if not projection_config["projection_enabled"] or projection_config["approval"] is None:
        raise ValueError("medical projection is disabled or lacks approval metadata")

    def value_at(observation: dict, path: str):
        value = observation
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                raise ValueError(
                    f"projection path {path!r} is unavailable on {observation['observation_id']}"
                )
            value = value[part]
        return value

    projected: dict[str, dict] = {}
    for rule in sorted(projection_config["field_rules"], key=lambda item: item["field_name"]):
        candidates: list[tuple[object, dict, dict]] = []
        for variable in data.get("variables", []):
            if variable["variable_kind"] != rule["variable_kind"]:
                continue
            for observation in variable["observations"]:
                if observation["value_state"] != "asserted" or observation.get("value_type") != rule["value_type"]:
                    continue
                candidates.append((value_at(observation, rule["value_path"]), variable, observation))
        if not candidates:
            continue

        grouped: dict[str, list[tuple[object, dict, dict]]] = {}
        for candidate in candidates:
            key = json.dumps(candidate[0], ensure_ascii=False, sort_keys=True)
            grouped.setdefault(key, []).append(candidate)
        if len(grouped) != 1:
            raise ValueError(
                f"requires_human_projection: field {rule['field_name']} has {len(grouped)} distinct canonical candidates"
            )
        selected = next(iter(grouped.values()))
        value = selected[0][0]
        if any(
            "quote" not in locator
            for _, _, observation in selected
            for locator in observation["evidence"]
        ):
            continue
        evidence_references: list[dict] = []
        seen_evidence: set[str] = set()
        for _, _, observation in selected:
            for locator in observation["evidence"]:
                evidence = {
                    "document_id": locator["document_id"],
                    "page": locator["page"],
                    "quote": locator["quote"],
                }
                key = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                if key not in seen_evidence:
                    seen_evidence.add(key)
                    evidence_references.append(evidence)
        field = {
            "value": value,
            "confidence": rule["compatibility_confidence"],
            "evidence_references": evidence_references,
            "review_required": rule["review_required"],
            "provenance_mode": "canonical_medical_projection",
            "source_variable_ids": sorted({variable["variable_id"] for _, variable, _ in selected}),
            "source_observation_ids": sorted({observation["observation_id"] for _, _, observation in selected}),
            "projection_rule_id": rule["rule_id"],
            "projection_config_version": projection_config["config_version"],
        }
        if rule["review_required"]:
            field["reviewer_role"] = rule["reviewer_role"]
        projected[rule["field_name"]] = field
    return projected


_MEDICAL_REVIEW_TRANSITIONS: dict[str, set[str]] = {
    "open": {"none"},
    "record_decision": {"decision_pending"},
    "provide_information": {"needs_information"},
    "assign": {"package_ready"},
    "request_information": {"awaiting_expert"},
    "supplement_package": {"expert_needs_information"},
    "reassign": {"awaiting_expert"},
    "submit_response": {"awaiting_expert"},
    "amend_response": {"answered", "closed"},
    "withdraw_response": {"answered", "closed"},
    "flag_conflict": {"answered"},
    "adjudicate": {"adjudication_required"},
    "cancel": {
        "decision_pending", "needs_information", "package_ready",
        "awaiting_expert", "expert_needs_information", "adjudication_required",
    },
    "close": {"answered", "cancelled"},
    "reopen": {"do_not_refer", "cancelled", "closed"},
}


def transition_allowed(
    current_state: str,
    action: str,
    *,
    actor: dict,
    role_policy: dict,
    has_current_response: bool,
    has_pending_adjudication: bool,
) -> bool:
    """Apply the closed lifecycle and enabled named-actor permission gate."""
    if _schema_errors(role_policy, "medical_review_role_config.schema.json"):
        return False
    actor_ids = [item["actor_id"] for item in role_policy["named_actors"]]
    action_keys = [item["action"] for item in role_policy["action_permissions"]]
    if (
        len(actor_ids) != len(set(actor_ids))
        or len(action_keys) != len(set(action_keys))
    ):
        return False
    if not role_policy["operations_enabled"] or role_policy["approval"] is None:
        return False
    if current_state not in _MEDICAL_REVIEW_TRANSITIONS.get(action, set()):
        return False
    actor_fields = {
        "actor_id", "display_name", "declared_role", "specialty_code",
        "attested_at", "assertion_source", "operator_actor_id",
        "operator_policy_version", "authentication_method",
    }
    if (
        set(actor) != actor_fields
        or actor["assertion_source"] != "authenticated_operator_policy"
        or actor["authentication_method"] != "bearer_sha256_policy"
    ):
        return False
    named = next(
        (record for record in role_policy["named_actors"]
         if record["actor_id"] == actor["actor_id"]),
        None,
    )
    if named is None or any(
        named[field] != actor[field]
        for field in ("display_name", "declared_role", "specialty_code")
    ):
        return False
    permission = next(
        (item for item in role_policy["action_permissions"] if item["action"] == action),
        None,
    )
    if permission is None or actor["declared_role"] not in permission["allowed_roles"]:
        return False
    if (
        action in {"close", "amend_response", "withdraw_response"}
        and current_state in {"answered", "closed"}
        and not has_current_response
    ):
        return False
    if action == "close" and has_pending_adjudication:
        return False
    return True