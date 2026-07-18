"""dao.py's check-forbidden-expressions -- the deterministic floor critic.md
uses for the listed literal forbidden phrases (templates/forbidden-expressions.md).
The semantic/implied cases stay with critic's P3 pass; this tool is the floor,
not full coverage."""
import json

import dao


# ---- Task 1: helpers ----

def test_normalize_strips_wrapping_quotes_and_collapses_space():
    assert dao._normalize_expr('  "보험사는   반드시  지급해야 한다"  ') == "보험사는 반드시 지급해야 한다"


def test_normalize_maps_curly_quotes_to_straight():
    # " / " are curly double-quotes; must normalize the same as straight.
    curly = dao._normalize_expr('“의학적으로 명백하다”')
    straight = dao._normalize_expr('"의학적으로 명백하다"')
    assert curly == straight == "의학적으로 명백하다"


def test_load_forbidden_phrases_reads_avoid_column(tmp_path):
    md = tmp_path / "forbidden-expressions.md"
    md.write_text(
        "# 표현 대조표\n\n"
        "| 위험 표현 | 대체 표현 |\n"
        "| --- | --- |\n"
        '| "보험사는 반드시 지급해야 한다" | "지급 가능성을 검토할 여지가 있다" |\n'
        '| "의학적으로 명백하다" | "의무기록상 확인이 필요하다" |\n',
        encoding="utf-8",
    )
    phrases = dao._load_forbidden_phrases(md)
    assert "보험사는 반드시 지급해야 한다" in phrases
    assert "의학적으로 명백하다" in phrases
    # Header row and separator row must not leak in as phrases.
    assert "위험 표현" not in phrases
    assert all("---" not in p for p in phrases)


def test_load_forbidden_phrases_no_table_returns_empty(tmp_path):
    md = tmp_path / "forbidden-expressions.md"
    md.write_text("# 배경\n\n표가 없는 문서.\n", encoding="utf-8")
    assert dao._load_forbidden_phrases(md) == []


def test_load_forbidden_phrases_missing_file_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        dao._load_forbidden_phrases(tmp_path / "nope.md")


# ---- Task 2: the subcommand ----

def _write_draft(root, text):
    p = root / "outputs" / "CASE_009" / "draft_report_v1.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_listed_phrase_is_a_hit(isolated_dao, make_args, capsys):
    draft = _write_draft(isolated_dao, "결론적으로 보험사는 반드시 지급해야 한다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["clean"] is False
    assert out["hits"][0]["phrase"] == "보험사는 반드시 지급해야 한다"
    assert out["hits"][0]["line"] == 1


def test_clean_draft_is_clean(isolated_dao, make_args, capsys):
    draft = _write_draft(isolated_dao, "지급 가능성을 검토할 여지가 있다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["clean"] is True
    assert out["hits"] == []


def test_curly_quote_variant_still_a_hit(isolated_dao, make_args, capsys):
    # The template is straight-quoted; a draft using curly quotes must still hit.
    draft = _write_draft(isolated_dao, "“의학적으로 명백하다” 라고 볼 수 있다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert any(h["phrase"] == "의학적으로 명백하다" for h in out["hits"])


def test_internal_whitespace_variant_still_a_hit(isolated_dao, make_args, capsys):
    draft = _write_draft(isolated_dao, "보험사는  반드시   지급해야 한다 고 판단된다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert any(h["phrase"] == "보험사는 반드시 지급해야 한다" for h in out["hits"])


def test_near_miss_paraphrase_is_not_a_literal_hit(isolated_dao, make_args, capsys):
    # Proves the floor's boundary honestly: paraphrase is the critic's job, not this tool's.
    draft = _write_draft(isolated_dao, "당연히 지급되어야 마땅하다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["clean"] is True


def test_missing_draft_reports_not_found(isolated_dao, make_args, capsys):
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(isolated_dao / "nope.md")))
    assert rc == 1
    assert "NOT_FOUND" in capsys.readouterr().out


def test_missing_template_reports_no_template(isolated_dao, make_args, capsys, monkeypatch):
    draft = _write_draft(isolated_dao, "보험사는 반드시 지급해야 한다.\n")
    monkeypatch.setattr(dao, "FORBIDDEN_TEMPLATE", isolated_dao / "absent.md")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    assert rc == 2
    assert "NO_TEMPLATE" in capsys.readouterr().out


def test_phrase_split_across_lines_hits_with_null_line(isolated_dao, make_args, capsys):
    # Soft-wrapped: the phrase spans a line break, so it's found only in the
    # whole-doc normalization, not any single line -> hit with line: None.
    draft = _write_draft(isolated_dao, "결론적으로 보험사는 반드시\n지급해야 한다.\n")
    rc = dao.cmd_check_forbidden_expressions(make_args(doc_path=str(draft)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["clean"] is False
    hit = next(h for h in out["hits"] if h["phrase"] == "보험사는 반드시 지급해야 한다")
    assert hit["line"] is None
