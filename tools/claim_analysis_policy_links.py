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


def _configured_field_terms(config: Mapping[str, Any] | None):
    """The field-justified term list, from config when present."""
    rows = ((config or {}).get("policy_linking") or {}).get("coverage_term_fields")
    if not rows:
        return COVERAGE_HINT_FIELDS
    return tuple((row["field_id"], row["term"]) for row in rows
                 if row.get("field_id") and row.get("term"))


def case_type_terms(
    case_type_assessment: Sequence[Mapping[str, Any]] | None,
    config: Mapping[str, Any] | None,
) -> list[tuple[str, str]]:
    """(coverage_id, term) pairs justified by the case TYPE rather than a fact.

    A liability policy never prints 수술/입원/후유장해 -- its clause headings are
    보상하는 손해 / 보상하지 않는 손해 and its coverage identity sits in the
    owning 약관's name. Searching only on medical facts therefore finds nothing
    in one, which is exactly what CASE_053 did: seven terms, zero heading
    matches, while the index held the two clauses the dispute turns on.

    Types are included when the assessment did not rule them out. A verdict of
    `불확실` still means the type is in play -- and on this lane it is the
    ordinary outcome for liability, so requiring a positive verdict here would
    reproduce the gap this exists to close.
    """
    rows = ((config or {}).get("policy_linking") or {}).get(
        "coverage_terms_by_case_type") or {}
    if not rows:
        return []
    in_play = {
        row.get("case_type") for row in case_type_assessment or []
        if row.get("verdict") not in {"not_applicable", "excluded"}
    }
    terms: list[tuple[str, str]] = []
    for case_type in sorted(t for t in in_play if t):
        for entry in rows.get(case_type) or []:
            pair = (entry.get("coverage_id"), entry.get("term"))
            if all(pair) and pair not in terms:
                terms.append(pair)
    return terms


