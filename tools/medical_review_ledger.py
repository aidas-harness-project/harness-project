"""Canonical medical-review ledger owner for the reconstructed harness.

This module contains only the canonical storage/validation and clearance seam until
later reconstruction phases earn lifecycle transitions.  It receives the DAO module
as its filesystem, locking, and atomic-I/O boundary; it never resolves project data
paths independently.
"""
from __future__ import annotations

from collections import Counter
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from _validation import load_registry, validate_instance
from medical_contracts import transition_allowed
import operator_auth


_EFFECTIVE_UID = os.geteuid() if hasattr(os, "geteuid") else None
_INGRESS_OWNER_TOKEN = (
    str(_EFFECTIVE_UID)
    if _EFFECTIVE_UID is not None
    else hashlib.sha256(getpass.getuser().encode("utf-8")).hexdigest()[:16]
)
MEDICAL_REVIEW_INGRESS_ROOT = Path(
    os.environ.get(
        "AIDAS_MEDICAL_REVIEW_INGRESS_ROOT",
        str(Path(tempfile.gettempdir()) / f"aidas-medical-review-ingress-{_INGRESS_OWNER_TOKEN}"),
    )
)
MAX_SUBMISSION_BYTES = 256 * 1024


BLOCKING_STATES = {
    "decision_pending",
    "needs_information",
    "package_ready",
    "awaiting_expert",
    "expert_needs_information",
    "answered",
    "adjudication_required",
}

_EVENT_DESTINATIONS = {
    "open": {"decision_pending"},
    "record_decision": {"do_not_refer", "needs_information", "package_ready"},
    "provide_information": {"decision_pending"},
    "assign": {"awaiting_expert"},
    "request_information": {"expert_needs_information"},
    "supplement_package": {"package_ready"},
    "reassign": {"awaiting_expert"},
    "submit_response": {"answered"},
    "amend_response": {"answered"},
    "withdraw_response": {"awaiting_expert"},
    "flag_conflict": {"adjudication_required"},
    "adjudicate": {"answered"},
    "cancel": {"cancelled"},
    "close": {"closed"},
    "reopen": {"decision_pending"},
}

