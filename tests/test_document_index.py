"""The derived policy navigation index (2026-08-15).

Two things the analysis stages could not see, both measured on CASE_142:

  - `claim_analysis` ran 678.4s with the whole stage unattributed, and 13 of
    that run's 29 `search-document-text` calls exited 0-hit. The agent hunts
    through 323 chunks for clause headings the page prints outright.
  - A born-digital policy's table structure is destroyed before any analysis
    stage sees it. `pymupdf` reads DOC_010 p13 as two columns over four data
    rows; the embedded-text extraction the pipeline actually consumes flattens
    it into a run of lines.

The index is DERIVED and ADVISORY on purpose -- regenerable, obligation-free,
never a finalize gate. Clause normalization (retired 2026-08-15) failed the
other way: it gated on an artifact nothing could produce, and the gate was
bypassed rather than met.
"""
import textwrap

import pytest

import document_index as di


def _write_pages(tmp_path, case_id, doc_id, pages):
    base = tmp_path / "data" / "processed" / case_id / doc_id
    base.mkdir(parents=True)
    for number, body in pages.items():
        (base / f"page_{number:03d}.md").write_text(
            textwrap.dedent(body), encoding="utf-8")
    return base


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "ROOT", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------
# clause extraction
# --------------------------------------------------------------------------

def test_article_headings_are_found_with_their_page(isolated_root):
    _write_pages(isolated_root, "CASE_900", "DOC_001", {
        1: """\
            시설소유(관리)자 특별약관
            제1조(사고)
            이 특별약관에서 사고라 함은 ...
            제2조(보상하지 않는 손해)
            """,
    })

    clauses = di.extract_clauses("CASE_900", "DOC_001")

    assert [(c["article"], c["heading"], c["page"]) for c in clauses] == [
        ("제1조", "사고", 1),
        ("제2조", "보상하지 않는 손해", 1),
    ]


def test_a_clause_carries_the_policy_that_owns_it(isolated_root):
    """제1조 means nothing without its 약관 -- a Korean policy bundle prints
    dozens of them and each restarts its own numbering."""
    _write_pages(isolated_root, "CASE_900", "DOC_001", {
        1: """\
            시설소유(관리)자 특별약관
            제1조(사고)
            """,
        2: """\
            구내치료비 추가특별약관
            제1조(보상하는 손해)
            """,
    })

    clauses = di.extract_clauses("CASE_900", "DOC_001")

    assert [c["policy_name"] for c in clauses] == [
        "시설소유(관리)자 특별약관",
        "구내치료비 추가특별약관",
    ]
    # Both are 제1조; only the owner separates them.
    assert {c["article"] for c in clauses} == {"제1조"}


def test_a_clause_before_any_title_is_unowned_not_guessed(isolated_root):
    """Front matter and tables of contents legitimately precede the first
    약관. Attributing those articles to whatever title appears LATER would
    invent an ownership the document does not state."""
    _write_pages(isolated_root, "CASE_900", "DOC_001", {
        1: """\
            제1조(목적)
            """,
        2: """\
            시설소유(관리)자 특별약관
            제1조(사고)
            """,
    })

    clauses = di.extract_clauses("CASE_900", "DOC_001")

    assert clauses[0]["policy_name"] is None
    assert clauses[1]["policy_name"] == "시설소유(관리)자 특별약관"


def test_a_cross_reference_opening_a_line_is_not_a_heading(isolated_root):
    """The defect this pins: a wrapped body sentence can BEGIN a line with a
    reference to another article. Real text from CASE_142 DOC_010.

    Measured there: strict whole-line anchoring drops 3 of 325 matches in
    DOC_010 and 0 of 242 in DOC_009, so precision costs almost nothing.
    """
    _write_pages(isolated_root, "CASE_900", "DOC_001", {
        1: """\
            제8조(보험금 등의 지급한도)
            제3조(보상하는 손해)에 의한 손해에서 다음의 금액을 뺍니다.
            제30조(계약의 해지)의 규정을 준용하여 회사가 보장을 하지 않을 수 있는 경우
            """,
    })

    clauses = di.extract_clauses("CASE_900", "DOC_001")

    assert [c["article"] for c in clauses] == ["제8조"]


