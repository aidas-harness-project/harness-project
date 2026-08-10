"""Truthfulness contract for the medical technical baseline documentation."""
from pathlib import Path
import sys

import dao
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_medical_docs_separate_implemented_tested_and_operational_states():
    requirements = _text("docs/medical-appropriateness-screening-requirements.md")
    deferrals = _text("docs/medical-appropriateness-screening-deferrals.md")
    pipeline = _text("pipeline.md")
    readme = _text("README.md")
    frontend_readme = _text("frontend/README.md")
    known_gaps = _text("known-gaps.md")
    human_review_ui = _text("frontend/web/src/components/HumanReviewPanel.jsx")
    pipeline_definition = _text("frontend/web/src/pipelineDefinition.js")
    static_case_data = _text("frontend/web/src/staticCaseData.js")
    backend = _text("frontend/backend/main.py")
    run_state_schema = _text("schemas/run_state.schema.json")
    critic = _text(".claude/agents/critic.md")

    for phrase in (
        "Current technical implementation status",
        "synthetic fixtures",
        "localhost-only",
        "not operationally activated",
        "submits the approved structured lifecycle actions",
        "operator policy is disabled and unapproved",
    ):
        assert phrase in requirements, phrase
    assert "Any future lifecycle mutation surface" not in requirements
    assert "authenticated mutation remain deferred" not in requirements

    for phrase in (
        "bearer-token authentication",
        "disabled-by-default operator policy",
        "no approved operator tokens",
        "no named human actors",
    ):
        assert phrase in deferrals, phrase
    assert "actor metadata is explicitly not authentication" not in deferrals

    for phrase in (
        "check-medical-reviews-clear",
        "read-medical-review-outcomes",
        "Capability versus activation",
    ):
        assert phrase in pipeline, phrase

    assert "medical-appropriateness-screening-requirements.md" in readme
    assert "medical-appropriateness-screening-deferrals.md" in readme
    assert "session-only" in frontend_readme
    assert "disabled-by-default" in frontend_readme
    assert "Medical appropriateness technical baseline" in known_gaps
    assert "not operationally activated" in known_gaps
    for stale in (
        "evaluation may read ground truth",
        "opening D1's versioned gate",
        "D1's actual evaluation gate",
        "Refuses to fork if any `.lock` file is present",
    ):
        assert stale not in "\n".join(
            (human_review_ui, pipeline_definition, backend, known_gaps)
        )
    assert 'key: "human-review-v1"' in pipeline_definition
    assert 'key: "human-review-v2"' in pipeline_definition
    assert 'key: "evaluation-' not in pipeline_definition
    assert "isolated Unit 11" in human_review_ui
    assert '"evaluation"' not in run_state_schema
    assert '"evaluation-v1"' not in static_case_data
    assert '"evaluation_result.json"' not in static_case_data
    assert "evaluation may access ground truth" not in static_case_data
    assert "Downstream: human review, then `evaluation`" not in critic
    assert "review the proposed raw/ground_truth classification yourself" not in backend
    assert "A genuine human reviews every pending intake entry" in backend


def test_authoritative_lifecycle_cli_surface_matches_argparse(
    isolated_dao, monkeypatch, capsys
):
    requirements = _text("docs/medical-appropriateness-screening-requirements.md")
    expected_commands = (
        "read-medical-review-ledger CASE_ID",
        "read-medical-review-evidence CASE_ID REVIEW_ITEM_ID REQUEST_ID REQUEST_VERSION LOCATOR_ID",
        "read-medical-review-outcomes CASE_ID --caller-stage STAGE --run-id RUN_ID",
        "open-medical-review-item CASE_ID --issue-id MCI_NNNN --decision-owner {policy|human} --operation-id OPERATION_ID --held-by NAME --run-id RUN_ID",
        "record-medical-referral-decision CASE_ID REVIEW_ITEM_ID DECISION_FILE --operation-id OPERATION_ID --held-by NAME --run-id RUN_ID",
        "provide-medical-review-information CASE_ID REVIEW_ITEM_ID --reason TEXT --operation-id OPERATION_ID --held-by NAME --run-id RUN_ID",
        "transition-medical-review CASE_ID REVIEW_ITEM_ID --action ACTION [--data-file PATH] [--reason TEXT] --operation-id OPERATION_ID --held-by NAME --run-id RUN_ID",
        "check-medical-reviews-clear CASE_ID",
        "reconcile-medical-review-waits CASE_ID --operation-id OPERATION_ID --held-by NAME --run-id RUN_ID",
    )
    for command in expected_commands:
        assert command in requirements
    assert "--actor-file" not in requirements
    assert "--decision-file" not in requirements

    concrete_invocations = (
        ["read-medical-review-ledger", "CASE_9001"],
        [
            "read-medical-review-evidence", "CASE_9001", "MRI_0001",
            "MRR_0001", "1", "LOC_0001",
        ],
        [
            "read-medical-review-outcomes", "CASE_9001",
            "--caller-stage", "screening_report",
            "--run-id", "RUN_20260723_001",
        ],
        [
            "open-medical-review-item", "CASE_9001",
            "--issue-id", "MCI_0001", "--decision-owner", "policy",
            "--operation-id", "medical:docs-open-0001",
            "--held-by", "synthetic-test", "--run-id", "RUN_20260723_001",
        ],
        [
            "record-medical-referral-decision", "CASE_9001", "MRI_0001",
            "synthetic-decision.json",
            "--operation-id", "medical:docs-decision-0001",
            "--held-by", "synthetic-test",
            "--run-id", "RUN_20260723_001",
        ],
        [
            "provide-medical-review-information", "CASE_9001", "MRI_0001",
            "--reason", "Synthetic reason.",
            "--operation-id", "medical:docs-information-0001",
            "--held-by", "synthetic-test",
            "--run-id", "RUN_20260723_001",
        ],
        [
            "transition-medical-review", "CASE_9001", "MRI_0001",
            "--action", "close", "--reason", "Synthetic reason.",
            "--operation-id", "medical:docs-transition-0001",
            "--held-by", "synthetic-test", "--run-id", "RUN_20260723_001",
        ],
        ["check-medical-reviews-clear", "CASE_9001"],
        [
            "reconcile-medical-review-waits", "CASE_9001",
            "--operation-id", "medical:docs-reconcile-0001",
            "--held-by", "synthetic-test", "--run-id", "RUN_20260723_001",
        ],
    )
    for invocation in concrete_invocations:
        monkeypatch.setattr(sys, "argv", ["dao.py", *invocation])
        with pytest.raises(SystemExit) as exited:
            dao.main()
        assert exited.value.code in {0, 1}, invocation
        captured = capsys.readouterr()
        assert "usage:" not in captured.err.lower(), (invocation, captured.err)
        assert "unrecognized arguments" not in captured.err.lower(), (
            invocation,
            captured.err,
        )
