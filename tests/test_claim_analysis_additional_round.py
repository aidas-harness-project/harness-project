"""Stage 3-a: the type-conditional round only re-opens what the common pass lost.

The defect this exists to close, measured on CASE_053 (2026-08-21): the case
turned on whether a facility defect caused the fall, and
`facility_defect_or_third_party_responsibility` came back
`unavailable / sources_exhausted / not_mentioned` -- honestly, since no
진료기록 discusses stair de-icing -- while the same case held two 법률의견서
arguing exactly that question to opposite conclusions. `accident_context` ranks
only medical form kinds, so no route could reach them.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import claim_analysis_additional as additional  # noqa: E402
import claim_analysis_selection as selection  # noqa: E402

CONFIG_PATH = (ROOT / "config" / "claim_analysis"
               / "claim_analysis_routing_v0.1.json")


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


class _Outcome:
    """Stands in for FieldExtractionOutcome; only `status` is read."""

    def __init__(self, status: str) -> None:
        self.status = status


def _settled_except(*unavailable: str) -> dict:
    """Every 3-a field settled by the common pass, except the named ones.

    A test that supplies only ONE field's outcome would see every OTHER
    declared field come back eligible-because-unplanned, which is correct
    behaviour (a `liability_basis` field is on no route and never gets an
    outcome) but drowns the property under test. This fixes the rest at
    `asserted` so each test isolates the field it is about.
    """
    declared = set()
    for row in _config()["additional_fields_by_case_type"].values():
        declared.update(row["field_ids"])
    return {field_id: _Outcome("unavailable" if field_id in unavailable
                               else "asserted")
            for field_id in declared}


def _assessments(**statuses: str) -> list[dict]:
    """Four verdicts, defaulting to `uncertain` -- CASE_053's real shape."""
    return [
        {"case_type": case_type, "status": statuses.get(case_type, "uncertain")}
        for case_type in ("personal_insurance", "traffic_accident",
                          "industrial_accident", "liability")
    ]


def _docs(**types: str) -> list[selection.DocumentRef]:
    return [
        selection.DocumentRef(document_id=doc_id, kind=None, ambiguous=False,
                              document_type=doc_type)
        for doc_id, doc_type in sorted(types.items())
    ]


CASE_053_DOCS = _docs(
    DOC_006="legal_opinion",     # claimant side: 배상책임 있음
    DOC_008="legal_opinion",     # insurer side: 배상책임 없음
    DOC_007="insurer_response",  # the covering letter citing DOC_008
    DOC_013="medical_record",
)

LIABILITY_FIELD = "facility_defect_or_third_party_responsibility"


# --- the trigger -----------------------------------------------------------

def test_an_uncertain_type_is_still_in_play():
    """The rule the whole round depends on, and the easiest one to get wrong.

    Requiring `applicable` would be circular: liability is `uncertain` BECAUSE
    its deciding field is unavailable, so gating the re-read on a decided
    verdict guarantees the read that would decide it never happens.
    """
    assert "liability" in additional.case_types_in_play(_assessments())


def test_only_an_affirmative_negative_closes_a_round():
    in_play = additional.case_types_in_play(
        _assessments(liability="not_applicable", traffic_accident="applicable"))
    assert "liability" not in in_play
    assert "traffic_accident" in in_play


def test_a_type_with_no_declared_fields_yields_no_round():
    """personal_insurance/traffic_accident ship empty -- an answer, not a gap."""
    live = dict(additional.rounds_for(_config(), _assessments()))
    assert "personal_insurance" not in live
    assert "traffic_accident" not in live
    assert "liability" in live


# --- eligibility: unavailable only -----------------------------------------

def test_an_unavailable_field_is_re_opened():
    eligible = additional.eligible_field_ids(
        _config(), _assessments(), _settled_except(LIABILITY_FIELD))
    assert eligible[LIABILITY_FIELD] == ["liability"]


@pytest.mark.parametrize("status", ["asserted", "conflict", "explicitly_absent",
                                    "not_applicable"])
def test_a_field_the_common_pass_settled_is_never_re_opened(status):
    """The cost-regression guard AND the authority guard, in one rule.

    A 진료기록 that stated the fact keeps its reading -- a legal opinion never
    overwrites a clinical one -- and a case whose medical records covered
    everything pays nothing for this round. `conflict` is excluded too:
    re-reading a field two sources already disagree on is adjudication, which
    belongs to consistency_check.
    """
    eligible = additional.eligible_field_ids(
        _config(), _assessments(), {**_settled_except(), LIABILITY_FIELD: _Outcome(status)})
    assert eligible == {}


def test_a_field_the_common_pass_never_planned_is_eligible():
    """Absence of an outcome means UNREAD, not out of scope.

    Inverted 2026-08-21 with the `liability_basis` domain. Every field there
    sits on no medical route (`source_route_id: null`), so no wave plans it and
    the common pass produces no outcome at all -- the earlier rule, which
    treated a missing outcome as "not in this run", would have made the six
    fields added for exactly this round permanently unreachable.
    """
    eligible = additional.eligible_field_ids(_config(), _assessments(), {})
    assert "liability_opinion_conclusion" in eligible
    assert eligible["liability_opinion_conclusion"] == ["liability"]


