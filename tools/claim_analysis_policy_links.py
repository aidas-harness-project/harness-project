"""Link extracted claim facts to clauses the policy stage already processed.

This is the smallest connector that satisfies the requirement, and it is
deliberately not a policy pipeline. `policy_clause_processing` owns the policy
layer: it processes the text, builds `_document_index.json`, and registers
canonical UIDs. This module only *reads* that work and points at it.

Three things it therefore does not do:

* **Re-extract.** Clause text comes from the index and the DAO's processed-text
  reads. Nothing here opens a policy PDF or re-derives a clause.
* **Become a second writer.** It returns `policy_links` for the Claim Analysis
  contract. It writes nothing to the policy layer.
* **Decide coverage.** A link says "this clause is what the claim would be
  assessed against", never "this claim is covered". Final applicability and
  payout stay out of scope for the whole lane.

Every clause reference is verified twice over: the quote must appear verbatim
in the DAO-served processed text (so a fabricated clause cannot be cited), and
at write time `dao._downstream_policy_ref_errors` re-checks canonical UID
state, policy-stage-passed, and snapshot freshness on exactly the same terms as
the legacy contracts. That second check is why this lane was registered in that
function rather than given a verifier of its own.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

# Only a document the manifest types as an insurance policy may carry a
# clause_ref. Same rule as the legacy lane's `policy_documents()`, and for the
# same reason: the DAO's canonical-state gate refuses a reference to anything
# that never went through policy registration, so naming the permitted set up
# front turns a write-time refusal into a search that never strays.
POLICY_DOCUMENT_TYPE = "insurance_policy"

# Claim facts that suggest which coverage is in play. Kept small and explicit:
# a wider net produces more candidate clauses, and a candidate a reviewer has to
# dismiss costs more than a gap they can see.
COVERAGE_HINT_FIELDS = (
    ("surgery_or_major_procedure_status", "수술"),
    ("surgery_or_procedure_name", "수술"),
    ("admission_status", "입원"),
    ("disability_type", "후유장해"),
    ("disability_related_diagnosis", "후유장해"),
    ("injury_event_present", "상해"),
    ("primary_diagnosis", "진단"),
)


def policy_document_ids(manifest: Mapping[str, Any]) -> list[str]:
    """The case's policy documents, in stable order."""
    return sorted({
        document["document_id"]
        for document in manifest.get("documents") or []
        if isinstance(document, dict)
        and document.get("document_type") == POLICY_DOCUMENT_TYPE
    })


