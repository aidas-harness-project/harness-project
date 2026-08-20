"""Liability facts are not medical facts, and the config now says so.

Every one of the original 56 fields was a clinical finding, graded on what a
의사 needs. That left the single field liability is judged on --
`facility_defect_or_third_party_responsibility` -- sitting at
`medical_advisory_grade: C`, medically unimportant and therefore low priority,
while being the entire basis of the case type. And it left 과실비율, 적용 법조
and 자기부담금 with nowhere to live at all.

`liability_basis` (2026-08-21) is the first `legal_factual` domain: its fields
declare `priority_grade` instead of a medical grade, and `legal_authority`
separates a fact stated in a document from an opinion a party's counsel
reached. That second axis is what stops two opposing 법률의견서 from being
filed as a records contradiction -- on CASE_053, DOC_006 (claimant) concluded
배상책임 성립 under 민법 제758조 and DOC_008 (insurer) concluded 불성립 on the
same facts. That is the dispute, not an error to reconcile.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import claim_analysis_case_types as case_types  # noqa: E402
import claim_analysis_contracts as contracts  # noqa: E402
import claim_analysis_selection as selection  # noqa: E402
from _validation import load_registry, validate_instance  # noqa: E402

CONFIG_PATH = (ROOT / "config" / "claim_analysis"
               / "claim_analysis_routing_v0.1.json")


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _errors(config: dict) -> list[str]:
    schemas, registry = load_registry()
    return validate_instance(
        config, "claim_analysis_routing_config.schema.json", schemas, registry)


def _fact(field_id: str, status: str, *values, doc: str = "DOC_006") -> dict:
    fact = {"field_id": field_id, "resolution_status": status,
            "selected_observation_ids": [], "observations": []}
    for index, value in enumerate(values, start=1):
        observation_id = f"CAO_{index:04d}"
        fact["selected_observation_ids"].append(observation_id)
        fact["observations"].append({
            "observation_id": observation_id,
            "value_state": "asserted",
            "value": value,
            "evidence_references": [{"document_id": doc, "page": 1,
                                     "quote": "인용", "start_char": 0,
                                     "end_char": 2}],
        })
    return fact


DEFECT = "facility_defect_or_third_party_responsibility"
OPINION = "liability_opinion_conclusion"


def _liability(facts) -> dict:
    return {row["case_type"]: row
            for row in case_types.assess_case_types(facts)}["liability"]


# --- the domain exists and is not medical ----------------------------------

def test_the_shipped_config_declares_one_legal_domain():
    config = _config()
    assert _errors(config) == []
    assert contracts.validate_routing_config_semantics(config) == []
    kinds = {row["code"]: row.get("domain_kind", "medical")
             for row in config["domains"]}
    assert kinds["liability_basis"] == "legal_factual"
    assert sum(1 for k in kinds.values() if k == "medical") == 8


def test_a_legal_domain_declares_no_medical_grade_ceiling():
    """`effective_first_required_grade` compares against a medical grade its
    fields do not have, so declaring one would be meaningless."""
    config = _config()
    config["domains"] = [
        {**row, "effective_first_required_grade": "A"}
        if row["code"] == "liability_basis" else row
        for row in config["domains"]
    ]
    assert contracts.validate_routing_config_semantics(config)


def test_a_ninth_medical_domain_is_still_refused():
    """The clinical taxonomy stays pinned; only the non-medical slot opened."""
    config = _config()
    config["domains"] = [
        {**row, "domain_kind": "medical"} if row["code"] == "liability_basis"
        else row for row in config["domains"]
    ]
    assert contracts.validate_routing_config_semantics(config)


# --- exactly one grading axis ----------------------------------------------

def test_a_legal_field_grades_on_priority_not_medical_advice():
    config = _config()
    legal = [row for row in config["fields"]
             if row["domain_code"] == "liability_basis"]
    assert legal, "the liability fields are not registered"
    for row in legal:
        assert "medical_advisory_grade" not in row, row["field_id"]
        assert row["priority_grade"] in {"A", "B", "C"}


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda row: row.update({"medical_advisory_grade": "B"}),
                 id="both_axes"),
    pytest.param(lambda row: row.pop("priority_grade"), id="neither_axis"),
])
def test_a_field_carries_exactly_one_grading_axis(mutate):
    """Both would leave two rankings free to disagree; neither would make the
    field's wave unorderable."""
    config = _config()
    row = next(r for r in config["fields"]
               if r["domain_code"] == "liability_basis")
    mutate(row)
    assert _errors(config)


