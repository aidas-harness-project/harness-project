"""screening_report.schema.json's review_point v0.3 fields.

`review_points` is P3's aggregation point for Phase 1. P3 says an
inference-bearing claim is hedged and flagged, explicitly "does not halt the
current stage", and that flagged claims "surface together at the
aggregation/report stage for batch human review" -- screening-report is that
stage. Nothing upstream blocks on a flag, so a question that does not reach
this array reaches nobody.

The array was populated on all six cases that reached the stage, and routed
sensibly. What it lacked was structure, in three ways that each cost something
concrete:

* No id. Critic findings have carried `finding_id` since the beginning and
  `expert_review.json` disposes of each by `finding_ref` -- so a human's answer
  to a critic binds to something, while an answer to a review point had nowhere
  to attach and nothing could later say which points were addressed.
* No source link. On CASE_909, 5 of 10 points named no document at all in their
  prose, and not one named its source in a machine-resolvable form, so nothing
  could check whether a flagged upstream contract had been surfaced.
* No way to say "answering this changes nothing". A value erased by redaction
  is not recoverable, and asking an expert to confirm it is missing spends
  attention for no return.

All three fields are optional: the point of the change is to make the
information expressible and to guide the agent, not to invalidate six real
reports written before it existed.

The real corpus is the fixture base on purpose -- these constraints are only
worth having if the reports the pipeline actually produced satisfy them.
"""
import copy
import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
SCHEMA_NAME = "screening_report.schema.json"

CASE_909 = OUTPUTS / "CASE_909" / "screening_report.json"

# CASE_021/CASE_024 predate insurer_position's has_denial/has_reduction split
# (2026-07-22) and already fail on those required fields alone, independent of
# anything here. Named rather than silently skipped, so a real regression in
# them cannot hide behind a blanket exclusion.
PREEXISTING_INVALID = {
    OUTPUTS / "CASE_021" / "screening_report.json",
    OUTPUTS / "CASE_024" / "screening_report.json",
}


@pytest.fixture(scope="module")
def validate():
    schemas, registry = load_registry()

    def _validate(instance):
        return validate_instance(instance, SCHEMA_NAME, schemas, registry)

    return _validate


@pytest.fixture(scope="module")
def report():
    return json.loads(CASE_909.read_text(encoding="utf-8"))


def _first_point(report):
    return report["review_points"][0]


# ------------------------------------------------- the corpus still passes --

def test_every_shipped_report_still_validates(validate):
    """The v0.3 fields are additive. A report written before they existed must
    validate untouched -- otherwise the change quietly invalidates real work."""
    checked = 0
    for path in sorted(OUTPUTS.glob("CASE_*/screening_report.json")):
        if path in PREEXISTING_INVALID:
            continue
        assert validate(json.loads(path.read_text(encoding="utf-8"))) == [], path
        checked += 1
    assert checked >= 4, "corpus sweep found too few reports to be meaningful"


def test_a_point_with_none_of_the_new_fields_is_valid(validate, report):
    """Exactly the shape all six shipped reports use."""
    data = copy.deepcopy(report)
    data["review_points"] = [{
        "point": "후유장해 평가의 의학적 적정성 검토",
        "reviewer_role": "의사",
        "priority": "high",
    }]
    assert validate(data) == []


# ------------------------------------------------------------- point_id -----

def test_point_id_accepts_the_rp_form(validate, report):
    data = copy.deepcopy(report)
    _first_point(data)["point_id"] = "RP-1"
    assert validate(data) == []


@pytest.mark.parametrize("bad", ["CF-1", "RP_1", "1", "RP-", "rp-1", ""])
def test_point_id_rejects_anything_but_rp_n(validate, report, bad):
    """CF-N is the critic's namespace. A review point borrowing it would make
    an expert_review finding_ref ambiguous about which artifact it disposes
    of -- which is the entire reason the id exists."""
    data = copy.deepcopy(report)
    _first_point(data)["point_id"] = bad
    assert validate(data) != []


# ----------------------------------------------------------- source_refs ----

def test_source_refs_records_a_contract_and_element(validate, report):
    data = copy.deepcopy(report)
    _first_point(data)["source_refs"] = [
        {"contract": "requirement_matching_result.json", "element_id": "REQ-3"},
        {"contract": "denial_reason_result.json", "element_id": "DR_1"},
    ]
    assert validate(data) == []


def test_a_contract_level_flag_needs_no_element(validate, report):
    """Not every flag points at an element inside its contract --
    extracted_claim_fields' status: partial is about the whole contract."""
    data = copy.deepcopy(report)
    _first_point(data)["source_refs"] = [
        {"contract": "extracted_claim_fields.json", "element_id": None},
        {"contract": "coverage_result.json"},
    ]
    assert validate(data) == []


def test_a_source_ref_must_name_its_contract(validate, report):
    """An element_id alone cannot be resolved -- REQ-3 means nothing without
    the contract that defines it."""
    data = copy.deepcopy(report)
    _first_point(data)["source_refs"] = [{"element_id": "REQ-3"}]
    assert validate(data) != []


def test_empty_source_refs_is_legitimate(validate, report):
    """A point raised from the agent's own reading rather than an upstream
    flag. Forcing a source here would push the agent to invent one."""
    data = copy.deepcopy(report)
    _first_point(data)["source_refs"] = []
    assert validate(data) == []


# ------------------------------------------------------------ answerable ----

def test_answerable_false_is_expressible(validate, report):
    """The field exists so a batch review does not fill with items no expert
    can act on -- a date erased by redaction is gone, not pending."""
    data = copy.deepcopy(report)
    _first_point(data)["answerable"] = False
    assert validate(data) == []


def test_answerable_must_be_boolean(validate, report):
    data = copy.deepcopy(report)
    _first_point(data)["answerable"] = "no"
    assert validate(data) != []


# ------------------------------------------- the guidance stays in the file --

def test_reviewer_role_description_keeps_the_per_question_rule():
    """The routing rule is the one finding worth carrying, and it lives only
    in prose: a contract's own reviewer_role is a coarse default (all 14 of
    CASE_909's contract-level flags read 손해사정사) while the questions behind
    them split across all three roles. If this description is ever dropped,
    the next agent copies the contract value and medical questions land on a
    손해사정사."""
    schema = json.loads(
        (ROOT / "schemas" / SCHEMA_NAME).read_text(encoding="utf-8"))
    desc = schema["$defs"]["review_point"]["properties"]["reviewer_role"]["description"]
    assert "per question" in desc
    assert "의사" in desc


def test_answerable_description_names_the_preferred_alternative():
    """Marking a point unanswerable must not read as permission to drop it --
    the consequence of the missing value is usually itself answerable."""
    desc = json.loads(
        (ROOT / "schemas" / SCHEMA_NAME).read_text(encoding="utf-8")
    )["$defs"]["review_point"]["properties"]["answerable"]["description"]
    assert "DOWNSTREAM CONSEQUENCE" in desc.upper()


def test_the_agent_spec_tells_the_agent_to_sweep():
    """A schema field with no producer instruction stays empty forever -- the
    exact shape that left review_required flags unread for months."""
    spec = (ROOT / ".claude" / "agents" / "screening-report.md").read_text(
        encoding="utf-8")
    assert "point_id" in spec
    assert "source_refs" in spec
    assert "answerable" in spec
    # The load-bearing instruction: aggregate, do not transcribe one per flag.
    assert "Do not emit one point per flag" in spec
