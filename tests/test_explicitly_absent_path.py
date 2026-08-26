"""A stated absence, followed end to end.

`explicitly_absent` existed in the schema before it existed in the pipeline:
the provider prompt told the model to return `found: false` for a stated
negative, which is the same answer it gives for silence, so no real run could
ever produce the state. A schema-level test would have passed the whole time.

So these tests start at a provider's structured output and follow it through
the parser, the driver's read loop, the published field, and finally the
case-type verdict -- because that last hop is where the distinction pays off:
`not_applicable` for a case type is reachable ONLY from an affirmative
negative, never from silence.
"""
from __future__ import annotations

import copy

import pytest

import claim_analysis_case_types as case_types
import claim_analysis_contracts as contracts
import claim_analysis_extraction as extraction
import claim_analysis_selection as selection
import run_claim_analysis_selective as driver


ABSENCE_QUOTE = "골절 소견 없음"
PAGE_TEXT = f"영상판독지\n판독소견: {ABSENCE_QUOTE}\n"
VEHICLE_ABSENCE = "차량 관련 사고 아님"


def _config_with(*field_ids: str) -> dict:
    config = copy.deepcopy(contracts.load_default_routing_config())
    config["fields"] = [row for row in config["fields"]
                        if row["field_id"] in field_ids]
    return config


# --------------------------------------------------- provider -> parser --

def test_the_output_schema_offers_three_presence_verdicts() -> None:
    """A boolean cannot express the distinction, so the axis is an enum."""
    schema = extraction.output_schema([{"field_id": "primary_diagnosis"}])
    presence = schema["properties"]["primary_diagnosis"]["properties"]["presence"]
    assert set(presence["enum"]) == {
        "asserted", "explicitly_absent", "not_mentioned"}
    assert schema["properties"]["primary_diagnosis"]["required"] == ["presence"]


def test_the_prompt_asks_for_a_stated_absence_rather_than_a_blank() -> None:
    prompt = extraction.build_prompt(
        document_id="DOC_001", document_kind="imaging_interpretation",
        pages=[{"page": 1, "text": PAGE_TEXT}],
        field_rows=[{"field_id": "primary_diagnosis", "label": "Primary diagnosis",
                     "value_shape": "text"}])
    assert "explicitly_absent" in prompt
    assert "not_mentioned" in prompt
    # The rule that previously made the state unreachable must be gone.
    assert 'return {"found": false}' not in prompt


def test_the_parser_keeps_absence_and_silence_apart() -> None:
    rows = [{"field_id": "primary_diagnosis"}]
    absent = extraction.parse_result({"primary_diagnosis": {
        "presence": "explicitly_absent", "page": 1, "quote": ABSENCE_QUOTE,
        "reason": "판독소견에 골절 없음으로 기재"}}, rows)
    assert absent["primary_diagnosis"]["presence"] == "explicitly_absent"
    assert "value" not in absent["primary_diagnosis"]

    silent = extraction.parse_result(
        {"primary_diagnosis": {"presence": "not_mentioned"}}, rows)
    assert silent == {}


def test_an_absence_without_a_quote_is_dropped_not_repaired() -> None:
    """Ungrounded, so it has no standing -- and inventing a quote is worse."""
    rows = [{"field_id": "primary_diagnosis"}]
    assert extraction.parse_result({"primary_diagnosis": {
        "presence": "explicitly_absent", "page": 1, "quote": "  "}}, rows) == {}
    assert extraction.parse_result({"primary_diagnosis": {
        "presence": "explicitly_absent", "quote": ABSENCE_QUOTE}}, rows) == {}


def test_an_absence_is_not_the_boolean_false() -> None:
    """The two must not collapse: one is a recorded finding, one is a value."""
    rows = [{"field_id": "surgery_or_major_procedure_status"}]
    absent = extraction.parse_result({"surgery_or_major_procedure_status": {
        "presence": "explicitly_absent", "page": 1, "quote": "수술 시행하지 않음"}}, rows)
    asserted = extraction.parse_result({"surgery_or_major_procedure_status": {
        "presence": "asserted", "value": False, "page": 1, "quote": "수술: 무"}}, rows)
    assert absent["surgery_or_major_procedure_status"]["presence"] == "explicitly_absent"
    assert asserted["surgery_or_major_procedure_status"]["value"] is False


# ------------------------------------------------------ parser -> driver --