def coverage_terms(
    claim_facts: Sequence[Mapping[str, Any]],
    *,
    config: Mapping[str, Any] | None = None,
    case_type_assessment: Sequence[Mapping[str, Any]] | None = None,
) -> list[tuple[str, str]]:
    """(coverage_id, search term) pairs this case justifies searching on.

    Two independent justifications, because a policy can be identified either
    way. A medical fact the case established suggests a coverage; so does the
    case type itself, and for a non-medical policy the type is the ONLY signal
    -- see `case_type_terms`.
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
    terms = [(field_id, term) for field_id, term in _configured_field_terms(config)
             if field_id in established]
    for pair in case_type_terms(case_type_assessment, config):
        if pair not in terms:
            terms.append(pair)
    return terms


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

    A match is a clause whose heading or policy name contains the term. Picking
    one of several genuinely different clauses is a judgement this stage does
    not make, so those come back as candidates.

    But "several hits" and "several clauses" are not the same thing. A Korean
    coverage is a 약관, and a 약관 is a handful of articles -- 제1조 사고,
    제2조 보상하지 않는 손해, 제3조 준용규정 -- often printed in more than one
    policy document of the same bundle. Requiring a single hit therefore made a
    coverage-level term unmatchable BY CONSTRUCTION: on CASE_053, 시설소유
    returned 6 hits that were one 약관 (3 articles x 2 documents) and
    구내치료비 returned 14 that were one 약관, so both stayed `candidate` while
    the case's central exclusion clause went uncited.

    When every hit names the same `policy_name`, the coverage IS identified.
    The reference then points at the article a reviewer needs first: the one
    whose heading states what is NOT covered, since that is what a denial turns
    on, else the 약관's opening article. Article ordering is preserved, so the
    remaining articles still come back as candidates for the reviewer to open.
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
    if not hits:
        return None, []

    # Collapse only when the term matched the 약관's NAME. A term that matched
    # article headings instead has found several distinct coverages that merely
    # share a 약관 -- 수술보험금의 지급사유 and 수술급여금의 지급사유 are two
    # benefits, and electing one would be the judgement this stage refuses to
    # make. So the collapse requires that every hit's policy_name contains the
    # term and that the hits differ only by article and printing location.
    names = {clause.get("policy_name") for clause in hits}
    matched_by_policy_name = (
        len(names) == 1
        and None not in names
        and needle in _normalize(str(next(iter(names))))
    )
    if matched_by_policy_name:
        primary = _primary_article(hits)
        # Identity comparison would not work here: `_primary_article` returns a
        # copy, so `is not` keeps every hit and the reviewer is handed the
        # matched article back among its own alternatives.
        key = (primary.get("document_id"), primary.get("page"),
               primary.get("article"))
        return primary, [
            c for c in hits
            if (c.get("document_id"), c.get("page"), c.get("article")) != key
        ]
    return None, hits


# The article a reviewer opens first when a coverage resolves to a whole 약관.
# An exclusion clause is what a denial rests on, so it leads; otherwise the
# 약관's own first article. Never a 준용규정, which only points elsewhere.
_EXCLUSION_HEADING = "보상하지않는손해"


def _primary_article(hits: Sequence[Mapping[str, Any]]) -> dict:
    def article_number(clause: Mapping[str, Any]) -> int:
        digits = re.findall(r"\d+", str(clause.get("article") or ""))
        return int(digits[0]) if digits else 9999

    exclusions = [c for c in hits
                  if _normalize(str(c.get("heading") or "")) == _EXCLUSION_HEADING]
    pool = exclusions or [
        c for c in hits
        if "준용" not in str(c.get("heading") or "")
    ] or list(hits)
    return dict(min(pool, key=lambda c: (article_number(c),
                                         str(c.get("document_id")),
                                         c.get("page") or 0)))


def candidate_pages(
    *,
    claim_facts: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    index: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    case_type_assessment: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, list[int]]:
    """The policy pages a link verification could actually need, per document.

    Selection happens against the INDEX -- headings and policy names, already
    derived -- so the pages are known before any text is read. Only these pages
    are then fetched, which is the whole point: a policy bundle is the largest
    thing in a case, and verifying a clause quote needs the page that clause
    sits on, not the document.

    Every coverage term contributes the pages of every clause it hits, matched
    or merely candidate. A candidate is included because promotion to `matched`
    is decided by whether the quote verifies, and that decision needs the page.
    """
    if index is None:
        return {}
    doc_ids = policy_document_ids(manifest)
    if not doc_ids:
        return {}

    pages: dict[str, set[int]] = {}
    for _coverage_id, term in coverage_terms(
            claim_facts, config=config,
            case_type_assessment=case_type_assessment):
        match, candidates = find_clause(index, doc_ids, term)
        for clause in ([match] if match is not None else []) + candidates:
            page = clause.get("page")
            document_id = clause.get("document_id")
            if isinstance(page, int) and isinstance(document_id, str):
                pages.setdefault(document_id, set()).add(page)
    return {document_id: sorted(found)
            for document_id, found in sorted(pages.items())}


def build_policy_links(
    *,
    claim_facts: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    index: Mapping[str, Any] | None,
    verify_quote: Callable[[str, int, str], dict | None],
    conflict_candidate_ids_by_field: Mapping[str, Sequence[str]] | None = None,
    config: Mapping[str, Any] | None = None,
    case_type_assessment: Sequence[Mapping[str, Any]] | None = None,
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

    # Which coverages rest on a fact the case actually established, as opposed
    # to a case type the assessment merely did not rule out. `coverage_terms`
    # merges both and does not say which is which, so recompute the fact side
    # here rather than inferring it from the merged list.
    fact_backed = {
        field_id for field_id, _ in coverage_terms(
            claim_facts, config=config, case_type_assessment=None)
    }

    for coverage_id, term in coverage_terms(
            claim_facts, config=config,
            case_type_assessment=case_type_assessment):
        conflicts = list(by_field.get(coverage_id) or [])
        # `supported` is an affirmative claim that the requirement is met, and
        # P1 does not let one stand uncited. A coverage justified only by a
        # case type in play establishes nothing about the requirement -- it
        # says which clauses are worth SEARCHING for, which is a different
        # question. Measured 2026-08-26 on CASE_7077: `liability_premises_owner`
        # and `liability_premises_medical_expense` both published `supported`
        # with zero evidence_references, on a case whose liability-grounding
        # facts are all `unavailable`, all four case types `uncertain`, and no
        # policy document present at all. It told a 손해사정사 the requirement
        # was met.
        if conflicts:
            status = "conflict"
            reason = ("이 요건이 근거하는 사실에 서로 다른 두 기재가 있습니다. 검토자가 "
                      "양쪽을 조항에 대조할 수 있도록 조항을 연결했습니다.")
        elif coverage_id in fact_backed:
            status = "supported"
            reason = "확인된 사실이 이 요건을 충족한다고 기재하고 있습니다."
        else:
            status = "unknown"
            reason = ("사건유형상 검토 대상이어서 조항을 찾았을 뿐, 이 요건을 "
                      "충족한다고 확인된 사실은 없습니다. 검토자가 직접 판단해야 합니다.")
        requirement = {
            "requirement_id": "REQ-1",
            "requirement_text": f"{term} 관련 담보 요건",
            "evidence_status": status,
            "conflict_candidate_ids": conflicts,
            "reason": reason,
            "evidence_references": [],
        }

        if not doc_ids or index is None:
            links.append({
                "coverage_id": coverage_id,
                "coverage_name": term,
                "clause_link_status": "not_found",
                "uncertainty_reason": (
                    "처리된 약관 문서가 없어 검색할 조항 자료가 없습니다."
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
                link = {
                    "coverage_id": coverage_id,
                    "coverage_name": term,
                    "clause_link_status": "matched",
                    "clause_ref": reference,
                    "requirements": [requirement],
                }
                if candidates:
                    # A coverage that is a whole 약관 elects ONE article into
                    # `clause_ref` (the schema holds no more -- see
                    # open-decisions.md), so its remaining articles would
                    # otherwise vanish. A reviewer comparing a denial against
                    # the policy needs to know 제1조 and 제3조 exist and where
                    # they are, not only the article this stage put first.
                    link["uncertainty_reason"] = (
                        "이 담보는 여러 조항으로 구성되어 있어 대표 조항 1건을 "
                        f"연결했습니다. 같은 담보의 나머지 조항 {len(candidates)}건: "
                        + _candidate_summary(candidates)
                    )
                links.append(link)
                continue
            # The index named a clause the processed text does not confirm.
            # Publishing it anyway would be exactly the fabricated reference
            # this lane must not produce.
            links.append({
                "coverage_id": coverage_id,
                "coverage_name": term,
                "clause_link_status": "candidate",
                "uncertainty_reason": (
                    "색인에서 조항을 찾았으나 처리된 원문에서 문구를 그대로 "
                    "확인하지 못해, 확정 조항이 아닌 후보로 제시합니다."
                ),
                "requirements": [requirement],
            })
            continue

        links.append({
            "coverage_id": coverage_id,
            "coverage_name": term,
            "clause_link_status": "candidate" if candidates else "not_found",
            # Korean, and it NAMES the candidates. The previous wording was an
            # English sentence reporting only a count -- it printed verbatim
            # into a Korean deliverable, and a reviewer told "6 clauses match"
            # with no article, page or heading could not act on it without
            # re-deriving the search by hand.
            "uncertainty_reason": (
                "이 담보에 해당할 수 있는 조항이 "
                f"{len(candidates)}건입니다(어느 조항이 적용되는지는 검토자 판단): "
                + _candidate_summary(candidates)
                if candidates else
                "처리된 약관 자료에서 이 담보를 명시한 조항을 찾지 못했습니다."
            ),
            "requirements": [requirement],
        })
    return links


def _candidate_summary(candidates: Sequence[Mapping[str, Any]],
                       limit: int = 6) -> str:
    """Name the candidate clauses so the reason is actionable.

    The candidate list itself has nowhere to live -- `policy_link` carries a
    single `clause_ref` and no candidates array (see open-decisions.md) -- so
    until that contract changes, this sentence is the only place a reviewer
    learns WHICH clauses were found rather than merely how many.
    """
    parts = []
    for clause in candidates[:limit]:
        where = f"{clause.get('document_id')} p{clause.get('page')}"
        name = str(clause.get("policy_name") or "").strip()
        article = str(clause.get("article") or "").strip()
        heading = str(clause.get("heading") or "").strip()
        label = " ".join(bit for bit in (name, article, heading) if bit)
        parts.append(f"{label}({where})" if label else where)
    if len(candidates) > limit:
        parts.append(f"외 {len(candidates) - limit}건")
    return ", ".join(parts)


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
