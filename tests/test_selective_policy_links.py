"""Linking claim facts to clauses the policy stage already processed.

The contract carried `policy_links` and its three statuses from the start, but
`run()` published an empty list, so nothing exercised any of it. These tests
cover the five outcomes a real case can produce, and — more importantly — the
two ways a bad reference could get in: an index entry the processed text does
not confirm, and a citation that skips the DAO's policy verification.

That second one is why the selective schema was registered in
`dao._downstream_policy_ref_errors` rather than given its own checker. A lane
with weaker verification is a second door into the policy layer.
"""
from __future__ import annotations

import json

import pytest

import claim_analysis_policy_links as linker
import dao


CLAUSE_TEXT = "제3조(수술보험금의 지급사유) 회사는 피보험자가 수술을 받은 경우"
POLICY_PAGE = f"보통약관\n{CLAUSE_TEXT}\n"


def _manifest(*, with_policy: bool = True) -> dict:
    documents = [{"document_id": "DOC_001", "document_type": "diagnosis_certificate"}]
    if with_policy:
        documents.append(
            {"document_id": "DOC_900", "document_type": "insurance_policy"})
    return {"documents": documents}


def _index(headings) -> dict:
    return {"documents": [{
        "document_id": "DOC_900",
        "clauses": [
            {"page": 1, "policy_name": "보통약관", "article": f"제{i+3}조",
             "heading": heading}
            for i, heading in enumerate(headings)
        ],
    }]}


def _facts(*field_ids: str) -> list[dict]:
    return [{"field_id": field_id, "resolution_status": "asserted"}
            for field_id in field_ids]


def _verifier(pages: dict):
    def verify(document_id, page, quote):
        text = pages.get((document_id, page))
        if text is None or quote not in text:
            return None
        start = text.index(quote)
        return {"document_id": document_id, "page": page, "quote": quote,
                "start_char": start, "end_char": start + len(quote)}
    return verify


def _build(**over):
    kwargs = dict(
        claim_facts=_facts("surgery_or_major_procedure_status"),
        manifest=_manifest(),
        index=_index(["수술보험금의 지급사유"]),
        verify_quote=_verifier({("DOC_900", 1): POLICY_PAGE}),
    )
    kwargs.update(over)
    return linker.build_policy_links(**kwargs)


# --------------------------------------------------------- the five cases --

def test_a_single_matching_clause_links() -> None:
    links = _build()
    assert len(links) == 1
    assert links[0]["clause_link_status"] == "matched"
    assert links[0]["clause_ref"]["document_id"] == "DOC_900"
    assert links[0]["clause_ref"]["quote"] in POLICY_PAGE


def test_several_equally_matching_clauses_stay_candidates() -> None:
    """Choosing among them is a reviewer's judgement, not this stage's."""
    links = _build(index=_index(["수술보험금의 지급사유", "수술급여금의 지급사유"]))
    assert links[0]["clause_link_status"] == "candidate"
    assert "clause_ref" not in links[0]
    assert links[0]["uncertainty_reason"]


def test_no_clause_names_the_coverage() -> None:
    links = _build(index=_index(["화재손해의 보상"]))
    assert links[0]["clause_link_status"] == "not_found"
    assert links[0]["uncertainty_reason"]


def test_a_case_with_no_policy_document_records_not_found() -> None:
    """Unrunnable is not the same as uncovered -- the reason says which."""
    links = _build(manifest=_manifest(with_policy=False))
    assert links[0]["clause_link_status"] == "not_found"
    # The reason prints in the report, so it is Korean; what it must say
    # is that there is no policy layer to search -- not that the coverage
    # was searched for and missed.
    assert "약관 문서가 없어" in links[0]["uncertainty_reason"]


def test_no_document_index_records_not_found() -> None:
    links = _build(index=None)
    assert links[0]["clause_link_status"] == "not_found"


# ------------------------------------------------- fabrication is refused --

