"""Selective reading: what gets opened, and what deliberately does not.

The budget test file next door proves a document is never read twice. This one
proves the harder half -- that a document is never read *at all* unless some
still-unresolved field asked for it. Those are different guarantees: an
implementation that reads everything up front and then applies the stop rule to
the cache satisfies the first and fails the second, which is exactly the state
this replaced.

Every test isolates the field set it needs, because with all 56 fields active
almost every document is legitimately wanted by someone, and "was DOC_003
opened?" then answers a question about the other 55 fields rather than the one
under test.
"""
from __future__ import annotations

import copy

import pytest

import claim_analysis_contracts as contracts
import claim_analysis_selection as selection
import run_claim_analysis_selective as driver


QUOTE = "정형외과"
VALUE = "정형외과"

# A non-critical field (no comparison budget) whose route ladder puts each of
# three document kinds on its own rung -- so "did it stop?" is directly visible
# as "which documents were opened?".
ORDINARY_FIELD = "diagnosis_department"
# A critical field: it may buy exactly one extra independent source.
CRITICAL_FIELD = "primary_diagnosis"

THREE_DOCS = [
    selection.DocumentRef("DOC_001", "diagnosis_certificate"),
    selection.DocumentRef("DOC_002", "admission_discharge_summary"),
    selection.DocumentRef("DOC_003", "outpatient_record"),
]


def _config_with(*field_ids: str) -> dict:
    config = copy.deepcopy(contracts.load_default_routing_config())
    config["fields"] = [row for row in config["fields"]
                        if row["field_id"] in field_ids]
    return config


def _pages(*doc_ids: str) -> dict:
    return {(doc_id, 1): f"진료과: {QUOTE}" for doc_id in doc_ids}


class Recorder:
    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.opened: list[str] = []

    def __call__(self, document_id, kind, field_rows):
        self.opened.append(document_id)
        wanted = {row["field_id"] for row in field_rows}
        return {k: v for k, v in self.answers.get(document_id, {}).items()
                if k in wanted}


def _answer_everywhere(field_id: str, *doc_ids: str) -> dict:
    return {doc_id: {field_id: {"value": VALUE, "page": 1, "quote": QUOTE}}
            for doc_id in doc_ids}


def _run(config, recorder, documents=None, pages=None):
    return driver.extract_all(
        config=config,
        documents=documents or THREE_DOCS,
        extract=recorder,
        page_text=pages or _pages("DOC_001", "DOC_002", "DOC_003"),
        observation_ids=driver._observation_id_sequence(),
    )


# ------------------------------------------------- the stop actually stops --

def test_an_ordinary_field_opens_only_its_first_priority_source() -> None:
    """The defect this exists for: reading the whole ladder anyway.

    All three rungs can answer. A field that stops at the first trusted value
    must leave rungs 2 and 3 unopened -- not read them and discard the result,
    which costs exactly as much as needing them.
    """
    config = _config_with(ORDINARY_FIELD)
    ladder = selection.plan_field(config["fields"][0], config, THREE_DOCS)
    assert len(ladder.steps) == 3, "fixture must offer a real 3-rung ladder"

    recorder = Recorder(_answer_everywhere(
        ORDINARY_FIELD, "DOC_001", "DOC_002", "DOC_003"))
    outcomes, cache = _run(config, recorder)

    assert recorder.opened == ["DOC_001"]
    assert cache.calls == {"DOC_001": 1}
    assert outcomes[0].status == "asserted"
    assert outcomes[0].documents_read == 1


def test_the_ladder_is_walked_in_priority_order_when_it_is_needed() -> None:
    """Falling back is still allowed -- it just has to be earned."""
    config = _config_with(ORDINARY_FIELD)
    recorder = Recorder(_answer_everywhere(ORDINARY_FIELD, "DOC_003"))
    outcomes, _ = _run(config, recorder)

    assert recorder.opened == ["DOC_001", "DOC_002", "DOC_003"]
    assert outcomes[0].status == "asserted"


def test_no_source_at_all_leaves_the_field_unavailable() -> None:
    config = _config_with(ORDINARY_FIELD)
    recorder = Recorder({})
    outcomes, _ = _run(config, recorder)

    assert recorder.opened == ["DOC_001", "DOC_002", "DOC_003"]
    assert outcomes[0].status == "unavailable"
    assert outcomes[0].unavailable_reason == "not_mentioned"


# ------------------------------------------- the critical comparison holds --

