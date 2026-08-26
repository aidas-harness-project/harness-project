"""How much policy text clause verification is allowed to pull.

Verifying a clause quote needs the page the clause sits on. The previous
implementation read every page of every insurance_policy document in the case,
before knowing whether any clause would be linked at all.

That is not a model call, so it costs no tokens and breaks no gate -- which is
exactly why it survived. What it does break is measurement: a policy bundle is
routinely the largest artifact in a case (on CASE_027 two of them were 81% of
the stage's input while five pages were cited), so a whole-bundle read makes
"how much policy text did this stage need?" unanswerable, and it makes the
read-scope discipline the rest of the lane enforces stop at the policy layer.

The index already knows the answer: every clause entry carries its page. So
pages are selected first, from derived data, and only those pages are fetched.
"""
from __future__ import annotations

import pytest

import claim_analysis_policy_links as links
import run_claim_analysis_selective as driver


MANIFEST = {"documents": [
    {"document_id": "DOC_001", "document_type": "medical_record"},
    {"document_id": "DOC_900", "document_type": "insurance_policy"},
    {"document_id": "DOC_901", "document_type": "insurance_policy"},
]}

INDEX = {"documents": [
    {"document_id": "DOC_900", "clauses": [
        {"page": 3, "policy_name": "상해보험 보통약관", "article": "제3조",
         "heading": "진단 담보"},
        {"page": 40, "policy_name": "상해보험 보통약관", "article": "제9조",
         "heading": "수술 담보"},
        {"page": 77, "policy_name": "상해보험 보통약관", "article": "제20조",
         "heading": "보험료의 납입"},
    ]},
    {"document_id": "DOC_901", "clauses": [
        {"page": 12, "policy_name": "질병 특별약관", "article": "제2조",
         "heading": "입원 담보"},
    ]},
]}


def _facts(*field_ids):
    return [{"field_id": field_id, "resolution_status": "asserted"}
            for field_id in field_ids]


# ------------------------------------------------------- page selection --

def test_only_the_pages_of_matching_clauses_are_selected() -> None:
    pages = links.candidate_pages(
        claim_facts=_facts("primary_diagnosis"), manifest=MANIFEST, index=INDEX)

    assert pages == {"DOC_900": [3]}
    assert 77 not in pages.get("DOC_900", []), "an unrelated clause page"


def test_several_coverage_terms_select_several_pages() -> None:
    pages = links.candidate_pages(
        claim_facts=_facts("primary_diagnosis", "admission_status"),
        manifest=MANIFEST, index=INDEX)

    assert pages == {"DOC_900": [3], "DOC_901": [12]}


def test_a_term_with_several_hits_selects_every_candidate_page() -> None:
    """Promotion to `matched` is decided by the quote, so both pages are needed."""
    index = {"documents": [{"document_id": "DOC_900", "clauses": [
        {"page": 3, "heading": "진단 담보"},
        {"page": 55, "heading": "진단 관련 추가 담보"},
    ]}]}
    pages = links.candidate_pages(
        claim_facts=_facts("primary_diagnosis"), manifest=MANIFEST, index=index)

    assert pages == {"DOC_900": [3, 55]}


def test_no_established_fact_selects_no_page() -> None:
    """Nothing to search means nothing to read."""
    assert links.candidate_pages(
        claim_facts=[{"field_id": "primary_diagnosis",
                      "resolution_status": "unavailable"}],
        manifest=MANIFEST, index=INDEX) == {}


def test_a_case_with_no_index_selects_no_page() -> None:
    assert links.candidate_pages(
        claim_facts=_facts("primary_diagnosis"), manifest=MANIFEST,
        index=None) == {}


def test_a_case_with_no_policy_document_selects_no_page() -> None:
    assert links.candidate_pages(
        claim_facts=_facts("primary_diagnosis"),
        manifest={"documents": [{"document_id": "DOC_001",
                                 "document_type": "medical_record"}]},
        index=INDEX) == {}


def test_a_conflicting_fact_still_selects_its_pages() -> None:
    """A disagreement is why the clause is needed, not a reason to skip it."""
    pages = links.candidate_pages(
        claim_facts=[{"field_id": "primary_diagnosis",
                      "resolution_status": "conflict"}],
        manifest=MANIFEST, index=INDEX)
    assert pages == {"DOC_900": [3]}


# ----------------------------------------------------------- the DAO read --

def _capture(monkeypatch):
    calls: list[list[str]] = []

    def fake_dao(args, allow_missing=False):
        calls.append(args)
        return {"documents": [{"document_id": "DOC_900",
                               "pages": [{"page": 3, "text": "제3조 진단 담보"}]}]}

    monkeypatch.setattr(driver, "_dao_json", fake_dao)
    return calls


def test_the_read_is_narrowed_to_the_selected_pages(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    driver._policy_page_text("CASE_9401", {"DOC_900": [3]},
                             run_id="RUN_20260820_1")

    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "read-redacted-text-bundle"
    assert "--doc-id=DOC_900" in args
    assert "--pages=DOC_900=3" in args


def test_the_whole_bundle_is_never_requested(monkeypatch) -> None:
    """The defect, stated directly: a --doc-id with no --pages beside it."""
    calls = _capture(monkeypatch)
    driver._policy_page_text("CASE_9401", {"DOC_900": [3, 40], "DOC_901": [12]},
                             run_id="RUN_20260820_1")

    args = calls[0]
    requested = [a.split("=", 1)[1] for a in args if a.startswith("--doc-id=")]
    narrowed = {a.split("=", 1)[1].split("=", 1)[0]
                for a in args if a.startswith("--pages=")}
    assert set(requested) == narrowed, "every requested document must be narrowed"
    assert "--pages=DOC_900=3,40" in args
    assert "--pages=DOC_901=12" in args


def test_the_read_is_attributed_to_the_run(monkeypatch) -> None:
    """Without --run-id the read lands in unattributed time and measures nothing."""
    calls = _capture(monkeypatch)
    driver._policy_page_text("CASE_9401", {"DOC_900": [3]},
                             run_id="RUN_20260820_1")

    assert "--run-id=RUN_20260820_1" in calls[0]


def test_an_empty_selection_reads_nothing_at_all(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    assert driver._policy_page_text("CASE_9401", {}, run_id="RUN_1") == {}
    assert driver._policy_page_text("CASE_9401", {"DOC_900": []},
                                    run_id="RUN_1") == {}
    assert calls == []


def test_the_selected_text_is_what_verification_uses(monkeypatch) -> None:
    """End of the path: the narrowed read still verifies a quote exactly."""
    _capture(monkeypatch)
    text = driver._policy_page_text("CASE_9401", {"DOC_900": [3]}, run_id="R1")
    verify = driver.make_quote_verifier(text)

    reference = verify("DOC_900", 3, "진단 담보")
    assert reference["document_id"] == "DOC_900"
    assert reference["quote"] == "진단 담보"
    # A page that was not read cannot verify, and is refused rather than assumed.
    assert verify("DOC_900", 77, "보험료의 납입") is None
