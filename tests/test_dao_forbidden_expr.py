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