def test_an_unverifiable_clause_is_demoted_not_published() -> None:
    """The index named it; the processed text does not confirm it.

    Publishing anyway is exactly the fabricated reference this lane must not
    produce, so it drops to `candidate` with the reason stated.
    """
    links = _build(verify_quote=lambda *a: None)
    assert links[0]["clause_link_status"] == "candidate"
    assert "clause_ref" not in links[0]
    assert "그대로 확인하지 못해" in links[0]["uncertainty_reason"]


def test_only_a_policy_typed_document_can_be_cited() -> None:
    """The DAO refuses a clause_ref to a non-policy document; so does the search."""
    manifest = {"documents": [
        {"document_id": "DOC_777", "document_type": "other"},
        {"document_id": "DOC_900", "document_type": "insurance_policy"},
    ]}
    assert linker.policy_document_ids(manifest) == ["DOC_900"]


def test_an_unresolved_fact_justifies_no_search() -> None:
    """A link whose basis is a field the case never established is a guess."""
    unresolved = [{"field_id": "surgery_or_major_procedure_status",
                   "resolution_status": "unavailable"}]
    assert linker.build_policy_links(
        claim_facts=unresolved, manifest=_manifest(),
        index=_index(["수술보험금의 지급사유"]),
        verify_quote=_verifier({("DOC_900", 1): POLICY_PAGE})) == []


# --------------------------------- a medical conflict does not stop the search --

def test_a_conflicting_fact_still_gets_its_clause_linked() -> None:
    """Which clause applies and which reading is right are separate questions.

    Suppressing the search because the facts disagree would withhold the very
    clause a reviewer needs to judge the disagreement against.
    """
    links = _build(conflict_candidate_ids_by_field={
        "surgery_or_major_procedure_status": ["CAC_0001"]})
    assert links[0]["clause_link_status"] == "matched"
    requirement = links[0]["requirements"][0]
    assert requirement["evidence_status"] == "conflict"
    assert requirement["conflict_candidate_ids"] == ["CAC_0001"]


def test_a_conflicting_fact_still_justifies_the_search_at_all() -> None:
    """Caught by an end-to-end run, not by the unit tests above.

    `build_policy_links` marks a conflicted requirement correctly -- but only
    for coverages the search reached. Filtering the search itself to `asserted`
    meant a case whose only diagnosis reading was in conflict produced NO link,
    silently, which is the same suppression stated the other way round.
    """
    conflicted = [{"field_id": "primary_diagnosis",
                   "resolution_status": "conflict"}]
    assert linker.coverage_terms(conflicted) == [("primary_diagnosis", "진단")]

    links = _build(claim_facts=conflicted,
                   index=_index(["진단보험금의 지급사유"]),
                   conflict_candidate_ids_by_field={
                       "primary_diagnosis": ["CAC_0001"]})
    assert len(links) == 1
    assert links[0]["requirements"][0]["evidence_status"] == "conflict"


@pytest.mark.parametrize("status", ["unavailable", "not_applicable",
                                    "explicitly_absent"])
def test_a_field_the_case_never_established_justifies_nothing(status: str) -> None:
    assert linker.coverage_terms(
        [{"field_id": "primary_diagnosis", "resolution_status": status}]) == []


def test_no_link_asserts_coverage_or_a_payout() -> None:
    """Linking says what the claim is assessed against, never the outcome.

    Checked on the fields this stage AUTHORS, not on quoted clause text -- a
    clause headed 수술보험금의 지급사유 legitimately contains 보험금, and
    scanning the whole payload would flag the source for saying what it says.
    """
    links = _build()
    authored = json.dumps(
        [{k: v for k, v in link.items() if k not in {"clause_ref"}}
         for link in links], ensure_ascii=False)
    for forbidden in ("지급 가능성", "지급된다", "covered", "payout", "eligible"):
        assert forbidden not in authored
    # And no status beyond the three the contract allows.
    assert {link["clause_link_status"] for link in links} <= {
        "matched", "candidate", "not_found"}


# ------------------------------------------- the DAO verifies this lane too --