def test_a_field_no_live_type_asks_for_is_still_skipped():
    """The scope rule still holds: eligibility comes from a live round, not
    from merely existing in the catalogue."""
    eligible = additional.eligible_field_ids(
        _config(),
        _assessments(liability="not_applicable",
                     industrial_accident="not_applicable"),
        {})
    assert eligible == {}


# --- the read plan ---------------------------------------------------------

def test_the_legal_opinions_are_read_and_the_medical_record_is_not():
    plan = additional.read_plan(
        _config(), _assessments(),
        _settled_except(LIABILITY_FIELD), CASE_053_DOCS)
    opened = {entry["document_id"] for entry in plan}
    assert {"DOC_006", "DOC_008"} <= opened
    assert "DOC_013" not in opened, (
        "3-a reads non-medical sources; the medical ladder already had its turn")


def test_both_opposing_opinions_are_read_not_just_the_first():
    """CASE_053 held two, concluding oppositely. Reading one would make the
    verdict depend on which document happened to sort first -- and the case's
    real state is a disagreement, which only surfaces if both are read."""
    plan = additional.read_plan(
        _config(), _assessments(),
        _settled_except(LIABILITY_FIELD), CASE_053_DOCS)
    opinions = [e for e in plan if e["source_type"] == "legal_opinion"]
    assert sorted(e["document_id"] for e in opinions) == ["DOC_006", "DOC_008"]


def test_a_field_ranks_the_opinion_above_the_insurers_letter():
    """Rank is per (document, field), not per document.

    One document legitimately holds two different ranks at once: DOC_007 is
    rank 1 for the 산재 filing fields, which name no other source, and rank 2
    for a liability field that ranks the 법률의견서 first. The document's own
    `priority_rank` is the best of those (it orders the reads), so collapsing
    the per-field ranks onto it would promote the insurer's covering letter to
    the rank of the opinion it merely summarizes -- and the observation would
    record the wrong `source_priority_rank` for the field it answers. Verified
    by making `read_plan` overwrite `field_ranks` with `priority_rank`: this
    test is the only one that fails.
    """
    plan = additional.read_plan(
        _config(), _assessments(),
        _settled_except(LIABILITY_FIELD, "industrial_accident_filing_basis"),
        CASE_053_DOCS)
    by_id = {entry["document_id"]: entry for entry in plan}
    assert by_id["DOC_006"]["field_ranks"][LIABILITY_FIELD] == 1
    assert by_id["DOC_007"]["field_ranks"][LIABILITY_FIELD] == 2
    assert by_id["DOC_007"]["field_ranks"]["industrial_accident_filing_basis"] == 1


def test_fields_wanting_the_same_document_share_one_read():
    """Batched like the common pass: one call serves every asking field."""
    plan = additional.read_plan(
        _config(), _assessments(),
        _settled_except(LIABILITY_FIELD, "accident_date"), CASE_053_DOCS)
    reads = [e for e in plan if e["document_id"] == "DOC_006"]
    assert len(reads) == 1, "two fields wanting one document must not cost two reads"
    assert reads[0]["field_ids"] == sorted([LIABILITY_FIELD, "accident_date"])


def test_a_case_without_the_source_documents_plans_nothing():
    """No legal opinion, no insurer letter -- the round costs zero."""
    plan = additional.read_plan(
        _config(), _assessments(),
        _settled_except(LIABILITY_FIELD),
        _docs(DOC_013="medical_record"))
    assert plan == []


def test_a_closed_type_plans_nothing_even_with_the_documents_present():
    plan = additional.read_plan(
        _config(), _assessments(liability="not_applicable",
                                industrial_accident="not_applicable"),
        _settled_except(LIABILITY_FIELD), CASE_053_DOCS)
    assert plan == []


def test_a_field_two_live_types_both_want_is_read_once():
    """Union across types, not one read per type."""
    config = _config()
    config["additional_fields_by_case_type"]["traffic_accident"] = {
        "trigger": "in_play",
        "field_ids": [LIABILITY_FIELD],
        "sources": ["legal_opinion"],
    }
    eligible = additional.eligible_field_ids(
        config, _assessments(), _settled_except(LIABILITY_FIELD))
    assert eligible[LIABILITY_FIELD] == ["liability", "traffic_accident"]
    plan = additional.read_plan(
        config, _assessments(),
        _settled_except(LIABILITY_FIELD), CASE_053_DOCS)
    assert len([e for e in plan if e["document_id"] == "DOC_006"]) == 1


# --- the config contract ---------------------------------------------------

def test_every_declared_field_id_exists_in_the_catalogue():
    """A typo would extract nothing, silently. 3-a introduces no new fields --
    it re-opens catalogue fields from sources the medical routes cannot reach.
    """
    config = _config()
    known = {row["field_id"] for row in config["fields"]}
    for case_type, row in config["additional_fields_by_case_type"].items():
        unknown = [f for f in row["field_ids"] if f not in known]
        assert not unknown, f"{case_type} names fields absent from `fields`: {unknown}"


