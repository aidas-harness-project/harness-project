"""Canonical medical-variable publication and read boundary.

The public CLI and all filesystem primitives remain owned by ``tools/dao.py``.
This module isolates medical-specific validation, revision, and compatibility
projection behavior so reconstruction does not expand the generic DAO monolith.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
import sys

from _validation import load_registry, validate_instance
from medical_contracts import project_legacy_medical_fields, validate_medical_variables


MEDICAL_OWNED_CONTRACTS = {
    "medical_variables.json",
    "extracted_claim_fields.json",
    "_medical_review_ledger.json",
}


def normalized_case_relative_target(dao, case_id: str, filename: str) -> str:
    if (
        not isinstance(filename, str)
        or not filename
        or "\x00" in filename
        or Path(filename).is_absolute()
    ):
        raise ValueError(f"unsafe path component {filename!r}")
    base = dao.case_dir(case_id).resolve()
    target = (base / filename).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"path escapes {base} -- refusing {target}")
    return target.relative_to(base).as_posix()


def require_generic_target_allowed(dao, case_id: str, filename: str) -> None:
    normalized = normalized_case_relative_target(dao, case_id, filename)
    parts = Path(normalized).parts
    if normalized in MEDICAL_OWNED_CONTRACTS or (
        parts and parts[0] == "_medical_variable_revisions"
    ):
        raise ValueError(
            f"{filename} is owned by a purpose-built medical DAO command"
        )


def require_generic_read_allowed(dao, case_id: str, filename: str) -> None:
    normalized = normalized_case_relative_target(dao, case_id, filename)
    parts = Path(normalized).parts
    if normalized == "_medical_review_ledger.json" or (
        parts and parts[0] == "_medical_variable_revisions"
    ):
        raise ValueError(
            f"{filename} is owned by a purpose-built medical DAO command"
        )
    target = dao.case_dir(case_id) / normalized
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"cannot inspect generic read target {filename}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(
            f"{filename} is owned by a purpose-built medical DAO command"
        )


def medical_variables_path(dao, case_id: str) -> Path:
    return dao.case_dir(case_id) / "medical_variables.json"


def revisions_dir(dao, case_id: str) -> Path:
    return dao._require_within(dao.case_dir(case_id), "_medical_variable_revisions")


def revision_namespace_error(path: Path) -> str | None:
    """Return an error unless an existing revision namespace is a real directory."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot inspect immutable medical revision namespace: {exc}"
    if not stat.S_ISDIR(metadata.st_mode):
        return "immutable medical revision namespace is not a real directory"
    return None


def canonical_json_bytes(data: dict) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_dependencies(dao, case_id: str) -> tuple[dict | None, ...]:
    directory = dao.case_dir(case_id)
    return (
        dao.load_json(directory / "document_manifest.json"),
        dao.load_json(directory / "page_chunks.json"),
        dao.validated_conflict_ledger(case_id),
        dao.load_json(directory / "case_type_result.json"),
    )


def _load_candidate(dao, args) -> dict:
    path = Path(args.data_file)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load medical candidate: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("medical candidate must be one JSON object")
    return value


