"""Checkpoint 2's cross-validation gate must cover every status OCR can emit.

The gate exists to stop a document whose extraction question is still OPEN
(`disagreed_pending_review`) from being built on. It is not a quality bar --
and reading it as one broke it twice, both times silently, both times only
discovered when a real run stalled:

  * `assume_reading_a_unreviewed` (--on-disagreement, added 2026-08-11) was
    never added to the allow-set, so that flag could not reach checkpoint 2.
  * `single_reader_no_cross_validation` (--single-reader) hit the same wall on
    CASE_911, where the case's only two scanned documents were unredactable
    while the three born-digital ones passed on the embedded-text path.

Neither was caught by the suite: nothing asserted a relationship between the
statuses OCR can produce and the statuses redaction accepts. This does.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import redact_document  # noqa: E402

# Every value ocr_result.schema.json permits, classified by whether checkpoint 2
# should redact a document carrying it. Spelled out rather than derived so that
# adding a status to the schema fails this test until someone decides which
# side it belongs on -- the decision is the point, and a derivation would make
# it automatically and silently.
SETTLED = {
    "agreed",                            # readers concurred
    "disagreed_resolved",                # a human picked the correct reading
    "assume_reading_a_unreviewed",       # policy took reading_a, recorded
    "single_reader_no_cross_validation",  # one read, recorded, nothing compared
}
UNSETTLED_OR_NO_TEXT = {
    "disagreed_pending_review",  # OPEN -- the case this gate exists for
    "non_text_verified",         # visual evidence, never fed to the redactor
    "not_run",                   # no extraction happened
}


def _schema_statuses() -> set[str]:
    schema = json.loads(
        (ROOT / "schemas" / "ocr_result.schema.json").read_text(encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if (key == "cross_validation_status" and isinstance(value, dict)
                        and isinstance(value.get("enum"), list)):
                    return set(value["enum"])
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(schema) or set()


def test_every_schema_status_is_classified() -> None:
    """No status may be unaccounted for on either side."""
    schema = _schema_statuses()
    assert schema, "could not read cross_validation_status enum from the schema"
    classified = SETTLED | UNSETTLED_OR_NO_TEXT
    missing = schema - classified
    assert not missing, (
        f"ocr_result.schema.json permits {sorted(missing)}, which this test does "
        "not classify. Decide whether checkpoint 2 should redact a document "
        "carrying it, add it to SETTLED or UNSETTLED_OR_NO_TEXT, and update "
        "redact_document.REDACTABLE_CROSS_VALIDATION_STATUS to match."
    )
    stale = classified - schema
    assert not stale, f"{sorted(stale)} is classified here but not in the schema"


def test_gate_admits_every_settled_status() -> None:
    missing = SETTLED - redact_document.REDACTABLE_CROSS_VALIDATION_STATUS
    assert not missing, (
        f"checkpoint 2 refuses {sorted(missing)}, whose extraction question is "
        "settled. A throughput mode that cannot be redacted cannot be used at "
        "all -- this is exactly how --on-disagreement and --single-reader were "
        "each shipped unusable."
    )


def test_gate_refuses_pending_review() -> None:
    """The one case the gate actually exists for."""
    assert ("disagreed_pending_review"
            not in redact_document.REDACTABLE_CROSS_VALIDATION_STATUS), (
        "a document with two conflicting reads and no human resolution must "
        "never be redacted -- everything downstream would treat unsettled text "
        "as established fact"
    )


def test_gate_refuses_non_text_and_not_run() -> None:
    for status in ("non_text_verified", "not_run"):
        assert status not in redact_document.REDACTABLE_CROSS_VALIDATION_STATUS, (
            f"{status!r} has no text for the redactor to process"
        )
