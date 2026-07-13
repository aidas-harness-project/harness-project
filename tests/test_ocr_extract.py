"""ocr_extract.py -- compare()'s verdict parsing and scratch_dir's
placement/cleanup. Regression coverage for the two known-gaps.md item 3
bugs: startswith("AGREE") false-failing sentence-form verdicts, and page
images staging under system /tmp where the nested claude CLI can't read
them.

subprocess.run is mocked throughout -- these tests never shell out to a
real `claude` binary.
"""
import tempfile
from unittest import mock

import pytest

import ocr_extract as oe


def _mock_run(verdict_text):
    def _run(cmd, **kw):
        r = mock.Mock()
        r.returncode = 0
        r.stdout = verdict_text
        r.stderr = ""
        return r
    return _run


@pytest.mark.parametrize("verdict,expected", [
    ("AGREE: same facts", "agreed"),
    ("DISAGREE: date mismatch", "disagreed"),
    ("The two transcriptions AGREE on all major facts.", "agreed"),
    ("I have to say these transcriptions DISAGREE on the diagnosis code.", "disagreed"),
    ("completely garbled output with neither token", "disagreed"),
])
def test_compare_word_boundary_parsing(monkeypatch, verdict, expected):
    monkeypatch.setattr(oe.subprocess, "run", _mock_run(verdict))
    result = oe.compare("text A", "text B")
    assert result["agreement"] == expected


def test_compare_identical_texts_short_circuits_without_subprocess(monkeypatch):
    def _fail_if_called(*a, **kw):
        raise AssertionError("subprocess.run should not be called for identical texts")
    monkeypatch.setattr(oe.subprocess, "run", _fail_if_called)

    result = oe.compare("same text", "same text")
    assert result["agreement"] == "agreed"


def test_compare_disagree_substring_inside_word_does_not_false_trigger(monkeypatch):
    """'DISAGREE' contains 'AGREE' as a substring but not on a word
    boundary -- must not be misread as an AGREE verdict."""
    monkeypatch.setattr(oe.subprocess, "run", _mock_run("DISAGREE"))
    result = oe.compare("a", "b")
    assert result["agreement"] == "disagreed"


def test_unparseable_verdict_records_the_raw_text_for_audit(monkeypatch):
    monkeypatch.setattr(oe.subprocess, "run", _mock_run("???"))
    result = oe.compare("a", "b")
    assert result["agreement"] == "disagreed"
    assert "???" in result["disagreement_details"][0]


def test_scratch_dir_is_project_local_not_system_tmp():
    with oe.scratch_dir("CASE_009", "DOC_001") as d:
        assert str(oe.ROOT) in str(d)
        assert not str(d).startswith(tempfile.gettempdir())
        assert d.exists()
    assert not d.exists(), "scratch dir must be cleaned up on exit"


def test_scratch_dir_cleans_up_even_on_exception():
    d_ref = {}
    with pytest.raises(RuntimeError):
        with oe.scratch_dir("CASE_009", "DOC_001") as d:
            d_ref["d"] = d
            raise RuntimeError("boom")
    assert not d_ref["d"].exists()


def test_compare_prompt_asks_about_one_sided_extraneous_content():
    """known-gaps.md item 11: compare()'s original prompt only checked for
    conflicting facts, so a fabricated appendix present in only one reading
    (no conflicting fact anywhere) slipped through as 'agreed' on a real
    document. The prompt must explicitly ask about content one reading has
    that the other lacks, not just fact conflicts -- lock that in so a
    future edit can't silently drop it."""
    prompt = oe.COMPARE_PROMPT_TEMPLATE
    assert "the other" in prompt and "lacks" in prompt
    assert "hallucinated" in prompt.lower()


def test_scratch_dir_distinct_per_process_id(monkeypatch):
    """PID-tagged so a retry racing a stale process can't collide on one path."""
    monkeypatch.setattr(oe.os, "getpid", lambda: 111)
    with oe.scratch_dir("CASE_009", "DOC_001") as d1:
        path1 = d1
    monkeypatch.setattr(oe.os, "getpid", lambda: 222)
    with oe.scratch_dir("CASE_009", "DOC_001") as d2:
        path2 = d2
    assert path1 != path2
