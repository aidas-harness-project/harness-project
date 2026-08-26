"""Selective Claim Analysis driver tests.

Synthetic only: version-controlled config/schemas plus in-memory documents.
Never reads source-cases, outputs, data, or ground truth.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance
import claim_analysis_selection as selection
import run_claim_analysis_selective as driver


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = (
    ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
)


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _errors(instance: dict, schema_name: str) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(instance, schema_name, schemas, registry)


def _revision() -> dict:
    return {
        "sha256": "a" * 64,
        "run_id": "RUN_20260819_1",
        "schema_version": "medical_variables.v0.1",
        "config_version": "medical_structuring.v0.1",
    }


# ------------------------------------------------------ document intake --

def test_missing_medical_block_is_untyped_not_non_medical() -> None:
    manifest = {"documents": [
        {"document_id": "DOC_001"},
        {"document_id": "DOC_002", "medical_classification": {
            "status": "not_medical", "kind": None}},
        {"document_id": "DOC_003", "medical_classification": {
            "status": "ambiguous", "kind": None}},
        {"document_id": "DOC_004", "medical_classification": {
            "status": "deterministic_title", "kind": "diagnosis_certificate"}},
    ]}
    refs = {ref.document_id: ref for ref in driver.classified_documents(manifest)}
    assert refs["DOC_001"].kind is None and not refs["DOC_001"].ambiguous
    assert refs["DOC_002"].kind is None
    assert refs["DOC_003"].ambiguous is True
    assert refs["DOC_004"].kind == "diagnosis_certificate"


# ------------------------------------------------- evidence verification --

def test_exact_range_is_located_only_when_the_quote_is_unique() -> None:
    text = "우측 요골 골절 진단. 우측 요골 골절 재확인."
    located = driver.locate_exact(text, "재확인")
    assert located == (text.index("재확인"), text.index("재확인") + 3)
    assert text[located[0]:located[1]] == "재확인"
    # Occurs twice -- refused rather than bound to the first occurrence.
    assert driver.locate_exact(text, "우측 요골 골절") is None
    assert driver.locate_exact(text, "없는문구") is None


def test_reference_offsets_round_trip_against_the_page_text() -> None:
    text = "환자는 2026-03-02 계단에서 넘어져 우측 요골 골절."
    reference = driver.build_reference("DOC_001", 1, "계단에서 넘어져", text)
    assert reference is not None
    assert text[reference["start_char"]:reference["end_char"]] == reference["quote"]


# ------------------------------------------------------------ stop rule --

def _extractor(answers):
    """Build an extract() that answers per (field_id, document_id)."""
    calls: list[tuple[str, str]] = []

    def extract(field_row, document_id, kind, rank):
        calls.append((field_row["field_id"], document_id))
        return answers.get((field_row["field_id"], document_id))

    return extract, calls


def test_search_stops_at_the_first_trusted_value_in_priority_order() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
        selection.DocumentRef("DOC_003", "outpatient_record"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    page_text = {
        ("DOC_001", 1): "진단명: 우측 요골 골절",
        ("DOC_002", 1): "진단명: 우측 요골 골절",
        ("DOC_003", 1): "진단명: 우측 요골 골절",
    }
    extract, calls = _extractor({
        ("primary_diagnosis", "DOC_001"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        ("primary_diagnosis", "DOC_002"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        ("primary_diagnosis", "DOC_003"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())

    assert outcome.status == "asserted"
    assert outcome.stop_reason == "trusted_value_found"
    # primary_diagnosis is critical, so exactly ONE comparison is bought --
    # the third-priority document is never opened.
    assert [doc for _, doc in calls] == ["DOC_001", "DOC_002"]
    assert outcome.comparisons == 1


def test_a_non_critical_field_never_opens_a_second_source() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "diagnosis_department"
    )
    assert not field_row["critical_conflict_field"]
    documents = [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    page_text = {("DOC_001", 1): "진료과: 정형외과", ("DOC_002", 1): "진료과: 정형외과"}
    extract, calls = _extractor({
        ("diagnosis_department", "DOC_001"): {
            "value": "정형외과", "page": 1, "quote": "정형외과"},
        ("diagnosis_department", "DOC_002"): {
            "value": "정형외과", "page": 1, "quote": "정형외과"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())
    assert outcome.status == "asserted"
    assert [doc for _, doc in calls] == ["DOC_001"]


def test_two_disagreeing_sources_preserve_both_and_elect_no_canonical() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    page_text = {
        ("DOC_001", 1): "진단명: 우측 요골 골절",
        ("DOC_002", 1): "진단명: 좌측 요골 골절",
    }
    extract, _ = _extractor({
        ("primary_diagnosis", "DOC_001"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        ("primary_diagnosis", "DOC_002"): {
            "value": "좌측 요골 골절", "page": 1, "quote": "좌측 요골 골절"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())

    assert outcome.status == "conflict"
    assert outcome.stop_reason == "conflict_found"
    assert outcome.selected_ids == []
    assert len(outcome.observations) == 2
    assert {o["value"] for o in outcome.observations} == {
        "우측 요골 골절", "좌측 요골 골절"
    }


def test_silence_in_a_source_is_not_a_conflict() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    extract, _ = _extractor({
        ("primary_diagnosis", "DOC_001"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        # DOC_002 simply does not mention it.
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract,
        {("DOC_001", 1): "진단명: 우측 요골 골절"},
        driver._observation_id_sequence())
    assert outcome.status == "asserted"


def test_the_comparison_budget_holds_across_priority_groups() -> None:
    """One extra source total, not one per rung."""
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("D1", "diagnosis_certificate"),
        selection.DocumentRef("D2", "admission_discharge_summary"),
        selection.DocumentRef("D3", "initial_visit_record"),
        selection.DocumentRef("D4", "outpatient_record"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    assert len(plan.steps) == 4, "the fixture must span four priority rungs"

    page_text = {(d, 1): "우측 요골 골절" for d in ("D1", "D2", "D3", "D4")}
    extract, calls = _extractor({
        ("primary_diagnosis", d): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"}
        for d in ("D1", "D2", "D3", "D4")
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())
    assert [doc for _, doc in calls] == ["D1", "D2"]
    assert outcome.comparisons == 1


def test_a_silent_source_does_not_consume_the_comparison_budget() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    documents = [
        selection.DocumentRef("D1", "diagnosis_certificate"),
        selection.DocumentRef("D2", "admission_discharge_summary"),
        selection.DocumentRef("D3", "initial_visit_record"),
    ]
    plan = selection.plan_field(field_row, config, documents)
    page_text = {(d, 1): "우측 요골 골절" for d in ("D1", "D2", "D3")}
    extract, calls = _extractor({
        # D1 says nothing; the trusted value and its one comparison come from
        # D2 and D3.
        ("primary_diagnosis", "D2"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
        ("primary_diagnosis", "D3"): {
            "value": "우측 요골 골절", "page": 1, "quote": "우측 요골 골절"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract, page_text,
        driver._observation_id_sequence())
    assert [doc for _, doc in calls] == ["D1", "D2", "D3"]
    assert outcome.status == "asserted"
    assert outcome.comparisons == 1


def test_a_quote_absent_from_the_page_is_dropped_never_repaired() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "primary_diagnosis"
    )
    plan = selection.plan_field(
        field_row, config, [selection.DocumentRef("DOC_001", "diagnosis_certificate")])
    extract, _ = _extractor({
        ("primary_diagnosis", "DOC_001"): {
            "value": "우측 요골 골절", "page": 1, "quote": "이 문구는 페이지에 없다"},
    })
    outcome = driver.resolve_field(
        plan, field_row, config, extract,
        {("DOC_001", 1): "진단명: 우측 요골 골절"},
        driver._observation_id_sequence())
    assert outcome.status == "unavailable"
    assert outcome.unavailable_reason == "not_mentioned"
    assert outcome.observations == []


# -------------------------------------------------------- result shape --

_OBSERVATION_IDS = driver._observation_id_sequence()


def _resolved_outcome(field_id: str, domain: str, value, grade="A") -> driver.FieldExtractionOutcome:
    # Observation IDs are unique case-wide, not per field: the semantic
    # validator rejects the same id appearing under two claim facts.
    observation_id = next(_OBSERVATION_IDS)
    outcome = driver.FieldExtractionOutcome(
        field_id=field_id, domain_code=domain, grade=grade)
    outcome.status = "asserted"
    outcome.stop_reason = "trusted_value_found"
    outcome.selected_ids = [observation_id]
    outcome.observations = [{
        "observation_id": observation_id,
        "value_state": "asserted",
        "value": value,
        "source_document_kind": "diagnosis_certificate",
        "source_priority_rank": 1,
        "extraction_wave": "A",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": "q",
            "start_char": 0, "end_char": 1,
        }],
    }]
    outcome.documents_read = 1
    return outcome


def test_built_result_validates_against_the_selective_schema() -> None:
    config = _config()
    outcomes = [
        _resolved_outcome("primary_diagnosis", "diagnosis", "우측 요골 골절"),
        _resolved_outcome("injury_event_present", "event_timeline", True, grade="C"),
    ]
    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=outcomes,
        config=config,
        documents=[selection.DocumentRef("DOC_001", "diagnosis_certificate")],
        medical_revision_context=None,
    )
    assert _errors(result, "claim_analysis_result.schema.json") == []
    assert len(result["case_type_assessment"]) == 4


def test_medical_domain_facts_are_published_as_source_extraction_not_projection() -> None:
    """A medical value read from a document is labelled as what it is.

    The lane resolves no field id to a canonical variable id, so calling this
    a projection would claim a binding nothing computed. `event_timeline`
    stays claim-native: accident circumstances are this stage's own finding.
    """
    config = _config()
    outcomes = [
        _resolved_outcome("primary_diagnosis", "diagnosis", "골절"),
        _resolved_outcome("injury_event_present", "event_timeline", True),
    ]
    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=outcomes,
        config=config, documents=[], medical_revision_context=None)
    by_field = {row["field_id"]: row for row in result["claim_facts"]}
    assert by_field["primary_diagnosis"]["authority"] == "source_document_extraction"
    assert by_field["injury_event_present"]["authority"] == "claim_analysis_native"


def test_conflict_becomes_a_candidate_and_never_a_ledger_entry() -> None:
    config = _config()
    outcome = driver.FieldExtractionOutcome(
        field_id="primary_diagnosis", domain_code="diagnosis", grade="A")
    outcome.status = "conflict"
    outcome.stop_reason = "conflict_found"
    outcome.reason = "two sources disagree"
    outcome.observations = [{
        "observation_id": f"CAO_000{n}",
        "value_state": "asserted",
        "value": value,
        "source_document_kind": "diagnosis_certificate",
        "source_priority_rank": n,
        "extraction_wave": "A",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": "q",
            "start_char": 0, "end_char": 1,
        }],
    } for n, value in ((1, "우측"), (2, "좌측"))]

    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=[outcome],
        config=config, documents=[], medical_revision_context=None)

    assert _errors(result, "claim_analysis_result.schema.json") == []
    assert len(result["conflict_candidates"]) == 1
    candidate = result["conflict_candidates"][0]
    assert candidate["consistency_status"] == "pending_consistency_check"
    assert candidate["field_id"] == "primary_diagnosis"
    assert len(candidate["observation_ids"]) == 2
    fact = result["claim_facts"][0]
    assert fact["selected_observation_ids"] == []
    assert fact["conflict_candidate_ids"] == [candidate["conflict_candidate_id"]]


def test_unavailable_field_retains_no_asserted_observation() -> None:
    config = _config()
    outcome = driver.FieldExtractionOutcome(
        field_id="primary_diagnosis", domain_code="diagnosis", grade="A")
    outcome.observations = [{
        "observation_id": "CAO_0001",
        "value_state": "asserted",
        "value": "골절",
        "source_document_kind": "diagnosis_certificate",
        "source_priority_rank": 1,
        "extraction_wave": "A",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": "q",
            "start_char": 0, "end_char": 1,
        }],
    }]
    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=[outcome],
        config=config, documents=[], medical_revision_context=None)
    assert _errors(result, "claim_analysis_result.schema.json") == []
    assert result["claim_facts"][0]["observations"][0]["value_state"] == "unavailable"


def test_c_grade_override_field_publishes_as_b_not_c() -> None:
    config = _config()
    field_row = next(
        r for r in config["fields"] if r["field_id"] == "injury_event_present"
    )
    assert field_row["medical_advisory_grade"] == "C"
    assert driver.priority_grade_for(field_row) == "B"


# --------------------------------------------------------------- trace --

def test_trace_is_separate_from_the_result_and_validates() -> None:
    config = _config()
    outcomes = [_resolved_outcome("primary_diagnosis", "diagnosis", "골절")]
    trace = driver.build_trace(
        case_id="CASE_9001", run_id="RUN_20260819_1", config=config,
        outcomes=outcomes,
        document_dispositions=[
            {"document_id": "DOC_001",
             "medical_document_kind": "diagnosis_certificate",
             "disposition": "read", "wave": "A",
             "reason": "priority source for primary_diagnosis",
             "field_ids": ["primary_diagnosis"]},
            {"document_id": "DOC_002",
             "medical_document_kind": "medical_expense_receipt",
             "disposition": "presence_only", "wave": "not_read",
             "reason": "cost document -- presence only",
             "field_ids": []},
        ],
        provider_calls=1)
    assert _errors(trace, "claim_analysis_trace.schema.json") == []
    assert trace["metrics"]["documents_presence_only"] == 1
    assert trace["metrics"]["documents_read"] == 1
    # The authority contract must not carry reading telemetry.
    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260819_1", outcomes=outcomes,
        config=config, documents=[], medical_revision_context=None)
    assert "documents" not in result and "field_stops" not in result


def test_unmeasured_token_counts_record_as_null_not_zero() -> None:
    config = _config()
    trace = driver.build_trace(
        case_id="CASE_9001", run_id="RUN_20260819_1", config=config,
        outcomes=[], document_dispositions=[], provider_calls=0)
    assert trace["metrics"]["input_tokens"] is None
    assert trace["metrics"]["wall_time_seconds"] is None


# ----------------------------------------------------------- activation --

def test_driver_refuses_to_run_when_the_flag_is_turned_off() -> None:
    """The retained flag's closed state is still a hard stop.

    Inverted 2026-08-20: the shipped config is now activated, so this asserted
    against a live value that had changed. The legacy spine it used to fall
    back to is deleted, which makes the refusal MORE important, not less --
    a disabled config must halt the stage rather than run some other path.
    """
    config = _config()
    assert config["behavior_enabled"] is True, "the shipped config is activated"
    config["behavior_enabled"] = False
    with pytest.raises(RuntimeError, match="behavior_enabled=false"):
        driver.require_enabled(config)


@pytest.mark.parametrize("missing", [
    "approved_by", "authority_role", "approved_at", "scope",
])
def test_activation_metadata_requires_every_field(missing: str) -> None:
    config = _config()
    config["behavior_enabled"] = True
    config["activation"] = {
        "approved_by": "홍길동",
        "authority_role": "손해사정사",
        "approved_at": "2026-08-19T09:00:00Z",
        "scope": "traumatic injury PoC",
    }
    assert _errors(config, "claim_analysis_routing_config.schema.json") == []
    del config["activation"][missing]
    assert _errors(config, "claim_analysis_routing_config.schema.json"), (
        f"activation without {missing} must not validate"
    )


def test_a_recorded_activation_survives_rollback_to_disabled() -> None:
    """Turning the feature off must not erase who approved it."""
    config = _config()
    config["activation"] = {
        "approved_by": "홍길동",
        "authority_role": "손해사정사",
        "approved_at": "2026-08-19T09:00:00Z",
        "scope": "traumatic injury PoC",
    }
    config["behavior_enabled"] = False
    assert _errors(config, "claim_analysis_routing_config.schema.json") == []


def test_enabled_config_requires_recorded_activation_metadata() -> None:
    config = _config()
    config["behavior_enabled"] = True
    # The shipped config carries its approval; strip it to prove the schema is
    # what forbids an enabled config from having none.
    config.pop("activation", None)
    assert _errors(config, "claim_analysis_routing_config.schema.json"), (
        "enabling without an activation block must not validate"
    )
    config["activation"] = {
        "approved_by": "reviewer",
        "authority_role": "손해사정사",
        "approved_at": "2026-08-19T00:00:00Z",
        "scope": "traumatic injury PoC",
    }
    assert _errors(config, "claim_analysis_routing_config.schema.json") == []
    driver.require_enabled(config)


# --- the driver must ask the DAO for contracts by their real names ----------
# Measured on CASE_489 (2026-08-20), the first case ever run on this lane. The
# driver's first DAO read asked for "_document_manifest.json" -- with a leading
# underscore, which marks the DAO's bookkeeping files (_run_state,
# _source_ledger, _conflict_ledger), not contracts. Every case therefore died
# at that line with NOT_FOUND before any field was read.
#
# It survived because the lane shipped disabled: no test reaches this call, and
# the suite's selective coverage stubs the provider rather than the DAO, so
# nothing ever asserted the contract names the driver actually requests. The
# name is checked against the source here because that is where the defect was
# -- a mocked DAO would have accepted the wrong name just as happily.

def test_the_driver_reads_the_manifest_by_its_contract_name():
    source = Path(driver.__file__).read_text(encoding="utf-8")
    assert '"_document_manifest.json"' not in source, (
        "the manifest is a contract, not a DAO bookkeeping file: it has no "
        "leading underscore, and read-contract returns NOT_FOUND for one")
    assert '"document_manifest.json"' in source, (
        "the driver must read the manifest through read-contract")


# --- the extraction schema must fit claude-cli's inline argv budget --------
# Measured on CASE_489 (2026-08-20), the second defect the lane's first real run
# exposed. `claim_analysis_extraction.output_schema` enumerated all eight member
# keys per field, so the schema grew ~400 chars per field and the real run built
# one of 16,868 chars against claude-cli's 8,000 limit:
#
#   claude-cli structured-output schema is too large for inline argv
#   (16868 chars; limit 8000). Use a compact transport schema ...
#
# The legacy driver already solved this: one shared member spec under
# `additionalProperties`, which makes the transport schema constant-size, with
# the full shape kept for the local validation gate. Two constraints ride along,
# both learned the expensive way on the legacy side and recorded in its
# comments: claude-cli validates with ajv in STRICT mode, so a union
# `"type": ["integer", "null"]` is refused outright; and the member keys must
# stay DECLARED, or the model drops them.

import json as _json
import claim_analysis_extraction as _extraction


def _rows(n):
    return [{"field_id": f"field_{i:02d}"} for i in range(n)]


@pytest.mark.parametrize("field_count", [10, 30, 60, 120])
def test_the_extraction_schema_stays_within_the_cli_argv_budget(field_count):
    from llm_providers import CLAUDE_CLI_SCHEMA_MAX_CHARS
    schema = _extraction.output_schema(_rows(field_count))
    size = len(_json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    assert size <= CLAUDE_CLI_SCHEMA_MAX_CHARS, (
        f"{field_count} fields produced {size} chars, over the "
        f"{CLAUDE_CLI_SCHEMA_MAX_CHARS} inline-argv limit")


def test_the_extraction_schema_declares_no_union_types():
    """claude-cli validates --json-schema with ajv in strict mode, which
    refuses a union type outright -- a CASE_042 legacy run died on exactly that
    AFTER the call was paid for."""
    found = []

    def walk(node, path=""):
        if isinstance(node, dict):
            declared = node.get("type")
            if isinstance(declared, list):
                found.append(f"{path}: {declared}")
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(_extraction.output_schema(_rows(30)))
    assert found == [], f"union types are refused by ajv strict mode: {found}"


def test_the_extraction_schema_still_declares_every_member_key():
    """Compacting must not drop the key declarations -- the legacy driver
    measured sonnet-5 omitting `review_required` on 38 of 57 fields when the
    member spec was a bare object."""
    schema = _extraction.output_schema(_rows(3))
    member = schema["properties"]["fields"]["additionalProperties"]
    for key in ("presence", "value", "page", "quote", "reason",
                "complete", "unambiguous"):
        assert key in member.get("properties", {}), f"{key} is not declared"


def test_the_extraction_schema_requires_no_key_it_does_not_declare():
    """CASE_489, third attempt. The compacted schema moved the field members
    under `additionalProperties` but left the field ids in a top-level
    `required`, so the schema demanded keys it never declared in `properties`.
    claude-cli accepted it and returned an empty structured_output --
    "subtype='success', is_error=False" with nothing in it -- which costs a
    full provider call before failing. Which fields to return is the prompt's
    job; it names them under "Extract exactly these fields"."""
    schema = _extraction.output_schema(_rows(5))
    declared = set(schema.get("properties", {}))
    for key in schema.get("required", []):
        assert key in declared, (
            f"required names {key!r}, which `properties` does not declare")


def test_the_extraction_schema_declares_a_top_level_property():
    """CASE_489, fourth attempt. A top-level object whose only content is
    `additionalProperties` does not survive claude-cli's tool-input encoding:
    the argument arrives as a string rather than an object and the schema
    rejects it, while the envelope still reports subtype='success',
    is_error=False with `structured_output: null` -- a full provider call paid
    for nothing. Verified directly against claude-cli: the same map wrapped in
    a declared `fields` property returned a populated structured_output.
    The legacy driver has always had this shape; only the selective one did
    not."""
    schema = _extraction.output_schema(_rows(3))
    assert schema.get("properties"), (
        "a bare open map is not encodable as a tool input -- declare at least "
        "one property")
    assert "fields" in schema["properties"]
    assert isinstance(schema["properties"]["fields"].get("additionalProperties"),
                      dict), "the open field map belongs under `fields`"


def test_parse_result_unwraps_the_fields_envelope():
    rows = [{"field_id": "primary_diagnosis"}]
    wrapped = {"fields": {"primary_diagnosis": {
        "presence": "asserted", "value": "우측 요골 골절",
        "page": 1, "quote": "진단명: 우측 요골 골절"}}}
    parsed = _extraction.parse_result(wrapped, rows)
    assert parsed["primary_diagnosis"]["value"] == "우측 요골 골절"
    # the older flat shape is still read rather than discarded
    flat = {"primary_diagnosis": wrapped["fields"]["primary_diagnosis"]}
    assert _extraction.parse_result(flat, rows)["primary_diagnosis"]["value"] == (
        "우측 요골 골절")


# ------------------------------------------- partial (untrusted) readings --
# A reading that cites a real value but fails the trusted-value requirements
# (complete/unambiguous) used to be DISCARDED at the gate. `_finish` settles a
# field from `outcome.observations`, so the field kept its constructor default
# and the contract reported `sources_exhausted` / "어떤 출처도 이 항목을 기재하지
# 않았습니다" -- about text the model had quoted verbatim. Measured on CASE_7015
# (2026-08-26): 5 of 40 unavailable fields, including 사고일 and 수술명.
#
# These tests assert the value and the quote SURVIVE, and equally that the
# reading never becomes canonical. Reintroducing the `continue` fails the first
# group; relaxing is_trusted_value to admit it fails the second.

def _progress_for(field_id: str, documents, *, config=None):
    """A FieldProgress on the live path, the way extract_all builds one."""
    config = config or _config()
    field_row = next(r for r in config["fields"] if r["field_id"] == field_id)
    plan = selection.plan_field(field_row, config, documents)
    outcome = driver.FieldExtractionOutcome(
        field_id=plan.field_id, domain_code=plan.domain_code,
        grade=selection.field_grade(field_row))
    return driver.FieldProgress(
        plan, field_row, selection.comparison_budget(field_row, config),
        outcome), outcome


def test_incomplete_reading_is_recorded_not_discarded() -> None:
    quote = "CRIF with K-wires, distal radius, Lt"
    documents = [selection.DocumentRef("DOC_001", "surgery_procedure_record")]
    progress, outcome = _progress_for("surgery_or_procedure_name", documents)
    page_text = {("DOC_001", 1): "수술명\n " + quote + "\n External fixator apply, wrist, Lt"}

    driver._consume(
        progress, "DOC_001", "surgery_procedure_record",
        {"presence": "asserted", "value": quote, "page": 1, "quote": quote,
         "complete": False, "unambiguous": True},
        page_text, driver._observation_id_sequence())
    driver._finish(progress)

    # The evidence survives -- this is the whole point.
    assert len(outcome.observations) == 1
    observation = outcome.observations[0]
    assert observation["value"] == quote
    assert observation["partial_reading"] is True
    assert observation["complete"] is False
    assert observation["evidence_references"][0]["quote"] == quote

    # ...and it is reported as a partial reading, not as silence.
    assert outcome.status == "unavailable"
    assert outcome.stop_reason == "partial_value_only"
    assert outcome.unavailable_reason == "partial_reading_only"
    assert "기재가 없었습니다" not in outcome.reason
    assert "신뢰값 요건" in outcome.reason

    # ...but it is never canonical.
    assert outcome.selected_ids == []
    assert progress.trusted is None


def test_ambiguous_reading_is_recorded_and_named_as_ambiguous() -> None:
    documents = [selection.DocumentRef("DOC_001", "surgery_procedure_record")]
    progress, outcome = _progress_for("surgery_or_procedure_name", documents)
    page_text = {("DOC_001", 1): "수술명 관혈적 정복술"}

    driver._consume(
        progress, "DOC_001", "surgery_procedure_record",
        {"presence": "asserted", "value": "관혈적 정복술", "page": 1,
         "quote": "관혈적 정복술", "complete": True, "unambiguous": False},
        page_text, driver._observation_id_sequence())
    driver._finish(progress)

    assert outcome.observations[0]["unambiguous"] is False
    assert outcome.stop_reason == "partial_value_only"
    assert "다의적" in outcome.reason
    assert outcome.selected_ids == []


def test_a_trusted_source_still_wins_over_an_earlier_partial_reading() -> None:
    """The ladder must keep walking: a partial reading must not stop the search."""
    documents = [
        selection.DocumentRef("DOC_001", "surgery_procedure_record"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    ]
    progress, outcome = _progress_for("surgery_or_procedure_name", documents)
    page_text = {("DOC_001", 1): "수술명 부분값", ("DOC_002", 1): "수술명 완전값"}
    ids = driver._observation_id_sequence()

    driver._consume(progress, "DOC_001", "surgery_procedure_record",
                    {"presence": "asserted", "value": "부분값", "page": 1,
                     "quote": "부분값", "complete": False, "unambiguous": True},
                    page_text, ids)
    assert progress.trusted is None, "a partial reading must not become trusted"
    assert not progress.done, "a partial reading must not end the search"

    driver._consume(progress, "DOC_002", "admission_discharge_summary",
                    {"presence": "asserted", "value": "완전값", "page": 1,
                     "quote": "완전값", "complete": True, "unambiguous": True},
                    page_text, ids)
    driver._finish(progress)

    assert outcome.status == "asserted"
    assert outcome.stop_reason == "trusted_value_found"
    assert len(outcome.selected_ids) == 1
    selected = [o for o in outcome.observations
                if o["observation_id"] == outcome.selected_ids[0]][0]
    assert selected["value"] == "완전값"
    assert not selected.get("partial_reading")


def test_a_stated_absence_alone_settles_the_field() -> None:
    """With no value cited anywhere, a stated absence is the finding."""
    documents = [selection.DocumentRef("DOC_002", "admission_discharge_summary")]
    progress, outcome = _progress_for("surgery_or_procedure_name", documents)
    page_text = {("DOC_002", 1): "수술 시행하지 않음"}

    driver._consume(progress, "DOC_002", "admission_discharge_summary",
                    {"presence": "explicitly_absent", "page": 1,
                     "quote": "수술 시행하지 않음", "reason": "미시행"},
                    page_text, driver._observation_id_sequence())
    driver._finish(progress)

    assert outcome.status == "explicitly_absent"
    assert outcome.stop_reason == "explicitly_absent"


def test_a_cited_value_outranks_a_stated_absence() -> None:
    """One source denying the thing does not undo another source quoting it.

    Written the other way round first, from an assumption rather than from
    data, and CASE_9417 refuted it: `objective_change` held `요통 호전 경향`
    and `처음보다는 40% 호전된 상태` alongside an imaging line `No definite
    abnormal signal change in bones`, and settling the field as
    `explicitly_absent` told a reviewer the records were silent about a change
    the records had described twice.

    The absence reading is still PRESERVED as an observation -- it simply does
    not decide the field.
    """
    documents = [
        selection.DocumentRef("DOC_001", "surgery_procedure_record"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    ]
    progress, outcome = _progress_for("surgery_or_procedure_name", documents)
    page_text = {("DOC_001", 1): "수술명 부분값", ("DOC_002", 1): "수술 시행하지 않음"}
    ids = driver._observation_id_sequence()

    driver._consume(progress, "DOC_001", "surgery_procedure_record",
                    {"presence": "asserted", "value": "부분값", "page": 1,
                     "quote": "부분값", "complete": False, "unambiguous": True},
                    page_text, ids)
    driver._consume(progress, "DOC_002", "admission_discharge_summary",
                    {"presence": "explicitly_absent", "page": 1,
                     "quote": "수술 시행하지 않음", "reason": "미시행"},
                    page_text, ids)
    driver._finish(progress)

    assert outcome.status == "unavailable"
    assert outcome.stop_reason == "partial_value_only"
    # Both readings survive; the absence is recorded, not discarded.
    states = [o.get("value_state") for o in outcome.observations]
    assert states.count("explicitly_absent") == 1
    assert any(o.get("partial_reading") for o in outcome.observations)


def test_partial_reading_survives_into_the_published_contract() -> None:
    """End-to-end: the driver's own build_result must publish the quote, and
    the schema must accept the shape -- and refuse it being made canonical."""
    config = _config()
    documents = [selection.DocumentRef("DOC_001", "surgery_procedure_record")]
    progress, outcome = _progress_for("surgery_or_procedure_name", documents)
    quote = "CRIF with K-wires, distal radius, Lt"
    driver._consume(
        progress, "DOC_001", "surgery_procedure_record",
        {"presence": "asserted", "value": quote, "page": 1, "quote": quote,
         "complete": False, "unambiguous": True},
        {("DOC_001", 1): "수술명 " + quote},
        _OBSERVATION_IDS)
    driver._finish(progress)

    result = driver.build_result(
        case_id="CASE_9002", run_id="RUN_20260826_1", outcomes=[outcome],
        config=config, documents=documents, medical_revision_context=None)
    assert _errors(result, "claim_analysis_result.schema.json") == []

    published = next(f for f in result["claim_facts"]
                     if f["field_id"] == "surgery_or_procedure_name")
    assert published["stop_reason"] == "partial_value_only"
    assert published["unavailable_reason"] == "partial_reading_only"
    assert published["selected_observation_ids"] == []
    # The quote a reviewer needs is actually in the contract.
    assert published["observations"][0]["value"] == quote
    assert published["observations"][0]["evidence_references"][0]["quote"] == quote

    # Making that partial reading canonical must be refused by the schema.
    canonical = deepcopy(result)
    field = next(f for f in canonical["claim_facts"]
                 if f["field_id"] == "surgery_or_procedure_name")
    field["selected_observation_ids"] = [field["observations"][0]["observation_id"]]
    assert _errors(canonical, "claim_analysis_result.schema.json")

    # ...as must claiming the sources were silent about it.
    mislabelled = deepcopy(result)
    field = next(f for f in mislabelled["claim_facts"]
                 if f["field_id"] == "surgery_or_procedure_name")
    field["unavailable_reason"] = "not_mentioned"
    assert _errors(mislabelled, "claim_analysis_result.schema.json")