_EVENT_SOURCES = {
    "open": {None},
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


def ledger_path(dao, case_id: str) -> Path:
    return dao.case_dir(case_id) / "_medical_review_ledger.json"


def empty_ledger(dao, case_id: str) -> dict:
    return {
        "case_id": case_id,
        "schema_version": "medical_review_ledger.v0.1",
        "next_review_item_number": 1,
        "next_request_number": 1,
        "next_human_input_number": 1,
        "next_adjudication_number": 1,
        "next_event_number": 1,
        "updated_at": dao.now_iso(),
        "review_items": [],
        "wait_episodes": [],
        "adjudications": [],
        "events": [],
    }


def validate_ledger_semantics(ledger: dict, case_id: str) -> list[str]:
    errors: list[str] = []
    if ledger.get("case_id") != case_id:
        errors.append("ledger case_id does not match the requested case")

    items = ledger.get("review_items", [])
    item_ids = [item.get("review_item_id") for item in items]
    if len(item_ids) != len(set(item_ids)):
        errors.append("duplicate review_item_id")
    expected_item_ids = [
        f"MRI_{number:04d}" for number in range(1, len(items) + 1)
    ]
    if item_ids != expected_item_ids:
        errors.append("medical review item IDs are not contiguous")
    if ledger.get("next_review_item_number") != len(items) + 1:
        errors.append("next medical review item number is inconsistent")
    item_by_id = {
        item["review_item_id"]: item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("review_item_id"), str)
    }
    all_request_ids: list[str] = []

    event_ids = [event.get("event_id") for event in ledger.get("events", [])]
    if len(event_ids) != len(set(event_ids)):
        errors.append("duplicate medical review event_id")
    expected_event_ids = [
        f"MRE_{number:06d}" for number in range(1, len(event_ids) + 1)
    ]
    if event_ids != expected_event_ids:
        errors.append("medical review event IDs are not contiguous")
    if ledger.get("next_event_number") != len(event_ids) + 1:
        errors.append("next medical review event number is inconsistent")
    for event in ledger.get("events", []):
        if event.get("review_item_id") not in item_by_id:
            errors.append(
                f"{event.get('event_id')} references an unknown review item"
            )
    operations: dict[str, list[dict]] = {}
    for event in ledger.get("events", []):
        operation_values = (
            event.get("operation_id"),
            event.get("operation_request_sha256"),
            event.get("operation_result"),
        )
        if not all(value is not None for value in operation_values):
            errors.append(
                f"{event.get('event_id')} has incomplete operation metadata"
            )
            continue
        operations.setdefault(event["operation_id"], []).append(event)
    for operation_id, operation_events in operations.items():
        request_shas = {
            event.get("operation_request_sha256")
            for event in operation_events
        }
        results = [event.get("operation_result") for event in operation_events]
        if len(request_shas) != 1 or any(
            result != results[0] for result in results[1:]
        ):
            errors.append(
                f"operation_id {operation_id} maps to inconsistent committed requests"
            )
            continue
        result = results[0]
        if not isinstance(result, dict):
            errors.append(f"operation_id {operation_id} has no committed result")
            continue
        destination_states = {
            event.get("to_state") for event in operation_events
        }
        if result.get("state") not in destination_states or len(destination_states) != 1:
            errors.append(
                f"operation_id {operation_id} result does not match its destination state"
            )
        if set(result) != {"action", "review_item_id", "state", "decision_id"}:
            errors.append(
                f"operation_id {operation_id} has an incomplete committed result"
            )
            continue
        result_item_id = result["review_item_id"]
        if result_item_id not in {
            event.get("review_item_id") for event in operation_events
        }:
            errors.append(
                f"operation_id {operation_id} result names a foreign review item"
            )
        result_action = result["action"]
        if result_action not in {
            event.get("action") for event in operation_events
        }:
            errors.append(
                f"operation_id {operation_id} result names a foreign action"
            )
        event_decision_ids = {
            event.get("decision_id") for event in operation_events
        }
        if result["decision_id"] not in event_decision_ids:
            errors.append(
                f"operation_id {operation_id} result names a foreign decision"
            )
    wait_ids = [wait.get("human_input_id") for wait in ledger.get("wait_episodes", [])]
    if len(wait_ids) != len(set(wait_ids)):
        errors.append("duplicate medical review human_input_id")
    expected_wait_ids = [
        f"MRH_{number:06d}" for number in range(1, len(wait_ids) + 1)
    ]
    if wait_ids != expected_wait_ids:
        errors.append("medical review human-input IDs are not contiguous")
    if ledger.get("next_human_input_number") != len(wait_ids) + 1:
        errors.append("next medical review human-input number is inconsistent")

    for item in items:
        request_records = item.get("requests", [])
        request_ids = [record.get("request_id") for record in request_records]
        all_request_ids.extend(request_ids)
        if len(request_ids) != len(set(request_ids)):
            errors.append(f"{item['review_item_id']} has duplicate request records")
        for record in request_records:
            versions = record.get("versions", [])
            expected_versions = list(range(1, len(versions) + 1))
            if [version.get("request_version") for version in versions] != expected_versions:
                errors.append(
                    f"{record.get('request_id', '<unknown>')} request versions are not contiguous"
                )
            if any(
                version.get("request_id") != record.get("request_id")
                for version in versions
            ):
                errors.append(
                    f"{record.get('request_id', '<unknown>')} contains a foreign request version"
                )
            request_id_value = record.get("request_id", "<unknown>")
            assignments = record.get("assignments", [])
            assignment_ids = [row.get("assignment_id") for row in assignments]
            if len(assignment_ids) != len(set(assignment_ids)):
                errors.append(f"{request_id_value} has duplicate assignment IDs")
            assignment_history_events = [
                event
                for event in ledger.get("events", [])
                if event.get("review_item_id") == item.get("review_item_id")
            ]
            assignment_by_id = {
                row.get("assignment_id"): row for row in assignments
            }
            version_numbers = {
                version.get("request_version") for version in versions
            }
            for assignment_index, assignment in enumerate(assignments, start=1):
                if assignment.get("request_id") != request_id_value:
                    errors.append(f"{request_id_value} has a foreign assignment")
                if assignment.get("assignment_id") != (
                    f"{request_id_value}-A{assignment_index:02d}"
                ):
                    errors.append(
                        f"{assignment.get('assignment_id')} is not derived from its request"
                    )
                if assignment.get("request_version") not in version_numbers:
                    errors.append(
                        f"{assignment.get('assignment_id')} targets an unknown request version"
                    )
                if assignment.get("reviewer", {}).get("declared_role") != "medical_reviewer":
                    errors.append(
                        f"{assignment.get('assignment_id')} targets a non-reviewer actor"
                    )
                if assignment.get("assignment_event_id") not in event_ids:
                    errors.append(
                        f"{assignment.get('assignment_id')} has no assignment event"
                    )
                else:
                    assignment_event = next(
                        event for event in ledger.get("events", [])
                        if event.get("event_id") == assignment.get("assignment_event_id")
                    )
                    assignment_snapshot = assignment_event.get(
                        "role_policy_snapshot", {}
                    )
                    snapshot_reviewers = [
                        row
                        for row in assignment_snapshot.get("named_actors", [])
                        if row.get("actor_id")
                        == assignment.get("reviewer", {}).get("actor_id")
                    ]
                    assignment_event_positions = [
                        position
                        for position, candidate_event in enumerate(
                            assignment_history_events
                        )
                        if candidate_event is assignment_event
                    ]
                    event_time_request_version = (
                        1 + sum(
                            prior_event.get("action") == "supplement_package"
                            and prior_event.get("request_id") == request_id_value
                            for prior_event in assignment_history_events[
                                :assignment_event_positions[0]
                            ]
                        )
                        if len(assignment_event_positions) == 1
                        else None
                    )
                    if (
                        assignment_event.get("action") not in {"assign", "reassign"}
                        or assignment_event.get("review_item_id")
                        != item.get("review_item_id")
                        or assignment_event.get("assignment_id")
                        != assignment.get("assignment_id")
                        or assignment_event.get("actor", {}).get("actor_id")
                        != assignment.get("assigned_by_actor_id")
                        or assignment_event.get("created_at")
                        != assignment.get("assigned_at")
                        or assignment_snapshot.get("config_version")
                        != assignment.get("role_policy_version")
                        or assignment.get("package_review_attestation", {}).get(
                            "reviewed_by_actor_id"
                        )
                        != assignment.get("assigned_by_actor_id")
                        or assignment.get("request_version")
                        != event_time_request_version
                        or len(snapshot_reviewers) != 1
                        or snapshot_reviewers[0] != assignment.get("reviewer")
                    ):
                        errors.append(
                            f"{assignment.get('assignment_id')} assignment event binding is invalid"
                        )
            current_assignment_id = record.get("current_assignment_id")
            if (
                current_assignment_id is not None
                and current_assignment_id not in assignment_by_id
            ):
                errors.append(f"{request_id_value} has a dangling assignment head")
            if (
                item.get("state") == "closed"
                and current_assignment_id is not None
            ):
                errors.append(
                    f"{request_id_value} closed has an active assignment"
                )

            responses = record.get("responses", [])
            response_history_events = [
                event
                for event in ledger.get("events", [])
                if event.get("review_item_id") == item.get("review_item_id")
            ]
            response_ids = [row.get("response_id") for row in responses]
            if len(response_ids) != len(set(response_ids)):
                errors.append(f"{request_id_value} has duplicate response IDs")
            if [row.get("response_version") for row in responses] != list(
                range(1, len(responses) + 1)
            ):
                errors.append(f"{request_id_value} response versions are not contiguous")
            replay_response_head = None
            for index, response in enumerate(responses):
                assignment = assignment_by_id.get(response.get("assignment_id"))
                if response.get("request_id") != request_id_value:
                    errors.append(f"{request_id_value} has a foreign response")
                if response.get("response_id") != (
                    f"{request_id_value}-R{index + 1:02d}"
                ):
                    errors.append(
                        f"{response.get('response_id')} is not derived from its request"
                    )
                if assignment is None:
                    errors.append(
                        f"{response.get('response_id')} has a dangling assignment"
                    )
                elif response.get("reviewer", {}).get("actor_id") != assignment.get(
                    "reviewer", {}
                ).get("actor_id"):
                    errors.append(
                        f"{response.get('response_id')} reviewer differs from assignment"
                    )
                response_events = [
                    event for event in ledger.get("events", [])
                    if event.get("response_id") == response.get("response_id")
                    and event.get("action")
                    in {"submit_response", "amend_response", "withdraw_response"}
                ]
                expected_supersedes = None
                expected_status = None
                if len(response_events) != 1:
                    errors.append(
                        f"{response.get('response_id')} does not own exactly one response event"
                    )
                else:
                    response_event = response_events[0]
                    response_action = response_event.get("action")
                    response_event_positions = [
                        position
                        for position, candidate_event in enumerate(
                            response_history_events
                        )
                        if candidate_event is response_event
                    ]
                    response_event_index = (
                        response_event_positions[0]
                        if len(response_event_positions) == 1
                        else 0
                    )
                    prior_assignment_ids = [
                        prior_event.get("assignment_id")
                        for prior_event in response_history_events[:response_event_index]
                        if prior_event.get("action") in {"assign", "reassign"}
                        and prior_event.get("assignment_id") in assignment_by_id
                    ]
                    active_assignment_id = (
                        prior_assignment_ids[-1] if prior_assignment_ids else None
                    )
                    authoritative_assignment = assignment_by_id.get(
                        active_assignment_id
                    )
                    authoritative_request = next(
                        (
                            version
                            for version in versions
                            if authoritative_assignment is not None
                            and version.get("request_version")
                            == authoritative_assignment.get("request_version")
                        ),
                        None,
                    )
                    if (
                        response_action
                        not in {"submit_response", "amend_response", "withdraw_response"}
                        or response_event.get("review_item_id")
                        != item.get("review_item_id")
                        or response_event.get("actor") != response.get("reviewer")
                        or response_event.get("created_at")
                        != response.get("submitted_at")
                        or len(response_event_positions) != 1
                    ):
                        errors.append(
                            f"{response.get('response_id')} response event binding is invalid"
                        )
                    if (
                        authoritative_assignment is None
                        or authoritative_request is None
                        or response.get("assignment_id") != active_assignment_id
                        or response.get("request_version_reviewed")
                        != authoritative_assignment.get("request_version")
                        or response.get("issue_id") != item.get("issue_id")
                        or response.get("issue_category")
                        != authoritative_request.get("issue_category")
                        or response.get("decision_id")
                        != authoritative_request.get("decision_id")
                        or response.get("request_config_version_reviewed")
                        != authoritative_request.get("request_config_version")
                        or response.get("referral_policy_version_reviewed")
                        != authoritative_request.get("referral_policy_version")
                        or not set(
                            response.get("evidence_locator_ids_reviewed", [])
                        ).issubset(
                            authoritative_request.get(
                                "included_evidence_locator_ids", []
                            )
                        )
                    ):
                        errors.append(
                            f"{response.get('response_id')} authoritative request binding is invalid"
                        )
                    if response_action == "submit_response":
                        expected_supersedes = None
                        expected_status = "completed"
                        replay_response_head = response.get("response_id")
                    elif response_action == "amend_response":
                        expected_supersedes = replay_response_head
                        expected_status = "amended"
                        replay_response_head = response.get("response_id")
                    else:
                        expected_supersedes = replay_response_head
                        expected_status = "withdrawn"
                        replay_response_head = None
                if (
                    response.get("supersedes_response_id") != expected_supersedes
                    or response.get("response_status") != expected_status
                ):
                    errors.append(
                        f"{response.get('response_id')} breaks the response supersession chain"
                    )
            current_response_id = record.get("current_response_id")
            if current_response_id is not None and current_response_id not in response_ids:
                errors.append(f"{request_id_value} has a dangling response head")
            last_response_event_index = max(
                (
                    event_index
                    for event_index, event in enumerate(response_history_events)
                    if event.get("response_id") in response_ids
                ),
                default=-1,
            )
            last_cancel_event_index = max(
                (
                    event_index
                    for event_index, event in enumerate(response_history_events)
                    if event.get("action") == "cancel"
                ),
                default=-1,
            )
            response_head_cleared_by_cancel = (
                last_cancel_event_index > last_response_event_index
            )
            expected_record_head = (
                replay_response_head
                if (
                    item.get("current_request_id") == request_id_value
                    and item.get("state")
                    in {"answered", "adjudication_required", "closed"}
                    and not response_head_cleared_by_cancel
                )
                else None
            )
            if current_response_id != expected_record_head:
                errors.append(
                    f"{request_id_value} response head differs from event replay"
                )
        current_record = next(
            (
                record
                for record in request_records
                if record.get("request_id") == item.get("current_request_id")
            ),
            None,
        )
        if current_record is not None:
            if item.get("state") == "package_ready" and (
                current_record.get("current_assignment_id") is not None
                or current_record.get("current_response_id") is not None
            ):
                errors.append(
                    f"{item['review_item_id']} package_ready has an active assignment/response"
                )
            if item.get("state") == "awaiting_expert" and (
                current_record.get("current_assignment_id") is None
                or current_record.get("current_response_id") is not None
            ):
                errors.append(
                    f"{item['review_item_id']} awaiting_expert has invalid heads"
                )
            if item.get("state") == "answered" and (
                current_record.get("current_assignment_id") is None
                or current_record.get("current_response_id") is None
            ):
                errors.append(f"{item['review_item_id']} answered has invalid heads")
        request_id = item.get("current_request_id")
        if request_id and sum(
            request.get("request_id") == request_id
            for request in request_records
        ) != 1:
            errors.append(f"{item['review_item_id']} has a dangling current_request_id")
        reinspections = item.get("source_reinspection_records", [])
        reinspection_ids = [
            record.get("source_reinspection_id") for record in reinspections
        ]
        if len(reinspection_ids) != len(set(reinspection_ids)):
            errors.append(
                f"{item['review_item_id']} has duplicate source reinspection IDs"
            )
        referenced_reinspection_ids = {
            source_id
            for record in request_records
            for version in record.get("versions", [])
            for source_id in version.get("source_reinspection_ids", [])
        } | {
            decision.get("referral_inputs", {}).get("source_reinspection_id")
            for decision in item.get("decisions", [])
        }
        referenced_reinspection_ids.discard(None)
        for source_id in referenced_reinspection_ids:
            if sum(
                record.get("source_reinspection_id") == source_id
                for record in reinspections
            ) != 1:
                errors.append(
                    f"{source_id} does not resolve to exactly one source reinspection"
                )
        for reinspection in reinspections:
            source_id = reinspection.get("source_reinspection_id")
            if (
                reinspection.get("performed") is not True
                or source_id not in referenced_reinspection_ids
            ):
                errors.append(
                    f"{source_id} is not a completed referenced source reinspection"
                )
        item_events = [
            event
            for event in ledger.get("events", [])
            if event.get("review_item_id") == item.get("review_item_id")
        ]
        if not item_events:
            errors.append(
                f"{item.get('review_item_id', '<unknown>')} has no lifecycle events"
            )
        else:
            source_event_owners: dict[str, set[str]] = {}

            def own_sources(owner_event: dict, source_ids: list[str]) -> None:
                event_id = owner_event.get("event_id")
                if not isinstance(event_id, str):
                    return
                for source_id in source_ids:
                    source_event_owners.setdefault(source_id, set()).add(event_id)

            for decision_index, decision in enumerate(
                item.get("decisions", []),
                start=1,
            ):
                expected_decision_id = (
                    f"{item.get('review_item_id')}-D{decision_index:02d}"
                )
                expected_referral_input_id = (
                    f"{item.get('review_item_id')}-I{decision_index:02d}"
                )
                if (
                    decision.get("decision_id") != expected_decision_id
                    or decision.get("decision_version") != decision_index
                    or decision.get("referral_inputs", {}).get(
                        "referral_input_id"
                    )
                    != expected_referral_input_id
                ):
                    errors.append(
                        f"{decision.get('decision_id')} decision identity is not derived from its item"
                    )
                decision_events = [
                    event
                    for event in item_events
                    if event.get("action") == "record_decision"
                    and event.get("decision_id") == decision.get("decision_id")
                ]
                if len(decision_events) != 1:
                    errors.append(
                        f"{decision.get('decision_id')} does not own exactly one decision event"
                    )
                else:
                    source_id = decision.get("referral_inputs", {}).get(
                        "source_reinspection_id"
                    )
                    if isinstance(source_id, str):
                        own_sources(decision_events[0], [source_id])
            for source_index, reinspection in enumerate(reinspections, start=1):
                if reinspection.get("source_reinspection_id") != (
                    f"{item.get('review_item_id')}-S{source_index:02d}"
                ):
                    errors.append(
                        f"{reinspection.get('source_reinspection_id')} source identity is not derived from its item"
                    )
            for record in request_records:
                request_id_value = record.get("request_id")
                versions = record.get("versions", [])
                initial_events = [
                    event
                    for event in item_events
                    if event.get("action") == "record_decision"
                    and event.get("request_id") == request_id_value
                ]
                if not versions or len(initial_events) != 1:
                    errors.append(
                        f"{request_id_value} initial version does not own exactly one decision event"
                    )
                else:
                    own_sources(
                        initial_events[0],
                        versions[0].get("source_reinspection_ids", []),
                    )
                supplement_events = [
                    event
                    for event in item_events
                    if event.get("action") == "supplement_package"
                    and event.get("request_id") == request_id_value
                ]
                if len(supplement_events) != max(0, len(versions) - 1):
                    errors.append(
                        f"{request_id_value} supplemental versions do not have exact event ownership"
                    )
                for version, owner_event in zip(
                    versions[1:],
                    supplement_events,
                    strict=False,
                ):
                    own_sources(
                        owner_event,
                        version.get("source_reinspection_ids", []),
                    )
            for reinspection in reinspections:
                source_id = reinspection.get("source_reinspection_id")
                if len(source_event_owners.get(source_id, set())) != 1:
                    errors.append(
                        f"{source_id} does not own exactly one lifecycle event"
                    )
            if item.get("decision_owner") != "human":
                errors.append(
                    f"{item.get('review_item_id')} has unsupported decision ownership"
                )
            replay_state = None
            replay_request_id = None
            replay_assignment_heads = {
                record.get("request_id"): None for record in request_records
            }
            for index, event in enumerate(item_events):
                action = event.get("action")
                if event.get("actor") is None:
                    errors.append(f"{event.get('event_id')} has no authenticated actor")
                snapshot = event.get("role_policy_snapshot")
                actor = event.get("actor")
                if not isinstance(snapshot, dict):
                    errors.append(
                        f"{event.get('event_id')} has no role-policy snapshot"
                    )
                else:
                    actor_ids = [
                        row.get("actor_id")
                        for row in snapshot.get("named_actors", [])
                    ]
                    action_keys = [
                        row.get("action")
                        for row in snapshot.get("action_permissions", [])
                    ]
                    if (
                        snapshot.get("operations_enabled") is not True
                        or not isinstance(snapshot.get("approval"), dict)
                        or len(actor_ids) != len(set(actor_ids))
                        or len(action_keys) != len(set(action_keys))
                    ):
                        errors.append(
                            f"{event.get('event_id')} has an invalid role-policy snapshot"
                        )
                    if isinstance(actor, dict):
                        named = [
                            row
                            for row in snapshot.get("named_actors", [])
                            if row.get("actor_id") == actor.get("actor_id")
                        ]
                        permissions = [
                            row
                            for row in snapshot.get("action_permissions", [])
                            if row.get("action") == action
                        ]
                        if (
                            len(named) != 1
                            or named[0].get("display_name")
                            != actor.get("display_name")
                            or named[0].get("declared_role")
                            != actor.get("declared_role")
                            or named[0].get("specialty_code")
                            != actor.get("specialty_code")
                            or named[0].get("operator_actor_id")
                            != actor.get("operator_actor_id")
                            or len(permissions) != 1
                            or actor.get("declared_role")
                            not in permissions[0].get("allowed_roles", [])
                        ):
                            errors.append(
                                f"{event.get('event_id')} actor is not authorized "
                                "by its role-policy snapshot"
                            )
                if index == 0 and action != "open":
                    errors.append(
                        f"{item.get('review_item_id')} does not begin with open"
                    )
                if event.get("from_state") != replay_state:
                    errors.append(
                        f"{event.get('event_id')} breaks the lifecycle state chain"
                    )
                if (
                    event.get("from_state") not in _EVENT_SOURCES.get(action, set())
                    or event.get("to_state")
                    not in _EVENT_DESTINATIONS.get(action, set())
                ):
                    errors.append(
                        f"{event.get('event_id')} is not an allowed lifecycle transition"
                    )
                if action == "record_decision":
                    matching_decisions = [
                        decision
                        for decision in item.get("decisions", [])
                        if decision.get("decision_id") == event.get("decision_id")
                    ]
                    event_request_id = event.get("request_id")
                    matching_requests = [
                        record
                        for record in request_records
                        if record.get("request_id") == event_request_id
                    ]
                    if len(matching_decisions) != 1:
                        errors.append(
                            f"{event.get('event_id')} has an invalid decision binding"
                        )
                    else:
                        decision = matching_decisions[0]
                        outcome = decision.get("decision")
                        source_id = decision.get("referral_inputs", {}).get(
                            "source_reinspection_id"
                        )
                        matching_reinspections = [
                            record
                            for record in reinspections
                            if record.get("source_reinspection_id") == source_id
                        ]
                        if (
                            item.get("decision_owner") != "human"
                            or decision.get("decision_origin")
                            != "authorized_human_override"
                            or decision.get("actor") != actor
                            or decision.get("policy_version") is not None
                            or decision.get("issue_id") != item.get("issue_id")
                            or decision.get("created_at") != event.get("created_at")
                            or decision.get("referral_inputs", {}).get(
                                "medical_variables_revision"
                            )
                            != event.get("medical_variables_revision")
                            or len(matching_reinspections) != 1
                        ):
                            errors.append(
                                f"{event.get('event_id')} decision authority binding differs"
                            )
                        elif (
                            matching_reinspections[0].get("performed") is not True
                            or matching_reinspections[0].get("actor_or_process")
                            != actor.get("actor_id")
                            or matching_reinspections[0].get("performed_at")
                            != event.get("created_at")
                            or matching_reinspections[0].get(
                                "medical_variables_revision"
                            )
                            != event.get("medical_variables_revision")
                        ):
                            errors.append(
                                f"{event.get('event_id')} source reinspection binding differs"
                            )
                        if event_request_id is not None and (
                            len(matching_requests) != 1
                            or not matching_requests[0].get("versions")
                            or matching_requests[0]["versions"][0].get(
                                "source_reinspection_ids"
                            )
                            != [source_id]
                            or matching_requests[0]["versions"][0].get("created_at")
                            != event.get("created_at")
                            or matching_requests[0]["versions"][0].get(
                                "medical_variables_revision"
                            )
                            != event.get("medical_variables_revision")
                        ):
                            errors.append(
                                f"{event.get('event_id')} initial request source binding differs"
                            )
                        if (outcome == "refer") != (event_request_id is not None):
                            errors.append(
                                f"{event.get('event_id')} has an invalid request outcome binding"
                            )
                    if event_request_id is not None and (
                        len(matching_requests) != 1
                        or not matching_requests[0].get("versions")
                        or matching_requests[0]["versions"][0].get("decision_id")
                        != event.get("decision_id")
                    ):
                        errors.append(
                            f"{event.get('event_id')} has an invalid request binding"
                        )
                    elif (
                        event_request_id is not None
                        and len(matching_decisions) == 1
                        and any(
                            version.get("decision_id") != event.get("decision_id")
                            or version.get("referral_policy_version")
                            != matching_decisions[0].get("policy_version")
                            for version in matching_requests[0].get("versions", [])
                        )
                    ):
                        errors.append(
                            f"{event.get('event_id')} request policy binding differs"
                        )
                    replay_request_id = event_request_id
                elif action in {"provide_information", "reopen"}:
                    replay_request_id = None
                    for key in replay_assignment_heads:
                        replay_assignment_heads[key] = None
                elif action in {"cancel", "close"}:
                    for key in replay_assignment_heads:
                        replay_assignment_heads[key] = None
                elif action in {"amend_response", "withdraw_response"}:
                    event_response_id = event.get("response_id")
                    response_owners = [
                        (
                            record.get("request_id"),
                            response.get("assignment_id"),
                        )
                        for record in request_records
                        for response in record.get("responses", [])
                        if response.get("response_id") == event_response_id
                    ]
                    if (
                        len(response_owners) != 1
                        or response_owners[0][0] != replay_request_id
                    ):
                        errors.append(
                            f"{event.get('event_id')} has an invalid response assignment binding"
                        )
                    else:
                        replay_assignment_heads[response_owners[0][0]] = (
                            response_owners[0][1]
                        )
                elif action == "supplement_package":
                    event_request_id = event.get("request_id")
                    matching_request_records = [
                        record
                        for record in request_records
                        if record.get("request_id") == event_request_id
                    ]
                    prior_supplements = sum(
                        prior_event.get("action") == "supplement_package"
                        and prior_event.get("request_id") == event_request_id
                        for prior_event in item_events[:index]
                    )
                    expected_version_number = 2 + prior_supplements
                    matching_versions = [
                        version
                        for record in matching_request_records
                        for version in record.get("versions", [])
                        if version.get("request_version")
                        == expected_version_number
                    ]
                    if (
                        event_request_id != replay_request_id
                        or len(matching_request_records) != 1
                        or len(matching_versions) != 1
                    ):
                        errors.append(
                            f"{event.get('event_id')} has an invalid supplement request binding"
                        )
                    else:
                        supplement_version = matching_versions[0]
                        source_ids = supplement_version.get(
                            "source_reinspection_ids", []
                        )
                        matching_reinspections = [
                            reinspection
                            for reinspection in reinspections
                            if reinspection.get("source_reinspection_id") in source_ids
                        ]
                        if (
                            len(source_ids) != 1
                            or len(matching_reinspections) != 1
                            or supplement_version.get("created_at")
                            != event.get("created_at")
                            or supplement_version.get("medical_variables_revision")
                            != event.get("medical_variables_revision")
                        ):
                            errors.append(
                                f"{event.get('event_id')} supplement version binding differs"
                            )
                        else:
                            supplement_reinspection = matching_reinspections[0]
                            if (
                                supplement_reinspection.get("performed") is not True
                                or supplement_reinspection.get("actor_or_process")
                                != event.get("actor", {}).get("actor_id")
                                or supplement_reinspection.get("performed_at")
                                != event.get("created_at")
                                or supplement_reinspection.get(
                                    "medical_variables_revision"
                                )
                                != event.get("medical_variables_revision")
                            ):
                                errors.append(
                                    f"{event.get('event_id')} supplement reinspection binding differs"
                                )
                    if replay_request_id in replay_assignment_heads:
                        replay_assignment_heads[replay_request_id] = None
                if action in {"assign", "reassign"}:
                    assignment_id = event.get("assignment_id")
                    assignment_owners = [
                        record.get("request_id")
                        for record in request_records
                        if any(
                            assignment.get("assignment_id") == assignment_id
                            for assignment in record.get("assignments", [])
                        )
                    ]
                    if (
                        len(assignment_owners) != 1
                        or assignment_owners[0] != replay_request_id
                    ):
                        errors.append(
                            f"{event.get('event_id')} has an invalid assignment binding"
                        )
                    else:
                        replay_assignment_heads[assignment_owners[0]] = assignment_id
                replay_state = event.get("to_state")
            if replay_state != item.get("state"):
                errors.append(
                    f"{item.get('review_item_id')} state does not match replayed history"
                )
            if replay_request_id != item.get("current_request_id"):
                errors.append(
                    f"{item.get('review_item_id')} request head differs from event replay"
                )
            for record in request_records:
                request_id_value = record.get("request_id")
                expected_assignment_id = (
                    replay_assignment_heads.get(request_id_value)
                    if replay_request_id == request_id_value
                    else None
                )
                if record.get("current_assignment_id") != expected_assignment_id:
                    errors.append(
                        f"{request_id_value} assignment head differs from event replay"
                    )

    if len(all_request_ids) != len(set(all_request_ids)):
        errors.append("duplicate medical review request_id across review items")
    expected_request_ids = [
        f"MRR_{number:04d}" for number in range(1, len(all_request_ids) + 1)
    ]
    if sorted(all_request_ids) != expected_request_ids:
        errors.append("medical review request IDs are not contiguous")
    if ledger.get("next_request_number") != len(all_request_ids) + 1:
        errors.append("next medical review request number is inconsistent")

    response_owner: dict[str, str] = {}
    for candidate in items:
        for request in candidate.get("requests", []):
            for response in request.get("responses", []):
                response_id = response.get("response_id")
                if isinstance(response_id, str):
                    response_owner[response_id] = candidate.get("review_item_id")

    adjudications = ledger.get("adjudications", [])
    adjudication_ids = [row.get("adjudication_id") for row in adjudications]
    expected_adjudication_ids = [
        f"MRA_{number:06d}" for number in range(1, len(adjudications) + 1)
    ]
    if adjudication_ids != expected_adjudication_ids:
        errors.append("medical adjudication IDs are not contiguous")
    if ledger.get("next_adjudication_number") != len(adjudications) + 1:
        errors.append("next medical adjudication number is inconsistent")
    for adjudication in adjudications:
        adjudication_id = adjudication.get("adjudication_id")
        review_item_ids = adjudication.get("review_item_ids", [])
        response_ids = adjudication.get("response_ids", [])
        participants = [item_by_id.get(value) for value in review_item_ids]
        if (
            len(review_item_ids) < 2
            or len(review_item_ids) != len(set(review_item_ids))
            or len(response_ids) != len(review_item_ids)
        ):
            errors.append(f"{adjudication_id} has invalid participant membership")
        if any(participant is None for participant in participants):
            errors.append(f"{adjudication_id} references an unknown review item")
        elif len({
            participant.get("issue_id")
            for participant in participants
            if participant is not None
        }) != 1:
            errors.append(f"{adjudication_id} mixes medical issues")
        if any(
            response_owner.get(response_id) != review_item_id
            for review_item_id, response_id in zip(review_item_ids, response_ids)
        ):
            errors.append(f"{adjudication_id} has invalid response ownership")
        conflict_events = [
            event for event in ledger.get("events", [])
            if event.get("action") == "flag_conflict"
            and event.get("adjudication_id") == adjudication_id
        ]
        if {
            (event.get("review_item_id"), event.get("response_id"))
            for event in conflict_events
        } != set(zip(review_item_ids, response_ids)):
            errors.append(f"{adjudication_id} has invalid conflict event binding")
        shared_waits = [
            wait for wait in ledger.get("wait_episodes", [])
            if wait.get("adjudication_id") == adjudication_id
            and wait.get("review_item_id") is None
        ]
        if len(shared_waits) != 1:
            errors.append(f"{adjudication_id} does not own exactly one shared wait")
            shared_wait = None
        else:
            shared_wait = shared_waits[0]
            if set(shared_wait.get("related_review_item_ids", [])) != set(review_item_ids):
                errors.append(f"{adjudication_id} shared wait membership differs")
        if adjudication.get("status") == "pending":
            if any(
                participant is None
                or participant.get("state") != "adjudication_required"
                or participant.get("current_adjudication_id") != adjudication_id
                for participant in participants
            ):
                errors.append(f"{adjudication_id} pending participant state is invalid")
            if shared_wait is None or shared_wait.get("status") != "waiting":
                errors.append(f"{adjudication_id} pending shared wait is invalid")
        elif adjudication.get("status") == "resolved":
            adjudicate_events = [
                event for event in ledger.get("events", [])
                if event.get("action") == "adjudicate"
                and event.get("adjudication_id") == adjudication_id
            ]
            if {event.get("review_item_id") for event in adjudicate_events} != set(
                review_item_ids
            ):
                errors.append(f"{adjudication_id} has invalid resolution event binding")
            if any(
                event.get("actor") != adjudication.get("adjudicator")
                or event.get("created_at") != adjudication.get("resolved_at")
                for event in adjudicate_events
            ):
                errors.append(f"{adjudication_id} resolution event metadata differs")
            if any(
                participant is None
                or participant.get("current_adjudication_id") == adjudication_id
                for participant in participants
            ):
                errors.append(f"{adjudication_id} resolved participant pointer is invalid")
            if (
                shared_wait is None
                or shared_wait.get("status") != "received"
                or shared_wait.get("resolution") != "adjudicated"
                or shared_wait.get("resolved_at") != adjudication.get("resolved_at")
            ):
                errors.append(f"{adjudication_id} resolved shared wait is invalid")
        elif adjudication.get("status") == "cancelled":
            cancel_events = [
                event
                for event in ledger.get("events", [])
                if event.get("action") == "cancel"
                and event.get("adjudication_id") == adjudication_id
            ]
            if {event.get("review_item_id") for event in cancel_events} != set(
                review_item_ids
            ):
                errors.append(
                    f"{adjudication_id} has invalid cancellation event binding"
                )
            if (
                adjudication.get("adjudicator") is not None
                or adjudication.get("record") is not None
                or any(
                    event.get("created_at") != adjudication.get("resolved_at")
                    for event in cancel_events
                )
            ):
                errors.append(
                    f"{adjudication_id} cancellation event metadata differs"
                )
            if any(
                participant is None
                or participant.get("current_adjudication_id") == adjudication_id
                for participant in participants
            ):
                errors.append(
                    f"{adjudication_id} cancelled participant pointer is invalid"
                )
            if (
                shared_wait is None
                or shared_wait.get("status") != "no_longer_required"
                or shared_wait.get("resolution") != "cancelled"
                or shared_wait.get("resolved_at") != adjudication.get("resolved_at")
            ):
                errors.append(
                    f"{adjudication_id} cancelled shared wait is invalid"
                )

    expected_wait_identities: list[tuple] = []
    decision_cycles = {item_id: 0 for item_id in item_by_id}
    replay_request_heads = {item_id: None for item_id in item_by_id}

    def expect_item_wait(
        event: dict,
        input_kind: str,
        request_id: str | None,
        wait_cycle: int,
    ) -> None:
        expected_wait_identities.append((
            input_kind,
            event.get("review_item_id"),
            (),
            request_id,
            None,
            wait_cycle,
            event.get("created_at"),
        ))

    for event in ledger.get("events", []):
        item_id = event.get("review_item_id")
        item = item_by_id.get(item_id)
        if item is None:
            continue
        action = event.get("action")
        if action == "open":
            expect_item_wait(event, "referral_decision", None, 1)
        elif action == "record_decision":
            decision_cycles[item_id] += 1
            replay_request_heads[item_id] = event.get("request_id")
            if event.get("to_state") == "needs_information":
                expect_item_wait(
                    event,
                    "medical_evidence",
                    None,
                    decision_cycles[item_id],
                )
            elif event.get("to_state") == "package_ready":
                expect_item_wait(
                    event,
                    "expert_assignment",
                    event.get("request_id"),
                    decision_cycles[item_id],
                )
        elif action in {"provide_information", "reopen"}:
            replay_request_heads[item_id] = None
            expect_item_wait(
                event,
                "referral_decision",
                None,
                decision_cycles[item_id] + 1,
            )
        elif action in {"assign", "reassign"}:
            assignment_id = event.get("assignment_id")
            assignment_requests = [
                record.get("request_id")
                for record in item.get("requests", [])
                if any(
                    assignment.get("assignment_id") == assignment_id
                    for assignment in record.get("assignments", [])
                )
            ]
            request_id = (
                assignment_requests[0]
                if len(assignment_requests) == 1
                else replay_request_heads[item_id]
            )
            replay_request_heads[item_id] = request_id
            expect_item_wait(
                event,
                "expert_response",
                request_id,
                decision_cycles[item_id],
            )
        elif action == "request_information":
            expect_item_wait(
                event,
                "medical_evidence",
                replay_request_heads[item_id],
                decision_cycles[item_id],
            )
        elif action == "supplement_package":
            expect_item_wait(
                event,
                "expert_assignment",
                replay_request_heads[item_id],
                decision_cycles[item_id],
            )
        elif action in {"submit_response", "amend_response"}:
            input_kind = (
                "medical_evidence"
                if event.get("to_state") == "expert_needs_information"
                else "coordinator_disposition"
            )
            if (
                action == "submit_response"
                or input_kind == "medical_evidence"
                or event.get("from_state") == "closed"
            ):
                expect_item_wait(
                    event,
                    input_kind,
                    replay_request_heads[item_id],
                    decision_cycles[item_id],
                )
        elif action == "withdraw_response":
            expect_item_wait(
                event,
                "expert_response",
                replay_request_heads[item_id],
                decision_cycles[item_id],
            )
        elif action == "adjudicate":
            expect_item_wait(
                event,
                "coordinator_disposition",
                replay_request_heads[item_id],
                decision_cycles[item_id],
            )

    expected_wait_identities.extend(
        (
            "medical_adjudication",
            None,
            tuple(adjudication.get("review_item_ids", [])),
            None,
            adjudication.get("adjudication_id"),
            1,
            adjudication.get("created_at"),
        )
        for adjudication in adjudications
    )
    actual_wait_identities = [
        (
            wait.get("input_kind"),
            wait.get("review_item_id"),
            tuple(wait.get("related_review_item_ids", [])),
            wait.get("request_id"),
            wait.get("adjudication_id"),
            wait.get("wait_cycle"),
            wait.get("created_at"),
        )
        for wait in ledger.get("wait_episodes", [])
    ]
    if Counter(actual_wait_identities) != Counter(expected_wait_identities):
        errors.append("medical review wait identities differ from event replay")

    active_wait_keys: set[tuple] = set()
    wait_resolution_rules = {
        "provide_information": ("received", "additional_information_committed"),
        "assign": ("received", "assigned"),
        "request_information": (
            "no_longer_required",
            "expert_requested_information",
        ),
        "supplement_package": ("received", "package_supplemented"),
        "reassign": ("no_longer_required", "reassigned"),
        "submit_response": ("received", "response_submitted"),
        "withdraw_response": ("no_longer_required", "response_withdrawn"),
        "flag_conflict": ("no_longer_required", "adjudication_required"),
        "close": ("received", "closed"),
        "cancel": ("no_longer_required", "cancelled"),
    }
    for wait in ledger.get("wait_episodes", []):
        if wait.get("status") == "waiting":
            key = (
                ("shared", wait.get("adjudication_id"), wait.get("input_kind"))
                if wait.get("review_item_id") is None
                else (
                    "item",
                    wait.get("review_item_id"),
                    wait.get("input_kind"),
                    wait.get("wait_cycle"),
                )
            )
            if key in active_wait_keys:
                errors.append(
                    f"{wait['human_input_id']} duplicates an active medical-review wait identity"
                )
            active_wait_keys.add(key)
        item = item_by_id.get(wait.get("review_item_id"))
        if wait.get("review_item_id") is None:
            related = wait.get("related_review_item_ids", [])
            if (
                wait.get("adjudication_id") is None
                or len(related) < 2
                or any(value not in item_by_id for value in related)
            ):
                errors.append(
                    f"{wait['human_input_id']} has an invalid shared adjudication identity"
                )
            elif wait.get("status") == "waiting" and any(
                item_by_id[value].get("state") != "adjudication_required"
                for value in related
            ):
                errors.append(
                    f"{wait['human_input_id']} remains waiting for a non-blocking adjudication"
                )
        elif item is None:
            errors.append(
                f"{wait['human_input_id']} references an unknown review item"
            )
        elif wait.get("status") != "waiting":
            resolution_events = [
                event
                for event in ledger.get("events", [])
                if event.get("review_item_id") == wait.get("review_item_id")
                and event.get("created_at") == wait.get("resolved_at")
            ]
            resolution_matches = []
            for event in resolution_events:
                action = event.get("action")
                if action == "record_decision":
                    expected = (
                        "received",
                        f"decision:{event.get('to_state')}",
                    )
                    decision = next(
                        (
                            row
                            for row in item.get("decisions", [])
                            if row.get("decision_id") == event.get("decision_id")
                        ),
                        None,
                    )
                    if decision is not None:
                        expected = (
                            "received",
                            f"decision:{decision.get('decision')}",
                        )
                else:
                    expected = wait_resolution_rules.get(action)
                if expected == (wait.get("status"), wait.get("resolution")):
                    resolution_matches.append(event)
            if len(resolution_matches) != 1:
                errors.append(
                    f"{wait['human_input_id']} resolution differs from event replay"
                )
        elif (
            wait.get("status") == "waiting"
            and item.get("state") not in BLOCKING_STATES
        ):
            errors.append(
                f"{wait['human_input_id']} remains waiting for a non-blocking item"
            )
    return errors


