"""The two cross_validation_status enums must not drift apart.

`ocr_result.schema.json` and `document_manifest.schema.json` both carry a
`cross_validation_status`, and checkpoint 1 writes the SAME value to both: the
contract first, then the manifest patch. So a value added to one and not the
other is not a documentation inconsistency -- it is a run that dies at the
manifest write with OCR already paid for.

That has now happened twice for real:

  * `disagreed_resolved` (CASE_012) -- recorded in the manifest field's own
    description at the time.
  * `assume_reading_a_unreviewed` (CASE_911/DOC_005, 2026-08-11) -- 19 pages
    of a scan OCR'd, 8 disagreements auto-resolved, then rejected at the
    manifest write because only ocr_result had learned the new value.

Both were found by a real run failing, which is the expensive way. This test is
the cheap way. It is deliberately a parity check rather than a list of expected
values: an assertion listing today's values would need editing on every legitimate
addition, and a test that must be edited to add a value is a test that gets
edited rather than heeded.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _find_enum(node, key: str) -> set[str] | None:
    """Return the enum for the first `key` property carrying one.

    Both schemas nest the field differently (ocr_result puts it directly under
    an allOf branch's properties; document_manifest puts it inside the per-
    document item schema), and the manifest's is wrapped in an anyOf with a
    null branch. Walking for it keeps this test from breaking on a structural
    move that does not change the value set -- which is the only thing it is
    asserting about.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict):
                if isinstance(v.get("enum"), list):
                    return set(v["enum"])
                for branch in v.get("anyOf", []):
                    if isinstance(branch, dict) and isinstance(branch.get("enum"), list):
                        return set(branch["enum"])
            found = _find_enum(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_enum(item, key)
            if found is not None:
                return found
    return None


def test_cross_validation_status_enums_match() -> None:
    ocr = _find_enum(_load("ocr_result.schema.json"), "cross_validation_status")
    manifest = _find_enum(_load("document_manifest.schema.json"), "cross_validation_status")

    assert ocr, "ocr_result.schema.json has no cross_validation_status enum"
    assert manifest, "document_manifest.schema.json has no cross_validation_status enum"

    only_ocr = ocr - manifest
    only_manifest = manifest - ocr
    assert not only_ocr, (
        f"cross_validation_status values {sorted(only_ocr)} exist in "
        "ocr_result.schema.json but NOT in document_manifest.schema.json. "
        "Checkpoint 1 writes this value to both, so a document the OCR contract "
        "can legally describe would be rejected at the manifest patch -- after "
        "OCR has already been paid for. This exact desync has shipped twice "
        "(disagreed_resolved, assume_reading_a_unreviewed). Add it to the "
        "manifest schema."
    )
    assert not only_manifest, (
        f"cross_validation_status values {sorted(only_manifest)} exist in "
        "document_manifest.schema.json but NOT in ocr_result.schema.json. The "
        "manifest value is copied FROM the OCR contract, so a value only the "
        "manifest knows can never be written by checkpoint 1 -- it is either "
        "dead or a typo."
    )


@pytest.mark.parametrize(
    "value",
    ["agreed", "disagreed_pending_review", "disagreed_resolved",
     "assume_reading_a_unreviewed", "single_reader_no_cross_validation",
     "non_text_verified", "not_run"],
)
def test_known_status_present_in_both(value: str) -> None:
    """Every status the tools can currently emit is in both enums.

    The parity test above passes if both schemas are equally wrong (both
    missing a value the code writes). This pins the values checkpoint 1
    actually produces today, so deleting one from both schemas still fails.
    """
    assert value in _find_enum(_load("ocr_result.schema.json"), "cross_validation_status")
    assert value in _find_enum(_load("document_manifest.schema.json"), "cross_validation_status")