def test_a_mid_line_reference_is_never_a_heading(isolated_root):
    _write_pages(isolated_root, "CASE_900", "DOC_001", {
        1: """\
            회사는 제3조(보상하는 손해) 제2호의 비용을 보상합니다.
            """,
    })

    assert di.extract_clauses("CASE_900", "DOC_001") == []


def test_pages_are_read_in_numeric_not_lexical_order(isolated_root):
    """`page_10.md` sorts before `page_9.md` as a string. The real layout is
    zero-padded, but the page number is the index's join key and getting it
    from the filename must not depend on that."""
    base = isolated_root / "data" / "processed" / "CASE_900" / "DOC_001"
    base.mkdir(parents=True)
    for number in (2, 10):
        (base / f"page_{number}.md").write_text(
            f"제{number}조(조항)\n", encoding="utf-8")

    clauses = di.extract_clauses("CASE_900", "DOC_001")

    assert [c["page"] for c in clauses] == [2, 10]


def test_a_document_with_no_processed_pages_yields_no_clauses(isolated_root):
    assert di.extract_clauses("CASE_900", "DOC_404") == []


# --------------------------------------------------------------------------
# table filtering
# --------------------------------------------------------------------------

def test_a_real_data_table_is_kept():
    """CASE_142 DOC_010 p13's 지연이자율표 verbatim -- the one substantive
    table among that case's 55 detected candidates, and the reason detection
    is worth keeping at all: embedded-text extraction flattens it into a run
    of lines and loses which rate belongs to which period."""
    rows = [
        ["기 간", "지 급 이 자"],
        ["지급기일의 다음 날부터 30일 이내 기간", "보험계약대출이율"],
        ["지급기일의 31일이후부터 60일 이내 기간", "보험계약대출이율+가산이율(4.0%)"],
        ["지급기일의 61일이후부터 90일 이내 기간", "보험계약대출이율+가산이율(6.0%)"],
        ["지급기일의 91일 이후 기간", "보험계약대출이율+가산이율(8.0%)"],
    ]

    assert di._is_substantive(rows)


def test_a_blank_intake_form_is_rejected():
    """CASE_142 DOC_009 p4 verbatim. The defect this pins is a per-CELL
    density measure: this form has two body cells, one of which (`시설명세`)
    is a filled label, so cell-counting scores it 50% and admits it. Counting
    LINES scores it 2-of-13, which is what a blank form is.
    """
    rows = [
        ["구 분", "내 용"],
        ["시설명세",
         "상호(성명):\n구조:\n용도\n면적:\n권리관계(소유, 임차, 관리 등의 구별)\n기타:"],
        ["업무내용", "시설 내의 업무:\n시설 밖의 업무:"],
    ]

    assert not di._is_substantive(rows)


def test_a_single_row_definition_box_is_rejected():
    """24 of CASE_142's 55 candidates are this shape -- a term and its
    definition, printed in a box. A table typographically, prose in fact."""
    rows = [["치료비", "치료비라 함은 응급처치, 구급차, 입원(건강보험 기준병실) ..."]]

    assert not di._is_substantive(rows)


def test_a_filled_form_is_kept():
    """The filter must reject BLANK forms, not the form shape. A form whose
    prompts carry answers is data."""
    rows = [
        ["구 분", "내 용"],
        ["시설명세", "상호(성명): 대한빌딩\n구조: 철근콘크리트\n용도: 근린생활시설"],
        ["업무내용", "시설 내의 업무: 임대\n시설 밖의 업무: 없음"],
    ]

    assert di._is_substantive(rows)