def load_ledger(dao, case_id: str, *, allow_initialize: bool = False) -> dict:
    path = ledger_path(dao, case_id)
    if path.is_symlink():
        raise ValueError("medical review ledger may not be a symlink")
    if not path.exists():
        if allow_initialize:
            return empty_ledger(dao, case_id)
        raise ValueError("medical review ledger is missing")
    try:
        ledger = dao.load_json(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("medical review ledger is malformed JSON") from exc
    if not isinstance(ledger, dict):
        raise ValueError("medical review ledger is malformed JSON")

    schemas, registry = load_registry()
    errors = validate_instance(
        ledger,
        "medical_review_ledger.schema.json",
        schemas,
        registry,
    )
    if errors:
        raise ValueError(
            "medical review ledger failed validation: " + "; ".join(errors)
        )
    semantic_errors = validate_ledger_semantics(ledger, case_id)
    if semantic_errors:
        raise ValueError(
            "medical review ledger failed semantic validation: "
            + "; ".join(semantic_errors)
        )
    referenced_revision_shas = {
        revision.get("sha256")
        for item in ledger.get("review_items", [])
        for revision in (
            [
                decision.get("referral_inputs", {}).get(
                    "medical_variables_revision", {}
                )
                for decision in item.get("decisions", [])
            ]
            + [
                reinspection.get("medical_variables_revision", {})
                for reinspection in item.get("source_reinspection_records", [])
            ]
            + [
                version.get("medical_variables_revision", {})
                for request in item.get("requests", [])
                for version in request.get("versions", [])
            ]
        )
        if isinstance(revision, dict)
    } | {
        event.get("medical_variables_revision", {}).get("sha256")
        for event in ledger.get("events", [])
    }
    for revision_sha in referenced_revision_shas:
        if not isinstance(revision_sha, str):
            raise ValueError("medical review ledger has an invalid revision reference")
        _, revision_error = dao._load_medical_revision(case_id, revision_sha)
        if revision_error:
            raise ValueError(
                "medical review ledger references an unavailable immutable revision: "
                f"{revision_error}"
            )
    return ledger


def reconcile_wait_projection(
    dao,
    args,
    *,
    blocking: bool = True,
    automatic: bool = False,
) -> tuple[bool, bool, str | None]:
    """Project canonical waits to run state after the ledger lock is released."""
    run_state_path = dao.run_state_path(args.case_id)
    acquire = (
        dao.acquire_owned_lock_blocking
        if blocking
        else dao.acquire_owned_lock
    )
    owned_lock, existing_lock = acquire(
        run_state_path,
        args.held_by,
        args.run_id,
        "reconcile medical review waits",
    )
    if existing_lock is not None:
        return False, False, f"run-state lock is held: {existing_lock}"
    assert owned_lock is not None
    try:
        ledger = load_ledger(dao, args.case_id)
        variables, revision_error = dao._load_medical_revision(args.case_id, None)
        if (
            revision_error
            or variables is None
            or variables.get("run_id") != args.run_id
        ):
            return (
                False,
                False,
                "wait reconciliation does not match the canonical revision run owner",
            )
        state = dao.validated_run_state(args.case_id)
        if state.get("run_id") not in {None, args.run_id}:
            return False, False, "run state belongs to a different run_id"
        _required_operation_id(args)
        if not automatic and args.operation_id.startswith("medical-projection:"):
            return (
                False,
                False,
                "medical-projection: is reserved for automatic reconciliation",
            )
        ledger_sha256 = hashlib.sha256(
            dao._canonical_json_bytes(ledger)
        ).hexdigest()
        operation_id = (
            f"medical-projection:{ledger_sha256}"
            if automatic
            else args.operation_id
        )
        request_sha256 = hashlib.sha256(dao._canonical_json_bytes(
            dao._reconciliation_request(
                args.case_id,
                args.run_id,
                operation_id,
                ledger_sha256,
            )
        )).hexdigest()
        operations = state.setdefault(
            "medical_review_wait_reconciliation_operations", []
        )
        existing_operation = next(
            (
                operation
                for operation in operations
                if operation.get("operation_id") == operation_id
            ),
            None,
        )
        if existing_operation is not None:
            if existing_operation.get("request_sha256") != request_sha256:
                return (
                    False,
                    False,
                    "operation_id was already committed for a different "
                    "wait-reconciliation request",
                )
            return True, False, None
        generic_entries = [
            entry
            for entry in state.get("human_input_status", [])
            if "human_input_id" not in entry
        ]
        projected_entries = [
            {
                "stage_name": "claim_analysis",
                "status": wait["status"],
                "description": (
                    f"medical review {wait['input_kind']} "
                    f"for {wait['review_item_id'] or wait['adjudication_id']}"
                ),
                "requested_at": wait["created_at"],
                "received_at": wait["resolved_at"],
                "input_kind": wait["input_kind"],
                "human_input_id": wait["human_input_id"],
                "review_item_id": wait["review_item_id"],
                "related_review_item_ids": wait["related_review_item_ids"],
                "request_id": wait["request_id"],
                "adjudication_id": wait["adjudication_id"],
                "wait_cycle": wait["wait_cycle"],
                "resolved_at": wait["resolved_at"],
                "resolution": wait["resolution"],
            }
            for wait in ledger["wait_episodes"]
        ]
        desired_entries = generic_entries + projected_entries
        projection_changed = (
            state.get("run_id") != args.run_id
            or state.get("human_input_status", []) != desired_entries
        )
        state["run_id"] = args.run_id
        state["human_input_status"] = desired_entries
        completed_at = dao.now_iso()
        receipt = {
            "operation_id": operation_id,
            "request_sha256": request_sha256,
            "medical_review_ledger_sha256": ledger_sha256,
            "completed_at": completed_at,
        }
        receipt["receipt_sha256"] = dao._reconciliation_receipt_sha(receipt)
        operations.append(receipt)
        errors = dao._schema_check(state, "run_state.schema.json")
        if errors:
            return False, False, "; ".join(errors)
        dao.save_run_state(args.case_id, state)
        return True, projection_changed, None
    except (OSError, ValueError) as exc:
        return False, False, str(exc)
    finally:
        dao.release_owned_lock(owned_lock)


def cmd_read_ledger(dao, args) -> int:
    try:
        operator_auth.authenticate_environment("medical_review")
        ledger = load_ledger(dao, args.case_id)
    except (operator_auth.OperatorAuthorizationError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(ledger, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_read_evidence(dao, args) -> int:
    try:
        operator_auth.authenticate_environment("medical_review")
        ledger = load_ledger(dao, args.case_id)
        item = _exactly_one(
            ledger["review_items"],
            "review_item_id",
            args.review_item_id,
            "medical review item",
        )
        request = _exactly_one(
            item["requests"],
            "request_id",
            args.request_id,
            "medical review request",
        )
        version = _request_version(request, args.request_version)
        if args.locator_id not in version["included_evidence_locator_ids"]:
            raise ValueError("evidence locator is not included in the request version")
        from medical_repository import read_evidence_payload

        payload = read_evidence_payload(
            dao,
            args.case_id,
            args.locator_id,
            version["medical_variables_revision"]["sha256"],
        )
        payload.update({
            "review_item_id": item["review_item_id"],
            "request_id": request["request_id"],
            "request_version": version["request_version"],
        })
    except (operator_auth.OperatorAuthorizationError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _load_role_policy(dao) -> dict:
    path = Path(dao.MEDICAL_REVIEW_ROLE_CONFIG)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("medical-review role policy is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("medical-review role policy is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            policy = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("medical-review role policy is invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    errors = dao._schema_check(policy, "medical_review_role_config.schema.json")
    if errors:
        raise ValueError(
            "medical-review role policy is invalid: " + "; ".join(errors)
        )
    if not policy["operations_enabled"] or policy["approval"] is None:
        raise ValueError("medical-review operations are disabled")
    actor_ids = [item["actor_id"] for item in policy["named_actors"]]
    if len(actor_ids) != len(set(actor_ids)):
        raise ValueError("medical-review role policy has a duplicate actor_id")
    action_keys = [item["action"] for item in policy["action_permissions"]]
    if len(action_keys) != len(set(action_keys)):
        raise ValueError("medical-review role policy has a duplicate action permission")
    return policy


def _authenticated_actor(
    dao,
    action: str,
    *,
    policy: dict | None = None,
) -> dict:
    identity = operator_auth.authenticate_environment("medical_review")
    medical_actor_id = identity.get("medical_actor_id")
    if not isinstance(medical_actor_id, str):
        raise ValueError(
            "authenticated operator has no server-configured medical actor"
        )
    if policy is None:
        policy = _load_role_policy(dao)
    matching = [
        actor
        for actor in policy["named_actors"]
        if actor["actor_id"] == medical_actor_id
    ]
    if len(matching) != 1:
        raise ValueError(
            "authenticated medical actor is not uniquely named in role policy"
        )
    actor = matching[0]
    if identity["role"] != actor["declared_role"]:
        raise ValueError(
            "authenticated operator role does not match the named medical actor"
        )
    permission = next(
        (
            row
            for row in policy["action_permissions"]
            if row["action"] == action
        ),
        None,
    )
    if permission is None or actor["declared_role"] not in permission["allowed_roles"]:
        raise ValueError(
            f"medical actor is not authorized for lifecycle action {action}"
        )
    actor["operator_actor_id"] = identity["actor_id"]
    return {
        **actor,
        "attested_at": dao.now_iso(),
        "assertion_source": "authenticated_operator_policy",
        "operator_actor_id": identity["actor_id"],
        "operator_policy_version": identity["policy_version"],
        "authentication_method": identity["authentication_method"],
    }


def _required_operation_id(args) -> str:
    operation_id = getattr(args, "operation_id", None)
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", operation_id
    ):
        raise ValueError("operation_id has an invalid format")
    return operation_id


def _stable_actor_identity(actor: dict) -> dict:
    return {
        key: value
        for key, value in actor.items()
        if key != "attested_at"
    }


def medical_operation_request_sha256(
    dao,
    args,
    actor: dict,
    action: str,
    payload: dict | None,
    ledger: dict,
) -> str:
    """Bind one caller operation ID to one stable authenticated request."""
    operation_id = _required_operation_id(args)
    request = {
        "case_id": args.case_id,
        "run_id": args.run_id,
        "review_item_id": getattr(args, "review_item_id", None),
        "issue_id": getattr(args, "issue_id", None),
        "decision_owner": getattr(args, "decision_owner", None),
        "action": action,
        "reason": getattr(args, "reason", None),
        "payload": payload,
        "authenticated_actor": _stable_actor_identity(actor),
        "operation_id": operation_id,
    }
    request_sha256 = hashlib.sha256(
        dao._canonical_json_bytes(request)
    ).hexdigest()
    for event in ledger.get("events", []):
        if event.get("operation_id") != operation_id:
            continue
        if event.get("operation_request_sha256") != request_sha256:
            raise ValueError(
                "operation_id was already committed for a different medical request"
            )
    return request_sha256


def committed_medical_operation_result(
    ledger: dict,
    operation_id: str,
    request_sha256: str,
) -> dict | None:
    events = [
        event
        for event in ledger.get("events", [])
        if event.get("operation_id") == operation_id
    ]
    if not events:
        return None
    if any(
        event.get("operation_request_sha256") != request_sha256
        for event in events
    ):
        raise ValueError(
            "operation_id was already committed for a different medical request"
        )
    results = [event.get("operation_result") for event in events]
    if not results or not isinstance(results[0], dict) or any(
        result != results[0] for result in results[1:]
    ):
        raise ValueError("committed medical operation result is inconsistent")
    return results[0]


def _operation_event_fields(
    args,
    request_sha256: str,
    result: dict,
) -> dict:
    return {
        "operation_id": _required_operation_id(args),
        "operation_request_sha256": request_sha256,
        "operation_result": result,
    }


def _write_valid_ledger(dao, case_id: str, ledger: dict) -> None:
    schemas, registry = load_registry()
    errors = validate_instance(
        ledger,
        "medical_review_ledger.schema.json",
        schemas,
        registry,
    )
    errors.extend(validate_ledger_semantics(ledger, case_id))
    if errors:
        raise ValueError(
            "prospective medical review ledger is invalid: "
            + "; ".join(sorted(set(errors)))
        )
    dao.atomic_write_json(ledger_path(dao, case_id), ledger)


def _current_revision(dao, case_id: str) -> tuple[dict, dict]:
    variables, error = dao._load_medical_revision(case_id, None)
    if error or variables is None:
        raise ValueError(error or "canonical medical variables are unavailable")
    sha256 = hashlib.sha256(
        dao.medical_variables_path(case_id).read_bytes()
    ).hexdigest()
    return variables, _revision_descriptor(variables, sha256)


def cmd_open(dao, args) -> int:
    try:
        role_policy = _load_role_policy(dao)
        actor = _authenticated_actor(dao, "open", policy=role_policy)
    except (operator_auth.OperatorAuthorizationError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    if args.decision_owner != "human":
        print("FAIL: policy-owned medical review is not reconstructed")
        return 1

    path = ledger_path(dao, args.case_id)
    existing_lock = dao.acquire_lock_blocking(
        path,
        args.held_by,
        args.run_id,
        f"open medical review item for {args.issue_id}",
    )
    if existing_lock is not None:
        print(
            f"LOCKED: held_by={existing_lock['held_by']} "
            f"run_id={existing_lock['run_id']}"
        )
        return 1
    try:
        try:
            ledger = load_ledger(dao, args.case_id)
            operation_sha256 = medical_operation_request_sha256(
                dao, args, actor, "open", None, ledger,
            )
            replay = committed_medical_operation_result(
                ledger, _required_operation_id(args), operation_sha256,
            )
            if replay is not None:
                print(json.dumps(replay, sort_keys=True))
                return 0
            variables, revision = _current_revision(dao, args.case_id)
            if variables.get("run_id") != args.run_id:
                raise ValueError(
                    "medical review open run owner is stale against the canonical revision"
                )
            if not any(
                issue["issue_id"] == args.issue_id
                for issue in variables["medical_issues"]
            ):
                raise ValueError("unknown medical issue")
            if any(
                item["issue_id"] == args.issue_id
                and item["state"] not in {"do_not_refer", "cancelled", "closed"}
                for item in ledger["review_items"]
            ):
                raise ValueError(
                    "medical issue already has a review item"
                )

            at = dao.now_iso()
            item_id = f"MRI_{ledger['next_review_item_number']:04d}"
            event_id = f"MRE_{ledger['next_event_number']:06d}"
            ledger["next_review_item_number"] += 1
            ledger["next_event_number"] += 1
            operation_result = {
                "action": "open",
                "review_item_id": item_id,
                "state": "decision_pending",
                "decision_id": None,
            }
            ledger["review_items"].append({
                "review_item_id": item_id,
                "issue_id": args.issue_id,
                "decision_owner": args.decision_owner,
                "state": "decision_pending",
                "source_reinspection_records": [],
                "decisions": [],
                "requests": [],
                "current_request_id": None,
                "current_adjudication_id": None,
                "opened_at": at,
                "updated_at": at,
            })
            ledger["events"].append({
                "event_id": event_id,
                "review_item_id": item_id,
                "action": "open",
                "from_state": None,
                "to_state": "decision_pending",
                "actor": actor,
                "role_policy_snapshot": role_policy,
                **_operation_event_fields(
                    args, operation_sha256, operation_result,
                ),
                "reason": None,
                "medical_variables_revision": revision,
                "created_at": at,
            })
            if args.decision_owner == "human":
                wait_id = f"MRH_{ledger['next_human_input_number']:06d}"
                ledger["next_human_input_number"] += 1
                ledger["wait_episodes"].append({
                    "human_input_id": wait_id,
                    "input_kind": "referral_decision",
                    "review_item_id": item_id,
                    "related_review_item_ids": [],
                    "request_id": None,
                    "adjudication_id": None,
                    "wait_cycle": 1,
                    "status": "waiting",
                    "created_at": at,
                    "resolved_at": None,
                    "resolution": None,
                })
            ledger["updated_at"] = at
            _write_valid_ledger(dao, args.case_id, ledger)
        except (KeyError, ValueError) as exc:
            print(f"FAIL: {exc}")
            return 1
        print(json.dumps(operation_result, sort_keys=True))
        return 0
    finally:
        dao.release_lock(path)


def _load_json_file(path_value: str, label: str) -> dict:
    path = Path(path_value)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be one JSON object")
    return value


def _private_ingress_root() -> Path:
    root = Path(MEDICAL_REVIEW_INGRESS_ROOT)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = root.lstat()
    except OSError as exc:
        raise ValueError("private medical-review ingress is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("private medical-review ingress is not a real directory")
    if _EFFECTIVE_UID is not None and metadata.st_uid != _EFFECTIVE_UID:
        raise ValueError("private medical-review ingress has the wrong owner")
    permissions = stat.S_IMODE(metadata.st_mode)
    if permissions & 0o077 or permissions & 0o700 != 0o700:
        raise ValueError("private medical-review ingress permissions are not private")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("private medical-review ingress is unavailable") from exc


def _load_submission(path_value: str, label: str) -> dict:
    descriptor_match = re.fullmatch(r"fd:([0-9]+)", path_value)
    if descriptor_match is not None:
        try:
            descriptor = os.dup(int(descriptor_match.group(1)))
            metadata = os.fstat(descriptor)
        except OSError as exc:
            raise ValueError(f"{label} is unavailable") from exc
        try:
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"{label} is not a regular file")
            if (
                _EFFECTIVE_UID is not None
                and metadata.st_uid != _EFFECTIVE_UID
                or metadata.st_nlink != 0
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError(f"{label} has unsafe anonymous ownership")
            os.lseek(descriptor, 0, os.SEEK_SET)
            payload = bytearray()
            while len(payload) <= MAX_SUBMISSION_BYTES:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, MAX_SUBMISSION_BYTES + 1 - len(payload)),
                )
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > MAX_SUBMISSION_BYTES:
                raise ValueError(f"{label} exceeds the byte limit")
            try:
                value = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{label} is invalid") from exc
        finally:
            os.close(descriptor)
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be one JSON object")
        return value
    root = _private_ingress_root()
    path = Path(os.path.abspath(path_value))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} is outside the private medical-review ingress")
    if not relative.parts or any(
        component in {"", ".", ".."} for component in relative.parts
    ):
        raise ValueError(f"{label} is outside the private medical-review ingress")
    directory_descriptors: list[int] = []
    descriptor = -1
    try:
        root_descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        directory_descriptors.append(root_descriptor)
        root_metadata = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or (_EFFECTIVE_UID is not None and root_metadata.st_uid != _EFFECTIVE_UID)
            or stat.S_IMODE(root_metadata.st_mode) & 0o077
        ):
            raise OSError("unsafe private ingress root")
        current_descriptor = root_descriptor
        for component in relative.parts[:-1]:
            current_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_descriptor,
            )
            directory_descriptors.append(current_descriptor)
            metadata = os.fstat(current_descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or (_EFFECTIVE_UID is not None and metadata.st_uid != _EFFECTIVE_UID)
            ):
                raise OSError("unsafe private ingress ancestor")
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=current_descriptor,
        )
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} is not a regular file")
        if (
            (_EFFECTIVE_UID is not None and metadata.st_uid != _EFFECTIVE_UID)
            or metadata.st_nlink != 1
        ):
            raise ValueError(f"{label} has unsafe file ownership")
        payload = bytearray()
        while len(payload) <= MAX_SUBMISSION_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_SUBMISSION_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_SUBMISSION_BYTES:
            raise ValueError(f"{label} exceeds the byte limit")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} is invalid") from exc
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be one JSON object")
    return value