def test_the_driver_publishes_a_grounded_explicitly_absent_field() -> None:
    config = _config_with("primary_diagnosis")
    documents = [selection.DocumentRef("DOC_001", "diagnosis_certificate")]

    def extract(document_id, kind, field_rows):
        return extraction.parse_result({"primary_diagnosis": {
            "presence": "explicitly_absent", "page": 1, "quote": ABSENCE_QUOTE,
            "reason": "판독소견에 골절 없음으로 기재"}}, field_rows)

    outcomes, _ = driver.extract_all(
        config=config, documents=documents, extract=extract,
        page_text={("DOC_001", 1): PAGE_TEXT},
        observation_ids=driver._observation_id_sequence())

    field = driver.field_result(
        outcomes[0], priority_grade="A",
        authority=driver.authority_for(outcomes[0].domain_code))
    assert field["resolution_status"] == "explicitly_absent"
    # No value was elected -- an absence is not a value.
    assert field["selected_observation_ids"] == []
    observation = field["observations"][0]
    assert observation["value_state"] == "explicitly_absent"
    assert "value" not in observation
    assert observation["evidence_references"][0]["quote"] == ABSENCE_QUOTE


def test_silence_still_produces_unavailable_not_absent() -> None:
    config = _config_with("primary_diagnosis")
    documents = [selection.DocumentRef("DOC_001", "diagnosis_certificate")]
    outcomes, _ = driver.extract_all(
        config=config, documents=documents,
        extract=lambda *a: {}, page_text={("DOC_001", 1): PAGE_TEXT},
        observation_ids=driver._observation_id_sequence())
    assert outcomes[0].status == "unavailable"
    assert outcomes[0].unavailable_reason == "not_mentioned"
    assert outcomes[0].observations == []


def test_a_later_asserted_value_still_wins_over_an_earlier_absence() -> None:
    """An absence does not end the search -- another source may state a value.

    A 진단서 that omits an imaging finding has not overruled the 영상판독지 that
    records one; the ladder keeps walking.
    """
    config = _config_with("primary_diagnosis")
    documents = [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    ]
    answers = {
        "DOC_001": {"presence": "explicitly_absent", "page": 1,
                    "quote": ABSENCE_QUOTE},
        "DOC_002": {"presence": "asserted", "value": "우측 요골 골절",
                    "page": 1, "quote": "우측 요골 골절"},
    }

    def extract(document_id, kind, field_rows):
        return extraction.parse_result(
            {"primary_diagnosis": answers[document_id]}, field_rows)

    outcomes, _ = driver.extract_all(
        config=config, documents=documents, extract=extract,
        page_text={("DOC_001", 1): PAGE_TEXT,
                   ("DOC_002", 1): "진단명: 우측 요골 골절"},
        observation_ids=driver._observation_id_sequence())

    assert outcomes[0].status == "asserted"
    states = {o["value_state"] for o in outcomes[0].observations}
    assert states == {"explicitly_absent", "asserted"}


# ------------------------------------------------- driver -> case types --

def _claim_fact(field_id: str, *, state: str, value=None, quote: str = "") -> dict:
    observation = {
        "observation_id": "CAO_0001",
        "value_state": state,
        "extraction_wave": "B",
        "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": quote,
            "start_char": 0, "end_char": max(len(quote), 1),
        }] if state != "unavailable" else [],
    }
    if state == "asserted":
        observation["value"] = value
        observation["source_document_kind"] = "initial_visit_record"
        observation["source_priority_rank"] = 1
    elif state == "explicitly_absent":
        observation["reason"] = "the source states this is not present"
        observation["source_document_kind"] = "initial_visit_record"
        observation["source_priority_rank"] = 1
    else:
        observation["reason"] = "not stated"
        observation["unavailable_reason"] = "not_mentioned"

    status = {"asserted": "asserted", "explicitly_absent": "explicitly_absent",
              "unavailable": "unavailable"}[state]
    field = {
        "field_id": field_id,
        "domain_code": "event_timeline",
        "priority_grade": "B",
        "authority": "claim_analysis_native",
        "resolution_status": status,
        "selected_observation_ids": ["CAO_0001"] if state == "asserted" else [],
        "observations": [observation],
        "conflict_candidate_ids": [],
        "stop_reason": {"asserted": "trusted_value_found",
                        "explicitly_absent": "explicitly_absent",
                        "unavailable": "sources_exhausted"}[state],
    }
    if state != "asserted":
        field["resolution_reason"] = "see observation"
    if state == "unavailable":
        field["unavailable_reason"] = "not_mentioned"
    return field


