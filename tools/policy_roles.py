"""Declared-and-verified policy processing roles (Part 11H).

Before this, what a policy document owed was INFERRED from which artifacts
happened to exist:

    reference_table_only = (reference_table is not None and normalized is None)

That inference is backwards. It reads "no clause contract was written" as
"no clause contract is owed", so the way to be exempted from normalizing a
document was simply never to normalize it. A segment full of real policy
narrative could ship a single table contract and the completeness gate would
treat it as an appendix. The same shape appeared for parents: any document that
was some other document's `source_document_id` was treated as a segmented
parent, so one dummy child entry exempted a whole parent from normalization.

The role is now DECLARED in the manifest (`policy_processing_role`) and
VERIFIED here against the document's real source and real children. Declaring a
role is a claim, not a grant:

  reference_table_only     the processed source must carry no normative
                           narrative, and a reference-table contract must exist.
  clause_segment           a non-empty normalized clause contract must exist.
  mixed_clause_and_table   both must exist -- this is the honest role for an
                           appendix segment that also states rules.
  segmented_parent         real children (registered segments that actually have
                           processed text) AND a parent-coverage contract.
  standalone_policy        not a segment, has no children, and carries its own
                           non-empty normalized clause contract.

Every function returns a list of human-readable error strings (empty = clean),
the same contract as validate_instance()/_cross_contract, so the DAO can print
them and refuse finalization on any non-empty list.
"""
from __future__ import annotations

from policy_completeness import _OPERATIVE_PREDICATE_RE, _STRUCTURAL_ANCHOR_RE

POLICY_ROLES = (
    "segmented_parent",
    "clause_segment",
    "reference_table_only",
    "mixed_clause_and_table",
    "standalone_policy",
)


def declared_role(entry: dict) -> str | None:
    return entry.get("policy_processing_role")


def _children_of(manifest: dict, document_id: str) -> list[dict]:
    return [
        d for d in manifest.get("documents", [])
        if d.get("document_role") == "segment"
        and d.get("source_document_id") == document_id
    ]


def _has_normative_narrative(text: str | None) -> bool:
    """Whether processed text states a rule, as opposed to tabulating values.

    An operative predicate is the reliable signal (지급합니다 / 보상하지 /
    하여야 합니다 / 면책 / 말합니다). Structural anchors alone are NOT used
    here: a classification table legitimately carries 제N조 references and
    numbered rows, and treating those as narrative would refuse exactly the
    appendix segments the reference_table_only role exists for.
    """
    if not text:
        return False
    return bool(_OPERATIVE_PREDICATE_RE.search(text))


def check_policy_processing_roles(
        manifest: dict,
        *,
        normalized_for,
        reference_table_for,
        redacted_text_for,
        parent_coverage_for) -> list[str]:
    """Verify every automated insurance_policy document's declared role.

    The four callables take a document_id and return that document's normalized
    clause contract / reference-table contract / processed text / parent-coverage
    contract, or None. The DAO supplies real readers; tests pass dict-backed
    stubs.
    """
    errors: list[str] = []
    # RETIRED 2026-08-15 along with clause normalization: a role declares what
    # a document owes toward a NORMALIZED contract, and nothing owes one now.
    # Kept as an empty scope rather than deleted because the caller is the
    # policy completion gate, and a named empty scope states why it clears.
    #
    # This filter was a second, independent copy of the scope that
    # `dao._automated_policy_documents` also spelled out -- the drift shape
    # this project has been bitten by before (2026-07-17 forbidden
    # expressions). Retiring one and leaving the other would have left the
    # obligation half-alive: the completion gate would skip the per-document
    # checks while this function still demanded a role for every legacy
    # `automated_text_pipeline` entry, so the four pre-2026-08-04 cases would
    # fail here for a contract no stage produces.
    policy_docs: list[dict] = []
    for doc in policy_docs:
        doc_id = doc.get("document_id")
        role = declared_role(doc)
        if role is None:
            errors.append(
                f"{doc_id}: no policy_processing_role declared -- what this "
                "document owes may not be inferred from which artifacts happen "
                f"to exist; declare one of {list(POLICY_ROLES)}")
            continue
        if role not in POLICY_ROLES:
            errors.append(
                f"{doc_id}: unknown policy_processing_role {role!r}")
            continue

        normalized = normalized_for(doc_id)
        reference_table = reference_table_for(doc_id)
        text = redacted_text_for(doc_id)
        children = _children_of(manifest, doc_id)
        has_clauses = bool((normalized or {}).get("clauses"))

        if role == "reference_table_only":
            if reference_table is None:
                errors.append(
                    f"{doc_id}: declared reference_table_only but no "
                    "reference_table contract exists")
            if _has_normative_narrative(text):
                errors.append(
                    f"{doc_id}: declared reference_table_only, but its processed "
                    "source contains an operative policy predicate -- a document "
                    "that states rules is clause_segment or "
                    "mixed_clause_and_table, and its narrative must be "
                    "normalized rather than dismissed as an appendix")
            if has_clauses:
                errors.append(
                    f"{doc_id}: declared reference_table_only but carries "
                    "normalized clauses -- declare mixed_clause_and_table")

        elif role == "clause_segment":
            if not has_clauses:
                errors.append(
                    f"{doc_id}: declared clause_segment but has no non-empty "
                    "normalized clause contract")

        elif role == "mixed_clause_and_table":
            if not has_clauses:
                errors.append(
                    f"{doc_id}: declared mixed_clause_and_table but has no "
                    "non-empty normalized clause contract")
            if reference_table is None:
                errors.append(
                    f"{doc_id}: declared mixed_clause_and_table but no "
                    "reference_table contract exists")

        elif role == "segmented_parent":
            # A parent is exempt from normalizing its own body only because its
            # segments do it. That requires REAL segments -- registered children
            # that actually have processed text -- and the parent-coverage
            # contract that proves every page is accounted for. One dummy child
            # entry must not buy the exemption.
            real_children = [
                c for c in children
                if redacted_text_for(c.get("document_id")) is not None
            ]
            if not children:
                errors.append(
                    f"{doc_id}: declared segmented_parent but no segment "
                    "declares it as source_document_id")
            elif not real_children:
                errors.append(
                    f"{doc_id}: declared segmented_parent but none of its "
                    f"{len(children)} declared segment(s) has processed text -- "
                    "a placeholder child does not carve a parent, and does not "
                    "exempt it from normalization")
            if parent_coverage_for(doc_id) is None:
                errors.append(
                    f"{doc_id}: declared segmented_parent but has no "
                    "policy_parent_coverage contract -- without it nothing "
                    "proves the segments actually cover the parent")

        elif role == "standalone_policy":
            if children:
                errors.append(
                    f"{doc_id}: declared standalone_policy but "
                    f"{len(children)} segment(s) derive from it -- declare "
                    "segmented_parent")
            if not has_clauses:
                errors.append(
                    f"{doc_id}: declared standalone_policy but has no non-empty "
                    "normalized clause contract")

    return errors


def role_exempts_own_normalization(role: str | None) -> bool:
    """Whether this role legitimately owes no normalized clause contract of its
    own. Used by the completion gate in place of the old artifact-existence
    inference -- the exemption now follows from a VERIFIED declaration."""
    return role in ("segmented_parent", "reference_table_only")
