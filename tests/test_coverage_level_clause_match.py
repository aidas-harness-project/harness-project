"""A coverage that IS a 약관 matches; distinct coverages inside one stay candidates.

The defect, measured on CASE_053 (2026-08-20): `find_clause` matched only when
exactly one clause hit, but a Korean coverage is a 약관 and a 약관 is several
articles -- 제1조 사고, 제2조 보상하지 않는 손해, 제3조 준용규정 -- often
reprinted in more than one policy document of a bundle. 시설소유 returned 6 hits
that were one 약관 (3 articles x 2 documents) and 구내치료비 returned 14, so both
stayed `candidate` and the exclusion clause the insurer's denial rests on was
never cited. Coverage-level terms were unmatchable by construction.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import claim_analysis_policy_links as pl


def _index(clauses):
    by_doc = {}
    for doc_id, page, name, article, heading in clauses:
        by_doc.setdefault(doc_id, []).append(
            {"page": page, "policy_name": name, "article": article,
             "heading": heading})
    return {"documents": [{"document_id": d, "clauses": c}
                          for d, c in by_doc.items()]}


ONE_YAKGWAN_TWO_DOCS = _index([
    ("DOC_009", 2, "시설소유(관리)자 특별약관", "제1조", "사고"),
    ("DOC_009", 2, "시설소유(관리)자 특별약관", "제2조", "보상하지 않는 손해"),
    ("DOC_009", 4, "시설소유(관리)자 특별약관", "제3조", "준용규정"),
    ("DOC_010", 35, "시설소유(관리)자 특별약관", "제1조", "사고"),
    ("DOC_010", 35, "시설소유(관리)자 특별약관", "제2조", "보상하지 않는 손해"),
    ("DOC_010", 37, "시설소유(관리)자 특별약관", "제3조", "준용규정"),
])

DOCS = ["DOC_009", "DOC_010"]


def test_one_yakgwan_across_articles_and_documents_matches():
    match, candidates = pl.find_clause(ONE_YAKGWAN_TWO_DOCS, DOCS, "시설소유")
    assert match is not None, "a coverage that is one 약관 must match"
    # The exclusion clause leads: it is what a denial turns on.
    assert match["article"] == "제2조"
    assert match["heading"] == "보상하지 않는 손해"
    assert match["document_id"] == "DOC_009"
    # The matched article is not handed back among its own alternatives.
    assert len(candidates) == 5
    assert all(not (c["document_id"] == match["document_id"]
                    and c["page"] == match["page"]
                    and c["article"] == match["article"])
               for c in candidates)


def test_distinct_coverages_sharing_one_yakgwan_stay_candidates():
    """The safety property: two benefits are not one coverage."""
    index = _index([
        ("DOC_900", 1, "보통약관", "제3조", "수술보험금의 지급사유"),
        ("DOC_900", 1, "보통약관", "제4조", "수술급여금의 지급사유"),
    ])
    match, candidates = pl.find_clause(index, ["DOC_900"], "수술")
    assert match is None, "electing one of two benefits is a reviewer's call"
    assert len(candidates) == 2


def test_two_different_yakgwan_stay_candidates():
    index = _index([
        ("DOC_009", 2, "시설소유(관리)자 특별약관", "제2조", "보상하지 않는 손해"),
        ("DOC_009", 9, "물적손해 확장 추가특별약관", "제2조", "보상하지 않는 손해"),
    ])
    match, candidates = pl.find_clause(index, ["DOC_009"], "보상하지 않는 손해")
    assert match is None
    assert len(candidates) == 2


def test_a_yakgwan_with_no_exclusion_article_leads_with_its_first():
    index = _index([
        ("DOC_009", 5, "구내치료비 추가특별약관", "제1조", "보상하는 손해"),
        ("DOC_009", 6, "구내치료비 추가특별약관", "제3조", "준용규정"),
    ])
    match, _ = pl.find_clause(index, ["DOC_009"], "구내치료비")
    assert match is not None
    assert match["article"] == "제1조"


def test_a_yakgwan_of_only_reference_articles_still_returns_one():
    """준용규정 is never preferred, but it must not produce an empty pool."""
    index = _index([
        ("DOC_009", 6, "구내치료비 추가특별약관", "제3조", "준용규정"),
        ("DOC_010", 39, "구내치료비 추가특별약관", "제3조", "준용규정"),
    ])
    match, _ = pl.find_clause(index, DOCS, "구내치료비")
    assert match is not None


def test_a_single_hit_is_unchanged():
    index = _index([("DOC_009", 2, "시설소유(관리)자 특별약관", "제2조",
                     "보상하지 않는 손해")])
    match, candidates = pl.find_clause(index, ["DOC_009"], "시설소유")
    assert match is not None and candidates == []
