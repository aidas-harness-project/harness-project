"""Part 11H: policy processing roles are declared and verified, never inferred.

The old rule was an inference from artifact existence:

    reference_table_only = (reference_table is not None and normalized is None)

which reads "no clause contract was written" as "no clause contract is owed" --
so the way to be exempted from normalizing a document was simply never to
normalize it. The same shape let one dummy child entry exempt a whole parent.

The role is now declared in the manifest and checked against the document's real
source and real children.
"""
import policy_roles


NARRATIVE = (
    "<<<PAGE page=1>>>\n"
    "제3조(보험금의 지급) 회사는 피보험자가 사망한 경우 보험금을 지급합니다.\n"
)
TABLE_ONLY = (
    "<<<PAGE page=1>>>\n"
    "【별표3】 골절분류표\n"
    "장해분류 지급률\n"
    "눈의 장해 50%\n"
)


def _doc(document_id, role=None, **kw):
    entry = {
        "document_id": document_id,
        "document_type": "insurance_policy",
        "downstream_disposition": "automated_text_pipeline",
    }
    if role is not None:
        entry["policy_processing_role"] = role
    entry.update(kw)
    return entry


def _check(documents, *, normalized=None, tables=None, texts=None,
           coverage=None):
    manifest = {"case_id": "CASE_030", "documents": documents}
    normalized = normalized or {}
    tables = tables or {}
    texts = texts or {}
    coverage = coverage or {}
    return policy_roles.check_policy_processing_roles(
        manifest,
        normalized_for=normalized.get,
        reference_table_for=tables.get,
        redacted_text_for=texts.get,
        parent_coverage_for=coverage.get,
    )


def _clauses(n=1):
    return {"clauses": [{"clause_uid": f"PC-{i:016d}"} for i in range(n)]}


# --- the role must be declared at all -------------------------------------

def test_missing_role_is_a_blocker():
    errors = _check([_doc("DOC_005")])
    assert any("no policy_processing_role declared" in e for e in errors), errors


def test_unknown_role_is_rejected():
    errors = _check([_doc("DOC_005", role="whatever")])
    assert any("unknown policy_processing_role" in e for e in errors), errors


# --- reference_table_only cannot be claimed by writing only a table -------

def test_table_only_claim_without_a_table_contract_is_rejected():
    """Declaring the role does not grant it."""
    errors = _check(
        [_doc("DOC_007", role="reference_table_only")],
        texts={"DOC_007": TABLE_ONLY})
    assert any("no reference_table contract exists" in e for e in errors), errors


def test_table_only_with_normative_narrative_is_rejected():
    """A segment full of policy narrative cannot be dismissed as an appendix by
    shipping a table contract and skipping normalization."""
    errors = _check(
        [_doc("DOC_005", role="reference_table_only")],
        tables={"DOC_005": {"tables": []}},
        texts={"DOC_005": NARRATIVE})
    assert any("operative policy predicate" in e for e in errors), errors


def test_genuine_table_only_appendix_passes():
    errors = _check(
        [_doc("DOC_007", role="reference_table_only")],
        tables={"DOC_007": {"tables": []}},
        texts={"DOC_007": TABLE_ONLY})
    assert errors == [], errors


def test_table_only_that_also_has_clauses_must_declare_mixed():
    errors = _check(
        [_doc("DOC_007", role="reference_table_only")],
        normalized={"DOC_007": _clauses()},
        tables={"DOC_007": {"tables": []}},
        texts={"DOC_007": TABLE_ONLY})
    assert any("declare mixed_clause_and_table" in e for e in errors), errors


# --- mixed / clause segments ----------------------------------------------

def test_mixed_clause_and_table_requires_both():
    errors = _check(
        [_doc("DOC_006", role="mixed_clause_and_table")],
        normalized={"DOC_006": _clauses()},
        texts={"DOC_006": NARRATIVE})
    assert any("no reference_table contract exists" in e for e in errors), errors


def test_mixed_clause_and_table_with_both_passes():
    errors = _check(
        [_doc("DOC_006", role="mixed_clause_and_table")],
        normalized={"DOC_006": _clauses()},
        tables={"DOC_006": {"tables": []}},
        texts={"DOC_006": NARRATIVE})
    assert errors == [], errors


def test_clause_segment_without_clauses_is_rejected():
    errors = _check(
        [_doc("DOC_008", role="clause_segment")],
        texts={"DOC_008": NARRATIVE})
    assert any("no non-empty normalized clause contract" in e
               for e in errors), errors


def test_clause_segment_with_clauses_passes():
    errors = _check(
        [_doc("DOC_008", role="clause_segment")],
        normalized={"DOC_008": _clauses(3)},
        texts={"DOC_008": NARRATIVE})
    assert errors == [], errors


# --- a dummy child must not exempt a parent -------------------------------

def _parent_and_child(child_has_text):
    parent = _doc("DOC_001", role="segmented_parent", document_role="physical")
    child = _doc("DOC_009", role="clause_segment",
                 document_role="segment", source_document_id="DOC_001")
    texts = {"DOC_009": NARRATIVE} if child_has_text else {}
    return [parent, child], texts


def test_dummy_segment_does_not_exempt_a_parent():
    """A placeholder child with no processed text does not carve a parent."""
    docs, texts = _parent_and_child(child_has_text=False)
    errors = _check(docs, normalized={"DOC_009": _clauses()}, texts=texts,
                    coverage={"DOC_001": {"pages": []}})
    assert any("none of its" in e and "has processed text" in e
               for e in errors), errors


def test_segmented_parent_without_children_is_rejected():
    errors = _check(
        [_doc("DOC_001", role="segmented_parent", document_role="physical")],
        coverage={"DOC_001": {"pages": []}})
    assert any("no segment declares it as source_document_id" in e
               for e in errors), errors


def test_segmented_parent_without_parent_coverage_is_rejected():
    docs, texts = _parent_and_child(child_has_text=True)
    errors = _check(docs, normalized={"DOC_009": _clauses()}, texts=texts)
    assert any("no policy_parent_coverage contract" in e
               for e in errors), errors


def test_real_segmented_parent_passes():
    docs, texts = _parent_and_child(child_has_text=True)
    errors = _check(docs, normalized={"DOC_009": _clauses()}, texts=texts,
                    coverage={"DOC_001": {"pages": []}})
    assert errors == [], errors


# --- standalone -----------------------------------------------------------

def test_standalone_policy_with_children_must_declare_segmented_parent():
    docs = [
        _doc("DOC_001", role="standalone_policy", document_role="physical"),
        _doc("DOC_009", role="clause_segment", document_role="segment",
             source_document_id="DOC_001"),
    ]
    errors = _check(docs, normalized={
        "DOC_001": _clauses(), "DOC_009": _clauses()},
        texts={"DOC_009": NARRATIVE})
    assert any("declare segmented_parent" in e for e in errors), errors


def test_standalone_policy_without_clauses_is_rejected():
    errors = _check(
        [_doc("DOC_002", role="standalone_policy", document_role="physical")],
        texts={"DOC_002": NARRATIVE})
    assert any("no non-empty normalized clause contract" in e
               for e in errors), errors


def test_role_exemption_helper_matches_the_verified_roles():
    assert policy_roles.role_exempts_own_normalization("segmented_parent")
    assert policy_roles.role_exempts_own_normalization("reference_table_only")
    assert not policy_roles.role_exempts_own_normalization("clause_segment")
    assert not policy_roles.role_exempts_own_normalization(None)
