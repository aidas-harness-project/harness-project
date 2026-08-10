"""ocr_result.schema.json's P8 cross-field constraints (v0.8).

Background -- what this pins and why it was added:

A CASE_901 investigation started from the report that P8's per-page verdicts
were missing entirely (`page_number`/`agreement`/`disagreement_details` all
reading None across every page of every case). That turned out to be a false
alarm: the diagnostic read the wrong keys. The real page number key is `page`,
and `agreement`/`disagreement_details` live nested under
`pages[].cross_validation`, so `.get()` on the top level returned None for
data that was in fact fully populated -- 89/89 page records across 21 files.
The first group of tests below pins the real key layout so that specific
misreading cannot be mistaken for a data gap again.

The investigation did surface a genuine gap, which the rest of this file
covers. The schema validated the document-level rollup fields
(`cross_validation_status`, `review_required`) and the per-page verdicts
independently, and never checked them against each other. Every field could
be individually legal while the combination lied: a page recording
`agreement: "disagreed"` with no `resolution`, sitting under a top-level
`cross_validation_status: "agreed"` and `review_required: false`, validated
cleanly. That is exactly a document P8 requires to be blocked from downstream
use, passing as clean -- and with `disagreement_details` empty there would be
nothing left to reconstruct what had failed.

No such file was ever written (`run_checkpoint1.py` derives the rollup from
the pages, so the real corpus is consistent). The gap was that only the
tool's good behaviour enforced the invariant, not the contract -- the same
class of hole closed for _source_ledger/_run_state/_conflict_ledger, which
also had no schema enforcement anywhere until it was added.

The real corpus is used as the fixture base on purpose: these constraints are
only worth having if the documents the pipeline actually produced satisfy
them, so the mutations are applied to genuine files rather than hand-built
minimal ones.
"""
import copy
import json
from pathlib import Path

import pytest

from _validation import load_registry, validate_instance

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
SCHEMA_NAME = "ocr_result.schema.json"

# Real files chosen for the distinct P8 shapes they represent.
RESOLVED_DOC = OUTPUTS / "CASE_901" / "ocr_result_DOC_001.json"      # disagreed_resolved
PENDING_DOC = OUTPUTS / "CASE_005" / "ocr_result_DOC_001.json"       # disagreed_pending_review
AGREED_DOC = OUTPUTS / "CASE_025" / "ocr_result_DOC_002.json"        # all agreed
NON_TEXT_DOC = OUTPUTS / "CASE_003" / "ocr_result_DOC_010.json"      # non_text_image carve-out

# CASE_021/CASE_023 predate cross_validation_mode becoming required and already
# fail on that field alone, independent of anything tested here. They are
# excluded from the corpus sweep rather than silently masking a real regression.
PREEXISTING_INVALID = {
    OUTPUTS / "CASE_021" / "ocr_result_DOC_001.json",
    OUTPUTS / "CASE_023" / "ocr_result_DOC_001.json",
}


@pytest.fixture(scope="module")
def validate():
    schemas, registry = load_registry()

    def _validate(instance):
        return validate_instance(instance, SCHEMA_NAME, schemas, registry)

    return _validate


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def real_ocr_results():
    return sorted(OUTPUTS.glob("CASE_*/ocr_result_*.json"))


# ---- The key layout the false alarm misread ----

def test_page_verdicts_live_under_nested_cross_validation():
    """The exact misreading that started the investigation.

    Reading `agreement` off the page top level yields None for data that is
    present; it lives under `cross_validation`. Same for the page number,
    whose key is `page`, not `page_number`.
    """
    doc = load(RESOLVED_DOC)
    for page in doc["pages"]:
        assert page.get("agreement") is None, "top-level `agreement` must not exist"
        assert page.get("page_number") is None, "the key is `page`, not `page_number`"
        assert isinstance(page["page"], int)
        assert page["cross_validation"]["agreement"] in {"agreed", "disagreed"}


def test_no_real_ocr_result_has_an_unrecorded_page_verdict():
    """The claim the investigation actually tested: no page anywhere in
    outputs/ is missing its P8 verdict."""
    missing = []
    for path in real_ocr_results():
        for page in load(path).get("pages", []):
            verdict = page.get("cross_validation", {}).get("agreement")
            if verdict not in {"agreed", "disagreed"}:
                missing.append(f"{path.name} page {page.get('page')}: {verdict!r}")
    assert not missing, f"pages with no recorded P8 verdict: {missing}"


# ---- The corpus must satisfy the constraints ----