def test_a_critical_field_buys_exactly_one_second_source() -> None:
    """One extra, and only one. Not zero, and not the rest of the ladder."""
    config = _config_with(CRITICAL_FIELD)
    assert config["fields"][0]["critical_conflict_field"] is True

    recorder = Recorder(_answer_everywhere(
        CRITICAL_FIELD, "DOC_001", "DOC_002", "DOC_003"))
    outcomes, _ = _run(config, recorder)

    assert recorder.opened == ["DOC_001", "DOC_002"]
    assert outcomes[0].comparisons == 1
    assert outcomes[0].status == "asserted"


def test_a_disagreeing_second_source_becomes_a_conflict_and_stops() -> None:
    config = _config_with(CRITICAL_FIELD)
    answers = _answer_everywhere(CRITICAL_FIELD, "DOC_001", "DOC_002", "DOC_003")
    answers["DOC_002"][CRITICAL_FIELD]["value"] = "다른 값"
    recorder = Recorder(answers)
    outcomes, _ = _run(config, recorder)

    assert recorder.opened == ["DOC_001", "DOC_002"]
    assert outcomes[0].status == "conflict"
    assert outcomes[0].selected_ids == []


# --------------------------------------- a stopped field schedules nothing --

def test_a_resolved_field_does_not_drag_a_document_open_for_a_peer() -> None:
    """Two fields, different ladders. One stops; the other keeps going.

    The stopped field must not contribute its remaining documents to the
    schedule, and the open field must still get what it needs. This is the
    regression where a finished field's ladder is walked anyway because the
    scheduler asks the plan rather than the field's live position.
    """
    config = _config_with(ORDINARY_FIELD, "diagnosis_site")
    answers = {
        "DOC_001": {ORDINARY_FIELD: {"value": VALUE, "page": 1, "quote": QUOTE}},
        "DOC_003": {"diagnosis_site": {"value": VALUE, "page": 1, "quote": QUOTE}},
    }
    recorder = Recorder(answers)
    outcomes, _ = _run(config, recorder)

    by_field = {outcome.field_id: outcome for outcome in outcomes}
    assert by_field[ORDINARY_FIELD].status == "asserted"
    assert by_field[ORDINARY_FIELD].documents_read == 1
    # DOC_002 is opened for diagnosis_site's walk, not for the stopped field.
    assert recorder.opened.count("DOC_001") == 1
    assert recorder.opened.count("DOC_002") <= 1
    assert recorder.opened.count("DOC_003") == 1


def test_one_call_serves_every_field_asking_for_the_same_document() -> None:
    """Batching survives the demand-driven rewrite."""
    config = _config_with(ORDINARY_FIELD, CRITICAL_FIELD, "diagnosis_site")
    recorder = Recorder({"DOC_001": {
        ORDINARY_FIELD: {"value": VALUE, "page": 1, "quote": QUOTE},
        CRITICAL_FIELD: {"value": VALUE, "page": 1, "quote": QUOTE},
        "diagnosis_site": {"value": VALUE, "page": 1, "quote": QUOTE},
    }})
    _run(config, recorder)
    assert recorder.opened.count("DOC_001") == 1


def test_a_document_is_read_once_even_across_both_waves() -> None:
    """An A-wave field and a B-wave field wanting the same document.

    They resolve in different rounds, so a cache miss on the second round would
    be a second paid call for one document.
    """
    config = _config_with(CRITICAL_FIELD, "diagnosis_date")
    waves = {row["field_id"]: row["extraction_wave"] for row in config["fields"]}
    assert set(waves.values()) == {"A", "B"}, waves

    recorder = Recorder({"DOC_001": {
        CRITICAL_FIELD: {"value": VALUE, "page": 1, "quote": QUOTE},
        "diagnosis_date": {"value": VALUE, "page": 1, "quote": QUOTE},
    }})
    _, cache = _run(config, recorder)
    assert cache.calls.get("DOC_001") == 1
    assert recorder.opened.count("DOC_001") == 1


def test_trace_reports_the_documents_that_were_never_opened() -> None:
    """The record has to show the saving, or it cannot be checked later."""
    config = _config_with(ORDINARY_FIELD)
    recorder = Recorder(_answer_everywhere(
        ORDINARY_FIELD, "DOC_001", "DOC_002", "DOC_003"))
    _, cache = _run(config, recorder)

    assert cache.was_read("DOC_001") is True
    assert cache.was_read("DOC_002") is False
    assert cache.was_read("DOC_003") is False
    assert cache.calls.get("DOC_002", 0) == 0
