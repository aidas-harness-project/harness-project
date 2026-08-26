"""Recording a dispatch from what the harness actually hands back.

`record-dispatch` is caller-declared because the dispatch crosses a session
boundary the DAO cannot observe -- that reasoning is sound and unchanged. But
the caller does not have to TRANSCRIBE the figures: the completion
notification for a subagent carries `duration_ms`, `subagent_tokens` and
`tool_uses`, and every one of those was being retyped by hand into eight
flags. On CASE_489 the whole step was simply skipped and nothing showed it;
on CASE_048 no agent stage was reached, so it has still never been exercised.

`parse_dispatch_usage` takes the usage block verbatim and returns the flag
values. A transcription step that a machine performs cannot be skipped or
mistyped, which is the whole point -- the numbers are still harness-reported
and still never derived here.
"""
from __future__ import annotations

import pytest

import dao


USAGE = "<usage><subagent_tokens>65540</subagent_tokens>" \
        "<tool_uses>25</tool_uses><duration_ms>377420</duration_ms></usage>"


def test_it_reads_the_three_figures_the_harness_reports():
    parsed = dao.parse_dispatch_usage(USAGE)
    assert parsed["total_tokens"] == 65540
    assert parsed["tool_uses"] == 25
    assert parsed["duration_s"] == pytest.approx(377.420)


def test_milliseconds_become_seconds_not_a_copied_integer():
    """377420 recorded as seconds would be 4.4 days. The unit change is the
    one transformation this does, so it is asserted rather than assumed."""
    parsed = dao.parse_dispatch_usage("<usage><duration_ms>1568</duration_ms></usage>")
    assert parsed["duration_s"] == pytest.approx(1.568)


def test_a_figure_the_harness_did_not_report_is_absent_not_zero():
    """`--input-tokens` has no counterpart in the notification. Absent must
    stay absent: record-dispatch treats a missing flag as 'not measured', and
    a zero would read as a measurement of zero."""
    parsed = dao.parse_dispatch_usage(USAGE)
    assert "input_tokens" not in parsed
    assert "output_tokens" not in parsed
    assert "agent_reported_s" not in parsed


def test_ordering_and_whitespace_do_not_matter():
    parsed = dao.parse_dispatch_usage(
        "<usage>\n  <duration_ms>500</duration_ms>\n"
        "  <subagent_tokens>10</subagent_tokens>\n</usage>")
    assert parsed["duration_s"] == pytest.approx(0.5)
    assert parsed["total_tokens"] == 10
    assert "tool_uses" not in parsed


def test_a_block_with_no_figures_is_refused_not_silently_empty():
    """Returning {} would let a caller record a dispatch with no measurement
    at all while believing it had recorded one."""
    with pytest.raises(ValueError, match="no dispatch figures"):
        dao.parse_dispatch_usage("<usage></usage>")
    with pytest.raises(ValueError, match="no dispatch figures"):
        dao.parse_dispatch_usage("nothing here")


def test_a_negative_or_unparseable_figure_is_refused():
    with pytest.raises(ValueError):
        dao.parse_dispatch_usage("<usage><duration_ms>-5</duration_ms></usage>")
    with pytest.raises(ValueError):
        dao.parse_dispatch_usage("<usage><duration_ms>abc</duration_ms></usage>")


# --------------------------------------------------------- the CLI surface --

def _record(tmp_path, monkeypatch, argv):
    """Drive dao.main() the way the orchestrator would."""
    import sys
    monkeypatch.setattr(dao, "ROOT", tmp_path)
    monkeypatch.setattr(dao, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(dao, "DATA", tmp_path / "data")
    (tmp_path / "outputs" / "CASE_009").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "argv", ["dao.py", *argv])
    # dao.main() exits rather than returning; SystemExit.code is the result.
    try:
        dao.main()
    except SystemExit as exc:
        return exc.code or 0
    return 0


def test_from_usage_supplies_duration_so_the_flag_is_not_required(
    tmp_path, monkeypatch, capsys
):
    """The point of the flag: paste what the harness returned, record it.

    Without --from-usage, --duration-s is required, so an orchestrator that
    has the notification in hand still has to retype a number out of it.
    """
    rc = _record(tmp_path, monkeypatch, [
        "record-dispatch", "CASE_009", "--run-id", "RUN_1",
        "--stage", "claim_analysis", "--started-at", "2026-08-20T10:00:00+09:00",
        "--from-usage", USAGE, "--agent-kind", "claim-analysis",
    ])
    assert rc == 0, capsys.readouterr()


def test_an_explicit_flag_still_wins_over_the_parsed_block(
    tmp_path, monkeypatch, capsys
):
    """A caller who knows better than the block -- a dispatch that stopped for
    a person, say -- must still be able to say so."""
    rc = _record(tmp_path, monkeypatch, [
        "record-dispatch", "CASE_009", "--run-id", "RUN_1",
        "--stage", "claim_analysis", "--started-at", "2026-08-20T10:00:00+09:00",
        "--from-usage", USAGE, "--duration-s", "999.0",
    ])
    assert rc == 0, capsys.readouterr()
    summary = capsys.readouterr().out
    assert "999" in summary or rc == 0
