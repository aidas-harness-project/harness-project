"""Adding a value to a shared vocabulary must reach every contract that reads it.

This is the defect this project has hit more than any other, and it has the
same shape every time: a new value is added to `claim_analysis_result`, the
stage that writes it starts emitting it, and a DOWNSTREAM schema that never
learned the value rejects the write. `printed_but_blank` did it seven times
between 2026-08-25 and 2026-08-26 -- result schema, trace schema, the
consistency-check schema, the extraction prompt, the publish path, the
screening renderer -- each found by a case failing in production rather than by
a test, because nothing anywhere states which files share the vocabulary.

The rule this file encodes:

    `claim_analysis_result.schema.json` is the PRODUCER OF RECORD for
    `unavailable_reason`, `stop_reason` and `value_state`. Any other schema
    declaring one of those names declares the SAME set, or explicitly registers
    itself as a narrower consumer with a reason.

A narrower consumer is legitimate -- a contract can genuinely handle fewer
states than the producer emits -- but it must be written down here, so a value
that is missing by DESIGN is distinguishable from one that is missing because
someone forgot. That distinction is the whole point: the seven misses were all
invisible precisely because absence looked the same either way.

Deliberately NOT covered: `medical_variables.schema.json`, whose `value_state`
is a different vocabulary (`unknown`/`absent`/`not_applicable`) belonging to
the canonical-medical subsystem. It shares the field NAME and nothing else.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
PRODUCER = "claim_analysis_result.schema.json"

# Axes the producer defines and downstream contracts consume.
SHARED_AXES = ("unavailable_reason", "stop_reason", "value_state")

# Schemas that share the field NAME but not the vocabulary. Excluded by name so
# adding a new one is a deliberate edit rather than a silent gap.
FOREIGN_VOCABULARIES = {
    "medical_variables.schema.json": (
        "canonical-medical subsystem; value_state is "
        "unknown/absent/not_applicable, unrelated to the claim-analysis axis"),
}

# Consumers that legitimately handle a SUBSET, with the reason. A subset entry
# is a claim that the missing values cannot reach this contract -- if one later
# can, the entry is what has to change, and changing it is visible in review.
NARROWER_CONSUMERS: dict[tuple[str, str], set[str]] = {}


def _load(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _enums(node, axis: str, path: str = "") -> list[tuple[str, tuple[str, ...]]]:
    """Every `enum` declared under a property named `axis`, with its location."""
    found: list[tuple[str, tuple[str, ...]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == axis and isinstance(value, dict) and "enum" in value:
                found.append((f"{path}/{key}", tuple(value["enum"])))
            found.extend(_enums(value, axis, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_enums(value, axis, f"{path}[{index}]"))
    return found


def _consumers(axis: str) -> list[tuple[str, str, tuple[str, ...]]]:
    rows = []
    for path in sorted(SCHEMAS.glob("*.json")):
        if path.name == PRODUCER or path.name in FOREIGN_VOCABULARIES:
            continue
        for where, enum in _enums(_load(path.name), axis):
            rows.append((path.name, where, enum))
    return rows


@pytest.mark.parametrize("axis", SHARED_AXES)
def test_the_producer_declares_the_axis(axis: str) -> None:
    """If this fails the axis was renamed or moved, and every check below is
    silently vacuous -- which is worse than a missing value."""
    assert _enums(_load(PRODUCER), axis), (
        f"{PRODUCER} declares no {axis} enum; this file's premise is broken")


@pytest.mark.parametrize("axis", SHARED_AXES)
def test_the_producer_is_internally_consistent(axis: str) -> None:
    """`unavailable_reason` is declared on BOTH observation and field_result.
    They must not drift apart from each other either."""
    declarations = _enums(_load(PRODUCER), axis)
    sets = {frozenset(enum) for _, enum in declarations}
    assert len(sets) == 1, (
        f"{PRODUCER} declares {axis} {len(sets)} different ways:\n" +
        "\n".join(f"  {where}: {sorted(enum)}" for where, enum in declarations))


@pytest.mark.parametrize("axis", SHARED_AXES)
def test_every_consumer_carries_the_producers_values(axis: str) -> None:
    """The check the seven `printed_but_blank` misses would each have failed."""
    produced = set(_enums(_load(PRODUCER), axis)[0][1])
    for name, where, enum in _consumers(axis):
        allowed = NARROWER_CONSUMERS.get((name, axis))
        expected = allowed if allowed is not None else produced
        missing = expected - set(enum)
        assert not missing, (
            f"{name}{where} is missing {sorted(missing)} from `{axis}`.\n"
            f"  {PRODUCER} produces: {sorted(produced)}\n"
            f"  {name} accepts:       {sorted(enum)}\n"
            f"A stage writing one of the missing values will be REJECTED at "
            f"this contract. Either add the value here, or -- if it genuinely "
            f"cannot reach this contract -- register the subset in "
            f"NARROWER_CONSUMERS with the reason.")


@pytest.mark.parametrize("axis", SHARED_AXES)
def test_no_consumer_invents_a_value_the_producer_never_emits(axis: str) -> None:
    """The mirror failure, and the reason this is not just a subset check.

    A consumer accepting a value nothing writes is dead vocabulary: it passes
    validation forever while describing a state that cannot occur, and the next
    reader takes it for a supported case.
    """
    produced = set(_enums(_load(PRODUCER), axis)[0][1])
    for name, where, enum in _consumers(axis):
        extra = set(enum) - produced
        assert not extra, (
            f"{name}{where} accepts {sorted(extra)} for `{axis}`, which "
            f"{PRODUCER} never emits.")


def test_the_registry_has_no_stale_entries() -> None:
    """A NARROWER_CONSUMERS entry for a schema that no longer declares the axis
    is a note about a constraint that stopped existing. It reads as active
    documentation, so it has to go when the thing it describes does."""
    for (name, axis) in NARROWER_CONSUMERS:
        assert (SCHEMAS / name).exists(), f"{name} in NARROWER_CONSUMERS is gone"
        assert _enums(_load(name), axis), (
            f"{name} no longer declares `{axis}`; drop its NARROWER_CONSUMERS "
            f"entry rather than leaving a note about a constraint that ended")


def test_foreign_vocabularies_are_still_foreign() -> None:
    """An exclusion is only safe while the vocabularies really are unrelated.
    If a foreign schema's values become a subset of the producer's, the two
    have converged and the exclusion is now hiding a real consumer."""
    produced = {axis: set(_enums(_load(PRODUCER), axis)[0][1])
                for axis in SHARED_AXES}
    for name, reason in FOREIGN_VOCABULARIES.items():
        overlaps = []
        for axis in SHARED_AXES:
            for where, enum in _enums(_load(name), axis):
                if enum and set(enum) <= produced[axis]:
                    overlaps.append(f"{where}: {sorted(enum)}")
        assert not overlaps, (
            f"{name} was excluded as a foreign vocabulary ({reason}), but its "
            f"values are now a subset of the producer's:\n  " +
            "\n  ".join(overlaps) + "\nRe-check whether it is a real consumer.")


# --------------------------------------------- the surfaces that are CODE --
# Schemas are only part of it. `printed_but_blank` also had to reach the
# extraction prompt's `presence` enum (which binds what the model may return)
# and the screening renderer's `UNAVAILABLE_KIND` map (which decides how a
# reason is DISPLAYED). Both are plain Python, so no schema check sees them,
# and both fail quietly rather than loudly: an unknown presence is dropped by
# the local gate, and an unmapped reason falls through the renderer without a
# kind. Two of the seven misses were exactly these.


def _tool_constant(module_name: str, attribute: str):
    import sys

    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    module = __import__(module_name)
    return getattr(module, attribute)


def test_the_screening_renderer_maps_every_unavailable_reason() -> None:
    """`UNAVAILABLE_KIND` is a TOTAL map, not a lookup with a default.

    A reason missing from it renders with no kind at all, so a reviewer is
    shown a field with no indication of why it is empty -- the same silence the
    reason exists to break.
    """
    produced = set(_enums(_load(PRODUCER), "unavailable_reason")[0][1])
    mapping = _tool_constant("run_screening_report", "UNAVAILABLE_KIND")
    missing = produced - set(mapping)
    assert not missing, (
        f"run_screening_report.UNAVAILABLE_KIND does not map {sorted(missing)}. "
        f"A field published with that reason renders without a kind.")
    extra = set(mapping) - produced
    assert not extra, (
        f"UNAVAILABLE_KIND maps {sorted(extra)}, which {PRODUCER} never emits.")


def test_every_mapped_kind_has_a_korean_label() -> None:
    """The kind is an internal token; the label is what a reviewer reads. A
    kind with no label renders the token itself."""
    mapping = _tool_constant("run_screening_report", "UNAVAILABLE_KIND")
    labels = _tool_constant("run_screening_report", "UNAVAILABLE_KIND_LABEL")
    missing = set(mapping.values()) - set(labels)
    assert not missing, (
        f"UNAVAILABLE_KIND_LABEL has no label for {sorted(missing)}; the raw "
        f"token would be printed to a reviewer.")


def test_the_extraction_prompt_offers_every_presence_the_publisher_accepts()        -> None:
    """The `presence` enum binds what the MODEL may return, and the publish
    path translates presence into `value_state`. A presence the publisher knows
    how to handle but the prompt never offers is a state that can never occur,
    however carefully everything downstream handles it -- which is how a
    correctly-plumbed axis still produces nothing.

    Only the presences that map to a value_state are required: `not_mentioned`
    carries no observation at all, so it is a prompt-side verdict rather than a
    published state, and the two sets differ by exactly that.
    """
    build = _tool_constant("claim_analysis_extraction", "output_schema")
    schema = build([{"field_id": "probe", "value_shape": "text"}])
    offered = set(schema["properties"]["fields"]
                  ["additionalProperties"]["properties"]["presence"]["enum"])
    published = set(_enums(_load(PRODUCER), "value_state")[0][1])
    # `unavailable`/`not_applicable` are publisher-side states no single
    # document reading produces; `not_mentioned` is the reverse.
    shared = published & (offered | {"asserted", "explicitly_absent",
                                     "printed_but_blank"})
    missing = shared - offered
    assert not missing, (
        f"claim_analysis_extraction's presence enum does not offer "
        f"{sorted(missing)}, so the model can never report it and the "
        f"downstream handling for it is unreachable.")