def _require_performed_source_reinspection(reinspection: dict) -> None:
    if reinspection.get("performed") is not True:
        raise ValueError(
            "source reinspection must be completed before package publication"
        )


def _validate_submission_references(
    variables: dict,
    submission: dict,
    issue_id: str,
) -> None:
    issues = {
        issue["issue_id"]: issue
        for issue in variables["medical_issues"]
    }
    if issue_id not in issues:
        raise ValueError("review issue does not resolve in the pinned revision")
    if (
        submission["decision"]["issue_id"] != issue_id
        or submission["referral_inputs"]["issue_id"] != issue_id
    ):
        raise ValueError("decision and referral inputs must match the review issue")
    observations = {
        observation["observation_id"]: (variable, observation)
        for variable in variables["variables"]
        for observation in variable["observations"]
    }
    variable_ids = {variable["variable_id"] for variable in variables["variables"]}
    locator_ids = {
        locator["locator_id"]
        for _, observation in observations.values()
        for locator in observation["evidence"]
    }
    issue_observation_ids = {
        link["observation_id"]
        for link in issues[issue_id]["evidence_links"]
    }
    issue_variable_ids = {
        observations[observation_id][0]["variable_id"]
        for observation_id in issue_observation_ids
        if observation_id in observations
    }
    issue_locator_ids = {
        locator["locator_id"]
        for observation_id in issue_observation_ids
        if observation_id in observations
        for locator in observations[observation_id][1]["evidence"]
    }
    reinspection = submission["source_reinspection"]
    _require_performed_source_reinspection(reinspection)
    if not set(reinspection["observation_ids"]).issubset(
        issue_observation_ids
    ) or not set(reinspection["evidence_locator_ids"]).issubset(
        issue_locator_ids
    ):
        raise ValueError(
            "source reinspection evidence does not belong to the review issue"
        )
    for anomaly in submission["referral_inputs"]["anomalies"]:
        if (
            not set(anomaly["variable_ids"]).issubset(issue_variable_ids)
            or not set(anomaly["observation_ids"]).issubset(issue_observation_ids)
            or not set(anomaly["evidence_locator_ids"]).issubset(
                issue_locator_ids
            )
        ):
            raise ValueError(
                "referral anomaly evidence does not belong to the review issue"
            )
        if not set(anomaly["variable_ids"]).issubset(variable_ids):
            raise ValueError("referral anomaly references an unknown variable")
    importance = {
        row["importance_id"]: row
        for row in variables["importance_assignments"]
    }
    submitted_importance_ids = set(
        submission["referral_inputs"]["importance_assignment_ids"]
    )
    if not submitted_importance_ids.issubset(importance) or any(
        importance[value]["issue_id"] != issue_id
        for value in submitted_importance_ids
    ):
        raise ValueError(
            "referral importance does not belong to the review issue"
        )
    if not set(submission["decision"]["evidence_locator_ids"]).issubset(
        issue_locator_ids
    ):
        raise ValueError(
            "referral decision evidence does not belong to the review issue"
        )
    canonical_coverage = sorted({
        row["coverage_status"] for row in variables["source_coverage"]
    })
    if sorted(
        submission["referral_inputs"]["source_coverage_statuses"]
    ) != canonical_coverage:
        raise ValueError(
            "referral source coverage statuses do not match the pinned revision"
        )