def test_declared_sources_are_real_document_types():
    from _validation import load_registry

    schemas, _ = load_registry()
    document_types = set(
        schemas["common_component_output.schema.json"]["$defs"]["document_type"]["enum"])
    config = _config()
    for case_type, row in config["additional_fields_by_case_type"].items():
        unknown = [s for s in row["sources"] if s not in document_types]
        assert not unknown, f"{case_type} names non-existent types: {unknown}"


def test_the_shipped_liability_round_targets_the_type_trigger_field():
    """Whatever else it reads, it must cover the field liability is judged on.

    `case_types.TYPE_TRIGGER_FIELDS['liability']` names exactly this field, so
    a round omitting it could never move the verdict it exists to move.
    """
    import claim_analysis_case_types as case_types

    declared = _config()["additional_fields_by_case_type"]["liability"]
    for field_id in case_types.TYPE_TRIGGER_FIELDS["liability"]:
        assert field_id in declared["field_ids"]


# --- settlement: what the round publishes ----------------------------------
#
# The gap that let CASE_701 fail. Every test above exercises the READ PLAN --
# which documents, which fields, what order -- and none exercised what happens
# once two documents both answer one field. `extract_additional` assigned the
# whole observation list to `selected_observation_ids` before branching, which
# violates BOTH arms of the result schema at once (`asserted` wants exactly
# one, `conflict` wants none) and is invisible until a field collects two
# readings. Stage 3-a is the first round where that is routine: two 법률의견서
# arguing opposite conclusions is the normal shape of a disputed liability
# case, not an edge case.

def _settle(readings_by_document):
    """Run the real settlement path over canned per-document readings."""
    import run_claim_analysis_selective as driver

    docs = _docs(DOC_006="legal_opinion", DOC_008="legal_opinion")
    quote = "인용문"
    page_text = {("DOC_006", 1): quote, ("DOC_008", 1): quote}

    def extract(document_id, kind, field_rows, document_type=None):
        assert kind is None, "a non-medical document has no medical form kind"
        return {
            field_id: {"presence": "asserted", "value": value,
                       "page": 1, "quote": quote}
            for field_id, value in readings_by_document[document_id].items()
        }

    settled, calls = driver.extract_additional(
        config=_config(), documents=docs, assessments=_assessments(),
        outcomes_by_field={}, extract=extract, page_text=page_text,
        observation_ids=driver._observation_id_sequence())
    return {outcome.field_id: outcome for outcome in settled}, calls


OPINION = "liability_opinion_conclusion"


def test_two_sources_that_agree_elect_exactly_one():
    settled, _ = _settle({
        "DOC_006": {OPINION: "성립"},
        "DOC_008": {OPINION: "성립"},
    })
    outcome = settled[OPINION]
    assert outcome.status == "asserted"
    assert len(outcome.observations) == 2, "both readings are kept"
    assert len(outcome.selected_ids) == 1, (
        "`asserted` requires exactly one elected observation")
    assert outcome.selected_ids[0] == outcome.observations[0]["observation_id"], (
        "the highest-priority reading is the elected one")


def test_two_sources_that_disagree_elect_none():
    settled, _ = _settle({
        "DOC_006": {OPINION: "성립"},
        "DOC_008": {OPINION: "불성립"},
    })
    outcome = settled[OPINION]
    assert outcome.status == "conflict"
    assert outcome.stop_reason == "conflict_found"
    assert len(outcome.observations) == 2
    assert outcome.selected_ids == [], (
        "electing one would pick a winner between two 법률의견서")


def test_a_single_source_elects_its_only_reading():
    settled, _ = _settle({
        "DOC_006": {OPINION: "성립"},
        "DOC_008": {},
    })
    outcome = settled[OPINION]
    assert outcome.status == "asserted"
    assert len(outcome.selected_ids) == 1


def test_settled_outcomes_validate_against_the_result_schema():
    """The check that would have caught this before a real run did.

    Asserting on the outcome object alone is not enough -- the defect was a
    contract violation, and only the schema states the contract.
    """
    import json

    import run_claim_analysis_selective as driver
    from _validation import load_registry, validate_instance

    settled, _ = _settle({
        "DOC_006": {OPINION: "성립", "comparative_negligence_rate": 0},
        "DOC_008": {OPINION: "불성립", "comparative_negligence_rate": 0},
    })
    outcomes = list(settled.values())
    result = driver.build_result(
        case_id="CASE_9001", run_id="RUN_20260821_1", outcomes=outcomes,
        config=_config(), documents=CASE_053_DOCS, model_name="test",
        filing_status_by_type=driver.filing_status_by_case_type(),
        conflict_candidate_ids_by_field=(
            driver.assign_conflict_candidate_ids(outcomes)))
    schemas, registry = load_registry()
    errors = validate_instance(
        result, "claim_analysis_result.schema.json", schemas, registry)
    assert errors == [], json.dumps(errors[:3], ensure_ascii=False)
