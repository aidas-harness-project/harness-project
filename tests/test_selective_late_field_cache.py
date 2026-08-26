"""A document is opened once, so that one call must serve every field it can.

The scheduler batches by *demand*: each round asks the still-open fields which
document they want next, and reads the one on the highest rung. That gives
priority order and one call per document for free -- and, in an earlier
revision, silently lost every field that arrived at a document later than the
first asker did.

The shape of the loss is worth stating precisely, because it looks like
correct behaviour from every angle except the outcome. `primary_diagnosis`
ranks DOC_001 first; `accident_mechanism` ranks DOC_005 first and DOC_001
second. DOC_001 is therefore opened on `primary_diagnosis`'s behalf, and the
provider is asked only for `primary_diagnosis`. When `accident_mechanism`
later walks down to DOC_001 it finds a cache entry -- a real one, from a real
read -- that simply has no key for it. The budget rule then forbids a second
call. So the field reports `unavailable`/`not_mentioned` about a document
whose text states it outright, and nothing anywhere records that a question
was dropped rather than answered.

The fix asks the opening call for the latecomers too, and holds their answers
until their own ladder reaches that rung. The two halves are separable and
both matter, so both are pinned here: extracting early must not become
*adopting* early, or the priority order the whole design exists to enforce
would be decided by whichever field happened to open the document first.
"""
from __future__ import annotations

import copy

import claim_analysis_contracts as contracts
import claim_analysis_selection as selection
import run_claim_analysis_selective as driver


# DOC_001 (진단서) is rank 1 for primary_diagnosis and rank 2 for
# accident_mechanism; DOC_005 (초진기록) is the reverse. Taken from the real
# routing config, not invented for the test -- the combination is the ordinary
# shape of a case, which is why the defect was reachable in production.
FIRST_FIELD = "primary_diagnosis"
LATE_FIELD = "accident_mechanism"

DOCS = [
    selection.DocumentRef("DOC_001", "diagnosis_certificate"),
    selection.DocumentRef("DOC_005", "initial_visit_record"),
]

PAGES = {
    ("DOC_001", 1): "진단명: 골절 / 사고경위: 넘어짐",
    ("DOC_005", 1): "사고경위: 추락",
}


def _config() -> dict:
    config = copy.deepcopy(contracts.load_default_routing_config())
    config["fields"] = [row for row in config["fields"]
                        if row["field_id"] in {FIRST_FIELD, LATE_FIELD}]
    return config


class Recorder:
    """A provider that answers only what it was actually asked for.

    This is the whole point of the fixture: a stub that returned every field
    regardless of `field_rows` would pass even with the defect present, since
    the defect is precisely that the ask omits a field.
    """

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, document_id, kind, field_rows):
        asked = sorted(row["field_id"] for row in field_rows)
        self.calls.append((document_id, asked))
        return {k: v for k, v in self.answers.get(document_id, {}).items()
                if k in set(asked)}


def _answers(mechanism_in_005: str | None = "추락") -> dict:
    answers = {
        "DOC_001": {
            FIRST_FIELD: {"value": "골절", "page": 1, "quote": "골절"},
            LATE_FIELD: {"value": "넘어짐", "page": 1, "quote": "넘어짐"},
        },
        "DOC_005": {},
    }
    if mechanism_in_005 is not None:
        answers["DOC_005"][LATE_FIELD] = {
            "value": mechanism_in_005, "page": 1, "quote": mechanism_in_005}
    return answers


def _run(recorder):
    return driver.extract_all(
        config=_config(), documents=DOCS, extract=recorder, page_text=PAGES,
        observation_ids=driver._observation_id_sequence())


def _outcome(outcomes, field_id):
    return next(o for o in outcomes if o.field_id == field_id)


# ------------------------------------------------- the ask, and the answer --

def test_the_two_fields_really_do_rank_this_document_differently() -> None:
    """Guards the fixture: without this, the rest tests nothing.

    If a config change ever put both fields on the same rung for DOC_001, the
    tests below would keep passing while no longer exercising a latecomer.
    """
    config = _config()
    by_field = {row["field_id"]: row for row in config["fields"]}
    ranks = {}
    for field_id in (FIRST_FIELD, LATE_FIELD):
        plan = selection.plan_field(by_field[field_id], config, DOCS)
        ranks[field_id] = {
            document_id: step.priority_rank
            for step in plan.steps for document_id in step.document_ids
        }
    assert ranks[FIRST_FIELD]["DOC_001"] == 1
    assert ranks[LATE_FIELD]["DOC_001"] > 1, "DOC_001 must be a LATE rung here"
    assert ranks[LATE_FIELD]["DOC_005"] < ranks[LATE_FIELD]["DOC_001"]


def test_the_first_call_asks_for_the_late_field_too() -> None:
    """The document is opened for one field and asked about both."""
    recorder = Recorder(_answers())
    _run(recorder)

    first = next(call for call in recorder.calls if call[0] == "DOC_001")
    assert first[1] == sorted([FIRST_FIELD, LATE_FIELD])