def test_an_explicit_negative_makes_a_case_type_not_applicable() -> None:
    """The payoff. This verdict must be reachable only this way."""
    result = case_types.assess_case_types([
        _claim_fact("vehicle_involvement", state="explicitly_absent",
                    quote=VEHICLE_ABSENCE)])
    by_type = {row["case_type"]: row for row in result}
    assert by_type["traffic_accident"]["status"] == "not_applicable"
    assert by_type["traffic_accident"]["evidence_references"]


def test_silence_leaves_the_same_case_type_uncertain() -> None:
    result = case_types.assess_case_types([
        _claim_fact("vehicle_involvement", state="unavailable")])
    by_type = {row["case_type"]: row for row in result}
    assert by_type["traffic_accident"]["status"] == "uncertain"
    assert by_type["traffic_accident"]["evidence_references"] == []


def test_the_absence_does_not_spill_onto_another_case_type() -> None:
    """One field's stated absence settles that field's type, and no other."""
    result = case_types.assess_case_types([
        _claim_fact("vehicle_involvement", state="explicitly_absent",
                    quote=VEHICLE_ABSENCE)])
    by_type = {row["case_type"]: row for row in result}
    for other in ("personal_insurance", "industrial_accident", "liability"):
        assert by_type[other]["status"] == "uncertain"


# ------------------------------------------------ publishable value guard --
# A structurally invalid value used to pass `parse_result` and only fail at
# contract write, which kills the stage AFTER every provider call is paid for.
# Measured on CASE_9412 (2026-08-26): a 진단서 stating `수술 후 약 8(팔)주 간의
# 안정가료` -- a DURATION with no start date -- produced
# {"start": null, "end": null} for `treatment_period` and `admission_period`,
# and those two fields failed the whole run's write.

def test_period_without_a_start_date_is_dropped_not_published() -> None:
    """The exact CASE_9412 payload must not reach the contract."""
    rows = [{"field_id": "treatment_period", "value_shape": "period"}]
    parsed = extraction.parse_result({"fields": {"treatment_period": {
        "presence": "asserted",
        "value": {"start": None, "end": None},
        "page": 1, "quote": "수술 후 약 8(팔)주 간의 안정가료 요하며",
    }}}, rows)
    assert parsed == {}, "a period with no start date must be dropped"


def test_a_real_period_still_publishes() -> None:
    rows = [{"field_id": "admission_period", "value_shape": "period"}]
    parsed = extraction.parse_result({"fields": {"admission_period": {
        "presence": "asserted",
        "value": {"start": "2025-02-25", "end": "2025-04-19"},
        "page": 1, "quote": "입원 일수 | 총53   일",
    }}}, rows)
    assert parsed["admission_period"]["value"] == {
        "start": "2025-02-25", "end": "2025-04-19"}


def test_an_open_period_still_publishes() -> None:
    """`end: null` is legitimate -- a period that has not closed."""
    rows = [{"field_id": "treatment_period", "value_shape": "period"}]
    parsed = extraction.parse_result({"fields": {"treatment_period": {
        "presence": "asserted",
        "value": {"start": "2025-02-25", "end": None},
        "page": 1, "quote": "2025-02-25 일에",
    }}}, rows)
    assert parsed["treatment_period"]["value"]["end"] is None


def test_a_duration_stated_as_text_publishes() -> None:
    """The documented fallback: a duration with no start date, as a string."""
    rows = [{"field_id": "treatment_period", "value_shape": "period"}]
    parsed = extraction.parse_result({"fields": {"treatment_period": {
        "presence": "asserted",
        "value": "기브스 및 목발 6주, 재활6 주 총12 주간의 안정가료",
        "page": 1, "quote": "기브스 및 목발 6주, 재활6 주 총12 주간의 안정가료가 필요합니다.",
    }}}, rows)
    assert parsed["treatment_period"]["value"].startswith("기브스")


