"""dao.py's search-document-text -- the deterministic floor under a NEGATIVE claim.

The asymmetry this closes: _cross_contract._normalize_ws already made proving
a quote PRESENT robust to whitespace, but proving a term ABSENT had only a raw
substring search, which finds exactly one spelling. Korean legal text spells one
term several ways (직접청구 / 직접 청구) and extraction adds mid-word line breaks,
so a raw search returns 0 hits for a term printed on the page. On CASE_907 that
produced three wrong absence findings in one day, one recorded as a fabrication
finding against a draft sentence that was correct.

The real-data cases below (직접 청구 on DOC_004 p15, 자연 현상 on DOC_002 p1) are
the exact strings that failed there.
"""
import json

import pytest

import dao


# ---- normalization ----

def test_normalize_folds_internal_space():
    assert dao._normalize_for_absence("직접 청구") == dao._normalize_for_absence("직접청구")


def test_normalize_folds_line_break_inside_word():
    # DOC_004 alone had 722 mid-word breaks; this is the shape that made a raw
    # search miss a term that is plainly on the page.
    assert dao._normalize_for_absence("배상\n책임") == dao._normalize_for_absence("배상책임")


def test_normalize_applies_nfkc():
    # Fullwidth digits/letters must fold to their ASCII forms.
    assert dao._normalize_for_absence("ＫＣＤ") == "KCD"


def test_normalize_empty_when_only_whitespace():
    assert dao._normalize_for_absence("  \n\t ") == ""


# ---- search over a fabricated document ----

def _write_doc(root, case_id, doc_id, pages):
    d = root / "data" / "processed" / case_id / doc_id
    d.mkdir(parents=True, exist_ok=True)
    body = "".join(f"<<<PAGE page={n}>>>\n{text}\n" for n, text in pages)
    (d / "redacted_text.md").write_text(body, encoding="utf-8")


class _Args:
    def __init__(self, case_id, doc_id=None, term="", all_docs=False, context=60):
        self.case_id, self.doc_id, self.term = case_id, doc_id, term
        self.all_docs, self.context = all_docs, context


def test_finds_spaced_spelling_of_joined_query(isolated_dao, capsys):
    """The CASE_907 직접청구 case: searched joined, the document spells it spaced."""
    _write_doc(isolated_dao, "CASE_907", "DOC_004",
               [(15, "회사에 대하여 보험금의 지급을 직접 청구할 수 있습니다.")])
    rc = dao.cmd_search_document_text(_Args("CASE_907", "DOC_004", "직접청구"))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["hit_count"] == 1
    assert out["hits"][0]["page"] == 15
    # The SOURCE spelling is reported, not the folded form -- an agent that has
    # to quote this needs what the document actually prints.
    assert out["hits"][0]["matched_source_text"] == "직접 청구"


def test_reported_span_is_not_shifted(isolated_dao, capsys):
    """Regression: an earlier version normalized the whole page at once and
    reported spans shifted by one character (surfacing `연현상으` for a
    `자연현상` match), because NFKC can change string length."""
    _write_doc(isolated_dao, "CASE_907", "DOC_002",
               [(4, "③ 강설 및 결빙은 자연현상으로서 위험성의 정도를 예측하기 어렵고")])
    dao.cmd_search_document_text(_Args("CASE_907", "DOC_002", "자연현상"))
    out = json.loads(capsys.readouterr().out)
    assert out["hits"][0]["matched_source_text"] == "자연현상"


def test_zero_hits_documents_what_was_searched(isolated_dao, capsys):
    """A 0-hit result must record the normalized form, so a reviewer can see
    which spelling space was covered instead of taking absence on faith."""
    _write_doc(isolated_dao, "CASE_907", "DOC_001", [(1, "보험금 지급 안내")])
    rc = dao.cmd_search_document_text(_Args("CASE_907", "DOC_001", "직접 청구"))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["hit_count"] == 0
    assert out["searched_normalized"] == "직접청구"
    assert "DOC_001" in out["documents_searched"]


def test_missing_processed_text_is_unsearched_not_zero_hits(isolated_dao, capsys):
    """A document that could not be searched must never read as 'term absent'.
    Silently counting it as 0 hits is how a negative claim gets made about a
    document nobody actually looked at."""
    rc = dao.cmd_search_document_text(_Args("CASE_907", "DOC_404", "직접청구"))
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["documents_searched"] == []
    assert out["documents_unsearched"][0]["document_id"] == "DOC_404"


def test_all_docs_searches_every_processed_document(isolated_dao, capsys):
    _write_doc(isolated_dao, "CASE_907", "DOC_001", [(1, "무관한 본문")])
    _write_doc(isolated_dao, "CASE_907", "DOC_002", [(1, "보험자는 자연 현상이라고 한다")])
    manifest = isolated_dao / "outputs" / "CASE_907"
    manifest.mkdir(parents=True, exist_ok=True)
    (manifest / "document_manifest.json").write_text(json.dumps(
        {"documents": [{"document_id": "DOC_001"}, {"document_id": "DOC_002"}]}), encoding="utf-8")

    rc = dao.cmd_search_document_text(_Args("CASE_907", None, "자연현상", all_docs=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [h["document_id"] for h in out["hits"]] == ["DOC_002"]


def test_empty_term_rejected(isolated_dao, capsys):
    rc = dao.cmd_search_document_text(_Args("CASE_907", "DOC_001", "   "))
    assert rc == 2
    assert "EMPTY_TERM" in capsys.readouterr().out


def test_traversal_id_rejected(isolated_dao):
    with pytest.raises(SystemExit):
        dao.cmd_search_document_text(_Args("CASE_907", "../../etc", "x"))