def publish(dao, args) -> int:
    """Fence publication to the canonical run owner, then publish atomically."""
    state_target = dao.run_state_path(args.case_id)
    owned_lock, existing_lock = dao.acquire_owned_lock_blocking(
        state_target,
        args.held_by,
        args.run_id,
        "authorize canonical medical publication run owner",
    )
    if existing_lock is not None:
        print(
            f"LOCKED: held_by={existing_lock['held_by']} "
            f"run_id={existing_lock['run_id']}"
        )
        return 1
    assert owned_lock is not None
    try:
        try:
            state = dao.validated_run_state(
                args.case_id, allow_missing=True
            )
        except ValueError as exc:
            print(f"BLOCKED: {exc}")
            return 1
        owner = state.get("run_id")
        current_target = medical_variables_path(dao, args.case_id)
        if current_target.exists() or current_target.is_symlink():
            variables, error = load_revision(dao, args.case_id, None)
            if (
                error
                or variables is None
                or variables.get("run_id") != args.run_id
            ):
                print(
                    "BLOCKED: medical publication does not match the "
                    "canonical revision run owner"
                )
                return 1
        if owner not in {None, args.run_id}:
            print("BLOCKED: medical publication does not match the canonical run owner")
            return 1
        state["run_id"] = args.run_id
        # Persist applicability before the publication commit point. A crash
        # after the medical pointer is published can therefore never make P11
        # disappear by leaving an old run state behind.
        state["medical_review_adopted"] = True
        errors = dao._schema_check(state, "run_state.schema.json")
        if errors:
            print("FAIL: canonical medical-adoption state is invalid")
            for error in errors:
                print(f"  - {error}")
            return 1
        try:
            dao.save_run_state(args.case_id, state)
        except OSError as exc:
            print(f"FAIL: cannot establish canonical medical adoption: {exc}")
            return 1
        return _publish_owned(dao, args)
    finally:
        dao.release_owned_lock(owned_lock)