def test_the_planner_reads_whichever_axis_the_field_declares():
    medical = {"field_id": "x", "medical_advisory_grade": "A"}
    legal = {"field_id": "y", "priority_grade": "B"}
    assert selection.field_grade(medical) == "A"
    assert selection.field_grade(legal) == "B"


# --- the verdict can now be reached ----------------------------------------

def test_a_legal_opinion_finding_liability_establishes_the_type():
    """The CASE_053 gap: no 진료기록 discusses stair de-icing, so the medical
    route leaves the defect field unavailable and the type stayed uncertain."""
    verdict = _liability([_fact(DEFECT, "unavailable"),
                          _fact(OPINION, "asserted", "성립")])
    assert verdict["status"] == "applicable"
    assert verdict["triggered_field_ids"] == [OPINION]
    assert verdict["evidence_references"]


def test_an_opinion_finding_no_liability_does_not_close_the_type():
    """`not_applicable` is reserved for an affirmative statement that no
    third-party responsibility exists. An insurer's counsel concluding 불성립
    is one side's position on the disputed question -- letting it close the
    type would hand the verdict to whichever party's opinion is in the file.
    """
    verdict = _liability([_fact(DEFECT, "unavailable"),
                          _fact(OPINION, "asserted", "불성립")])
    assert verdict["status"] == "uncertain"
    assert "일방의 견해" in verdict["reason"]


def test_two_opposing_opinions_establish_the_type_and_say_it_is_disputed():
    """CASE_053's real shape: DOC_006 성립, DOC_008 불성립."""
    verdict = _liability([_fact(DEFECT, "unavailable"),
                          _fact(OPINION, "asserted", "성립", "불성립")])
    assert verdict["status"] == "applicable"
    assert "쟁점" in verdict["reason"]


def test_a_conflict_field_elects_nothing_and_is_still_read():
    """The shape the RESULT SCHEMA requires, which the earlier test did not use.

    A `conflict` field must publish `selected_observation_ids: []` -- electing
    one would pick a winner between two 법률의견서. The verdict path therefore
    cannot read the elected set: doing so made exactly the disputed case
    invisible and sent liability back to `uncertain`. Caught on CASE_701, the
    first real run of stage 3-a: `claim_facts/47/selected_observation_ids`
    ["CAO_0029","CAO_0032","CAO_0035"] "is expected to be empty".
    """
    disputed = _fact(OPINION, "conflict")
    disputed["selected_observation_ids"] = []          # schema-required
    disputed["observations"] = [
        {"observation_id": "CAO_0001", "value_state": "asserted",
         "value": "성립",
         "evidence_references": [{"document_id": "DOC_006", "page": 10,
                                  "quote": "인용", "start_char": 0,
                                  "end_char": 2}]},
        {"observation_id": "CAO_0002", "value_state": "asserted",
         "value": "불성립",
         "evidence_references": [{"document_id": "DOC_008", "page": 12,
                                  "quote": "인용", "start_char": 0,
                                  "end_char": 2}]},
    ]
    verdict = _liability([_fact(DEFECT, "unavailable"), disputed])
    assert verdict["status"] == "applicable"
    assert verdict["triggered_field_ids"] == [OPINION]
    assert "쟁점" in verdict["reason"]


def test_a_disputed_opinion_field_still_triggers_the_verdict():
    """A `conflict` field can still be what established the type.

    Two opposing 법률의견서 make `liability_opinion_conclusion` a conflict, and
    one of them stated 성립. Requiring `asserted` in the trigger filter dropped
    it, which sent the verdict back to `uncertain` through the "could not
    connect the fact to an extracted field" branch -- undoing the whole point
    of reading the opinions. The disagreement is reported separately, in
    `conflicting_field_ids`.
    """
    disputed = _fact(OPINION, "conflict")
    disputed["selected_observation_ids"] = ["CAO_0001", "CAO_0002"]
    disputed["observations"] = [
        {"observation_id": "CAO_0001", "value_state": "asserted",
         "value": "성립",
         "evidence_references": [{"document_id": "DOC_006", "page": 10,
                                  "quote": "인용", "start_char": 0,
                                  "end_char": 2}]},
        {"observation_id": "CAO_0002", "value_state": "asserted",
         "value": "불성립",
         "evidence_references": [{"document_id": "DOC_008", "page": 12,
                                  "quote": "인용", "start_char": 0,
                                  "end_char": 2}]},
    ]
    verdict = _liability([_fact(DEFECT, "unavailable"), disputed])
    assert verdict["status"] == "applicable"
    assert verdict["triggered_field_ids"] == [OPINION]
    assert verdict["conflicting_field_ids"] == [OPINION]
    assert "쟁점" in verdict["reason"]