def _request_config(dao) -> dict:
    config = _load_json_file(
        str(dao.MEDICAL_REVIEW_REQUEST_CONFIG),
        "medical review request configuration",
    )
    errors = dao._schema_check(
        config,
        "medical_review_request_config.schema.json",
    )
    if errors:
        raise ValueError(
            "medical review request configuration is invalid: "
            + "; ".join(errors)
        )
    if not config["requests_enabled"] or config["approval"] is None:
        raise ValueError("medical review request creation is disabled")
    codes = [row["code"] for row in config["interpretations"]]
    if len(codes) != len(set(codes)):
        raise ValueError(
            "medical review request configuration has duplicate interpretations"
        )
    return config


def _build_request(
    dao,
    *,
    variables: dict,
    item: dict,
    submission: dict,
    decision: dict,
    reinspection: dict,
    revision: dict,
    request_id: str,
    created_at: str,
    request_version: int = 1,
) -> dict:
    request_input = submission["request"]
    issue = next(
        (
            row
            for row in variables["medical_issues"]
            if row["issue_id"] == item["issue_id"]
        ),
        None,
    )
    if issue is None:
        raise ValueError("request issue does not resolve in the pinned revision")
    if (
        request_input["issue_id"] != issue["issue_id"]
        or request_input["issue_category"] != issue["issue_category"]
    ):
        raise ValueError("request issue identity/category does not match the review issue")

    observations = {
        observation["observation_id"]: (variable, observation)
        for variable in variables["variables"]
        for observation in variable["observations"]
    }
    issue_links = {
        link["issue_evidence_link_id"]: link
        for link in issue["evidence_links"]
    }
    issue_observation_ids = {
        link["observation_id"] for link in issue_links.values()
    }
    issue_variable_ids = {
        observations[observation_id][0]["variable_id"]
        for observation_id in issue_observation_ids
        if observation_id in observations
    }
    scope = request_input["question_scope"]
    subject_observation_ids = set(scope["subject_observation_ids"])
    subject_variable_ids = set(scope["subject_variable_ids"])
    if not subject_observation_ids.issubset(issue_observation_ids):
        raise ValueError("request subject observations are outside the review issue")
    if not subject_variable_ids.issubset(issue_variable_ids):
        raise ValueError("request subject variables are outside the review issue")
    if any(
        observations[observation_id][0]["variable_id"] not in subject_variable_ids
        for observation_id in subject_observation_ids
    ):
        raise ValueError(
            "request subject observations are not owned by subject variables"
        )
    for field in (
        "timeline_observation_ids",
        "prior_condition_observation_ids",
        "prognostic_observation_ids",
    ):
        if not set(request_input[field]).issubset(issue_observation_ids):
            raise ValueError(f"request {field} contains evidence outside the issue")

    config = _request_config(dao)
    interpretation_code = scope["requested_interpretation"]
    interpretation = next(
        (
            row
            for row in config["interpretations"]
            if row["code"] == interpretation_code
        ),
        None,
    )
    if interpretation is None:
        raise ValueError("requested interpretation is not configured")
    if issue["issue_category"] not in interpretation["allowed_issue_categories"]:
        raise ValueError("requested interpretation is not allowed for the issue category")
    if request_input["suggested_specialty_code"] not in interpretation[
        "allowed_specialty_codes"
    ]:
        raise ValueError("suggested specialty is not allowed for the interpretation")

    included_link_ids = set(request_input["included_issue_evidence_link_ids"])
    if not included_link_ids.issubset(issue_links):
        raise ValueError("request includes an evidence link outside the issue")
    mandatory_link_ids = {
        link_id
        for link_id, link in issue_links.items()
        if link["materiality"] == "core"
        or link["relation"] in {"weakens", "conflicts"}
    }
    if not mandatory_link_ids.issubset(included_link_ids):
        raise ValueError(
            "request omits core, weakening, or conflicting issue evidence"
        )
    optional_link_ids = set(issue_links) - mandatory_link_ids
    omitted_ids = [
        row["issue_evidence_link_id"]
        for row in request_input["omitted_supporting_links"]
    ]
    if len(omitted_ids) != len(set(omitted_ids)):
        raise ValueError("request repeats an omitted supporting evidence link")
    if set(omitted_ids) != optional_link_ids - included_link_ids:
        raise ValueError(
            "request must explain every omitted supporting evidence link exactly once"
        )
    included_observation_ids = {
        issue_links[link_id]["observation_id"] for link_id in included_link_ids
    }
    required_locator_ids = {
        locator["locator_id"]
        for observation_id in included_observation_ids
        for locator in observations[observation_id][1]["evidence"]
    }
    included_locator_ids = set(request_input["included_evidence_locator_ids"])
    if included_locator_ids != required_locator_ids:
        raise ValueError(
            "request locators must exactly cover every included issue evidence link"
        )
    if not included_observation_ids.issubset(
        set(reinspection["observation_ids"])
    ) or not included_locator_ids.issubset(
        set(reinspection["evidence_locator_ids"])
    ):
        raise ValueError(
            "balanced request evidence is not covered by source reinspection"
        )

    request = {
        "request_id": request_id,
        "request_version": request_version,
        "decision_id": decision["decision_id"],
        "referral_input_id": decision["referral_inputs"]["referral_input_id"],
        "medical_variables_revision": revision,
        "source_reinspection_ids": [reinspection["source_reinspection_id"]],
        **request_input,
        "request_config_version": config["config_version"],
        "created_at": created_at,
    }
    errors = dao._schema_check(request, "medical_review_request.schema.json")
    if errors:
        raise ValueError(
            "prospective medical review request is invalid: " + "; ".join(errors)
        )
    return request


def _resolve_item_waits(
    ledger: dict,
    item_id: str,
    *,
    at: str,
    resolution: str,
    status: str = "received",
) -> None:
    for wait in ledger["wait_episodes"]:
        if wait["review_item_id"] == item_id and wait["status"] == "waiting":
            wait["status"] = status
            wait["resolved_at"] = at
            wait["resolution"] = resolution