def _publish_owned(dao, args) -> int:
    """Validate and publish current, revision, projection, and initial ledger."""
    import medical_review_ledger as ledger_owner

    target = medical_variables_path(dao, args.case_id)
    existing_lock = dao.acquire_lock_blocking(
        target,
        args.held_by,
        args.run_id,
        getattr(args, "purpose", None) or "publish medical variables",
    )
    if existing_lock is not None:
        print(
            f"LOCKED: held_by={existing_lock['held_by']} "
            f"run_id={existing_lock['run_id']} "
            f"since={existing_lock['started_at']} "
            f"purpose={existing_lock['purpose']}"
        )
        return 1

    ledger_target = ledger_owner.ledger_path(dao, args.case_id)
    ledger_lock_acquired = False
    try:
        existing_ledger_lock = dao.acquire_lock_blocking(
            ledger_target,
            args.held_by,
            args.run_id,
            getattr(args, "purpose", None)
            or "publish medical variables with review-ledger serialization",
        )
        if existing_ledger_lock is not None:
            print(
                f"LOCKED: held_by={existing_ledger_lock['held_by']} "
                f"run_id={existing_ledger_lock['run_id']} "
                f"since={existing_ledger_lock['started_at']} "
                f"purpose={existing_ledger_lock['purpose']}"
            )
            return 1
        ledger_lock_acquired = True
        try:
            candidate = _load_candidate(dao, args)
            config = json.loads(
                Path(dao.MEDICAL_STRUCTURING_CONFIG).read_text(encoding="utf-8")
            )
            projection_config = json.loads(
                Path(dao.MEDICAL_PROJECTION_CONFIG).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"FAIL: cannot load medical candidate/configuration: {exc}")
            return 1

        if candidate.get("case_id") != args.case_id:
            print("FAIL: candidate case_id does not match requested case")
            return 1
        if candidate.get("run_id") != args.run_id:
            print("FAIL: candidate run_id does not match --run-id")
            return 1

        try:
            dao.ensure_conflict_ledger(
                args.case_id, args.held_by, args.run_id
            )
            manifest, page_chunks, conflict_ledger, case_type_result = (
                _load_dependencies(dao, args.case_id)
            )
        except ValueError as exc:
            print(f"FAIL: canonical conflict ledger is invalid: {exc}")
            return 1
        if any(
            dependency is None
            for dependency in (
                manifest,
                page_chunks,
                conflict_ledger,
                case_type_result,
            )
        ):
            print(
                "FAIL: document_manifest.json, page_chunks.json, "
                "_conflict_ledger.json, and canonical case_type_result.json "
                "must exist before medical publication"
            )
            return 1
        assert manifest is not None
        assert page_chunks is not None
        assert conflict_ledger is not None
        assert case_type_result is not None

        dependency_schemas = (
            (manifest, "document_manifest.schema.json"),
            (page_chunks, "page_chunks.schema.json"),
            (conflict_ledger, "conflict_ledger.schema.json"),
            (case_type_result, "case_type_result.schema.json"),
        )
        for dependency, schema_name in dependency_schemas:
            if dao._schema_check(dependency, schema_name):
                print(
                    "FAIL: canonical "
                    f"{schema_name.removesuffix('.schema.json')}.json is invalid"
                )
                return 1

        errors = validate_medical_variables(
            candidate,
            manifest=manifest,
            page_chunks=page_chunks,
            conflict_ledger=conflict_ledger,
            config=config,
            canonical_case_type=case_type_result["case_type"],
        )
        if errors:
            print("FAIL: medical variable validation errors -- not written:")
            for error in errors:
                print(f"  - {error}")
            return 1

        canonical = canonical_json_bytes(candidate)
        digest = hashlib.sha256(canonical).hexdigest()
        try:
            projected_fields = project_legacy_medical_fields(
                candidate,
                projection_config=projection_config,
            )
        except ValueError as exc:
            print(f"FAIL: compatibility projection is unavailable: {exc}")
            return 1

        projection = {
            "case_id": args.case_id,
            "run_id": args.run_id,
            "component": "claim-analysis",
            "status": "success",
            "projection_mode": "canonical_medical_projection",
            "medical_revision_sha": digest,
            "fields": projected_fields,
        }
        projection_errors = dao._schema_check(
            projection,
            "extracted_claim_fields.schema.json",
        )
        if projection_errors:
            print("FAIL: deterministic compatibility projection is invalid")
            for error in projection_errors:
                print(f"  - {error}")
            return 1

        projection_target = dao.case_dir(args.case_id) / "extracted_claim_fields.json"
        if projection_target.is_symlink():
            print("FAIL: extracted_claim_fields.json may not be a symlink")
            return 1

        revision_directory = revisions_dir(dao, args.case_id)
        namespace_error = revision_namespace_error(revision_directory)
        if namespace_error is not None:
            print(f"FAIL: {namespace_error}")
            return 1
        revision = revision_directory / f"{digest}.json"
        if revision.is_symlink():
            print(
                "FAIL: immutable medical revision path may not be a symlink for "
                f"{digest}"
            )
            return 1
        if revision.exists():
            if not revision.is_file() or revision.read_bytes() != canonical:
                print(
                    "FAIL: immutable medical revision digest "
                    f"collision/mismatch for {digest}"
                )
                return 1

        ledger_initialized = False
        if ledger_target.is_symlink():
            print("FAIL: medical review ledger may not be a symlink")
            return 1
        if ledger_target.exists():
            try:
                existing_ledger = ledger_owner.load_ledger(dao, args.case_id)
            except (OSError, ValueError) as exc:
                print(f"FAIL: existing medical review ledger is invalid: {exc}")
                return 1
            if not target.exists():
                print(
                    "FAIL: canonical medical publication is missing while its "
                    "review ledger exists"
                )
                return 1
            current_variables, current_error = load_revision(
                dao,
                args.case_id,
                None,
            )
            if current_error or current_variables is None:
                print(
                    "FAIL: existing canonical medical publication is invalid: "
                    f"{current_error}"
                )
                return 1
            current_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            candidate_issue_ids = {
                issue["issue_id"] for issue in candidate["medical_issues"]
            }
            missing_owned_issue_ids = sorted({
                item.get("issue_id")
                for item in existing_ledger.get("review_items", [])
                if isinstance(item.get("issue_id"), str)
                and item.get("issue_id") not in candidate_issue_ids
            })
            if current_digest != digest and missing_owned_issue_ids:
                print(
                    "FAIL: cannot remove medical issues owned by review items: "
                    + ", ".join(missing_owned_issue_ids)
                )
                return 1
            if current_digest != digest and any(
                item.get("state") in ledger_owner.BLOCKING_STATES
                and item.get("state") not in {
                    "needs_information",
                    "expert_needs_information",
                }
                for item in existing_ledger.get("review_items", [])
            ):
                print(
                    "FAIL: cannot supersede the canonical medical revision while "
                    "medical reviews remain unresolved"
                )
                return 1
        else:
            if target.exists() or target.is_symlink():
                print(
                    "FAIL: medical review ledger is missing for an existing "
                    "canonical medical publication"
                )
                return 1
            try:
                created = dao.atomic_create_json(
                    ledger_target,
                    ledger_owner.empty_ledger(dao, args.case_id),
                )
            except (OSError, ValueError) as exc:
                print(f"FAIL: cannot initialize medical review ledger durably: {exc}")
                return 1
            if not created:
                print("FAIL: medical review ledger appeared concurrently")
                return 1
            ledger_initialized = True

        namespace_error = revision_namespace_error(revision_directory)
        if namespace_error is not None:
            if ledger_initialized:
                try:
                    ledger_target.unlink()
                except OSError as exc:
                    print(
                        "FAIL: immutable revision namespace became unsafe and "
                        f"initial-ledger rollback failed: {exc}"
                    )
                    return 1
            print(f"FAIL: {namespace_error}")
            return 1
        try:
            reused = not dao.atomic_create_bytes_in_directory(
                revision_directory,
                revision.name,
                canonical,
            )
        except (OSError, ValueError) as exc:
            if ledger_initialized:
                try:
                    ledger_target.unlink()
                except OSError as rollback_exc:
                    print(
                        "FAIL: immutable revision creation failed and "
                        f"initial-ledger rollback failed: {rollback_exc}"
                    )
                    return 1
            print(f"FAIL: cannot create immutable medical revision: {exc}")
            return 1

        # Projection is written before the canonical commit point. Readers bind it
        # to the current revision and therefore fail closed after interruption.
        try:
            dao.atomic_write_json(projection_target, projection)
            try:
                dao.atomic_write_bytes(target, canonical)
            except dao.AtomicWriteCommittedError as exc:
                if not ledger_initialized:
                    raise
                print(
                    "FAIL: canonical medical publication committed but durability "
                    f"was not confirmed; retry is safe: {exc}"
                )
                return 1
        except OSError as exc:
            if not ledger_initialized:
                raise
            if not reused:
                try:
                    dao.remove_managed_file_if_content(
                        revision_directory,
                        revision.name,
                        canonical,
                    )
                except OSError:
                    pass
            try:
                ledger_target.unlink(missing_ok=True)
            except OSError as rollback_exc:
                print(
                    "FAIL: initial medical publication failed and its ledger "
                    f"rollback also failed: {rollback_exc}"
                )
                return 1
            print(f"FAIL: initial medical publication failed: {exc}")
            return 1
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "case_id": args.case_id,
                    "revision_sha": digest,
                    "reused_revision": reused,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        if ledger_lock_acquired:
            dao.release_lock(ledger_target)
        dao.release_lock(target)


