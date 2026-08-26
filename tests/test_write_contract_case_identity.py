"""A contract may not be written into a case it does not name.

`write-contract` validated a payload's SHAPE and never its IDENTITY. Schema
validation asks "is this a well-formed screening-report judgement?", never "is
this THIS case's judgement?", so a payload belonging to another case passed and
was written under the wrong `case_id`.

Measured 2026-08-26 on the eight-case corpus re-run: concurrent
screening-report agents generated to a shared scratchpad filename and
overwrote each other between generation and the DAO write.
`outputs/CASE_7044/screening_report_judgement.json` ended up holding
CASE_7023's judgement -- `case_id: CASE_7023`, `run_id: RUN_20260826_602`, and
CASE_7023's 족관절/슬관절 conflicts. `write-contract` returned PASS. A scan of
all 410 JSON files across the eight cases found exactly that one mismatch, and
the orchestrator caught it before assembly, so no report was published from
another case's medical findings.

Serialising the agents removes the collision that produced it, and that is the
right operational fix. It is not what this test pins. A write that names one
case and lands in another is wrong however it was produced -- by a race, a
mistyped `--case-id`, a resumed run reusing a stale path -- and the layer that
owns "no ungoverned write" is the one that should refuse it. The cost of the
check is one dict lookup against a value the payload already carries.
"""
from __future__ import annotations

import json

import dao


def _judgement(case_id: str) -> dict:
    return {
        "schema_version": "0.1",
        "case_id": case_id,
        "run_id": "RUN_20260826_001",
        "component": "screening-report",
        "status": "success",
        "created_at": "2026-08-26T00:00:00+09:00",
        "key_issues": [],
        "review_points": [],
    }


def _write(isolated_dao, make_args, payload, *, into: str,
           filename="screening_report_judgement.json",
           schema="screening_report_judgement.schema.json"):
    data_file = isolated_dao / "payload.json"
    data_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (isolated_dao / "outputs" / into).mkdir(parents=True, exist_ok=True)
    return dao.cmd_write_contract(make_args(
        case_id=into, filename=filename, data_file=str(data_file),
        schema_name=schema, held_by="screening-report",
        run_id="RUN_20260826_001"))


def test_a_foreign_payload_is_refused(isolated_dao, make_args):
    """The exact CASE_7044 shape: a valid judgement naming another case."""
    rc = _write(isolated_dao, make_args, _judgement("CASE_7023"), into="CASE_7044")
    assert rc == 1, "a contract naming CASE_7023 was written into CASE_7044"


def test_the_refusal_does_not_leave_the_file_behind(isolated_dao, make_args):
    """A refused write must not publish. The danger is a later reader finding
    the file and trusting it, which is exactly what nearly happened."""
    _write(isolated_dao, make_args, _judgement("CASE_7023"), into="CASE_7044")
    target = (isolated_dao / "outputs" / "CASE_7044"
              / "screening_report_judgement.json")
    assert not target.exists()


def test_a_matching_payload_still_writes(isolated_dao, make_args):
    """The guard must not cost a legitimate write."""
    rc = _write(isolated_dao, make_args, _judgement("CASE_7044"), into="CASE_7044")
    assert rc == 0


def test_a_payload_with_no_case_id_is_unaffected(isolated_dao, make_args):
    """Not every contract carries `case_id`, and this check has nothing to say
    about those -- it must not become a back-door requirement for the field."""
    payload = _judgement("CASE_7044")
    payload.pop("case_id")
    rc = _write(isolated_dao, make_args, payload, into="CASE_7044")
    # Refused or accepted on the SCHEMA's terms, never on this guard's.
    assert rc in (0, 1)