def cmd_record_decision(dao, args) -> int:
    try:
        role_policy = _load_role_policy(dao)
        actor = _authenticated_actor(
            dao,
            "record_decision",
            policy=role_policy,
        )
    except (operator_auth.OperatorAuthorizationError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    path = ledger_path(dao, args.case_id)
    existing_lock = dao.acquire_lock_blocking(
        path,
        args.held_by,
        args.run_id,
        "record medical referral decision",
    )
    if existing_lock is not None:
        print(
            f"LOCKED: held_by={existing_lock['held_by']} "
            f"run_id={existing_lock['run_id']}"
        )
        return 1
    try:
        try:
            submission = _load_submission(
                args.decision_file,
                "referral decision submission",
            )
            errors = dao._schema_check(
                submission,
                "medical_referral_submission.schema.json",
            )
            if errors:
                raise ValueError(
                    "invalid referral submission: " + "; ".join(errors)
                )
            ledger = load_ledger(dao, args.case_id)
            operation_sha256 = medical_operation_request_sha256(
                dao, args, actor, "record_decision", submission, ledger,
            )
            replay = committed_medical_operation_result(
                ledger, _required_operation_id(args), operation_sha256,
            )
            if replay is not None:
                print(json.dumps(replay, sort_keys=True))
                return 0
            item = next(
                (
                    row
                    for row in ledger["review_items"]
                    if row["review_item_id"] == args.review_item_id
                ),
                None,
            )
            if item is None:
                raise ValueError("unknown medical review item")
            if item["decision_owner"] != "human":
                raise ValueError(
                    "authenticated human decision requires a human-owned item"
                )
            if item["state"] != "decision_pending":
                raise ValueError(
                    "referral decision is not authorized from the current state"
                )
            if (
                submission["decision"]["decision_origin"]
                != "authorized_human_override"
            ):
                raise ValueError(
                    "human-owned item requires authorized_human_override origin"
                )
            outcome = submission["decision"]["decision"]
            variables, revision = _current_revision(dao, args.case_id)
            if variables.get("run_id") != args.run_id:
                raise ValueError(
                    "medical review decision run owner is stale against the canonical revision"
                )
            _validate_submission_references(
                variables,
                submission,
                item["issue_id"],
            )
            at = dao.now_iso()
            cycle = len(item["decisions"]) + 1
            source_cycle = len(item["source_reinspection_records"]) + 1
            reinspection_id = f"{item['review_item_id']}-S{source_cycle:02d}"
            referral_input_id = f"{item['review_item_id']}-I{cycle:02d}"
            decision_id = f"{item['review_item_id']}-D{cycle:02d}"
            reinspection = {
                "source_reinspection_id": reinspection_id,
                "medical_variables_revision": revision,
                **submission["source_reinspection"],
                "actor_or_process": actor["actor_id"],
                "performed_at": at,
            }
            referral_inputs = {
                "referral_input_id": referral_input_id,
                "medical_variables_revision": revision,
                "source_reinspection_id": reinspection_id,
                **submission["referral_inputs"],
            }
            decision = {
                "decision_id": decision_id,
                "decision_version": cycle,
                **submission["decision"],
                "referral_inputs": referral_inputs,
                "actor": actor,
                "created_at": at,
            }
            decision_errors = dao._schema_check(
                decision,
                "medical_referral_decision.schema.json",
            )
            if decision_errors:
                raise ValueError(
                    "prospective referral decision is invalid: "
                    + "; ".join(decision_errors)
                )
            request = None
            if outcome == "refer":
                request_id = f"MRR_{ledger['next_request_number']:04d}"
                request = _build_request(
                    dao,
                    variables=variables,
                    item=item,
                    submission=submission,
                    decision=decision,
                    reinspection=reinspection,
                    revision=revision,
                    request_id=request_id,
                    created_at=at,
                )
                destination = "package_ready"
            elif outcome == "do_not_refer":
                destination = "do_not_refer"
            else:
                destination = "needs_information"
            previous = item["state"]
            item["source_reinspection_records"].append(reinspection)
            item["decisions"].append(decision)
            if request is not None:
                ledger["next_request_number"] += 1
                item["requests"].append({
                    "request_id": request["request_id"],
                    "versions": [request],
                    "assignments": [],
                    "responses": [],
                    "current_assignment_id": None,
                    "current_response_id": None,
                })
                item["current_request_id"] = request["request_id"]
            item["state"] = destination
            item["updated_at"] = at
            _resolve_item_waits(
                ledger,
                item["review_item_id"],
                at=at,
                resolution=f"decision:{outcome}",
            )
            if destination in {"needs_information", "package_ready"}:
                wait_id = f"MRH_{ledger['next_human_input_number']:06d}"
                ledger["next_human_input_number"] += 1
                ledger["wait_episodes"].append({
                    "human_input_id": wait_id,
                    "input_kind": (
                        "expert_assignment"
                        if destination == "package_ready"
                        else "medical_evidence"
                    ),
                    "review_item_id": item["review_item_id"],
                    "related_review_item_ids": [],
                    "request_id": (
                        request["request_id"] if request is not None else None
                    ),
                    "adjudication_id": None,
                    "wait_cycle": cycle,
                    "status": "waiting",
                    "created_at": at,
                    "resolved_at": None,
                    "resolution": None,
                })
            event_id = f"MRE_{ledger['next_event_number']:06d}"
            ledger["next_event_number"] += 1
            operation_result = {
                "action": "record_decision",
                "review_item_id": item["review_item_id"],
                "decision_id": decision_id,
                "state": destination,
            }
            ledger["events"].append({
                "event_id": event_id,
                "review_item_id": item["review_item_id"],
                "action": "record_decision",
                "from_state": previous,
                "to_state": destination,
                "actor": actor,
                "role_policy_snapshot": role_policy,
                **_operation_event_fields(
                    args, operation_sha256, operation_result,
                ),
                "reason": decision["rationale"],
                "decision_id": decision_id,
                "request_id": request["request_id"] if request is not None else None,
                "medical_variables_revision": revision,
                "created_at": at,
            })
            ledger["updated_at"] = at
            _write_valid_ledger(dao, args.case_id, ledger)
        except (KeyError, ValueError) as exc:
            print(f"FAIL: {exc}")
            return 1
        print(json.dumps(operation_result, sort_keys=True))
        return 0
    finally:
        dao.release_lock(path)


def _issue_snapshot(variables: dict, issue_id: str) -> bytes:
    matches = [
        issue
        for issue in variables["medical_issues"]
        if issue["issue_id"] == issue_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"medical issue {issue_id} does not resolve in the pinned revision"
        )
    issue = matches[0]
    observation_ids = {
        link["observation_id"] for link in issue["evidence_links"]
    }
    observations = [
        observation
        for variable in variables["variables"]
        for observation in variable["observations"]
        if observation["observation_id"] in observation_ids
    ]
    return json.dumps(
        {"issue": issue, "observations": observations},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def cmd_provide_information(dao, args) -> int:
    try:
        role_policy = _load_role_policy(dao)
        actor = _authenticated_actor(
            dao,
            "provide_information",
            policy=role_policy,
        )
    except (operator_auth.OperatorAuthorizationError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    path = ledger_path(dao, args.case_id)
    existing_lock = dao.acquire_lock_blocking(
        path,
        args.held_by,
        args.run_id,
        "rebind medical review item to revised information",
    )
    if existing_lock is not None:
        print(
            f"LOCKED: held_by={existing_lock['held_by']} "
            f"run_id={existing_lock['run_id']}"
        )
        return 1
    try:
        try:
            ledger = load_ledger(dao, args.case_id)
            operation_sha256 = medical_operation_request_sha256(
                dao, args, actor, "provide_information", None, ledger,
            )
            replay = committed_medical_operation_result(
                ledger, _required_operation_id(args), operation_sha256,
            )
            if replay is not None:
                print(json.dumps(replay, sort_keys=True))
                return 0
            item = next(
                (
                    row
                    for row in ledger["review_items"]
                    if row["review_item_id"] == args.review_item_id
                ),
                None,
            )
            if item is None:
                raise ValueError("unknown medical review item")
            if item["state"] != "needs_information":
                raise ValueError(
                    "additional information is only accepted from needs_information"
                )
            item_events = [
                event
                for event in ledger["events"]
                if event["review_item_id"] == item["review_item_id"]
            ]
            if not item_events:
                raise ValueError("medical review item has no revision history")
            previous_revision = item_events[-1]["medical_variables_revision"]
            current_variables, current_revision = _current_revision(
                dao,
                args.case_id,
            )
            if previous_revision["sha256"] == current_revision["sha256"]:
                raise ValueError(
                    "no newer canonical medical revision supplies information"
                )
            previous_variables, error = dao._load_medical_revision(
                args.case_id,
                previous_revision["sha256"],
            )
            if error or previous_variables is None:
                raise ValueError(
                    error or "prior pinned medical revision is unavailable"
                )
            if _issue_snapshot(
                previous_variables,
                item["issue_id"],
            ) == _issue_snapshot(current_variables, item["issue_id"]):
                raise ValueError(
                    "new revision does not change issue-linked evidence"
                )
            at = dao.now_iso()
            _resolve_item_waits(
                ledger,
                item["review_item_id"],
                at=at,
                resolution="additional_information_committed",
            )
            wait_id = f"MRH_{ledger['next_human_input_number']:06d}"
            ledger["next_human_input_number"] += 1
            ledger["wait_episodes"].append({
                "human_input_id": wait_id,
                "input_kind": "referral_decision",
                "review_item_id": item["review_item_id"],
                "related_review_item_ids": [],
                "request_id": None,
                "adjudication_id": None,
                "wait_cycle": len(item["decisions"]) + 1,
                "status": "waiting",
                "created_at": at,
                "resolved_at": None,
                "resolution": None,
            })
            previous_state = item["state"]
            item["state"] = "decision_pending"
            item["updated_at"] = at
            event_id = f"MRE_{ledger['next_event_number']:06d}"
            ledger["next_event_number"] += 1
            operation_result = {
                "action": "provide_information",
                "review_item_id": item["review_item_id"],
                "state": "decision_pending",
                "decision_id": None,
            }
            ledger["events"].append({
                "event_id": event_id,
                "review_item_id": item["review_item_id"],
                "action": "provide_information",
                "from_state": previous_state,
                "to_state": "decision_pending",
                "actor": actor,
                "role_policy_snapshot": role_policy,
                **_operation_event_fields(
                    args, operation_sha256, operation_result,
                ),
                "reason": args.reason,
                "medical_variables_revision": current_revision,
                "created_at": at,
            })
            ledger["updated_at"] = at
            _write_valid_ledger(dao, args.case_id, ledger)
        except (KeyError, StopIteration, ValueError) as exc:
            print(f"FAIL: {exc}")
            return 1
        print(json.dumps(operation_result, sort_keys=True))
        return 0
    finally:
        dao.release_lock(path)


def _action_data(dao, args) -> dict | None:
    if args.action not in {
        "provide_information",
        "assign",
        "request_information",
        "supplement_package",
        "reassign",
        "cancel",
        "submit_response",
        "amend_response",
        "withdraw_response",
        "flag_conflict",
        "adjudicate",
        "close",
        "reopen",
    }:
        raise ValueError("medical-review action is not reconstructed")
    if args.action == "close":
        if args.data_file is not None:
            raise ValueError("close does not accept an action data file")
        if not isinstance(args.reason, str) or not args.reason.strip():
            raise ValueError("close requires a nonblank disposition reason")
        data = None
    else:
        data = _load_submission(
            args.data_file,
            "medical-review action data",
        )
    errors = dao._schema_check(
        {"action": args.action, "data": data},
        "medical_review_action.schema.json",
    )
    if errors:
        raise ValueError(
            "invalid medical-review action payload: " + "; ".join(errors)
        )
    return data


def _exactly_one(rows: list[dict], field: str, value: str, label: str) -> dict:
    matches = [row for row in rows if row.get(field) == value]
    if len(matches) != 1:
        raise ValueError(f"{label} does not resolve exactly once")
    return matches[0]


def _current_request_record(item: dict) -> dict:
    request_id = item.get("current_request_id")
    if request_id is None:
        raise ValueError("medical review item has no current request")
    return _exactly_one(item["requests"], "request_id", request_id, "current request")


def _request_version(record: dict, version: int) -> dict:
    matches = [
        row for row in record["versions"] if row["request_version"] == version
    ]
    if len(matches) != 1:
        raise ValueError("request version does not resolve exactly once")
    return matches[0]


def _named_actor(policy: dict, actor_id: str) -> dict:
    return _exactly_one(
        policy["named_actors"],
        "actor_id",
        actor_id,
        "named medical actor",
    )


def _append_item_wait(
    ledger: dict,
    item: dict,
    *,
    input_kind: str,
    request_id: str | None,
    at: str,
    wait_cycle: int | None = None,
) -> None:
    wait_id = f"MRH_{ledger['next_human_input_number']:06d}"
    ledger["next_human_input_number"] += 1
    ledger["wait_episodes"].append({
        "human_input_id": wait_id,
        "input_kind": input_kind,
        "review_item_id": item["review_item_id"],
        "related_review_item_ids": [],
        "request_id": request_id,
        "adjudication_id": None,
        "wait_cycle": (
            len(item["decisions"]) if wait_cycle is None else wait_cycle
        ),
        "status": "waiting",
        "created_at": at,
        "resolved_at": None,
        "resolution": None,
    })


def _append_shared_wait(
    ledger: dict,
    *,
    adjudication_id: str,
    review_item_ids: list[str],
    at: str,
) -> None:
    wait_id = f"MRH_{ledger['next_human_input_number']:06d}"
    ledger["next_human_input_number"] += 1
    ledger["wait_episodes"].append({
        "human_input_id": wait_id,
        "input_kind": "medical_adjudication",
        "review_item_id": None,
        "related_review_item_ids": review_item_ids,
        "request_id": None,
        "adjudication_id": adjudication_id,
        "wait_cycle": 1,
        "status": "waiting",
        "created_at": at,
        "resolved_at": None,
        "resolution": None,
    })


def _build_assignment(
    ledger: dict,
    record: dict,
    data: dict,
    actor: dict,
    policy: dict,
    at: str,
) -> dict:
    if data["request_id"] != record["request_id"]:
        raise ValueError("assignment request_id is not the current request")
    package = _request_version(record, data["request_version"])
    if package is not record["versions"][-1]:
        raise ValueError("assignment must bind the latest request version")
    reviewer = _named_actor(policy, data["reviewer_actor_id"])
    if reviewer["declared_role"] != "medical_reviewer":
        raise ValueError("target assignment identity is not a medical reviewer")
    if reviewer["specialty_code"] != package["suggested_specialty_code"]:
        raise ValueError("target reviewer specialty does not match the package")
    attestation = data["package_review_attestation"]
    if attestation["reviewed_by_actor_id"] != actor["actor_id"]:
        raise ValueError("package-review attestation must belong to the assigning actor")
    return {
        "assignment_id": (
            f"{record['request_id']}-A{len(record['assignments']) + 1:02d}"
        ),
        "request_id": record["request_id"],
        "request_version": data["request_version"],
        "reviewer": reviewer,
        "role_policy_version": policy["config_version"],
        "assigned_by_actor_id": actor["actor_id"],
        "package_review_attestation": attestation,
        "assignment_event_id": f"MRE_{ledger['next_event_number']:06d}",
        "assigned_at": at,
    }


def _response_text_errors(submission: dict) -> list[str]:
    errors = []
    for field in ("interpretation", "basis", "uncertainty", "attestation"):
        if not submission[field].strip():
            errors.append(field)
    for field in ("alternative_interpretations", "additional_evidence_needed"):
        if any(not value.strip() for value in submission[field]):
            errors.append(field)
    advice = submission["downstream_adjustment_advice"]
    if advice is not None and not advice.strip():
        errors.append("downstream_adjustment_advice")
    return errors


def _build_response(
    dao,
    *,
    item: dict,
    record: dict,
    assignment: dict,
    actor: dict,
    submission: dict,
    action: str,
    at: str,
) -> dict:
    request = _request_version(record, assignment["request_version"])
    invalid_text = _response_text_errors(submission)
    if invalid_text:
        raise ValueError(
            "response fields must contain non-whitespace text: "
            + ", ".join(invalid_text)
        )
    if actor["actor_id"] != assignment["reviewer"]["actor_id"]:
        raise ValueError("response actor does not match the response assignment")
    expected = {
        "request_id": record["request_id"],
        "request_version_reviewed": assignment["request_version"],
        "issue_id": item["issue_id"],
        "issue_category": request["issue_category"],
        "decision_id": request["decision_id"],
        "request_config_version_reviewed": request["request_config_version"],
        "referral_policy_version_reviewed": request["referral_policy_version"],
    }
    for field, value in expected.items():
        if submission[field] != value:
            raise ValueError(f"response {field} does not match the assigned request")
    if not set(submission["evidence_locator_ids_reviewed"]).issubset(
        request["included_evidence_locator_ids"]
    ):
        raise ValueError("response evidence is outside the assigned request package")
    current_response_id = record["current_response_id"]
    if action == "submit_response":
        if (
            submission["response_status"] != "completed"
            or submission["supersedes_response_id"] is not None
        ):
            raise ValueError(
                "initial response must be completed and supersede nothing"
            )
    elif (
        submission["response_status"] != "amended"
        or submission["supersedes_response_id"] != current_response_id
    ):
        raise ValueError("amendment must supersede the current response")
    version = len(record["responses"]) + 1
    response = {
        "response_id": f"{record['request_id']}-R{version:02d}",
        "response_version": version,
        "assignment_id": assignment["assignment_id"],
        **submission,
        "reviewer": actor,
        "submitted_at": at,
    }
    errors = dao._schema_check(response, "medical_review_response.schema.json")
    if errors:
        raise ValueError(
            "prospective medical-review response is invalid: " + "; ".join(errors)
        )
    return response


def _append_transition_event(
    ledger: dict,
    item: dict,
    *,
    action: str,
    previous: str,
    destination: str,
    actor: dict,
    role_policy: dict,
    reason: str,
    revision: dict,
    at: str,
    adjudication_id: str | None = None,
    response_id: str | None = None,
) -> None:
    event_id = f"MRE_{ledger['next_event_number']:06d}"
    ledger["next_event_number"] += 1
    event = {
        "event_id": event_id,
        "review_item_id": item["review_item_id"],
        "action": action,
        "from_state": previous,
        "to_state": destination,
        "actor": actor,
        "role_policy_snapshot": role_policy,
        "reason": reason,
        "medical_variables_revision": revision,
        "created_at": at,
    }
    if adjudication_id is not None:
        event["adjudication_id"] = adjudication_id
    if response_id is not None:
        event["response_id"] = response_id
    ledger["events"].append(event)


def _item_response_head(item: dict) -> tuple[dict, str]:
    record = _current_request_record(item)
    response_id = record.get("current_response_id")
    if response_id is None:
        raise ValueError(f"{item['review_item_id']} has no current response")
    _exactly_one(record["responses"], "response_id", response_id, "current response")
    return record, response_id


def _assert_item_revision_current(item: dict, ledger: dict, revision: dict) -> None:
    events = [
        event for event in ledger["events"]
        if event["review_item_id"] == item["review_item_id"]
    ]
    if (
        not events
        or events[-1]["medical_variables_revision"]["sha256"] != revision["sha256"]
    ):
        raise ValueError(
            f"{item['review_item_id']} is stale against the canonical revision"
        )


def _assert_fresh_adjudication_cohort(item: dict, ledger: dict) -> None:
    _, anchor_response_id = _item_response_head(item)
    adjudication = next(
        (
            row for row in reversed(ledger["adjudications"])
            if row["status"] == "resolved"
            and item["review_item_id"] in row["review_item_ids"]
            and row["response_ids"][
                row["review_item_ids"].index(item["review_item_id"])
            ] == anchor_response_id
        ),
        None,
    )
    if adjudication is None:
        return
    item_by_id = {row["review_item_id"]: row for row in ledger["review_items"]}
    for item_id, response_id in zip(
        adjudication["review_item_ids"],
        adjudication["response_ids"],
        strict=True,
    ):
        participant = item_by_id.get(item_id)
        if participant is None:
            raise ValueError("resolved adjudication has an unknown participant")
        _, current_response_id = _item_response_head(participant)
        if current_response_id != response_id:
            raise ValueError(
                "resolved adjudication is stale against a current response head"
            )


def _apply_shared_adjudication_transition(
    ledger: dict,
    anchor: dict,
    *,
    action: str,
    data: dict,
    actor: dict,
    policy: dict,
    reason: str | None,
    revision: dict,
    at: str,
) -> str:
    item_by_id = {item["review_item_id"]: item for item in ledger["review_items"]}
    if action == "flag_conflict":
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("flag_conflict requires a nonblank reason")
        pairs = data["items"]
        review_item_ids = [pair["review_item_id"] for pair in pairs]
        if len(review_item_ids) < 2 or len(set(review_item_ids)) != len(review_item_ids):
            raise ValueError("flag_conflict requires at least two distinct review items")
        if anchor["review_item_id"] not in review_item_ids:
            raise ValueError("command-anchor item is not a conflict participant")
        participants: list[dict] = []
        response_ids: list[str] = []
        issue_id: str | None = None
        for pair in pairs:
            participant = item_by_id.get(pair["review_item_id"])
            if participant is None:
                raise ValueError("conflict names an unknown review item")
            _assert_item_revision_current(participant, ledger, revision)
            record, response_id = _item_response_head(participant)
            if pair["response_id"] != response_id:
                raise ValueError("conflict response is not the current response head")
            if not transition_allowed(
                participant["state"],
                action,
                actor=actor,
                role_policy=policy,
                has_current_response=record["current_response_id"] is not None,
                has_pending_adjudication=(
                    participant["current_adjudication_id"] is not None
                ),
            ):
                raise ValueError("flag_conflict is not authorized for every participant")
            if issue_id is None:
                issue_id = participant["issue_id"]
            elif participant["issue_id"] != issue_id:
                raise ValueError("conflict participants must share one medical issue")
            participants.append(participant)
            response_ids.append(response_id)
        adjudication_id = f"MRA_{ledger['next_adjudication_number']:06d}"
        ledger["next_adjudication_number"] += 1
        ledger["adjudications"].append({
            "adjudication_id": adjudication_id,
            "issue_id": issue_id,
            "review_item_ids": review_item_ids,
            "response_ids": response_ids,
            "status": "pending",
            "adjudicator": None,
            "record": None,
            "created_at": at,
            "resolved_at": None,
        })
        for participant, response_id in zip(participants, response_ids, strict=True):
            previous = participant["state"]
            _resolve_item_waits(
                ledger,
                participant["review_item_id"],
                at=at,
                resolution="adjudication_required",
                status="no_longer_required",
            )
            participant["state"] = "adjudication_required"
            participant["current_adjudication_id"] = adjudication_id
            participant["updated_at"] = at
            _append_transition_event(
                ledger,
                participant,
                action=action,
                previous=previous,
                destination="adjudication_required",
                actor=actor,
                role_policy=policy,
                reason=reason.strip(),
                revision=revision,
                at=at,
                adjudication_id=adjudication_id,
                response_id=response_id,
            )
        _append_shared_wait(
            ledger,
            adjudication_id=adjudication_id,
            review_item_ids=review_item_ids,
            at=at,
        )
        return "adjudication_required"

    if (
        action == "cancel"
        and data.get("adjudication_id") != anchor.get("current_adjudication_id")
    ):
        raise ValueError(
            "shared adjudication cancellation must name its adjudication_id"
        )
    adjudication = _exactly_one(
        ledger["adjudications"],
        "adjudication_id",
        data["adjudication_id"],
        "adjudication",
    )
    if adjudication["status"] != "pending":
        raise ValueError("adjudication is not pending")
    if anchor["review_item_id"] not in adjudication["review_item_ids"]:
        raise ValueError("command-anchor item is not an adjudication participant")
    participants = []
    for item_id in adjudication["review_item_ids"]:
        participant = item_by_id.get(item_id)
        if participant is None:
            raise ValueError("adjudication names an unknown review item")
        _assert_item_revision_current(participant, ledger, revision)
        record, _ = _item_response_head(participant)
        if participant["current_adjudication_id"] != adjudication["adjudication_id"]:
            raise ValueError("adjudication pointer is not shared by every participant")
        if not transition_allowed(
            participant["state"],
            action,
            actor=actor,
            role_policy=policy,
            has_current_response=record["current_response_id"] is not None,
            has_pending_adjudication=True,
        ):
            raise ValueError(
                f"{action} is not authorized for every adjudication participant"
            )
        participants.append(participant)
    waits = [
        wait for wait in ledger["wait_episodes"]
        if wait["adjudication_id"] == adjudication["adjudication_id"]
        and wait["status"] == "waiting"
    ]
    if len(waits) != 1 or waits[0]["review_item_id"] is not None:
        raise ValueError("pending adjudication does not have exactly one shared wait")
    wait = waits[0]
    if action == "cancel":
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancel requires a nonblank reason")
        cancel_reason = reason.strip()
        wait["status"] = "no_longer_required"
        wait["resolved_at"] = at
        wait["resolution"] = "cancelled"
        adjudication["status"] = "cancelled"
        adjudication["resolved_at"] = at
        for participant in participants:
            previous = participant["state"]
            participant["state"] = "cancelled"
            participant["current_adjudication_id"] = None
            participant["updated_at"] = at
            request = _current_request_record(participant)
            request["current_assignment_id"] = None
            request["current_response_id"] = None
            _resolve_item_waits(
                ledger,
                participant["review_item_id"],
                at=at,
                resolution="cancelled",
                status="no_longer_required",
            )
            _append_transition_event(
                ledger,
                participant,
                action=action,
                previous=previous,
                destination="cancelled",
                actor=actor,
                role_policy=policy,
                reason=cancel_reason,
                revision=revision,
                at=at,
                adjudication_id=adjudication["adjudication_id"],
            )
        return "cancelled"

    if not isinstance(data["record"], str) or not data["record"].strip():
        raise ValueError("adjudication record must be nonblank")
    wait["status"] = "received"
    wait["resolved_at"] = at
    wait["resolution"] = "adjudicated"
    adjudication["status"] = "resolved"
    adjudication["adjudicator"] = actor
    adjudication["record"] = data["record"].strip()
    adjudication["resolved_at"] = at
    for participant in participants:
        previous = participant["state"]
        participant["state"] = "answered"
        participant["current_adjudication_id"] = None
        participant["updated_at"] = at
        request = _current_request_record(participant)
        _append_item_wait(
            ledger,
            participant,
            input_kind="coordinator_disposition",
            request_id=request["request_id"],
            at=at,
        )
        _append_transition_event(
            ledger,
            participant,
            action=action,
            previous=previous,
            destination="answered",
            actor=actor,
            role_policy=policy,
            reason=data["record"].strip(),
            revision=revision,
            at=at,
            adjudication_id=adjudication["adjudication_id"],
        )
    return "answered"


def cmd_transition(dao, args) -> int:
    try:
        policy = _load_role_policy(dao)
        actor = _authenticated_actor(dao, args.action, policy=policy)
    except (operator_auth.OperatorAuthorizationError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    path = ledger_path(dao, args.case_id)
    existing_lock = dao.acquire_lock_blocking(
        path,
        args.held_by,
        args.run_id,
        f"medical review transition: {args.action}",
    )
    if existing_lock is not None:
        print(
            f"LOCKED: held_by={existing_lock['held_by']} "
            f"run_id={existing_lock['run_id']}"
        )
        return 1
    try:
        try:
            data = _action_data(dao, args)
            ledger = load_ledger(dao, args.case_id)
            operation_sha256 = medical_operation_request_sha256(
                dao, args, actor, args.action, data, ledger,
            )
            replay = committed_medical_operation_result(
                ledger, _required_operation_id(args), operation_sha256,
            )
            if replay is not None:
                print(json.dumps(replay, sort_keys=True))
                return 0
            item = next(
                (
                    row
                    for row in ledger["review_items"]
                    if row["review_item_id"] == args.review_item_id
                ),
                None,
            )
            if item is None:
                raise ValueError("unknown medical review item")
            variables, revision = _current_revision(dao, args.case_id)
            if variables.get("run_id") != args.run_id:
                raise ValueError(
                    "medical review transition run owner is stale against the canonical revision"
                )
            item_events = [
                event
                for event in ledger["events"]
                if event["review_item_id"] == item["review_item_id"]
            ]
            if args.action not in {
                "provide_information",
                "supplement_package",
                "reopen",
            } and (
                not item_events
                or item_events[-1]["medical_variables_revision"]["sha256"]
                != revision["sha256"]
            ):
                raise ValueError(
                    "medical review transition is stale against the canonical revision"
                )
            if args.action in {"flag_conflict", "adjudicate"} or (
                args.action == "cancel"
                and item.get("current_adjudication_id") is not None
            ):
                assert data is not None
                at = dao.now_iso()
                event_start = len(ledger["events"])
                destination = _apply_shared_adjudication_transition(
                    ledger,
                    item,
                    action=args.action,
                    data=data,
                    actor=actor,
                    policy=policy,
                    reason=args.reason,
                    revision=revision,
                    at=at,
                )
                operation_result = {
                    "action": args.action,
                    "review_item_id": item["review_item_id"],
                    "state": destination,
                    "decision_id": None,
                }
                operation_fields = _operation_event_fields(
                    args, operation_sha256, operation_result,
                )
                for event in ledger["events"][event_start:]:
                    event.update(operation_fields)
                ledger["updated_at"] = at
                _write_valid_ledger(dao, args.case_id, ledger)
                print(json.dumps(operation_result, sort_keys=True))
                return 0
            record = (
                _current_request_record(item)
                if item.get("current_request_id") is not None
                else None
            )
            if not transition_allowed(
                item["state"],
                args.action,
                actor=actor,
                role_policy=policy,
                has_current_response=(
                    record is not None
                    and record["current_response_id"] is not None
                ),
                has_pending_adjudication=item["current_adjudication_id"] is not None,
            ):
                raise ValueError(
                    "medical-review action is not authorized from the current state"
                )
            at = dao.now_iso()
            previous = item["state"]
            event_response_id = None
            event_assignment_id = None
            event_request_id = None
            if args.action == "provide_information":
                assert data is not None
                if not item_events:
                    raise ValueError("medical review item has no revision history")
                previous_revision = item_events[-1]["medical_variables_revision"]
                if previous_revision["sha256"] == revision["sha256"]:
                    raise ValueError(
                        "no newer canonical medical revision supplies information"
                    )
                previous_variables, error = dao._load_medical_revision(
                    args.case_id,
                    previous_revision["sha256"],
                )
                if error or previous_variables is None:
                    raise ValueError(
                        error or "prior pinned medical revision is unavailable"
                    )
                if _issue_snapshot(
                    previous_variables,
                    item["issue_id"],
                ) == _issue_snapshot(variables, item["issue_id"]):
                    raise ValueError(
                        "new revision does not change issue-linked evidence"
                    )
                _resolve_item_waits(
                    ledger,
                    item["review_item_id"],
                    at=at,
                    resolution="additional_information_committed",
                )
                _append_item_wait(
                    ledger,
                    item,
                    input_kind="referral_decision",
                    request_id=None,
                    at=at,
                    wait_cycle=len(item["decisions"]) + 1,
                )
                destination = "decision_pending"
                reason = data["information"]
            elif args.action == "cancel":
                assert data is not None
                if not isinstance(args.reason, str) or not args.reason.strip():
                    raise ValueError("cancel requires a nonblank reason")
                if data.get("adjudication_id") is not None:
                    raise ValueError(
                        "nonshared cancellation cannot name an adjudication"
                    )
                _resolve_item_waits(
                    ledger,
                    item["review_item_id"],
                    at=at,
                    resolution="cancelled",
                    status="no_longer_required",
                )
                item["current_adjudication_id"] = None
                if item.get("current_request_id") is not None:
                    assert record is not None
                    record["current_assignment_id"] = None
                    record["current_response_id"] = None
                destination = "cancelled"
                reason = args.reason.strip()
            elif args.action == "close":
                if item["state"] == "answered":
                    _assert_fresh_adjudication_cohort(item, ledger)
                _resolve_item_waits(
                    ledger,
                    item["review_item_id"],
                    at=at,
                    resolution="closed",
                )
                if item.get("current_request_id") is not None:
                    assert record is not None
                    record["current_assignment_id"] = None
                destination = "closed"
                reason = args.reason.strip()
            elif args.action in {"assign", "reassign"}:
                assert data is not None
                assert record is not None
                if args.action == "reassign" and (
                    not isinstance(args.reason, str) or not args.reason.strip()
                ):
                    raise ValueError("reassign requires a nonblank reason")
                assignment = _build_assignment(
                    ledger,
                    record,
                    data,
                    actor,
                    policy,
                    at,
                )
                record["assignments"].append(assignment)
                record["current_assignment_id"] = assignment["assignment_id"]
                event_assignment_id = assignment["assignment_id"]
                _resolve_item_waits(
                    ledger,
                    item["review_item_id"],
                    at=at,
                    resolution=(
                        "assigned" if args.action == "assign" else "reassigned"
                    ),
                    status=(
                        "received"
                        if args.action == "assign"
                        else "no_longer_required"
                    ),
                )
                destination = "awaiting_expert"
                reason = (
                    f"assigned {assignment['assignment_id']} to reviewer "
                    f"{data['reviewer_actor_id']}"
                    if args.action == "assign"
                    else args.reason.strip()
                )
                _append_item_wait(
                    ledger,
                    item,
                    input_kind="expert_response",
                    request_id=record["request_id"],
                    at=at,
                )
            elif args.action == "request_information":
                assert data is not None
                assert record is not None
                assignment = _exactly_one(
                    record["assignments"],
                    "assignment_id",
                    record["current_assignment_id"],
                    "current assignment",
                )
                if actor["actor_id"] != assignment["reviewer"]["actor_id"]:
                    raise ValueError(
                        "only the assigned expert may request information"
                    )
                if (
                    data["request_id"] != record["request_id"]
                    or data["request_version"] != assignment["request_version"]
                ):
                    raise ValueError(
                        "information request does not match the active assignment"
                    )
                _resolve_item_waits(
                    ledger,
                    item["review_item_id"],
                    at=at,
                    resolution="expert_requested_information",
                    status="no_longer_required",
                )
                _append_item_wait(
                    ledger,
                    item,
                    input_kind="medical_evidence",
                    request_id=record["request_id"],
                    at=at,
                )
                destination = "expert_needs_information"
                reason = data["information_required"]
            elif args.action == "supplement_package":
                assert data is not None
                assert record is not None
                event_request_id = record["request_id"]
                _require_performed_source_reinspection(
                    data["source_reinspection"]
                )
                if data["request_id"] != record["request_id"]:
                    raise ValueError("supplement request_id is not the current request")
                latest_request = record["versions"][-1]
                decision = _exactly_one(
                    item["decisions"],
                    "decision_id",
                    latest_request["decision_id"],
                    "request decision",
                )
                reinspection_number = len(item["source_reinspection_records"]) + 1
                reinspection = {
                    "source_reinspection_id": (
                        f"{item['review_item_id']}-S{reinspection_number:02d}"
                    ),
                    "medical_variables_revision": revision,
                    **data["source_reinspection"],
                    "actor_or_process": actor["actor_id"],
                    "performed_at": at,
                }
                request_version = len(record["versions"]) + 1
                request = _build_request(
                    dao,
                    variables=variables,
                    item=item,
                    submission={"request": data["request"]},
                    decision=decision,
                    reinspection=reinspection,
                    revision=revision,
                    request_id=record["request_id"],
                    created_at=at,
                    request_version=request_version,
                )
                item["source_reinspection_records"].append(reinspection)
                record["versions"].append(request)
                record["current_assignment_id"] = None
                record["current_response_id"] = None
                _resolve_item_waits(
                    ledger,
                    item["review_item_id"],
                    at=at,
                    resolution="package_supplemented",
                )
                _append_item_wait(
                    ledger,
                    item,
                    input_kind="expert_assignment",
                    request_id=record["request_id"],
                    at=at,
                )
                destination = "package_ready"
                reason = (
                    f"supplemented {record['request_id']} to version "
                    f"{request_version}"
                )
            elif args.action == "withdraw_response":
                assert data is not None
                assert record is not None
                if not isinstance(args.reason, str) or not args.reason.strip():
                    raise ValueError("withdraw_response requires a nonblank reason")
                current_response_id = record.get("current_response_id")
                if not isinstance(current_response_id, str):
                    raise ValueError("withdrawal requires a current response")
                current = _exactly_one(
                    record["responses"],
                    "response_id",
                    current_response_id,
                    "current response",
                )
                if data["response_id"] != current["response_id"]:
                    raise ValueError("withdrawal must target the current response")
                assignment = _exactly_one(
                    record["assignments"],
                    "assignment_id",
                    current["assignment_id"],
                    "response assignment",
                )
                if actor["actor_id"] != assignment["reviewer"]["actor_id"]:
                    raise ValueError(
                        "only the response's assigned expert may withdraw it"
                    )
                response_version = len(record["responses"]) + 1
                withdrawn = {
                    **current,
                    "response_id": (
                        f"{record['request_id']}-R{response_version:02d}"
                    ),
                    "response_version": response_version,
                    "reviewer": actor,
                    "submitted_at": at,
                    "supersedes_response_id": current["response_id"],
                    "response_status": "withdrawn",
                }
                errors = dao._schema_check(
                    withdrawn,
                    "medical_review_response.schema.json",
                )
                if errors:
                    raise ValueError(
                        "prospective withdrawal record is invalid: "
                        + "; ".join(errors)
                    )
                record["responses"].append(withdrawn)
                event_response_id = withdrawn["response_id"]
                record["current_response_id"] = None
                record["current_assignment_id"] = assignment["assignment_id"]
                _resolve_item_waits(
                    ledger,
                    item["review_item_id"],
                    at=at,
                    resolution="response_withdrawn",
                    status="no_longer_required",
                )
                _append_item_wait(
                    ledger,
                    item,
                    input_kind="expert_response",
                    request_id=record["request_id"],
                    at=at,
                )
                destination = "awaiting_expert"
                reason = args.reason.strip()
            elif args.action == "reopen":
                assert data is not None
                if data["decision_owner"] != item["decision_owner"]:
                    raise ValueError("reopen cannot change the immutable decision owner")
                if data["decision_owner"] != "human":
                    raise ValueError("policy-owned medical review is not reconstructed")
                if record is not None:
                    record["current_assignment_id"] = None
                    record["current_response_id"] = None
                item["current_request_id"] = None
                item["current_adjudication_id"] = None
                _append_item_wait(
                    ledger,
                    item,
                    input_kind="referral_decision",
                    request_id=None,
                    at=at,
                    wait_cycle=len(item["decisions"]) + 1,
                )
                destination = "decision_pending"
                reason = data["reason"]
            else:
                assert data is not None
                assert record is not None
                if args.action == "submit_response":
                    assignment = _exactly_one(
                        record["assignments"],
                        "assignment_id",
                        record["current_assignment_id"],
                        "current assignment",
                    )
                else:
                    current_response = _exactly_one(
                        record["responses"],
                        "response_id",
                        record["current_response_id"],
                        "current response",
                    )
                    assignment = _exactly_one(
                        record["assignments"],
                        "assignment_id",
                        current_response["assignment_id"],
                        "response assignment",
                    )
                response = _build_response(
                    dao,
                    item=item,
                    record=record,
                    assignment=assignment,
                    actor=actor,
                    submission=data["response"],
                    action=args.action,
                    at=at,
                )
                event_response_id = response["response_id"]
                record["responses"].append(response)
                record["current_assignment_id"] = assignment["assignment_id"]
                if args.action == "submit_response":
                    _resolve_item_waits(
                        ledger,
                        item["review_item_id"],
                        at=at,
                        resolution="response_submitted",
                    )
                    reason = f"submitted completed response {response['response_id']}"
                else:
                    reason = data["reason"]
                record["current_response_id"] = response["response_id"]
                destination = "answered"
                if not any(
                    wait["status"] == "waiting"
                    and wait["review_item_id"] == item["review_item_id"]
                    and wait["input_kind"] == "coordinator_disposition"
                    for wait in ledger["wait_episodes"]
                ):
                    _append_item_wait(
                        ledger,
                        item,
                        input_kind="coordinator_disposition",
                        request_id=record["request_id"],
                        at=at,
                    )
            event_id = f"MRE_{ledger['next_event_number']:06d}"
            ledger["next_event_number"] += 1
            item["state"] = destination
            item["updated_at"] = at
            operation_result = {
                "action": args.action,
                "review_item_id": item["review_item_id"],
                "state": destination,
                "decision_id": None,
            }
            event = {
                "event_id": event_id,
                "review_item_id": item["review_item_id"],
                "action": args.action,
                "from_state": previous,
                "to_state": destination,
                "actor": actor,
                "role_policy_snapshot": policy,
                **_operation_event_fields(
                    args, operation_sha256, operation_result,
                ),
                "reason": reason,
                "medical_variables_revision": revision,
                "created_at": at,
            }
            if event_response_id is not None:
                event["response_id"] = event_response_id
            if event_assignment_id is not None:
                event["assignment_id"] = event_assignment_id
            if event_request_id is not None:
                event["request_id"] = event_request_id
            ledger["events"].append(event)
            ledger["updated_at"] = at
            _write_valid_ledger(dao, args.case_id, ledger)
        except (KeyError, TypeError, ValueError) as exc:
            print(f"FAIL: {exc}")
            return 1
        print(json.dumps(operation_result, sort_keys=True))
        return 0
    finally:
        dao.release_lock(path)


def clearance_blockers(
    ledger: dict,
    variables: dict,
    *,
    current_revision_sha: str | None = None,
) -> tuple[list[str], list[str]]:
    """Return unresolved item IDs and issue-coverage errors.

    Coverage is fail-closed: publication cannot invent whether an issue is policy- or
    human-owned, so each issue must later be represented by an explicit lifecycle
    item before the gate may clear.
    """
    issue_ids = {
        issue["issue_id"]
        for issue in variables.get("medical_issues", [])
        if isinstance(issue, dict) and isinstance(issue.get("issue_id"), str)
    }
    item_issue_ids = [
        item["issue_id"]
        for item in ledger.get("review_items", [])
        if isinstance(item, dict) and isinstance(item.get("issue_id"), str)
    ]
    coverage_errors = [
        *(
            f"missing:{issue_id}"
            for issue_id in sorted(issue_ids - set(item_issue_ids))
        ),
        *(
            f"orphan:{issue_id}"
            for issue_id in sorted(set(item_issue_ids) - issue_ids)
        ),
    ]
    blocking = {
        item["review_item_id"]
        for item in ledger.get("review_items", [])
        if item.get("state") in BLOCKING_STATES
    }
    if current_revision_sha is not None:
        latest_event_by_item: dict[str, dict] = {}
        for event in ledger.get("events", []):
            item_id = event.get("review_item_id")
            if isinstance(item_id, str):
                latest_event_by_item[item_id] = event
        for item in ledger.get("review_items", []):
            item_id = item.get("review_item_id")
            event = latest_event_by_item.get(item_id)
            revision = (event or {}).get("medical_variables_revision")
            if not isinstance(revision, dict) or revision.get("sha256") != current_revision_sha:
                coverage_errors.append(f"stale:{item_id}")
    return sorted(blocking), sorted(coverage_errors)


def require_clearance(dao, case_id: str, run_id: str) -> None:
    ledger = load_ledger(dao, case_id)
    variables, error = dao._load_medical_revision(case_id, None)
    if error or variables is None:
        raise ValueError(error or "canonical medical variables are unavailable")
    if variables.get("run_id") != run_id:
        raise ValueError("medical clearance does not match the canonical run owner")
    current_revision_sha = hashlib.sha256(
        dao.medical_variables_path(case_id).read_bytes()
    ).hexdigest()
    blocking, coverage_errors = clearance_blockers(
        ledger,
        variables,
        current_revision_sha=current_revision_sha,
    )
    if blocking or coverage_errors:
        raise ValueError(
            "medical review clearance is blocked: "
            f"items={blocking}; coverage={coverage_errors}"
        )


def cmd_check_clear(dao, args) -> int:
    try:
        ledger = load_ledger(dao, args.case_id)
        variables, error = dao._load_medical_revision(args.case_id, None)
        if error or variables is None:
            raise ValueError(error or "canonical medical variables are unavailable")
    except ValueError as exc:
        print(json.dumps({"clear": False, "error": str(exc)}, sort_keys=True))
        return 1

    current_revision_sha = hashlib.sha256(
        dao.medical_variables_path(args.case_id).read_bytes()
    ).hexdigest()
    blocking, coverage_errors = clearance_blockers(
        ledger,
        variables,
        current_revision_sha=current_revision_sha,
    )
    print(
        json.dumps(
            {
                "clear": not blocking and not coverage_errors,
                "blocking_review_item_ids": blocking,
                "issue_coverage_errors": coverage_errors,
            },
            sort_keys=True,
        )
    )
    return 0 if not blocking and not coverage_errors else 1


def _revision_descriptor(variables: dict, sha256: str) -> dict:
    return {
        "sha256": sha256,
        "run_id": variables["run_id"],
        "schema_version": variables["schema_version"],
        "config_version": variables["config_version"],
    }


def _build_downstream_review_outcomes_locked(dao, case_id: str) -> dict:
    """Build outcomes while publication's variables->ledger locks are held."""
    ledger = load_ledger(dao, case_id)
    variables, error = dao._load_medical_revision(case_id, None)
    if error or variables is None:
        raise ValueError(error or "canonical medical variables are unavailable")
    current_sha = hashlib.sha256(
        dao.medical_variables_path(case_id).read_bytes()
    ).hexdigest()
    blocking, coverage_errors = clearance_blockers(
        ledger,
        variables,
        current_revision_sha=current_sha,
    )
    if blocking or coverage_errors:
        raise ValueError(
            "medical review outcomes are unavailable while clearance is blocked: "
            + ", ".join([*blocking, *coverage_errors])
        )

    latest_event_by_item: dict[str, dict] = {}
    for event in ledger["events"]:
        latest_event_by_item[event["review_item_id"]] = event
    projected_items = []
    for item in ledger["review_items"]:
        item_events = [
            event for event in ledger["events"]
            if event["review_item_id"] == item["review_item_id"]
        ]
        latest_decision_cycle_index = max(
            (
                index for index, event in enumerate(item_events)
                if event["action"] in {"reopen", "provide_information"}
            ),
            default=0,
        )
        current_cycle_decision_ids = {
            event["decision_id"]
            for event in item_events[latest_decision_cycle_index:]
            if event.get("decision_id") is not None
        }
        decision = next(
            (
                row for row in reversed(item["decisions"])
                if row["decision_id"] in current_cycle_decision_ids
            ),
            None,
        )
        projected_decision = None if decision is None else {
            **{
                key: decision[key]
                for key in (
                    "decision_id", "decision", "decision_origin", "rationale",
                    "policy_version", "actor",
                )
            },
            "medical_variables_revision": decision["referral_inputs"][
                "medical_variables_revision"
            ],
        }
        request = (
            _current_request_record(item)
            if item.get("current_request_id") is not None
            else None
        )
        response = None
        if request is not None and request.get("current_response_id") is not None:
            response = _exactly_one(
                request["responses"],
                "response_id",
                request["current_response_id"],
                "current response",
            )
        assignment = None
        if response is not None:
            assert request is not None
            assignment = _exactly_one(
                request["assignments"],
                "assignment_id",
                response["assignment_id"],
                "response assignment",
            )
        if response is not None:
            assert assignment is not None
        projected_response = None if response is None else {
            **{
                key: response[key]
                for key in (
                    "response_id", "response_version", "request_id",
                    "request_version_reviewed", "assignment_id",
                    "request_config_version_reviewed",
                    "referral_policy_version_reviewed", "issue_id", "issue_category",
                    "decision_id", "evidence_locator_ids_reviewed",
                    "interpretation", "basis", "uncertainty",
                    "alternative_interpretations", "downstream_adjustment_advice",
                    "supersedes_response_id", "response_status", "reviewed_at",
                    "submitted_at",
                )
            },
            "reviewer": dict(response["reviewer"]),
            "assignment_role_policy_version": assignment["role_policy_version"],
        }
        item_by_id = {
            candidate["review_item_id"]: candidate
            for candidate in ledger["review_items"]
        }

        def adjudication_cohort_is_current(adjudication: dict) -> bool:
            for participant_id, adjudicated_response_id in zip(
                adjudication["review_item_ids"],
                adjudication["response_ids"],
                strict=True,
            ):
                participant = item_by_id.get(participant_id)
                if participant is None:
                    return False
                if participant.get("current_request_id") is None:
                    return False
                _, current_response_id = _item_response_head(participant)
                if current_response_id != adjudicated_response_id:
                    return False
            return True

        related_adjudications = [
            row for row in ledger["adjudications"]
            if item["review_item_id"] in row["review_item_ids"]
            and row.get("status") == "resolved"
            and response is not None
            and response["response_id"] in row["response_ids"]
            and adjudication_cohort_is_current(row)
        ]
        adjudication = related_adjudications[-1] if related_adjudications else None
        projected_adjudication = None if adjudication is None else {
            "adjudication_id": adjudication["adjudication_id"],
            "response_ids": adjudication["response_ids"],
            "adjudicator": adjudication["adjudicator"],
            "record": adjudication["record"],
            "resolved_at": adjudication["resolved_at"],
        }
        projected_items.append({
            "review_item_id": item["review_item_id"],
            "issue_id": item["issue_id"],
            "state": item["state"],
            "target_medical_variables_revision": latest_event_by_item[
                item["review_item_id"]
            ]["medical_variables_revision"],
            "decision": projected_decision,
            "response": projected_response,
            "adjudication": projected_adjudication,
        })

    projection = {
        "schema_version": "medical_review_outcomes.v0.1",
        "case_id": case_id,
        "medical_variables_revision": _revision_descriptor(
            variables,
            current_sha,
        ),
        "ledger_updated_at": ledger["updated_at"],
        "review_items": projected_items,
    }
    errors = dao._schema_check(
        projection,
        "medical_review_outcomes.schema.json",
    )
    if errors:
        raise ValueError(
            "downstream medical-review outcomes are invalid: "
            + "; ".join(errors)
        )
    return projection


def build_downstream_review_outcomes(dao, case_id: str) -> dict:
    """Build a bounded outcome from one publication-consistent snapshot."""
    variables_target = dao.medical_variables_path(case_id)
    ledger_target = ledger_path(dao, case_id)
    run_id = dao.validated_run_state(case_id).get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("canonical run owner is unavailable for outcome projection")
    variables_lock = dao.acquire_lock_blocking(
        variables_target,
        "medical-outcome-reader",
        run_id,
        "read publication-consistent medical-review outcomes",
    )
    if variables_lock is not None:
        raise ValueError("medical variables are locked during outcome projection")
    ledger_acquired = False
    try:
        ledger_lock = dao.acquire_lock_blocking(
            ledger_target,
            "medical-outcome-reader",
            run_id,
            "read publication-consistent medical-review outcomes",
        )
        if ledger_lock is not None:
            raise ValueError("medical review ledger is locked during outcome projection")
        ledger_acquired = True
        return _build_downstream_review_outcomes_locked(dao, case_id)
    finally:
        if ledger_acquired:
            dao.release_lock(ledger_target)
        dao.release_lock(variables_target)


def cmd_read_outcomes(dao, args) -> int:
    try:
        projection = build_downstream_review_outcomes(dao, args.case_id)
    except (KeyError, ValueError) as exc:
        print(f"BLOCKED: {exc}")
        return 1
    print(json.dumps(projection, ensure_ascii=False, sort_keys=True))
    return 0