def test_an_all_empty_body_is_rejected():
    assert not di._is_substantive([["구 분", "내 용"], ["", ""], ["", ""]])


def test_a_prompt_whose_colon_is_followed_by_more_prompts_is_not_filled():
    """CASE_142 DOC_009 p44 verbatim. `면적:(옥내:  옥외:  )` has characters
    after its colon and every one of them is two further empty prompts, so a
    naive "text after the colon" check scores it filled. That plus one bare
    label put this form at exactly 4/8 -- through a `>= 0.5` threshold."""
    rows = [
        ["구 분", "내 용"],
        ["시설명세",
         "상호(성명):\n구조:\n면적:(옥내:  옥외:  )\n"
         "권리관계(소유, 임차, 관리 등의 구별)\n주차대수:\n관리인 여부:"],
        ["업무내용", ""],
    ]

    assert not di._is_substantive(rows)


def test_recall_is_favoured_over_precision():
    """The filter's standing bias, pinned so a later tightening cannot quietly
    reverse it: a real table dropped from the index is invisible to its
    reader, a spurious one is a line they skip.

    The rows here sit just above the threshold on purpose -- 4 of 7 body
    lines carry values, the shape a partly-completed form takes. It must be
    KEPT.

    Written by measuring rather than by eye: a first attempt at "marginal"
    scored 0.83 and passed even with the ratio tightened to 0.75, so it was
    pinning nothing. Reintroduce the tightening and this version fails, which
    is the whole point of having it.
    """
    rows = [
        ["구 분", "내 용"],
        ["시설명세", "상호(성명): 대한빌딩\n구조:\n용도:\n면적:"],
        ["업무내용", "시설 내의 업무: 임대\n관리자: 김"],
    ]

    lines = [line for row in rows[1:] for cell in row
             for line in cell.splitlines() if line.strip()]
    filled = sum(1 for line in lines if di._states_a_value(line))
    assert 0.5 <= filled / len(lines) < 0.75, (
        f"fixture must straddle the threshold to pin anything: "
        f"{filled}/{len(lines)}")
    assert di._is_substantive(rows)


def test_a_colon_free_data_line_is_a_value_not_a_label():
    """The over-correction this pins, and the more dangerous direction.

    Tightening the previous test's fix by treating any short colon-free
    string as a bare field label classified all eight cells of DOC_010 p13's
    지연이자율표 as labels and discarded the one substantive table in the
    case. The index is candidates: a missing real table is invisible to its
    reader, a spurious one is noise they skip.
    """
    assert di._states_a_value("보험계약대출이율+가산이율(4.0%)")
    assert di._states_a_value("지급기일의 91일 이후 기간")
    assert not di._states_a_value("상호(성명):")
    assert not di._states_a_value("면적:(옥내:  옥외:  )")


# --------------------------------------------------------------------------
# index assembly
# --------------------------------------------------------------------------

def _manifest(*entries):
    return {"documents": list(entries)}


def _policy(doc_id, **over):
    entry = {
        "document_id": doc_id,
        "document_type": "insurance_policy",
        "downstream_disposition": "text_only_no_normalization",
        "extraction_method": "embedded_text",
    }
    entry.update(over)
    return entry


def test_only_text_processed_policy_documents_are_indexed(isolated_root):
    _write_pages(isolated_root, "CASE_900", "DOC_001", {1: "제1조(목적)\n"})
    _write_pages(isolated_root, "CASE_900", "DOC_002", {1: "제1조(목적)\n"})
    _write_pages(isolated_root, "CASE_900", "DOC_003", {1: "제1조(목적)\n"})
    manifest = _manifest(
        _policy("DOC_001"),
        _policy("DOC_002", downstream_disposition="superseded_bundle"),
        _policy("DOC_003", document_type="diagnosis_certificate"),
    )

    index = di.build_index("CASE_900", manifest, raw_pdf_for=lambda d: None)

    assert [d["document_id"] for d in index["documents"]] == ["DOC_001"]