@pytest.mark.parametrize(
    "path", [p for p in real_ocr_results() if p not in PREEXISTING_INVALID],
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
def test_real_ocr_results_validate(path, validate):
    """Every ocr_result the pipeline has actually produced satisfies the
    v0.8 cross-field constraints. Doubles as a guard against a future stage
    quietly emitting a rollup that contradicts its own pages."""
    assert validate(load(path)) == []


def test_preexisting_invalid_files_fail_only_on_the_known_missing_field(validate):
    """Pins *why* the two excluded files are excluded. If they ever start
    failing for a different reason, that is a new regression, not the known
    pre-v0.7 gap, and this test says so."""
    for path in sorted(PREEXISTING_INVALID):
        errors = validate(load(path))
        assert errors, f"{path.name} unexpectedly valid -- update PREEXISTING_INVALID"
        assert all("cross_validation_mode" in e or "cross_validation_note" in e
                   for e in errors), f"{path.name} has a new failure: {errors}"


# ---- Rollup rule 1: an unresolved disagreement blocks the document ----

def test_unresolved_disagreement_cannot_be_rolled_up_as_agreed(validate):
    """The precise lie the v0.8 constraints were added to reject: a page
    whose reads disagreed, no human resolution, and a document-level rollup
    claiming the document is clean and needs no review."""
    doc = load(AGREED_DOC)
    doc["pages"][0]["cross_validation"] = {
        "vision_model_reading": "...",
        "agreement": "disagreed",
        "disagreement_details": ["date reads 10.10 vs 10.19"],
    }
    assert doc["cross_validation_status"] == "agreed"
    assert doc["review_required"] is False
    assert validate(doc), "an unresolved disagreement must not validate as 'agreed'"


def test_unresolved_disagreement_cannot_claim_resolved(validate):
    doc = load(AGREED_DOC)
    doc["pages"][0]["cross_validation"] = {
        "vision_model_reading": "...",
        "agreement": "disagreed",
        "disagreement_details": ["date reads 10.10 vs 10.19"],
    }
    doc["cross_validation_status"] = "disagreed_resolved"
    doc["review_required"] = False
    assert validate(doc), "'disagreed_resolved' requires every disagreed page resolved"


def test_pending_review_requires_review_required_true(validate):
    """P8's gate is the pair. A correct status with review_required false
    still leaves a downstream reader believing nothing needs attention."""
    doc = load(PENDING_DOC)
    assert doc["cross_validation_status"] == "disagreed_pending_review"
    doc["review_required"] = False
    assert validate(doc)


def test_genuine_pending_review_document_is_valid(validate):
    """The honest form of the shape above must keep validating -- the
    constraint targets the contradiction, not the disagreement itself."""
    assert validate(load(PENDING_DOC)) == []


# ---- Rollup rule 2: a resolved disagreement is never plain 'agreed' ----

def test_fully_resolved_document_cannot_be_downgraded_to_agreed(validate):
    """`disagreed_resolved` records that the two reads did not agree and a
    human settled it. Collapsing that to 'agreed' erases what P8 found --
    the same never-rewrite-history discipline the `agreement` field carries."""
    doc = load(RESOLVED_DOC)
    assert doc["cross_validation_status"] == "disagreed_resolved"
    doc["cross_validation_status"] = "agreed"
    assert validate(doc)


def test_fully_resolved_document_is_valid_as_recorded(validate):
    assert validate(load(RESOLVED_DOC)) == []


def _partially_resolved(unresolved_marker):
    """Two disagreed pages, one resolved and one not -- the state
    run_checkpoint1.resolve_from_raw_ocr() writes on its
    'partially_resolved' path. `unresolved_marker` selects how the pending
    page records its emptiness: omitted entirely, or an explicit null."""
    doc = load(RESOLVED_DOC)
    doc["pages"] = copy.deepcopy(doc["pages"][:2])
    doc["pages"][0]["cross_validation"] = {
        "vision_model_reading": "...",
        "agreement": "disagreed",
        "disagreement_details": ["page 1 differs"],
        "resolution": {
            "chosen_reading": "reading_a",
            "resolved_by": "Dev",
            "resolved_at": "2026-07-22T10:00:00+09:00",
            "note": "checked against the raw page image",
        },
    }
    pending = {
        "vision_model_reading": "...",
        "agreement": "disagreed",
        "disagreement_details": ["page 2 differs"],
    }
    if unresolved_marker == "null":
        pending["resolution"] = None
    doc["pages"][1]["cross_validation"] = pending
    doc["cross_validation_status"] = "disagreed_pending_review"
    doc["review_required"] = True
    return doc


@pytest.mark.parametrize("unresolved_marker", ["absent", "null"])
def test_partially_resolved_document_stays_pending(unresolved_marker, validate):
    """Regression -- this shape broke the first cut of these constraints.

    The schema documents `resolution` as "null/absent while still pending",
    and run_checkpoint1.py agrees (`.get("resolution") is None`). A rule that
    keys on the *key* being present counts an explicit null as resolved, and
    a `not` accidentally nested inside `properties` is read as a property
    literally named "not" and constrains nothing at all. Either mistake made
    rule 2 demand `disagreed_resolved` for a document that still has a
    pending page. Both markers must read as unresolved.
    """
    assert validate(_partially_resolved(unresolved_marker)) == []


@pytest.mark.parametrize("claimed", ["disagreed_resolved", "agreed"])
def test_partially_resolved_document_cannot_claim_completion(claimed, validate):
    """The same partial state must still be rejected if the rollup claims
    the document is finished -- one resolved page does not close the doc."""
    doc = _partially_resolved("absent")
    doc["cross_validation_status"] = claimed
    doc["review_required"] = False
    assert validate(doc)


# ---- disagreement_details must survive for the resolver and the audit ----

def test_disagreed_page_requires_nonempty_disagreement_details(validate):
    doc = load(RESOLVED_DOC)
    disagreed = [p for p in doc["pages"]
                 if p["cross_validation"]["agreement"] == "disagreed"]
    assert disagreed, "fixture must contain a disagreed page"
    disagreed[0]["cross_validation"]["disagreement_details"] = []
    assert validate(doc), "a disagreed page must record what disagreed"


def test_disagreed_page_requires_disagreement_details_present(validate):
    doc = load(RESOLVED_DOC)
    disagreed = [p for p in doc["pages"]
                 if p["cross_validation"]["agreement"] == "disagreed"]
    del disagreed[0]["cross_validation"]["disagreement_details"]
    assert validate(doc)


def test_agreed_page_may_omit_disagreement_details(validate):
    """The constraint is conditional -- agreed pages carry no details, and
    the whole real corpus depends on that staying true."""
    doc = load(AGREED_DOC)
    for page in doc["pages"]:
        page["cross_validation"].pop("disagreement_details", None)
    assert validate(doc) == []


# ---- Carve-outs that must not be caught by the new rules ----

def test_non_text_image_document_with_unresolved_disagreements_is_valid(validate):
    """CASE_003/DOC_010 is real: two pages whose reads disagreed, neither
    carrying a `resolution`, under `cross_validation_status:
    non_text_verified`. Legitimate per P8 -- a non-text page's extraction
    question is closed by the human non_text_verification decision, not by
    choosing a reading -- so rollup rule 1 must exempt it. Without the
    exemption this real file would fail."""
    doc = load(NON_TEXT_DOC)
    assert doc["extraction_method"] == "non_text_image"
    unresolved = [p for p in doc["pages"]
                  if p["cross_validation"]["agreement"] == "disagreed"
                  and not p["cross_validation"].get("resolution")]
    assert unresolved, "fixture must have unresolved disagreed pages"
    assert validate(doc) == []


def test_agreed_page_may_carry_a_human_resolution(validate):
    """The CASE_012/DOC_001 page 3 precedent: compare() passed a page as
    agreed, a human later found one read contained fabricated content, and
    the correction is recorded in `resolution`. An agreed page carrying a
    resolution must stay valid -- and must not be dragged into
    `disagreed_resolved` by rollup rule 2, which keys on disagreed pages."""
    doc = load(AGREED_DOC)
    doc["pages"][0]["cross_validation"]["resolution"] = {
        "chosen_reading": "reading_b",
        "resolved_by": "Reviewer",
        "resolved_at": "2026-07-22T10:00:00+09:00",
        "note": "reading_a appended meta-commentary absent from the source page",
    }
    assert doc["cross_validation_status"] == "agreed"
    assert validate(doc) == []


# ---- Pre-v0.8 page-level guarantees, pinned so they cannot silently lapse ----

def test_page_record_requires_its_verdict(validate):
    """The shape the false alarm assumed existed. It could not have
    validated then and must not now."""
    doc = load(RESOLVED_DOC)
    for page in doc["pages"]:
        page.pop("cross_validation", None)
        page.pop("page", None)
    errors = validate(doc)
    assert any("'cross_validation' is a required property" in e for e in errors)
    assert any("'page' is a required property" in e for e in errors)


def test_agreement_must_be_a_known_verdict(validate):
    doc = load(RESOLVED_DOC)
    doc["pages"][0]["cross_validation"]["agreement"] = None
    assert validate(doc)