def test_the_dispute_is_stated_even_when_the_facts_decide_it():
    """The sentence a 손해사정사 reads must say the conclusion is contested.

    CASE_701: stage 3-a read `공작물의 설치 보존상의 하자` from one opinion, so
    the DEFECT field came back asserted and settled the verdict on the facts
    branch -- which returned before the opinion branch could mention that the
    two 법률의견서 reach opposite conclusions. The dispute stayed visible in
    `conflicting_field_ids` and vanished from `reason`, which is the only part
    of this that reaches the report's prose.
    """
    disputed = _fact(OPINION, "conflict")
    disputed["selected_observation_ids"] = []
    disputed["observations"] = [
        {"observation_id": "CAO_0001", "value_state": "asserted",
         "value": "성립",
         "evidence_references": [{"document_id": "DOC_006", "page": 10,
                                  "quote": "인용", "start_char": 0,
                                  "end_char": 2}]},
        {"observation_id": "CAO_0002", "value_state": "asserted",
         "value": "불성립",
         "evidence_references": [{"document_id": "DOC_008", "page": 12,
                                  "quote": "인용", "start_char": 0,
                                  "end_char": 2}]},
    ]
    verdict = _liability([_fact(DEFECT, "asserted", "공작물의 설치 보존상의 하자"),
                          disputed])
    assert verdict["status"] == "applicable"
    assert "사고 경위에" in verdict["reason"], "the facts are what decided it"
    assert "쟁점" in verdict["reason"], (
        "opposing legal opinions must be stated in the sentence, not only in "
        "conflicting_field_ids")


def test_agreeing_opinions_add_no_dispute_note():
    """The note is earned, not decorative."""
    verdict = _liability([_fact(DEFECT, "asserted", "공작물의 하자"),
                          _fact(OPINION, "asserted", "성립")])
    assert verdict["status"] == "applicable"
    assert "쟁점" not in verdict["reason"]


def test_the_medical_route_still_wins_when_it_answers():
    """A 진료기록 that does record the mechanism keeps its reading; the legal
    opinion is a fallback, not an override."""
    verdict = _liability([_fact(DEFECT, "asserted", "계단 결빙 방치"),
                          _fact(OPINION, "asserted", "불성립")])
    assert verdict["status"] == "applicable"
    assert DEFECT in verdict["triggered_field_ids"]
    # Both fields carry an asserted value, so both appear; what matters is
    # which one produced the verdict. An insurer's counsel concluding 불성립
    # must not become the stated basis for a documented finding.
    assert "사고 경위에" in verdict["reason"]


def test_a_verdict_names_only_the_field_that_carried_it():
    """`field_id in fields` was enough while every type had ONE trigger.
    Liability now has two and only one may have answered -- naming both would
    credit an `unavailable` field with the finding."""
    verdict = _liability([_fact(DEFECT, "unavailable"),
                          _fact(OPINION, "asserted", "성립")])
    assert DEFECT not in verdict["triggered_field_ids"]


def test_neither_source_leaves_the_type_uncertain_not_negative():
    verdict = _liability([_fact(DEFECT, "unavailable")])
    assert verdict["status"] == "uncertain"
    assert verdict["triggered_field_ids"] == []


# --- the round reads them --------------------------------------------------

def test_the_liability_round_asks_for_the_new_fields():
    declared = _config()["additional_fields_by_case_type"]["liability"]
    for field_id in (OPINION, "comparative_negligence_rate",
                     "legal_basis_cited"):
        assert field_id in declared["field_ids"]
    assert declared["sources"][0] == "legal_opinion"


def test_the_new_fields_are_not_on_a_medical_route():
    """They must reach documents only through stage 3-a; a medical route would
    schedule them against 진료기록 that cannot answer them."""
    for row in _config()["fields"]:
        if row["domain_code"] == "liability_basis":
            assert row["source_route_id"] is None, row["field_id"]


def test_the_negligence_rate_records_what_is_written_not_a_computation():
    """`amount_calculation` is out of scope; this field is a records check.
    The note must say so, because a number that looks computed would be read
    as this stage taking a position on quantum."""
    row = next(r for r in _config()["fields"]
               if r["field_id"] == "comparative_negligence_rate")
    assert row["value_shape"] == "number"
    assert "계산하지 않" in row["notes"]