def coverage_terms(claim_facts: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """(coverage_id, search term) pairs the case's own facts justify.

    Derived from facts the case actually established -- asserted OR conflicting.
    An unresolved field suggests nothing, and searching on a value the case
    never established would produce a link whose basis is a guess.
    """
    # `conflict` counts alongside `asserted`, and that is the whole point of
    # searching under disagreement: the case DID establish that this coverage
    # is in play -- two sources say so, they just say different things about
    # the value. Suppressing the search would withhold the clause a reviewer
    # needs to judge the disagreement against, which is the opposite of what
    # the conflict should cause. `unavailable` and `not_applicable` stay out:
    # nothing was established there, so a link would rest on a guess.
    established = {
        row["field_id"] for row in claim_facts
        if row.get("resolution_status") in {"asserted", "conflict"}
    }
    return [(field_id, term) for field_id, term in COVERAGE_HINT_FIELDS
            if field_id in established]


def _clause_entries(index: Mapping[str, Any], doc_id: str) -> list[dict]:
    for document in index.get("documents") or []:
        if document.get("document_id") == doc_id:
            return list(document.get("clauses") or [])
    return []


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def find_clause(
    index: Mapping[str, Any],
    doc_ids: Sequence[str],
    term: str,
) -> tuple[dict | None, list[dict]]:
    """(exact match, candidates) for one coverage term against the index.

    A match is a clause whose heading or policy name contains the term. More
    than one hit is NOT a match -- it is a set of candidates, because picking
    one of several equally-matching clauses is a judgement this stage does not
    make.
    """
    hits: list[dict] = []
    needle = _normalize(term)
    for doc_id in doc_ids:
        for clause in _clause_entries(index, doc_id):
            haystack = _normalize(
                f"{clause.get('heading', '')}{clause.get('policy_name', '')}")
            if needle and needle in haystack:
                hits.append({**clause, "document_id": doc_id})
    if len(hits) == 1:
        return hits[0], []
    return None, hits


def build_policy_links(
    *,
    claim_facts: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    index: Mapping[str, Any] | None,
    verify_quote: Callable[[str, int, str], dict | None],
    conflict_candidate_ids_by_field: Mapping[str, Sequence[str]] | None = None,
) -> list[dict]:
    """Link each justified coverage term to the policy layer.

    `verify_quote(document_id, page, quote)` returns an exact evidence
    reference or None; the caller binds it to the DAO's processed text, so a
    clause reference that cannot be located verbatim is dropped rather than
    published.

    Statuses follow the contract: `matched` carries the clause_ref, while
    `candidate` and `not_found` carry a reason instead. A medical conflict does
    NOT suppress the search -- the requirement is marked `conflict` and points
    at the candidate, because which clause applies is a separate question from
    which reading of the facts is right.
    """
    doc_ids = policy_document_ids(manifest)
    by_field = dict(conflict_candidate_ids_by_field or {})
    links: list[dict] = []

    for coverage_id, term in coverage_terms(claim_facts):
        conflicts = list(by_field.get(coverage_id) or [])
        requirement = {
            "requirement_id": "REQ-1",
            "requirement_text": f"{term} 관련 담보 요건",
            "evidence_status": "conflict" if conflicts else "supported",
            "conflict_candidate_ids": conflicts,
            "reason": (
                "The claim fact this requirement rests on has two conflicting "
                "readings; the clause is linked so a reviewer can judge both "
                "against it."
                if conflicts else
                "An asserted claim fact states this condition."
            ),
            "evidence_references": [],
        }

        if not doc_ids or index is None:
            links.append({
                "coverage_id": coverage_id,
                "coverage_name": term,
                "clause_link_status": "not_found",
                "uncertainty_reason": (
                    "The case holds no processed insurance_policy document, so "
                    "there is no clause layer to search."
                ),
                "requirements": [requirement],
            })
            continue

        match, candidates = find_clause(index, doc_ids, term)
        if match is not None:
            reference = verify_quote(
                match["document_id"], match["page"],
                match.get("heading") or match.get("policy_name") or "")
            if reference is not None:
                links.append({
                    "coverage_id": coverage_id,
                    "coverage_name": term,
                    "clause_link_status": "matched",
                    "clause_ref": reference,
                    "requirements": [requirement],
                })
                continue
            # The index named a clause the processed text does not confirm.
            # Publishing it anyway would be exactly the fabricated reference
            # this lane must not produce.
            links.append({
                "coverage_id": coverage_id,
                "coverage_name": term,
                "clause_link_status": "candidate",
                "uncertainty_reason": (
                    "A clause was located in the document index, but its text "
                    "could not be verified verbatim in the processed source, "
                    "so it is offered as a candidate rather than a match."
                ),
                "requirements": [requirement],
            })
            continue

        links.append({
            "coverage_id": coverage_id,
            "coverage_name": term,
            "clause_link_status": "candidate" if candidates else "not_found",
            "uncertainty_reason": (
                f"{len(candidates)} clauses match this coverage term; choosing "
                "between them is a reviewer's judgement, not this stage's."
                if candidates else
                "No clause in the processed policy layer names this coverage."
            ),
            "requirements": [requirement],
        })
    return links


def referenced_documents(links: Sequence[Mapping[str, Any]]) -> list[str]:
    """Policy documents the links actually cite -- the snapshot's scope."""
    seen: set[str] = set()
    for link in links:
        for ref in (link.get("clause_ref"),) + tuple(
            requirement.get("clause_ref")
            for requirement in link.get("requirements") or []
        ):
            if isinstance(ref, dict) and ref.get("document_id"):
                seen.add(ref["document_id"])
    return sorted(seen)