def test_a_document_with_no_reachable_pdf_still_gets_its_clauses(isolated_root):
    """Clauses come from processed text and tables from the PDF. A scan, or a
    fork that did not copy `data/raw`, must lose only the tables -- returning
    nothing at all would make the index useless exactly where the corpus is
    heaviest."""
    _write_pages(isolated_root, "CASE_900", "DOC_001",
                 {1: "특별약관\n제1조(사고)\n"})

    index = di.build_index("CASE_900", _manifest(_policy("DOC_001")),
                           raw_pdf_for=lambda d: None)

    document = index["documents"][0]
    assert len(document["clauses"]) == 1
    assert document["tables"] == []


def test_a_missing_pdf_path_is_not_an_error(isolated_root):
    _write_pages(isolated_root, "CASE_900", "DOC_001", {1: "제1조(사고)\n"})

    index = di.build_index(
        "CASE_900", _manifest(_policy("DOC_001")),
        raw_pdf_for=lambda d: isolated_root / "nope" / "missing.pdf")

    assert index["documents"][0]["tables"] == []


def test_an_unreadable_pdf_yields_no_tables_rather_than_raising(isolated_root):
    """The index is advisory. A corrupt PDF costs an agent the structure it
    would have had; raising here would take down a stage with no stake in the
    outcome."""
    broken = isolated_root / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")

    assert di.extract_tables(broken) == []


def test_the_field_names_the_specs_promise_are_the_field_names_emitted(
        isolated_root):
    """CASE_022: the specs said the index "lists every article heading", so an
    agent looked for a `headings` array, found none, and nearly concluded the
    index was empty. `heading` IS a field -- just one inside a `clauses` entry,
    holding the article title -- which is exactly what made the wording
    plausible and wrong.

    Pinned because the drift is silent: renaming a key here breaks four
    documents (claim-analysis, denial-response, critic, the pipeline skill)
    that no test otherwise reads, and the failure mode is an agent quietly
    deciding the index is empty rather than an error.
    """
    _write_pages(isolated_root, "CASE_900", "DOC_001",
                 {1: "특별약관\n제1조(사고)\n"})

    index = di.build_index("CASE_900", _manifest(_policy("DOC_001")),
                           raw_pdf_for=lambda d: None)

    assert set(index) == {"index_version", "case_id", "documents"}
    document = index["documents"][0]
    assert set(document) == {
        "document_id", "extraction_method", "clauses", "tables"}
    assert "headings" not in document, (
        "if a `headings` array is ever added, the specs' wording becomes "
        "ambiguous again -- pick one name")
    assert set(document["clauses"][0]) == {
        "page", "policy_name", "article", "heading"}


def test_a_table_entry_uses_the_documented_keys(isolated_root, monkeypatch):
    """Same contract for the other array. Driven through `build_index` with a
    stubbed detector rather than a real PDF, so it checks the keys the code
    actually writes -- an earlier version of this test compared a literal set
    to itself and would have passed under any renaming."""
    _write_pages(isolated_root, "CASE_900", "DOC_001", {1: "제1조(사고)\n"})
    pdf = isolated_root / "DOC_001.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    monkeypatch.setattr(di, "extract_tables", lambda path: [{
        "page": 13, "rows": 5, "cols": 2,
        "header": ["기 간", "지 급 이 자"],
        "cells": [["30일 이내", "보험계약대출이율"]],
    }])

    index = di.build_index("CASE_900", _manifest(_policy("DOC_001")),
                           raw_pdf_for=lambda d: pdf)

    assert set(index["documents"][0]["tables"][0]) == {
        "page", "rows", "cols", "header", "cells"}


def test_the_index_records_its_version(isolated_root):
    index = di.build_index("CASE_900", _manifest(), raw_pdf_for=lambda d: None)

    assert index["index_version"] == di.INDEX_VERSION
    assert index["case_id"] == "CASE_900"
