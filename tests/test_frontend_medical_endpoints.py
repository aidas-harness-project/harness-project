"""Authenticated localhost medical API contract tests."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "frontend" / "backend"))

import main  # noqa: E402


def test_medical_review_list_authenticates_and_uses_dao_cli(monkeypatch):
    calls: list[tuple] = []
    ledger = {"case_id": "CASE_9001", "review_items": [], "wait_episodes": []}

    monkeypatch.setattr(
        main,
        "_authenticate_operator_request",
        lambda authorization, action, **kwargs: calls.append(
            ("authenticate", authorization, action, kwargs)
        )
        or (
            "test-token",
            {
                "actor_id": "OP_TEST",
                "display_name": "Test Operator",
                "medical_actor_id": "medical-test",
            },
        ),
        raising=False,
    )
    monkeypatch.setattr(
        main,
        "_require_case",
        lambda case_id: calls.append(("require_case", case_id)),
    )
    monkeypatch.setattr(
        main,
        "_read_dao_cli",
        lambda args, **kwargs: calls.append(
            ("read", args, kwargs.get("operator_token"))
        ) or ledger,
        raising=False,
    )

    assert main.medical_reviews("CASE_9001", "Bearer test-token") == ledger
    assert calls == [
        ("authenticate", "Bearer test-token", "medical_review", {}),
        ("require_case", "CASE_9001"),
        ("read", ["read-medical-review-ledger", "CASE_9001"], "test-token"),
    ]


def test_medical_variables_read_is_revision_pinned_and_hides_internal_paths(monkeypatch):
    calls: list[tuple] = []
    revision_sha = "a" * 64
    variables = {
        "case_id": "CASE_9001",
        "revision_sha": revision_sha,
        "file_path": "/internal/medical_variables.json",
        "domains": [],
        "variables": [],
        "medical_issues": [],
    }

    monkeypatch.setattr(
        main,
        "_authenticate_operator_request",
        lambda authorization, action, **kwargs: calls.append(
            ("authenticate", authorization, action, kwargs)
        )
        or ("test-token", {"medical_actor_id": "medical-test"}),
    )
    monkeypatch.setattr(
        main,
        "_require_case",
        lambda case_id: calls.append(("require_case", case_id)),
    )
    monkeypatch.setattr(
        main,
        "_read_dao_cli",
        lambda args, **_kwargs: calls.append(("read", args)) or variables,
    )

    assert main.medical_variables(
        "CASE_9001", revision_sha, "Bearer test-token",
    ) == {
        "case_id": "CASE_9001",
        "revision_sha": revision_sha,
        "domains": [],
        "variables": [],
        "medical_issues": [],
    }
    assert calls == [
        ("authenticate", "Bearer test-token", "medical_review", {}),
        ("require_case", "CASE_9001"),
        (
            "read",
            [
                "read-medical-variables",
                "CASE_9001",
                "--revision-sha",
                revision_sha,
            ],
        ),
    ]


def test_medical_close_validates_shape_and_invokes_authoritative_dao(monkeypatch):
    calls: list[tuple] = []
    actor = {
        "actor_id": "coordinator-1",
        "display_name": "Coordinator",
        "declared_role": "medical_coordinator",
        "specialty_code": "unspecified",
        "attested_at": "2026-08-05T12:00:00+00:00",
        "assertion_source": "authenticated_operator_policy",
        "operator_actor_id": "OP_TEST",
        "operator_policy_version": "operator_auth_policy.v0.1",
        "authentication_method": "bearer_sha256_policy",
    }

    monkeypatch.setattr(
        main,
        "_authenticated_medical_actor",
        lambda authorization: (
            calls.append(("authenticate", authorization))
            or ("test-token", actor, {"config_version": "medical_review_roles.v0.1"})
        ),
        raising=False,
    )
    monkeypatch.setattr(
        main,
        "_require_case",
        lambda case_id: calls.append(("require_case", case_id)),
    )
    monkeypatch.setattr(
        main,
        "_canonical_case_run_id",
        lambda case_id: calls.append(("run_id", case_id))
        or "RUN_20260805_120000",
        raising=False,
    )

    class Result:
        returncode = 0
        stdout = '{"action":"close","state":"closed"}'
        stderr = ""

    def run(command, **kwargs):
        calls.append((
            "subprocess",
            command,
            kwargs["env"].get("AIDAS_OPERATOR_TOKEN"),
            kwargs["timeout"],
        ))
        return Result()

    monkeypatch.setattr(main.subprocess, "run", run)

    body = main.MedicalReviewActionBody(
        data=None,
        reason="Reviewed response accepted for downstream use.",
        operation_id="medical:test-close-0001",
    )
    assert main.medical_review_action(
        "CASE_9001", "MRI_0001", "close", body, "Bearer test-token",
    ) == {"ok": True, "status": "committed"}
    assert calls == [
        ("authenticate", "Bearer test-token"),
        ("require_case", "CASE_9001"),
        ("run_id", "CASE_9001"),
        (
            "subprocess",
            [
                sys.executable,
                str(ROOT / "tools" / "dao.py"),
                "transition-medical-review",
                "CASE_9001",
                "MRI_0001",
                "--action",
                "close",
                "--reason",
                "Reviewed response accepted for downstream use.",
                "--operation-id",
                "medical:test-close-0001",
                "--held-by",
                "Coordinator",
                "--run-id",
                "RUN_20260805_120000",
            ],
            "test-token",
            1830,
        ),
    ]


def test_medical_action_exact_retry_reaches_dao_after_state_changes(monkeypatch):
    actor = {
        "actor_id": "coordinator-1",
        "display_name": "Coordinator",
        "declared_role": "medical_coordinator",
    }
    policy = {"config_version": "medical_review_roles.v0.1"}
    ledger = {
        "review_items": [{
            "review_item_id": "MRI_0001",
            "state": "answered",
            "current_request_id": "MRR_0001",
            "current_adjudication_id": None,
            "requests": [{
                "request_id": "MRR_0001",
                "current_response_id": "MRR_0001-R01",
            }],
        }],
    }
    dao_calls: list[list[str]] = []
    monkeypatch.setattr(
        main,
        "_authenticated_medical_actor",
        lambda _authorization: ("test-token", actor, policy),
    )
    monkeypatch.setattr(main, "_require_case", lambda _case_id: None)
    monkeypatch.setattr(main, "_read_dao_cli", lambda *_args, **_kwargs: ledger)
    def run_dao(args, **_kwargs):
        dao_calls.append(args)
        ledger["review_items"][0]["state"] = "closed"
        return {"ok": True, "status": "committed"}

    monkeypatch.setattr(main, "_run_medical_dao_cli", run_dao)
    body = main.MedicalReviewActionBody(
        reason="Reviewed response accepted.",
        operation_id="medical:test-lost-ack-0001",
    )

    assert main.medical_review_action(
        "CASE_9001", "MRI_0001", "close", body, "Bearer test-token",
    )["status"] == "committed"
    assert main.medical_review_action(
        "CASE_9001", "MRI_0001", "close", body, "Bearer test-token",
    )["status"] == "committed"
    assert len(dao_calls) == 2


def test_medical_cli_uses_anonymous_ingress_without_cleanup_path(monkeypatch, tmp_path):
    ingress = tmp_path / "ingress"
    ingress.mkdir()
    captured: dict = {}
    monkeypatch.setattr(main, "_canonical_case_run_id", lambda _case_id: "RUN_20260805_001")
    monkeypatch.setattr(main.medical_review_ledger, "_private_ingress_root", lambda: ingress)

    def inspect_ingress(command, **kwargs):
        descriptor_arg = command[command.index("--data-file") + 1]
        assert descriptor_arg.startswith("fd:")
        descriptor = int(descriptor_arg.removeprefix("fd:"))
        assert kwargs["pass_fds"] == (descriptor,)
        metadata = os.fstat(descriptor)
        assert metadata.st_nlink == 0
        assert metadata.st_mode & 0o777 == 0o600
        captured["payload"] = json.load(os.fdopen(os.dup(descriptor), encoding="utf-8"))
        captured["entries"] = list(ingress.iterdir())
        return type("Result", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    monkeypatch.setattr(main.subprocess, "run", inspect_ingress)
    assert main._run_medical_dao_cli(
        ["transition-medical-review", "CASE_9001", "MRI_0001", "--action", "close"],
        held_by="coordinator-1",
        operator_token="test-token",
        input_payload={"reason": "synthetic"},
    ) == {"ok": True, "status": "committed"}
    assert captured["payload"] == {"reason": "synthetic"}
    assert captured["entries"] == []


def test_medical_assignment_attestation_uses_server_actor_identity(monkeypatch):
    actor = {
        "actor_id": "coordinator-1",
        "display_name": "Coordinator",
        "declared_role": "medical_coordinator",
    }
    captured: dict = {}
    monkeypatch.setattr(
        main, "_authenticated_medical_actor",
        lambda _authorization: ("test-token", actor, {"config_version": "test"}),
    )
    monkeypatch.setattr(main, "_require_case", lambda _case_id: None)

    def run_dao(args, **kwargs):
        captured.update({"args": args, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(main, "_run_medical_dao_cli", run_dao)
    body = main.MedicalReviewActionBody(operation_id="medical:test-assign-0001", data={
        "request_id": "MRR_0001",
        "request_version": 1,
        "reviewer_actor_id": "reviewer-1",
        "package_review_attestation": {
            "reviewed_by_actor_id": "client-claimed-actor",
            "reviewed_at": "2026-08-05T12:00:00+00:00",
            "source_reinspection_confirmed": True,
            "focused_question_confirmed": True,
            "no_verdict_confirmed": True,
        },
    })

    assert main.medical_review_action(
        "CASE_9001", "MRI_0001", "assign", body, "Bearer test-token",
    ) == {"ok": True}
    assert captured["input_payload"]["package_review_attestation"][
        "reviewed_by_actor_id"
    ] == "coordinator-1"
    assert body.data["package_review_attestation"]["reviewed_by_actor_id"] == (
        "client-claimed-actor"
    )


def test_generic_frontend_routes_reject_medical_owned_contracts(isolated_dao):
    case_id = "CASE_9001"
    case_dir = isolated_dao / "outputs" / case_id
    case_dir.mkdir(parents=True)
    protected = (
        "medical_variables.json",
        "extracted_claim_fields.json",
        "_medical_review_ledger.json",
    )
    for name in protected:
        (case_dir / name).write_text('{"private": true}', encoding="utf-8")
        with pytest.raises(main.HTTPException) as contract_error:
            main.contract(case_id, name)
        assert contract_error.value.status_code == 403
        with pytest.raises(main.HTTPException) as report_error:
            main.report(case_id, name)
        assert report_error.value.status_code == 403

    (case_dir / "coverage_result.json").write_text(
        '{"status": "available"}', encoding="utf-8"
    )
    (case_dir / "screening_report.md").write_text(
        "# Synthetic screening", encoding="utf-8"
    )
    assert main.contract(case_id, "coverage_result.json") == {
        "status": "available"
    }
    assert main.report(case_id, "screening_report.md")["markdown"] == (
        "# Synthetic screening"
    )


def test_generic_frontend_routes_reject_hardlink_aliases(isolated_dao):
    case_id = "CASE_9001"
    case_dir = isolated_dao / "outputs" / case_id
    case_dir.mkdir(parents=True)
    protected = case_dir / "_medical_review_ledger.json"
    protected.write_text('{"private": true}', encoding="utf-8")
    contract_alias = case_dir / "ordinary.json"
    report_alias = case_dir / "ordinary.md"
    os.link(protected, contract_alias)
    os.link(protected, report_alias)

    with pytest.raises(main.HTTPException) as contract_error:
        main.contract(case_id, contract_alias.name)
    assert contract_error.value.status_code == 403
    with pytest.raises(main.HTTPException) as report_error:
        main.report(case_id, report_alias.name)
    assert report_error.value.status_code == 403


def test_medical_evidence_uses_explicit_historical_request_coordinates(monkeypatch):
    calls: list[tuple] = []
    ledger = {
        "case_id": "CASE_9001",
        "review_items": [{
            "review_item_id": "MRI_0001",
            "requests": [{
                "request_id": "MRR_0001",
                "versions": [
                    {
                        "request_version": 1,
                        "included_evidence_locator_ids": ["MEV_0002"],
                    },
                    {
                        "request_version": 2,
                        "included_evidence_locator_ids": ["MEV_0003"],
                    },
                ],
            }],
        }],
    }
    evidence = {
        "locator_id": "MEV_0002",
        "source_reference": {"document_id": "DOC_001", "page": 2},
    }

    monkeypatch.setattr(
        main,
        "_authenticate_operator_request",
        lambda authorization, action, **kwargs: calls.append(
            ("authenticate", authorization, action, kwargs)
        )
        or ("test-token", {"medical_actor_id": "medical-test"}),
    )
    monkeypatch.setattr(
        main,
        "_require_case",
        lambda case_id: calls.append(("require_case", case_id)),
    )

    def read(args, **_kwargs):
        calls.append(("read", args))
        return ledger if args[0] == "read-medical-review-ledger" else evidence

    monkeypatch.setattr(main, "_read_dao_cli", read)

    assert main.medical_review_evidence(
        "CASE_9001",
        "MRI_0001",
        "MRR_0001",
        1,
        "MEV_0002",
        "Bearer test-token",
    ) == evidence
    assert calls == [
        ("authenticate", "Bearer test-token", "medical_review", {}),
        ("require_case", "CASE_9001"),
        ("read", ["read-medical-review-ledger", "CASE_9001"]),
        (
            "read",
            [
                "read-medical-review-evidence",
                "CASE_9001",
                "MRI_0001",
                "MRR_0001",
                "1",
                "MEV_0002",
            ],
        ),
    ]


def test_medical_actor_is_derived_from_authenticated_policy(monkeypatch):
    identity = {
        "actor_id": "OP_TEST",
        "display_name": "Operator",
        "role": "medical_coordinator",
        "policy_version": "operator_auth_policy.v0.1",
        "authentication_method": "bearer_sha256_policy",
        "medical_actor_id": "coordinator-1",
    }
    policy = {
        "named_actors": [{
            "actor_id": "coordinator-1",
            "display_name": "Coordinator",
            "declared_role": "medical_coordinator",
            "specialty_code": "unspecified",
        }],
    }
    monkeypatch.setattr(
        main,
        "_authenticate_operator_request",
        lambda authorization, action: ("test-token", identity),
    )
    monkeypatch.setattr(
        main.medical_review_ledger,
        "_load_role_policy",
        lambda dao_module: policy,
    )
    monkeypatch.setattr(main.dao, "now_iso", lambda: "2026-08-05T12:00:00+00:00")

    token, actor, loaded_policy = main._authenticated_medical_actor("Bearer test-token")

    assert token == "test-token"
    assert loaded_policy is policy
    assert actor == {
        "actor_id": "coordinator-1",
        "display_name": "Coordinator",
        "declared_role": "medical_coordinator",
        "specialty_code": "unspecified",
        "attested_at": "2026-08-05T12:00:00+00:00",
        "assertion_source": "authenticated_operator_policy",
        "operator_actor_id": "OP_TEST",
        "operator_policy_version": "operator_auth_policy.v0.1",
        "authentication_method": "bearer_sha256_policy",
    }


def test_unknown_medical_action_is_rejected_before_ledger_read(monkeypatch):
    monkeypatch.setattr(
        main,
        "_authenticated_medical_actor",
        lambda authorization: ("test-token", {"display_name": "Coordinator"}, {}),
    )
    monkeypatch.setattr(main, "_require_case", lambda case_id: None)
    monkeypatch.setattr(
        main,
        "_read_dao_cli",
        lambda args, **_kwargs: pytest.fail("unknown action must not read the ledger"),
    )

    with pytest.raises(main.HTTPException) as exc_info:
        main.medical_review_action(
            "CASE_9001",
            "MRI_0001",
            "delete_everything",
            main.MedicalReviewActionBody(
                operation_id="medical:test-unknown-action-0001"
            ),
            "Bearer test-token",
        )

    assert exc_info.value.status_code == 400


def test_medical_mutation_stages_anonymous_private_payload(tmp_path, monkeypatch):
    ingress = tmp_path / "ingress"
    staged_descriptor = None
    monkeypatch.setattr(
        main.medical_review_ledger,
        "MEDICAL_REVIEW_INGRESS_ROOT",
        ingress,
    )
    monkeypatch.setattr(
        main,
        "_canonical_case_run_id",
        lambda case_id: "RUN_20260805_120000",
    )

    class Result:
        returncode = 0
        stdout = '{"action":"provide_information","state":"decision_pending"}'
        stderr = ""

    def run(command, **kwargs):
        nonlocal staged_descriptor
        index = command.index("--data-file")
        descriptor_arg = command[index + 1]
        assert descriptor_arg.startswith("fd:")
        staged_descriptor = int(descriptor_arg.removeprefix("fd:"))
        assert kwargs["pass_fds"] == (staged_descriptor,)
        metadata = os.fstat(staged_descriptor)
        assert metadata.st_nlink == 0
        assert metadata.st_mode & 0o777 == 0o600
        assert json.load(os.fdopen(os.dup(staged_descriptor), encoding="utf-8")) == {
            "information": "Synthetic clarification",
        }
        assert list(ingress.iterdir()) == []
        assert kwargs["env"][main.operator_auth.TOKEN_ENV] == "test-token"
        return Result()

    monkeypatch.setattr(main.subprocess, "run", run)

    assert main._run_medical_dao_cli(
        [
            "transition-medical-review",
            "CASE_9001",
            "MRI_0001",
            "--action",
            "provide_information",
        ],
        held_by="Coordinator",
        operator_token="test-token",
        input_payload={"information": "Synthetic clarification"},
    ) == {"ok": True, "status": "committed"}
    assert staged_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(staged_descriptor)


def test_historical_evidence_rejects_unlisted_locator_before_dao_read(monkeypatch):
    reads: list[list[str]] = []
    ledger = {
        "review_items": [{
            "review_item_id": "MRI_0001",
            "requests": [{
                "request_id": "MRR_0001",
                "versions": [{
                    "request_version": 1,
                    "included_evidence_locator_ids": ["MEV_0001"],
                }],
            }],
        }],
    }
    monkeypatch.setattr(
        main,
        "_authenticate_operator_request",
        lambda authorization, action: (
            "test-token",
            {"medical_actor_id": "coordinator-1"},
        ),
    )
    monkeypatch.setattr(main, "_require_case", lambda case_id: None)

    def read(args, **_kwargs):
        reads.append(args)
        return ledger

    monkeypatch.setattr(main, "_read_dao_cli", read)

    with pytest.raises(main.HTTPException) as exc_info:
        main.medical_review_evidence(
            "CASE_9001",
            "MRI_0001",
            "MRR_0001",
            1,
            "MEV_9999",
            "Bearer test-token",
        )

    assert exc_info.value.status_code == 404
    assert reads == [["read-medical-review-ledger", "CASE_9001"]]


def test_assign_delegates_foreign_request_check_to_authoritative_dao(monkeypatch):
    actor = {
        "actor_id": "coordinator-1",
        "display_name": "Coordinator",
        "declared_role": "medical_coordinator",
        "specialty_code": "unspecified",
    }
    monkeypatch.setattr(
        main, "_authenticated_medical_actor",
        lambda _authorization: ("test-token", actor, {}),
    )
    monkeypatch.setattr(main, "_require_case", lambda _case_id: ROOT)
    captured: dict = {}

    def run_dao(args, **kwargs):
        captured.update({"args": args, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(main, "_run_medical_dao_cli", run_dao)
    body = main.MedicalReviewActionBody(operation_id="medical:test-foreign-0001", data={
        "request_id": "MRR_9999",
        "request_version": 999,
        "reviewer_actor_id": "doctor-1",
        "package_review_attestation": {
            "reviewed_by_actor_id": "coordinator-1",
            "reviewed_at": "2026-08-05T00:00:00Z",
            "source_reinspection_confirmed": True,
            "focused_question_confirmed": True,
            "no_verdict_confirmed": True,
        },
    })
    assert main.medical_review_action(
        "CASE_9001", "MRI_0001", "assign", body, "Bearer test-token",
    ) == {"ok": True}
    assert captured["input_payload"]["request_id"] == "MRR_9999"
    assert captured["args"][-2:] == ["--operation-id", "medical:test-foreign-0001"]
