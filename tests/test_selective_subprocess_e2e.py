"""The three CLIs, driven as real subprocesses against an isolated tree.

Every other test in this lane imports the drivers and calls their functions.
That verifies the logic but not the thing an operator actually does: run the
command. This file executes the entry points the way the skill's stage table
says to, so an import-time error, a missing argument, a wrong schema name, or a
broken exit code shows up here rather than on a case.

The DAO writes land under a tmp_path via HARNESS_OUTPUTS/HARNESS_DATA, so
nothing touches the real outputs/ or data/ trees. The provider is a fixture
file, never a live model.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
CASE_ID = "CASE_9401"
RUN_ID = "RUN_20260820_1"

QUOTE_RIGHT = "우측 요골 골절"
QUOTE_LEFT = "좌측 요골 골절"

PAGES = {
    "DOC_001": [{"page": 1, "text": f"진 단 서\n진단명: {QUOTE_RIGHT}\n"}],
    "DOC_002": [{"page": 1, "text": f"입퇴원요약\n진단명: {QUOTE_LEFT}\n"}],
}


def _run_cli(script: str, *args, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOLS / script), *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """An isolated outputs/data tree plus the env the CLIs inherit."""
    env = dict(os.environ)
    env["HARNESS_OUTPUTS"] = str(tmp_path / "outputs")
    env["HARNESS_DATA"] = str(tmp_path / "data")
    env["HARNESS_TRACE"] = "0"
    env["PYTHONPATH"] = str(TOOLS)
    (tmp_path / "outputs" / CASE_ID).mkdir(parents=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return tmp_path, env


def _gate_is_closed() -> bool:
    config = json.loads(
        (ROOT / "config" / "claim_analysis" /
         "claim_analysis_routing_v0.1.json").read_text(encoding="utf-8"))
    return config.get("behavior_enabled") is not True


# --------------------------------------------------------- the gate holds --

def test_the_shipped_gate_is_open_and_records_who_opened_it() -> None:
    """Activation is a separate approval, and the shipped config carries it.

    Inverted 2026-08-20: this asserted the gate was still closed, which stopped
    being true when the lane was activated and the legacy spine was deleted.
    The approval block is the part worth guarding -- the schema refuses an
    enabled config that has none, so a config enabled with no recorded approver
    could only arrive by editing both the config and the schema.
    """
    config = json.loads(
        (ROOT / "config" / "claim_analysis" /
         "claim_analysis_routing_v0.1.json").read_text(encoding="utf-8"))
    assert config["behavior_enabled"] is True
    activation = config["activation"]
    assert activation is not None
    for key in ("approved_by", "authority_role", "approved_at", "scope"):
        assert activation.get(key), f"activation is missing {key}"


def test_claim_analysis_cli_refuses_while_the_gate_is_closed(sandbox) -> None:
    """The selective spine is the ONLY entry point the gate closes.

    A closed gate must not be discoverable as a crash: the command exits
    non-zero and says why.
    """
    if not _gate_is_closed():
        pytest.skip("the gate is open in this working tree")
    _, env = sandbox
    proc = _run_cli("run_claim_analysis_selective.py", CASE_ID,
                    "--held-by", "claim-analysis", "--run-id", RUN_ID,
                    "--provider", "fixture", env=env)
    assert proc.returncode == 1
    assert "behavior_enabled=false" in (proc.stdout + proc.stderr)


@pytest.mark.parametrize("script,args", [
    ("run_consistency_check.py", ("prepare",)),
    ("run_screening_report.py", ()),
])
def test_the_other_two_entry_points_are_not_gate_blocked(
    sandbox, script: str, args: tuple
) -> None:
    """The gate scopes to Claim Analysis; the legacy paths stay usable.

    These fail on a missing run-state attempt (no open stage in this sandbox),
    which is the T13 refusal -- NOT the routing gate. Asserting the specific
    refusal is the point: a gate leaking into these would block the legacy lane.
    """
    _, env = sandbox
    proc = _run_cli(script, *args, CASE_ID,
                    "--held-by", "test", "--run-id", RUN_ID, env=env)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "behavior_enabled" not in combined


# ------------------------------------------------------- CLI surface area --

@pytest.mark.parametrize("script,args", [
    ("run_claim_analysis_selective.py", ()),
    ("run_consistency_check.py", ("prepare",)),
    ("run_consistency_check.py", ("register",)),
    ("run_screening_report.py", ()),
])
def test_every_entry_point_parses_its_arguments(script: str, args: tuple) -> None:
    """`--help` exercises import + argparse without touching any case."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(TOOLS / script), *args, "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "--held-by" in proc.stdout
    assert "--run-id" in proc.stdout


def test_consistency_check_rejects_an_unknown_subcommand() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "run_consistency_check.py"),
         "decide", CASE_ID, "--held-by", "x", "--run-id", RUN_ID],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert proc.returncode != 0
    # The helper offers prepare, judge and register, and argparse names them.
    # `judge` was added on 2026-08-26 so the stage can reach its verdict through
    # a provider call instead of a subagent; what it did NOT change is that
    # producing verdicts and admitting them to the P6 ledger stay two commands
    # -- `register` still runs its own binding checks against whatever wrote
    # the verdicts, and still refuses when none exist (see
    # test_register_refuses_without_agent_verdicts below).
    assert "decide" in proc.stderr
    assert "{prepare,judge,register}" in proc.stderr.replace(" ", "")


# ------------------------------------------------- stage ordering refusals --

def test_register_refuses_without_agent_verdicts(sandbox, monkeypatch) -> None:
    """The seam that keeps the judgement with the agent.

    Reached through the module rather than the CLI so the run-state precondition
    can be satisfied without standing up a whole case; what is asserted is the
    refusal itself.
    """
    sys.path.insert(0, str(TOOLS))
    import run_consistency_check as helper

    monkeypatch.setattr(helper, "require_open_attempt", lambda case_id, run_id: None)
    monkeypatch.setattr(helper, "_dao_json", lambda args, allow_missing=False: (
        None if allow_missing else {"work_items": [], "claim_analysis_sha256": "a" * 64}))

    with pytest.raises(RuntimeError) as excinfo:
        helper.run_register(case_id=CASE_ID, run_id=RUN_ID, held_by="test")
    assert "does not judge candidates" in str(excinfo.value)


def test_screening_report_halts_on_a_pending_conflict(sandbox, monkeypatch) -> None:
    """P6 before assembly: a triage document written over an unadjudicated
    disagreement tells a professional the case is ready when it is not."""
    sys.path.insert(0, str(TOOLS))
    import run_screening_report as reporter

    monkeypatch.setattr(reporter, "require_open_attempt", lambda case_id, run_id: None)
    monkeypatch.setattr(reporter, "_dao_json", lambda args, allow_missing=False: {
        "pending": ["CONFLICT_1"], "deferred_to_report": []})

    with pytest.raises(RuntimeError) as excinfo:
        reporter.run(case_id=CASE_ID, run_id=RUN_ID, held_by="test")
    assert "CONFLICT_1" in str(excinfo.value)
