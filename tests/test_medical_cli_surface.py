"""CLI surface required by the first reconstructed medical loop."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"


def test_first_loop_medical_commands_are_exposed_by_dao_cli():
    commands = (
        "write-medical-variables",
        "read-medical-variables",
        "check-medical-reviews-clear",
        "reconcile-medical-review-waits",
        "open-medical-review-item",
        "read-medical-review-ledger",
        "read-medical-review-evidence",
        "read-medical-review-outcomes",
    )
    for command in commands:
        result = subprocess.run(
            [sys.executable, str(DAO), command, "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            command,
            result.stdout,
            result.stderr,
        )


def test_transition_command_exposes_all_declared_medical_actions():
    result = subprocess.run(
        [sys.executable, str(DAO), "transition-medical-review", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    for action in (
        "provide_information",
        "assign",
        "request_information",
        "supplement_package",
        "reassign",
        "submit_response",
        "amend_response",
        "withdraw_response",
        "flag_conflict",
        "adjudicate",
        "cancel",
        "close",
        "reopen",
    ):
        assert action in result.stdout