def test_the_late_field_gets_its_value_from_the_cache() -> None:
    """The defect's visible symptom: `unavailable` for a stated fact.

    DOC_005 says nothing, so the only source for the mechanism is DOC_001 --
    reachable only through the cache entry written when it was opened for
    `primary_diagnosis`.
    """
    recorder = Recorder(_answers(mechanism_in_005=None))
    outcomes, _ = _run(recorder)

    late = _outcome(outcomes, LATE_FIELD)
    assert late.status == "asserted"
    assert [observation["value"] for observation in late.observations] == ["넘어짐"]


def test_the_document_is_still_read_exactly_once() -> None:
    """Serving the latecomer must not buy a second call."""
    recorder = Recorder(_answers())
    _, cache = _run(recorder)

    assert cache.calls["DOC_001"] == 1
    assert [call[0] for call in recorder.calls].count("DOC_001") == 1
    assert all(count == 1 for count in cache.calls.values())


# ------------------------------------------------ extracting is not adopting --

def test_the_late_field_still_takes_its_own_rank_one_source_first() -> None:
    """Riding along must not reorder the ladder.

    Both documents state a mechanism, and they disagree. DOC_001's answer was
    in hand first -- extracted before DOC_005 was even opened -- but DOC_005 is
    this field's rank-1 source, so that is the reading consumed first. If the
    cached answer were adopted on arrival, rank 2 would have won purely because
    another field opened its document earlier.
    """
    recorder = Recorder(_answers(mechanism_in_005="추락"))
    outcomes, _ = _run(recorder)

    late = _outcome(outcomes, LATE_FIELD)
    ranks = [observation["source_priority_rank"] for observation in late.observations]
    values = [observation["value"] for observation in late.observations]
    assert ranks == sorted(ranks), "observations must arrive in ladder order"
    assert values[0] == "추락", "the rank-1 source is consumed first"
    assert late.observations[0]["source_priority_rank"] == 1


def test_an_early_extracted_answer_is_not_selected_before_its_turn() -> None:
    """`accident_mechanism` is critical, so the disagreement is a conflict.

    The point is what it is NOT: a field silently resolved to the value that
    happened to be extracted first, with the higher-priority source never
    consulted.
    """
    recorder = Recorder(_answers(mechanism_in_005="추락"))
    outcomes, _ = _run(recorder)

    late = _outcome(outcomes, LATE_FIELD)
    assert late.status == "conflict"
    assert late.selected_ids == []
    assert {observation["value"] for observation in late.observations} == {"추락", "넘어짐"}


def test_a_finished_field_is_not_extracted_for_later_documents() -> None:
    """A field that stopped contributes nothing to any later ask.

    `diagnosis_department` is non-critical: DOC_001 settles it outright. DOC_005
    is opened afterwards for the mechanism, and must not carry a request for a
    field that is done -- that would be paying (in prompt size, and in a value
    nobody may use) for a read the stop rule already ruled out.

    Deliberately not `primary_diagnosis`: that field is critical, so after
    DOC_001 it still owes one comparison and is legitimately still asking. The
    distinction is the rule, not an artifact -- riding along is for OPEN fields.
    """
    config = _config()
    everything = copy.deepcopy(contracts.load_default_routing_config())["fields"]
    settled = "diagnosis_department"
    config["fields"] = [row for row in everything
                        if row["field_id"] in {settled, LATE_FIELD}]
    recorder = Recorder({
        "DOC_001": {settled: {"value": "정형외과", "page": 1, "quote": "정형외과"},
                    LATE_FIELD: {"value": "넘어짐", "page": 1, "quote": "넘어짐"}},
        "DOC_005": {LATE_FIELD: {"value": "추락", "page": 1, "quote": "추락"}},
    })
    driver.extract_all(
        config=config, documents=DOCS, extract=recorder,
        page_text={("DOC_001", 1): "진료과: 정형외과 / 사고경위: 넘어짐",
                   ("DOC_005", 1): "사고경위: 추락"},
        observation_ids=driver._observation_id_sequence())

    first = next(call for call in recorder.calls if call[0] == "DOC_001")
    assert settled in first[1], "it was the field that opened DOC_001"
    later = next(call for call in recorder.calls if call[0] == "DOC_005")
    assert settled not in later[1], "it stopped, so it asks for nothing more"


def test_a_field_is_never_extracted_from_a_document_it_does_not_route_to() -> None:
    """Riding along is bounded by the route, not by what happens to be open."""
    config = _config()
    # `diagnosis_department` routes to 진단서 kinds, never to an 후유장해진단서.
    config["fields"] = [row for row in
                        copy.deepcopy(contracts.load_default_routing_config())["fields"]
                        if row["field_id"] == "diagnosis_department"]
    documents = [selection.DocumentRef("DOC_001", "diagnosis_certificate"),
                 selection.DocumentRef("DOC_009", "medical_expense_receipt")]
    recorder = Recorder({"DOC_001": {"diagnosis_department": {
        "value": "정형외과", "page": 1, "quote": "정형외과"}}})
    driver.extract_all(
        config=config, documents=documents, extract=recorder,
        page_text={("DOC_001", 1): "진료과: 정형외과"},
        observation_ids=driver._observation_id_sequence())

    assert [call[0] for call in recorder.calls] == ["DOC_001"]
