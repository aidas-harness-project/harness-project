"""The read budget: one provider call per document, per RUN.

Not per wave. The distinction is the whole optimisation: a document on both the
A and B ladders was previously opened twice, and a critical field's second
source opened it again. Planning both waves up front and inverting to
(document -> fields) makes one read serve every field that document can answer,
and later waves read the cache.

Also pinned here: an opportunistic field never pays for a read and never takes
a value from a document outside its own route, and the conditional industrial
filing route opens nothing until its trigger fires -- and never opens a medical
document even then.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import claim_analysis_contracts as contracts
import claim_analysis_extraction as extraction
import claim_analysis_selection as selection
import run_claim_analysis_selective as driver


QUOTE = "우측 요골 골절"


def _config() -> dict:
    return contracts.load_default_routing_config()


def _documents() -> list[selection.DocumentRef]:
    return [
        selection.DocumentRef("DOC_001", "diagnosis_certificate"),
        selection.DocumentRef("DOC_002", "admission_discharge_summary"),
        selection.DocumentRef("DOC_003", "surgery_procedure_record"),
        selection.DocumentRef("DOC_004", "medical_expense_receipt"),
    ]


def _page_text() -> dict:
    return {
        ("DOC_001", 1): f"진단명: {QUOTE}",
        ("DOC_002", 1): f"진단명: {QUOTE}",
        ("DOC_003", 1): f"수술명: {QUOTE}",
    }


class _Recorder:
    """Stands in for the provider, recording every document it is asked to read."""

    def __init__(self, answers: dict | None = None) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.answers = answers or {}

    def __call__(self, document_id, kind, field_rows):
        self.calls.append(
            (document_id, tuple(row["field_id"] for row in field_rows)))
        return self.answers.get(document_id, {})

    @property
    def documents(self) -> list[str]:
        return [document_id for document_id, _ in self.calls]


def _run(recorder, documents=None, page_text=None):
    return driver.extract_all(
        config=_config(),
        documents=documents or _documents(),
        extract=recorder,
        page_text=page_text or _page_text(),
        observation_ids=driver._observation_id_sequence(),
    )


# ------------------------------------------------------------ the budget --

def test_the_cache_refuses_a_second_read_of_the_same_document() -> None:
    """The guarantee at its source, not via the loop that happens to obey it.

    extract_all visits each document once, so a broken cache would still look
    correct there. This asks the cache the question directly: a later wave, or
    a critical field's second source, must be served from the stored result.
    """
    recorder = _Recorder({"DOC_001": {"primary_diagnosis": {
        "value": QUOTE, "page": 1, "quote": QUOTE}}})
    cache = driver.DocumentCache(recorder)
    field_rows = [{"field_id": "primary_diagnosis"}]

    first = cache.read("DOC_001", "diagnosis_certificate", field_rows)
    second = cache.read("DOC_001", "diagnosis_certificate", field_rows)
    third = cache.read("DOC_001", "diagnosis_certificate", field_rows)

    assert first == second == third
    assert cache.calls["DOC_001"] == 1
    assert recorder.documents == ["DOC_001"]


def test_one_read_serves_every_field_that_document_can_answer() -> None:
    """A 진단서 states diagnosis, date, site and department in one place.

    Asking for them separately pays repeatedly for one page.
    """
    recorder = _Recorder()
    _run(recorder)
    by_document = {document_id: fields for document_id, fields in recorder.calls}
    assert len(recorder.documents) == len(set(recorder.documents))
    assert len(by_document["DOC_001"]) > 1
    assert "primary_diagnosis" in by_document["DOC_001"]


def test_both_waves_are_planned_before_the_first_read() -> None:
    """If B were planned after A ran, a shared document would be read twice."""
    recorder = _Recorder()
    _run(recorder)
    fields = dict(recorder.calls)["DOC_001"]
    config = _config()
    waves = {row["field_id"]: row["extraction_wave"]
             for row in config["fields"]}
    asked = {waves.get(field_id) for field_id in fields}
    assert "A" in asked and "B" in asked


def test_cost_documents_are_never_opened() -> None:
    recorder = _Recorder()
    _, cache = _run(recorder)
    assert "DOC_004" not in recorder.documents
    assert cache.calls.get("DOC_004", 0) == 0


def test_a_document_no_field_routes_to_is_not_opened() -> None:
    recorder = _Recorder()
    _run(recorder, documents=[selection.DocumentRef("DOC_009", None)])
    assert recorder.documents == []


def test_provider_reader_accepts_a_lazy_page_loader() -> None:
    loaded: list[str] = []

    def load_pages(document_id):
        loaded.append(document_id)
        return [{"page": 1, "text": f"진단명: {QUOTE}"}]

    class Provider:
        def analyze_text_structured(self, prompt, prompt_version, schema):
            assert QUOTE in prompt
            return SimpleNamespace(structured_output={
                "primary_diagnosis": {
                    "presence": "asserted", "value": QUOTE,
                    "page": 1, "quote": QUOTE,
                },
            }, text=None)

    reader = extraction.make_reader(Provider(), load_pages)
    result = reader("DOC_001", "diagnosis_certificate", [{
        "field_id": "primary_diagnosis", "label": "Primary diagnosis",
        "value_shape": "text",
    }])

    assert loaded == ["DOC_001"]
    assert result["primary_diagnosis"]["value"] == QUOTE


def test_higher_priority_documents_are_read_first() -> None:
    """The stop rule still needs the most authoritative source first."""
    plans = selection.plan_wave(_config(), _documents(), "A")
    order = selection.ordered_documents(plans)
    assert order.index("DOC_001") < order.index("DOC_002")


# ------------------------------------------------------- opportunistic --

def test_opportunistic_field_opens_no_document_of_its_own() -> None:
    recorder = _Recorder()
    before = set(recorder.documents)
    _run(recorder)
    opportunistic = [row["field_id"] for row in
                     selection.opportunistic_fields(_config())]
    assert opportunistic, "the config should still carry an opportunistic field"
    # Every document read was requested for a searching field, never for an
    # opportunistic one alone.
    for document_id, fields in recorder.calls:
        assert not set(fields) <= set(opportunistic)


def test_opportunistic_field_rides_the_only_call_for_an_open_document() -> None:
    """The cache can only contain a field the provider was actually asked for."""
    field_row = selection.opportunistic_fields(_config())[0]
    field_id = field_row["field_id"]
    document_id = "DOC_010"
    documents = [selection.DocumentRef(document_id, "outpatient_record")]
    plan = selection.plan_field(field_row, _config(), documents)
    if not plan.steps:
        pytest.skip("the opportunistic field does not route to this kind")

    quote = "과거 동일 부위 골절 치료력 있음"
    recorder = _Recorder({document_id: {field_id: {
        "value": [quote], "page": 1, "quote": quote,
    }}})
    outcomes, _cache = _run(
        recorder, documents=documents,
        page_text={(document_id, 1): quote},
    )

    asked = dict(recorder.calls)[document_id]
    assert field_id in asked
    outcome = next(item for item in outcomes if item.field_id == field_id)
    assert outcome.status == "asserted"
    assert recorder.documents == [document_id]


def test_opportunistic_value_records_its_own_route_rank() -> None:
    """Not the order the document happened to be opened in."""
    field_rows = selection.opportunistic_fields(_config())
    field_row = field_rows[0]
    documents = [selection.DocumentRef("DOC_010", "outpatient_record")]
    plan = selection.plan_field(field_row, _config(), documents)
    if not plan.steps:
        pytest.skip("the opportunistic field does not route to this kind")

    recorder = _Recorder({"DOC_010": {field_row["field_id"]: {
        "value": QUOTE, "page": 1, "quote": QUOTE}}})
    cache = driver.DocumentCache(recorder)
    cache.read("DOC_010", "outpatient_record", [field_row])
    outcome = driver.resolve_opportunistic(
        plan, field_row, cache, {("DOC_010", 1): QUOTE},
        driver._observation_id_sequence())
    assert outcome.status == "asserted"
    assert outcome.observations[0]["source_priority_rank"] == plan.steps[0].priority_rank
    assert outcome.observations[0]["extraction_wave"] == "opportunistic"


def test_opportunistic_ignores_a_document_outside_its_route() -> None:
    field_row = selection.opportunistic_fields(_config())[0]
    documents = [selection.DocumentRef("DOC_020", "medical_expense_receipt")]
    plan = selection.plan_field(field_row, _config(), documents)
    cache = driver.DocumentCache(_Recorder({"DOC_020": {
        field_row["field_id"]: {"value": QUOTE, "page": 1, "quote": QUOTE}}}))
    cache.read("DOC_020", "medical_expense_receipt", [field_row])
    outcome = driver.resolve_opportunistic(
        plan, field_row, cache, {("DOC_020", 1): QUOTE},
        driver._observation_id_sequence())
    assert outcome.status == "unavailable"


# -------------------------------------------- conditional industrial route --

def _filing_fields() -> list[str]:
    return sorted(
        row["field_id"] for row in _config()["fields"]
        if row.get("source_route_id") == "industrial_accident_filing"
    )


def test_without_an_industrial_trigger_no_administrative_source_is_read() -> None:
    reads: list[str] = []
    outcomes, _ = driver.extract_all(
        config=_config(), documents=_documents(), extract=_Recorder(),
        page_text=_page_text(),
        observation_ids=driver._observation_id_sequence(),
        non_medical_reader=lambda row: reads.append(row["field_id"]),
    )
    assert reads == []
    by_field = {outcome.field_id: outcome for outcome in outcomes}
    for field_id in _filing_fields():
        assert by_field[field_id].status == "unavailable"
        # `route_not_activated`, not `not_mentioned` (changed 2026-08-21): the
        # assertion right above proves NOTHING was read, so reporting "no
        # source mentioned it" would describe a records gap that does not
        # exist and send a reviewer requesting documents that could not help.
        assert by_field[field_id].unavailable_reason == "route_not_activated"


def test_a_filed_case_reads_the_approval_document_once() -> None:
    """Trigger fires -> the administrative source may be opened."""
    config = _config()
    documents = _documents()
    work = driver.FieldExtractionOutcome(
        field_id="work_activity_context", domain_code="event_timeline", grade="C")
    work.status = "asserted"
    work.observations = [{"observation_id": "CAO_0001", "value": "work_activity"}]
    assert driver.industrial_trigger_active({"work_activity_context": work}) is True

    reads: list[str] = []

    def reader(field_row):
        reads.append(field_row["field_id"])
        return {
            "value": "요골 골절",
            "source_document_kind": "other_medical",
            "evidence_reference": {
                "document_id": "DOC_900", "page": 1, "quote": "승인 상병: 요골 골절",
                "start_char": 0, "end_char": 12,
            },
        }

    recorder = _Recorder({"DOC_001": {"work_activity_context": {
        "value": "work_activity", "page": 1, "quote": f"진단명: {QUOTE}"}}})
    outcomes, _ = driver.extract_all(
        config=config, documents=documents, extract=recorder,
        page_text=_page_text(),
        observation_ids=driver._observation_id_sequence(),
        non_medical_reader=reader,
    )
    assert sorted(reads) == _filing_fields()
    by_field = {outcome.field_id: outcome for outcome in outcomes}
    assert by_field["industrial_accident_approved_diagnosis"].status == "asserted"


def test_a_triggered_route_with_no_source_is_unavailable_not_absent() -> None:
    work = driver.FieldExtractionOutcome(
        field_id="work_activity_context", domain_code="event_timeline", grade="C")
    work.status = "asserted"
    work.observations = [{"observation_id": "CAO_0001", "value": "work_activity"}]

    recorder = _Recorder({"DOC_001": {"work_activity_context": {
        "value": "work_activity", "page": 1, "quote": f"진단명: {QUOTE}"}}})
    outcomes, _ = driver.extract_all(
        config=_config(), documents=_documents(), extract=recorder,
        page_text=_page_text(),
        observation_ids=driver._observation_id_sequence(),
        non_medical_reader=lambda row: None,
    )
    by_field = {outcome.field_id: outcome for outcome in outcomes}
    for field_id in _filing_fields():
        assert by_field[field_id].status == "unavailable"
        # `outside_poc_scope`, not `source_document_missing`: no stage in this
        # pipeline produces a filing declaration, so the case is not missing a
        # document -- the pipeline is missing a producer. Recording it as a
        # records gap would send someone hunting for a file nobody was ever
        # going to write. See test_industrial_filing_deferred.py.
        assert by_field[field_id].unavailable_reason == "outside_poc_scope"
        assert by_field[field_id].reason == driver.FILING_ROUTE_DEFERRED_REASON


def test_the_filing_route_opens_no_medical_document() -> None:
    """Its safety property is structural: it ranks no medical kinds at all."""
    route = next(row for row in _config()["source_routes"]
                 if row["route_id"] == "industrial_accident_filing")
    assert route["source_kind"] == "administrative_or_intake"
    assert route["priority_groups"] == []
    assert route["non_medical_sources"]

    recorder = _Recorder()
    work = driver.FieldExtractionOutcome(
        field_id="work_activity_context", domain_code="event_timeline", grade="C")
    work.status = "asserted"
    work.observations = [{"observation_id": "CAO_0001", "value": "work_activity"}]
    before = set(recorder.documents)
    driver.extract_all(
        config=_config(), documents=_documents(), extract=recorder,
        page_text=_page_text(),
        observation_ids=driver._observation_id_sequence(),
        non_medical_reader=lambda row: None,
    )
    # No document was opened that only a filing field routes to.
    for _, fields in recorder.calls:
        assert not set(fields) & set(_filing_fields())


# ------------------------------------- a ride-along that is not a value -----

def _opportunistic_plan():
    field_row = selection.opportunistic_fields(_config())[0]
    documents = [selection.DocumentRef("DOC_010", "outpatient_record")]
    plan = selection.plan_field(field_row, _config(), documents)
    if not plan.steps:
        pytest.skip("the opportunistic field does not route to this kind")
    return field_row, plan


def _ride_along(record):
    """Run the opportunistic path over one cached record."""
    field_row, plan = _opportunistic_plan()
    recorder = _Recorder({"DOC_010": {field_row["field_id"]: record}})
    cache = driver.DocumentCache(recorder)
    cache.read("DOC_010", "outpatient_record", [field_row])
    return driver.resolve_opportunistic(
        plan, field_row, cache, {("DOC_010", 1): QUOTE},
        driver._observation_id_sequence())


def test_an_explicitly_absent_ride_along_does_not_crash_the_driver():
    """A source stating the field is ABSENT carries no `value`.

    The main pass (`_absorb`) reads `presence` first and routes such a record
    to an observation with a reason and no value. This path did not check, so
    it reached `found["value"]` on exactly that record and took the whole
    driver down: CASE_712 (2026-08-21) died at stage 5 after 235.9s with
    `KeyError: 'value'`, on a case whose only peculiarity was that one
    ride-along source said a field was not present.
    """
    outcome = _ride_along(
        {"presence": "explicitly_absent", "page": 1, "quote": QUOTE,
         "reason": "해당 기재 없음"})
    assert outcome.status == "unavailable"
    assert outcome.observations == []


def test_a_ride_along_missing_its_value_is_skipped_not_fatal():
    outcome = _ride_along({"page": 1, "quote": QUOTE})
    assert outcome.status == "unavailable"


def test_a_ride_along_missing_its_quote_is_skipped_not_fatal():
    outcome = _ride_along({"value": QUOTE, "page": 1})
    assert outcome.status == "unavailable"


def test_a_well_formed_ride_along_still_asserts():
    """The guards must not swallow the case the path exists for."""
    outcome = _ride_along({"value": QUOTE, "page": 1, "quote": QUOTE})
    assert outcome.status == "asserted"
    assert outcome.observations[0]["extraction_wave"] == "opportunistic"
