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
        _config(), _assessments(), {LIABILITY_FIELD: _Outcome("unavailable")})
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
        _config(), _assessments(), {LIABILITY_FIELD: _Outcome(status)})
    assert eligible == {}


def test_a_field_absent_from_the_run_is_skipped_not_invented():
    assert additional.eligible_field_ids(_config(), _assessments(), {}) == {}


# --- the read plan ---------------------------------------------------------

def test_the_legal_opinions_are_read_and_the_medical_record_is_not():
    plan = additional.read_plan(
        _config(), _assessments(),
        {LIABILITY_FIELD: _Outcome("unavailable")}, CASE_053_DOCS)
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
        {LIABILITY_FIELD: _Outcome("unavailable")}, CASE_053_DOCS)
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
        {LIABILITY_FIELD: _Outcome("unavailable"),
         "industrial_accident_filing_basis": _Outcome("unavailable")},
        CASE_053_DOCS)
    by_id = {entry["document_id"]: entry for entry in plan}
    assert by_id["DOC_006"]["field_ranks"][LIABILITY_FIELD] == 1
    assert by_id["DOC_007"]["field_ranks"][LIABILITY_FIELD] == 2
    assert by_id["DOC_007"]["field_ranks"]["industrial_accident_filing_basis"] == 1


def test_fields_wanting_the_same_document_share_one_read():
    """Batched like the common pass: one call serves every asking field."""
    plan = additional.read_plan(
        _config(), _assessments(),
        {LIABILITY_FIELD: _Outcome("unavailable"),
         "accident_date": _Outcome("unavailable")}, CASE_053_DOCS)
    reads = [e for e in plan if e["document_id"] == "DOC_006"]
    assert len(reads) == 1, "two fields wanting one document must not cost two reads"
    assert reads[0]["field_ids"] == sorted([LIABILITY_FIELD, "accident_date"])


def test_a_case_without_the_source_documents_plans_nothing():
    """No legal opinion, no insurer letter -- the round costs zero."""
    plan = additional.read_plan(
        _config(), _assessments(),
        {LIABILITY_FIELD: _Outcome("unavailable")},
        _docs(DOC_013="medical_record"))
    assert plan == []


def test_a_closed_type_plans_nothing_even_with_the_documents_present():
    plan = additional.read_plan(
        _config(), _assessments(liability="not_applicable",
                                industrial_accident="not_applicable"),
        {LIABILITY_FIELD: _Outcome("unavailable")}, CASE_053_DOCS)
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
        config, _assessments(), {LIABILITY_FIELD: _Outcome("unavailable")})
    assert eligible[LIABILITY_FIELD] == ["liability", "traffic_accident"]
    plan = additional.read_plan(
        config, _assessments(),
        {LIABILITY_FIELD: _Outcome("unavailable")}, CASE_053_DOCS)
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