def load_revision(
    dao,
    case_id: str,
    revision_sha: str | None,
) -> tuple[dict | None, str | None]:
    if revision_sha is None:
        path = medical_variables_path(dao, case_id)
    else:
        if not re.fullmatch(r"[a-f0-9]{64}", revision_sha):
            return None, "revision SHA must be 64 lowercase hexadecimal characters"
        revision_directory = revisions_dir(dao, case_id)
        namespace_error = revision_namespace_error(revision_directory)
        if namespace_error is not None:
            return None, namespace_error
        path = revision_directory / f"{revision_sha}.json"
    if path.is_symlink():
        return None, "medical variable contract or revision may not be a symlink"
    if not path.exists():
        return None, "medical variable contract or revision not found"
    raw = path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if revision_sha is not None and actual_sha != revision_sha:
        return None, "immutable medical revision digest does not match its content"
    if revision_sha is None:
        revision = revisions_dir(dao, case_id) / f"{actual_sha}.json"
        if (
            not revision.is_file()
            or revision.is_symlink()
            or revision.read_bytes() != raw
        ):
            return None, "current medical contract has no matching immutable revision"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "medical variable contract is malformed JSON"
    if not isinstance(data, dict) or data.get("case_id") != case_id:
        return None, "medical variable contract belongs to a different case"
    schemas, registry = load_registry()
    errors = validate_instance(
        data,
        "medical_variables.schema.json",
        schemas,
        registry,
    )
    if errors:
        return (
            None,
            "medical variable contract failed schema validation: "
            + "; ".join(errors),
        )
    return data, None


