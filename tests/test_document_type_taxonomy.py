"""Document-type taxonomy: the enum and the classifier's own list must not drift.

The classifier in run_checkpoint1.py has its own DOCUMENT_TYPES list, and the
schema has the document_type enum in common_component_output.schema.json. If
those two ever disagree, the classifier can emit a value the schema rejects
(write fails) OR the schema can permit a value the classifier never offers
(dead enum entry). CASE_030 surfaced the real-world cost of an incomplete
taxonomy: a 청약서류 (application form) had no type to land in and was
absorbed into insurance_policy. These tests pin the taxonomy and its single
source of truth.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import run_checkpoint1


def _document_type_enum() -> list[str]:
    schema = json.loads(
        (ROOT / "schemas" / "common_component_output.schema.json").read_text(encoding="utf-8")
    )
    return schema["$defs"]["document_type"]["enum"]


def test_application_form_is_in_the_enum():
    """The gap CASE_030 found: no application-form type existed, so a 청약서류
    could not be classified correctly."""
    assert "application_form" in _document_type_enum()


def test_insurance_certificate_still_present():
    """증권서류's correct type -- it already existed; the CASE_030 miss there
    was a classifier problem, not a taxonomy gap, so the type must stay."""
    assert "insurance_certificate" in _document_type_enum()


def test_classifier_list_matches_schema_enum_exactly():
    """The single-source-of-truth guard: run_checkpoint1.DOCUMENT_TYPES must be
    exactly the schema enum, in the same set -- neither may carry a value the
    other lacks. This is the drift that let the classifier and schema diverge."""
    assert set(run_checkpoint1.DOCUMENT_TYPES) == set(_document_type_enum())


def test_classifier_prompt_offers_application_form_and_certificate():
    """The prompt is what the model actually sees. If a type is in the list but
    the guidance never mentions how to tell 청약/증권/약관 apart, the model
    keeps collapsing them into insurance_policy (the CASE_030 failure)."""
    prompt = run_checkpoint1.CLASSIFY_PROMPT_TEMPLATE
    # The distinguishing guidance must name the three easily-confused Korean forms.
    for token in ["청약", "증권", "약관"]:
        assert token in prompt, f"classifier prompt should distinguish {token!r}"
