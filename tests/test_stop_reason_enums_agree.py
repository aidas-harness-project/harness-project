"""One writer, two schemas: `stop_reason` must mean the same thing in both.

`run_claim_analysis_selective.py` sets `outcome.stop_reason` once and copies
that same value into two contracts -- the result's `claim_facts` (line ~1153)
and the trace's `field_stops` (line ~1333). They are validated against
different schema files, so a value added to one and not the other passes every
test and every earlier case, then fails a stage on its LAST write, after the
provider work is already paid for.

That is exactly what CASE_712 hit on 2026-08-21. `explicitly_absent` was added
to `claim_analysis_result.schema.json` on 2026-08-20 and not to
`claim_analysis_trace.schema.json`. The stage ran 212s, produced a complete
57,896-byte `claim_analysis_result.json`, and then died:

    field_stops/6/stop_reason: 'explicitly_absent' is not one of
    ['trusted_value_found', 'sources_exhausted', 'conflict_found',
     'not_applicable']

The reading itself was sound -- DOC_004 p.2, `"PMHx. n/s"`, a stated negative
rather than a value. Nothing was wrong with the case; two schemas disagreed
about one word.

This is the "contract axis propagation gap" the project has now hit several
times: an axis added to one schema and not carried to the consumers that
receive the same field. A test naming only `explicitly_absent` would guard the
last instance and miss the next one, so this compares the two enums as SETS.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"


def _find(node, key, path=""):
    """Every (path, subschema) whose property name is `key`."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict) and "enum" in v:
                out.append((f"{path}/{k}", v))
            out.extend(_find(v, key, f"{path}/{k}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_find(v, key, f"{path}[{i}]"))
    return out


def _enums(filename, key):
    schema = json.loads((SCHEMAS / filename).read_text(encoding="utf-8"))
    hits = _find(schema, key)
    assert hits, f"{filename} declares no {key} enum"
    return hits


def test_stop_reason_means_the_same_in_result_and_trace():
    result = _enums("claim_analysis_result.schema.json", "stop_reason")
    trace = _enums("claim_analysis_trace.schema.json", "stop_reason")

    result_values = set(result[0][1]["enum"])
    for path, node in trace:
        assert set(node["enum"]) == result_values, (
            f"claim_analysis_trace.schema.json{path} and the result schema "
            f"disagree about stop_reason.\n"
            f"  result only: {sorted(result_values - set(node['enum']))}\n"
            f"  trace only:  {sorted(set(node['enum']) - result_values)}\n"
            "One writer copies outcome.stop_reason into both contracts, so a "
            "value missing from either fails the stage on its last write "
            "after the provider work is already paid for (CASE_712)."
        )


def test_the_value_that_broke_case_712_is_carried_by_both():
    """The specific instance, kept alongside the class guard.

    A stated absence -- a source saying the field is NOT there -- is a real
    stop reason, not a missing value, and both contracts record it.
    """
    for filename in ("claim_analysis_result.schema.json",
                     "claim_analysis_trace.schema.json"):
        for path, node in _enums(filename, "stop_reason"):
            assert "explicitly_absent" in node["enum"], (
                f"{filename}{path} lost explicitly_absent")


def test_the_driver_only_emits_values_both_schemas_accept():
    """Whatever the driver can assign must be in the shared enum.

    Catches the gap from the writer's side: a new stop reason added in code
    fails here before it can fail a run.
    """
    import re

    source = (ROOT / "tools" / "run_claim_analysis_selective.py").read_text(
        encoding="utf-8")
    emitted = set(re.findall(r'stop_reason\s*=\s*"([a-z_]+)"', source))
    assert emitted, "no stop_reason assignment found -- has the driver moved?"
    allowed = set(
        _enums("claim_analysis_result.schema.json", "stop_reason")[0][1]["enum"])
    assert emitted <= allowed, (
        f"the driver emits stop_reason values no schema accepts: "
        f"{sorted(emitted - allowed)}")


# --------------------------------------------- the same drift, one axis over --
# `value_state` travels the same way `stop_reason` does, and had no guard.
# `run_consistency_check.py` copies each observation's `value_state` verbatim
# out of `claim_analysis_result.json` into `consistency_check_workitems.json`
# (lines ~158 and ~190), so a value the first schema accepts and the second
# rejects fails stage 6 on its write -- after stage 5's provider work is paid
# for and published.
#
# Measured 2026-08-26, and it is the SEVENTH propagation surface of one change:
# `printed_but_blank` was added to the result and trace schemas, stage 5 then
# published 25 such fields across five cases, and stage 6 refused every write:
#
#     work_items/1/readings/1/value_state: 'printed_but_blank' is not one of
#     ['asserted', 'explicitly_absent', 'unavailable', 'not_applicable']
#
# The identical shape as CASE_712 above, one contract further downstream. The
# lesson `contract-axis-propagation-gap` records is that adding a value
# upstream does not carry it anywhere; this test is what makes the next such
# addition fail here, cheaply, instead of mid-run.

def test_value_state_enum_agrees_between_result_and_workitems() -> None:
    # The observation vocabulary is the widest `value_state` enum in the
    # result schema; the per-state conditionals below it are `const`, not
    # `enum`, so they are not collected here.
    result = set().union(*(set(node["enum"]) for _, node in _enums(
        "claim_analysis_result.schema.json", "value_state")))
    workitems = set().union(*(set(node["enum"]) for _, node in _enums(
        "consistency_check_workitems.schema.json", "value_state")))
    missing = sorted(result - workitems)
    assert not missing, (
        "stage 6 copies value_state verbatim from the result contract, so a "
        f"value it cannot carry fails the write: {missing}")