def test_the_selective_schema_is_registered_in_the_policy_verifier() -> None:
    """The blocker this work had to clear.

    `_downstream_policy_ref_errors` dispatches on schema name. Before this
    change `claim_analysis_result.schema.json` was absent, so a selective
    clause_ref bypassed canonical-UID state, policy-stage-passed, and snapshot
    freshness -- every check the legacy contracts owe.
    """
    data = {"policy_links": [{
        "coverage_id": "c", "coverage_name": "수술",
        "clause_link_status": "matched",
        "clause_ref": {"document_id": "DOC_900", "page": 1, "quote": CLAUSE_TEXT,
                       "start_char": 0, "end_char": len(CLAUSE_TEXT)},
        "requirements": [],
    }]}
    errors = dao._downstream_policy_ref_errors(
        "CASE_9001", "claim_analysis_result.schema.json", data)
    # It must actually inspect the reference rather than returning [] because
    # the schema is unknown to it.
    assert errors, "a selective clause_ref must be verified, not waved through"


def test_a_result_with_no_clause_reference_is_not_policy_gated() -> None:
    """A case that linked nothing has nothing to bind to."""
    assert dao._downstream_policy_ref_errors(
        "CASE_9001", "claim_analysis_result.schema.json",
        {"policy_links": []}) == []


def test_referenced_documents_scopes_the_snapshot() -> None:
    links = _build()
    assert linker.referenced_documents(links) == ["DOC_900"]
    assert linker.referenced_documents([]) == []


# ------------------------------- `supported` must mean something is supported --
# `evidence_status` was `"conflict" if conflicts else "supported"`, with
# `evidence_references` initialised to `[]`. So a requirement whose underlying
# fact was never established still published `supported` -- an affirmative
# claim that the requirement is met, carrying no citation at all.
#
# Measured 2026-08-26 on CASE_7077: `liability_premises_owner` REQ-1 and
# `liability_premises_medical_expense` REQ-1 both read `supported` with zero
# `evidence_references`, on a case where every liability-grounding fact is
# `unavailable` and all four case types are `uncertain`. Those coverages come
# from `case_type_terms`, which deliberately includes a type the assessment did
# not rule OUT -- correct for deciding what to SEARCH, wrong as a basis for
# saying a requirement is satisfied.
#
# Under P1 an uncited affirmative claim is a defect, and this one is worse than
# an unlinked sentence: it tells a 손해사정사 the requirement is met.

def _real_config() -> dict:
    """The shipped routing config, because `case_type_terms` reads
    `policy_linking.coverage_terms_by_case_type` from it and returns nothing
    without it -- which is why a synthetic config cannot reach this path."""
    import pathlib
    return json.loads((pathlib.Path(__file__).resolve().parent.parent
                       / "config" / "claim_analysis"
                       / "claim_analysis_routing_v0.1.json"
                       ).read_text(encoding="utf-8"))


def test_a_requirement_with_no_evidence_is_not_reported_as_supported() -> None:
    """The CASE_7077 shape: a coverage justified only by an unresolved case
    type, whose facts established nothing."""
    # No policy documents at all, which is CASE_7077's actual state: its
    # links read `clause_link_status: not_found`, "사용할 약관 문서가
    # 없어" -- and the requirement still claimed to be supported.
    links = _build(
        claim_facts=[{"field_id": "surgery_or_major_procedure_status",
                      "resolution_status": "unavailable"}],
        case_type_assessment=[{"case_type": "liability", "verdict": "uncertain"}],
        manifest={"documents": []},
        index=None,
        config=_real_config(),
    )
    for link in links:
        for requirement in link.get("requirements") or []:
            if requirement.get("evidence_status") != "supported":
                continue
            assert requirement.get("evidence_references"), (
                f"{link.get('coverage_id')} {requirement.get('requirement_id')} "
                "claims the requirement is supported and cites nothing")


def test_an_established_fact_still_supports_its_requirement() -> None:
    """The guard must not cost a legitimate `supported`."""
    links = _build()
    statuses = {r.get("evidence_status")
                for link in links for r in link.get("requirements") or []}
    assert statuses, "no requirement was produced at all"
    assert "supported" in statuses
