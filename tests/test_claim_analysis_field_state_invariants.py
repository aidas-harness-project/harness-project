"""The five field states, and what each one is required to carry.

These states are the contract's way of keeping three different situations
apart that all *look* like "no value": a source that affirmatively said the
thing is not there, a search that found nothing, and a field the case's own
facts exclude. Collapsing them is how a missing document turns into a decided
finding, so every invariant below is asserted against the schema rather than
left to the driver's good behaviour.

Each test builds a VALID instance and then breaks exactly one invariant, so a
failure names the rule that stopped holding instead of reporting that some
large fixture no longer validates.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
SCHEMA = "claim_analysis_result.schema.json"

QUOTE = "우측 요골 골절"
EVIDENCE = {
    "document_id": "DOC_001", "page": 1, "quote": QUOTE,
    "start_char": 0, "end_char": len(QUOTE),
}


def _errors(instance: dict) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, SCHEMA, schemas, registry)


def _field_errors(field: dict) -> list[str]:
    """Validate one claim fact inside an otherwise-valid result."""
    result = _result([field])
    return [e for e in _errors(result) if e.startswith("claim_facts")]


def _result(claim_facts: list[dict]) -> dict:
    assessments = [{
        "case_type": case_type,
        "status": "uncertain",
        "triggered_field_ids": [],
        "conflicting_field_ids": [],
        "filing_status": "unknown",
        "reason": "No decisive fact was found.",
        "evidence_references": [],
    } for case_type in (
        "personal_insurance", "traffic_accident", "industrial_accident", "liability"
    )]
    return {
        "case_id": "CASE_9001",
        "run_id": "RUN_20260819_1",
        "component": "claim-analysis",
        "status": "success",
        "created_at": "2026-08-19T00:00:00+00:00",
        "model_info": {"model_name": "deterministic", "prompt_version": "v0.1"},
        "review_required": False,
        "warnings": [],
        "source_grounded": True,
        "schema_version": "claim_analysis_result.v0.1",
        "config_version": "claim_analysis_routing.v0.1",
        "medical_projection_status": "not_configured",
        "claim_facts": claim_facts,
        "case_type_assessment": assessments,
        "policy_links": [],
        "required_document_checklist": [],
        "conflict_candidates": [],
    }


def _base_field(**overrides) -> dict:
    field = {
        "field_id": "primary_diagnosis",
        "domain_code": "diagnosis",
        "priority_grade": "A",
        "authority": "source_document_extraction",
        "resolution_status": "asserted",
        "selected_observation_ids": ["CAO_0001"],
        "observations": [],
        "conflict_candidate_ids": [],
        "stop_reason": "trusted_value_found",
    }
    field.update(overrides)
    return field


def _asserted_observation(number: int = 1, value: str = QUOTE) -> dict:
    return {
        "observation_id": f"CAO_{number:04d}",
        "value_state": "asserted",
        "value": value,
        "source_document_kind": "diagnosis_certificate",
        "source_priority_rank": 1,
        "extraction_wave": "A",
        "evidence_references": [EVIDENCE],
    }


def _asserted_field() -> dict:
    return _base_field(observations=[_asserted_observation()])


def _explicitly_absent_field() -> dict:
    return _base_field(
        resolution_status="explicitly_absent",
        selected_observation_ids=[],
        stop_reason="explicitly_absent",
        resolution_reason="The report states the finding is not present.",
        observations=[{
            "observation_id": "CAO_0001",
            "value_state": "explicitly_absent",
            "reason": "The imaging report states there is no acute fracture.",
            "source_document_kind": "imaging_interpretation",
            "source_priority_rank": 1,
            "extraction_wave": "A",
            "evidence_references": [EVIDENCE],
        }],
    )


def _unavailable_field() -> dict:
    return _base_field(
        resolution_status="unavailable",
        selected_observation_ids=[],
        stop_reason="sources_exhausted",
        resolution_reason="No routed source stated this field.",
        unavailable_reason="not_mentioned",
        observations=[],
    )


def _not_applicable_field() -> dict:
    return _base_field(
        resolution_status="not_applicable",
        selected_observation_ids=[],
        stop_reason="not_applicable",
        resolution_reason="The recorded accident excludes this field.",
        observations=[{
            "observation_id": "CAO_0001",
            "value_state": "not_applicable",
            "reason": "The accident account excludes this field.",
            "extraction_wave": "A",
            "evidence_references": [EVIDENCE],
        }],
    )


def _conflict_field() -> dict:
    return _base_field(
        resolution_status="conflict",
        selected_observation_ids=[],
        stop_reason="conflict_found",
        resolution_reason="Two priority sources state different values.",
        conflict_candidate_ids=["CAC_0001"],
        observations=[_asserted_observation(1, "우측 요골 골절"),
                      _asserted_observation(2, "좌측 요골 골절")],
    )


ALL_STATES = {
    "asserted": _asserted_field,
    "explicitly_absent": _explicitly_absent_field,
    "unavailable": _unavailable_field,
    "not_applicable": _not_applicable_field,
    "conflict": _conflict_field,
}


@pytest.mark.parametrize("state", sorted(ALL_STATES))
def test_every_state_has_a_valid_shape(state: str) -> None:
    """Each state is reachable. Without this the negative tests prove nothing."""
    assert _field_errors(ALL_STATES[state]()) == []


def test_unavailable_reason_enum_matches_spec() -> None:
    schema = json.loads((ROOT / "schemas" / SCHEMA).read_text(encoding="utf-8"))
    expected = {
        "not_mentioned", "source_document_missing", "unreadable",
        "outside_poc_scope", "conflict_unresolved",
        # Added 2026-08-21. `not_mentioned` asserts that sources WERE read and
        # said nothing, which a reviewer can act on by requesting documents.
        # Two situations were being reported that way while nothing had been
        # read at all: a conditional route whose trigger never fired, and an
        # opportunistic field that schedules no read of its own.
        "route_not_activated", "not_scheduled",
        # Added 2026-08-26. The opposite error to the two above: a source WAS
        # read and DID state a value, but the reading failed the trusted-value
        # requirements (complete/unambiguous) and used to be discarded at the
        # gate -- leaving the field reporting `not_mentioned` about text the
        # model had quoted verbatim. Measured on CASE_7015 as 5 of 40
        # unavailable fields, among them 사고일 and 수술명. Requesting more
        # records cannot close it; a person must read the preserved quote.
        "partial_reading_only",
        # Added 2026-08-26. A form that PRINTS the field and leaves the cell
        # empty is neither of the above: the document did raise the item, so
        # `not_mentioned`'s promise of "a records gap a request may close" is
        # false -- no further document will ever fill that cell. Measured on
        # CASE_7061 DOC_003, a McBride disability certificate whose 기왕증 /
        # 기왕증 기여율 / 기존장애 / 수상일·초진일·장해진단일 rows were all
        # printed and all blank, every one published `not_mentioned`. The field
        # still holds NO value; only the cause, and the action it implies,
        # change.
        "printed_but_blank",
    }
    assert set(schema["$defs"]["field_result"]["properties"]["unavailable_reason"]["enum"]) == expected
    assert set(schema["$defs"]["observation"]["properties"]["unavailable_reason"]["enum"]) == expected


# ------------------------------------------------------------- asserted --

def test_asserted_selects_exactly_one_observation() -> None:
    two_selected = _asserted_field()
    two_selected["observations"].append(_asserted_observation(2, "골절"))
    two_selected["selected_observation_ids"] = ["CAO_0001", "CAO_0002"]
    assert _field_errors(two_selected) != []

    none_selected = _asserted_field()
    none_selected["selected_observation_ids"] = []
    assert _field_errors(none_selected) != []


def test_asserted_requires_exact_evidence() -> None:
    field = _asserted_field()
    field["observations"][0]["evidence_references"] = []
    assert _field_errors(field) != []


# ----------------------------------------------------- explicitly_absent --

def test_explicitly_absent_requires_the_quote_stating_the_absence() -> None:
    """An absence claim without the sentence making it is just silence."""
    field = _explicitly_absent_field()
    field["observations"][0]["evidence_references"] = []
    assert _field_errors(field) != []


def test_explicitly_absent_elects_no_value() -> None:
    field = _explicitly_absent_field()
    field["selected_observation_ids"] = ["CAO_0001"]
    assert _field_errors(field) != []


def test_explicitly_absent_carries_no_value() -> None:
    field = _explicitly_absent_field()
    field["observations"][0]["value"] = "우측 요골 골절"
    assert _field_errors(field) != []


def test_absence_scope_does_not_extend_to_another_field() -> None:
    """One absence quote may not underwrite a second field's absence.

    Each field's absence has to be stated about THAT field. Reusing one
    observation across two claim facts would let "no acute fracture" also mean
    "no surgery" -- the schema catches it because an observation id belongs to
    exactly one field.
    """
    first = _explicitly_absent_field()
    second = _explicitly_absent_field()
    second["field_id"] = "surgery_or_procedure_name"
    second["domain_code"] = "treatment"
    # Same observation id: the second field is borrowing the first's evidence.
    errors = [e for e in _errors(_result([first, second]))
              if "duplicated across claim facts" in e or e.startswith("claim_facts")]
    assert errors != []


# ---------------------------------------------------------- unavailable --

def test_unavailable_requires_a_reason() -> None:
    field = _unavailable_field()
    del field["unavailable_reason"]
    assert _field_errors(field) != []


def test_unavailable_cites_no_evidence() -> None:
    """Nothing was established, so there is nothing to cite."""
    field = _unavailable_field()
    field["observations"] = [{
        "observation_id": "CAO_0001",
        "value_state": "unavailable",
        "reason": "not stated",
        "unavailable_reason": "not_mentioned",
        "extraction_wave": "A",
        "evidence_references": [EVIDENCE],
    }]
    assert _field_errors(field) != []


def test_unavailable_retains_no_asserted_reading() -> None:
    field = _unavailable_field()
    field["observations"] = [_asserted_observation()]
    assert _field_errors(field) != []


def test_silence_is_unavailable_not_explicitly_absent() -> None:
    """The distinction the whole vocabulary exists for.

    A silent source produces an unavailable field with `not_mentioned`. Filing
    that same situation as `explicitly_absent` requires a quote it does not
    have, so the schema refuses the relabelling.
    """
    silent = _unavailable_field()
    assert silent["unavailable_reason"] == "not_mentioned"
    assert _field_errors(silent) == []

    relabelled = deepcopy(silent)
    relabelled["resolution_status"] = "explicitly_absent"
    relabelled["stop_reason"] = "explicitly_absent"
    del relabelled["unavailable_reason"]
    assert _field_errors(relabelled) != []


# -------------------------------------------------------- not_applicable --

def test_not_applicable_requires_a_grounded_observation() -> None:
    """No observations means no excluding fact was ever shown."""
    field = _not_applicable_field()
    field["observations"] = []
    assert _field_errors(field) != []


def test_not_applicable_requires_evidence_on_its_observation() -> None:
    field = _not_applicable_field()
    field["observations"][0]["evidence_references"] = []
    assert _field_errors(field) != []


def test_silence_cannot_become_not_applicable() -> None:
    """A search that found nothing is not a finding that the field is moot.

    This is the same relabelling as the explicitly_absent case and is refused
    for the same reason: `not_applicable` owes the case fact that excludes the
    field, and an exhausted search has none.
    """
    relabelled = _unavailable_field()
    relabelled["resolution_status"] = "not_applicable"
    relabelled["stop_reason"] = "not_applicable"
    del relabelled["unavailable_reason"]
    assert _field_errors(relabelled) != []


# --------------------------------------------------------------- conflict --

def test_conflict_keeps_both_readings_and_elects_neither() -> None:
    field = _conflict_field()
    assert _field_errors(field) == []

    elected = _conflict_field()
    elected["selected_observation_ids"] = ["CAO_0001"]
    assert _field_errors(elected) != []


def test_conflict_needs_two_observations_and_a_candidate() -> None:
    single = _conflict_field()
    single["observations"] = [_asserted_observation()]
    assert _field_errors(single) != []

    uncandidated = _conflict_field()
    uncandidated["conflict_candidate_ids"] = []
    assert _field_errors(uncandidated) != []