def test_value_guard_matches_the_contract_schema() -> None:
    """The guard must agree with `$defs/value`, case by case.

    Any divergence other than the documented whitespace-only case is a defect:
    stricter drops a value the contract would have accepted, looser lets a
    write-time failure back in.
    """
    import jsonschema
    from _validation import load_registry

    schemas, registry = load_registry()
    value_schema = schemas["claim_analysis_result.schema.json"]["$defs"]["value"]
    validator = jsonschema.Draft202012Validator(
        value_schema, registry=registry,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)

    cases = [
        "수술 후 약 8주간의 안정가료", "", 10, 0, True, False,
        ["a", "b"], [], ["a", ""],
        {"start": "2025-02-25", "end": "2025-05-25"},
        {"start": "2025-02-25", "end": None},
        {"start": None, "end": None},
        {"end": "2025-05-25"},
        {"start": "", "end": None},
        {"start": "2025-02-25", "end": None, "extra": 1},
        None,
    ]
    for value in cases:
        mine = extraction._value_is_publishable(value)
        theirs = not list(validator.iter_errors(value))
        assert mine == theirs, (
            f"guard and schema disagree on {value!r}: guard={mine} schema={theirs}")

    # The one deliberate divergence, asserted so it cannot drift silently.
    assert extraction._value_is_publishable("   ") is False
    assert not list(validator.iter_errors("   ")), (
        "schema is expected to accept whitespace-only; if this changes, the "
        "guard's documented divergence should be revisited")


# ------------------------------------------- a printed field left blank --
# A form that PRINTS a field and leaves the cell empty is a third thing, and
# until 2026-08-26 the pipeline had only two words for it. `not_mentioned`
# claims "sources were read and none discussed the field, a real records gap a
# reviewer may close by requesting documents" -- both halves false here: the
# document did raise the item, and no further request will ever fill that
# cell. `explicitly_absent` is equally wrong; a blank cell is not the form
# stating the thing is absent, and treating it as one would manufacture a
# finding out of an omission.
#
# Measured on CASE_7061 DOC_003, a McBride disability certificate:
#
#   | 기왕증 | |
#   | 기왕증의 현재 장애에 대한 기여율 | |
#   | 기존장애 및 장애율 | |
#   | 수상일 | | 초진일 | | 장해진단일 | | |
#
# Four printed rows, every cell blank, all four published `not_mentioned`.
#
# The value it carries is still NOTHING -- this does not weaken
# `patient_reported_prior_same_site_history`'s own rule ("never infer no
# history from silence"). Only the stated CAUSE changes, and with it what a
# person is asked to do about it.

BLANK_ROW = "| 기왕증 | |"


def test_the_output_schema_offers_the_blank_verdict() -> None:
    """The model cannot report what the transport will not carry."""
    schema = extraction.output_schema([{"field_id": "primary_diagnosis"}])
    presence = (schema["properties"]["fields"]["additionalProperties"]
                ["properties"]["presence"]["enum"])
    assert "printed_but_blank" in presence


def test_the_prompt_distinguishes_a_blank_cell_from_silence() -> None:
    prompt = extraction.build_prompt(
        document_id="DOC_003", document_kind="disability_assessment",
        pages=[{"page": 1, "text": "후유장애진단서" + chr(10) + BLANK_ROW + chr(10)}],
        field_rows=[{"field_id": "patient_reported_prior_same_site_history",
                     "label": "prior history", "value_shape": "enum"}])
    assert "printed_but_blank" in prompt


def test_the_parser_keeps_a_blank_cell_apart_from_silence() -> None:
    rows = [{"field_id": "patient_reported_prior_same_site_history"}]
    blank = extraction.parse_result(
        {"patient_reported_prior_same_site_history": {
            "presence": "printed_but_blank", "page": 1, "quote": BLANK_ROW,
            "reason": "서식에 항목은 있으나 칸이 비어 있음"}}, rows)
    assert (blank["patient_reported_prior_same_site_history"]["presence"]
            == "printed_but_blank")
    # No value, ever -- the distinction is about the cause, not the answer.
    assert "value" not in blank["patient_reported_prior_same_site_history"]

    silent = extraction.parse_result(
        {"patient_reported_prior_same_site_history":
         {"presence": "not_mentioned"}}, rows)
    assert silent == {}


def test_a_blank_cell_without_a_quote_is_dropped() -> None:
    """Same grounding bar as every other verdict: the printed label is the
    evidence, so a claim with no quote has nothing behind it."""
    rows = [{"field_id": "patient_reported_prior_same_site_history"}]
    assert extraction.parse_result(
        {"patient_reported_prior_same_site_history": {
            "presence": "printed_but_blank", "page": 1, "quote": "  "}},
        rows) == {}
    assert extraction.parse_result(
        {"patient_reported_prior_same_site_history": {
            "presence": "printed_but_blank", "quote": BLANK_ROW}}, rows) == {}