def load_projection(dao, case_id: str) -> tuple[dict | None, str | None]:
    path = dao.case_dir(case_id) / "extracted_claim_fields.json"
    if path.is_symlink():
        return None, "medical compatibility projection may not be a symlink"
    data = dao.load_json(path)
    if data is None:
        return None, "medical compatibility projection is unavailable"
    errors = dao._schema_check(data, "extracted_claim_fields.schema.json")
    if errors:
        return (
            None,
            "medical compatibility projection failed schema validation: "
            + "; ".join(errors),
        )
    projection_mode = data.get("projection_mode")
    if projection_mode is None:
        return None, "medical compatibility projection must declare projection_mode"
    if projection_mode == "legacy_pre_medical":
        authority_paths = (
            medical_variables_path(dao, case_id),
            dao.case_dir(case_id) / "_medical_review_ledger.json",
            revisions_dir(dao, case_id),
        )
        if any(path.exists() or path.is_symlink() for path in authority_paths):
            return None, "legacy pre-medical projection is invalid for a post-adoption case"
        return data, None
    if projection_mode != "canonical_medical_projection":
        return None, "unknown medical compatibility projection_mode"
    variables, error = load_revision(dao, case_id, None)
    if error or variables is None:
        return None, error or "canonical medical variables are unavailable"
    current_sha = hashlib.sha256(
        medical_variables_path(dao, case_id).read_bytes()
    ).hexdigest()
    if data["medical_revision_sha"] != current_sha:
        return (
            None,
            "medical compatibility projection does not match the current "
            "canonical revision",
        )
    return data, None


def cmd_read_variables(dao, args) -> int:
    data, error = load_revision(
        dao,
        args.case_id,
        getattr(args, "revision_sha", None),
    )
    if error:
        print(f"FAIL: {error}")
        return 1
    assert data is not None
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return 0


def read_evidence_payload(
    dao,
    case_id: str,
    locator_id: str,
    revision_sha: str | None,
) -> dict:
    data, error = load_revision(
        dao,
        case_id,
        revision_sha,
    )
    if error:
        raise ValueError(error)
    assert data is not None
    matches = [
        locator
        for variable in data["variables"]
        for observation in variable["observations"]
        for locator in observation["evidence"]
        if locator["locator_id"] == locator_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"evidence locator {locator_id} was not found exactly once"
        )
    locator = matches[0]
    manifest = dao.load_json(dao.case_dir(case_id) / "document_manifest.json")
    document = next(
        (
            entry
            for entry in (manifest or {}).get("documents", [])
            if entry["document_id"] == locator["document_id"]
        ),
        None,
    )
    if (
        document is None
        or document.get("downstream_disposition") == "expert_review_only"
    ):
        raise ValueError(
            "evidence source is not available through the automated evidence boundary"
        )
    return {"case_id": case_id, "revision_sha": revision_sha, "locator": locator}


def cmd_read_evidence(dao, args) -> int:
    try:
        payload = read_evidence_payload(
            dao,
            args.case_id,
            args.locator_id,
            getattr(args, "revision_sha", None),
        )
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0
