"""DAO publication and immutable revision tests for medical variables."""
import hashlib
import json
import os
from pathlib import Path

import dao
import medical_repository
import medical_review_ledger
import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "medical" / "minimal_variables.json"


def _legacy_projection(case_id: str, mode: str | None) -> dict:
    projection = {
        "case_id": case_id,
        "component": "claim-analysis",
        "status": "success",
        "fields": {},
    }
    if mode is not None:
        projection["projection_mode"] = mode
    return projection


def _write_dependencies(case_dir: Path) -> None:
    dao.atomic_write_json(case_dir / "document_manifest.json", {
        "case_id": "CASE_9001",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "synthetic.pdf",
            "file_path": "data/raw/CASE_9001/DOC_001/synthetic.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "pages": 1,
            "ocr_status": "completed",
            "cross_validation_status": "agreed",
            "document_type": "medical_record",
            "downstream_disposition": "automated_text_pipeline",
        }],
    })
    dao.atomic_write_json(case_dir / "page_chunks.json", {
        "case_id": "CASE_9001",
        "component": "document-pipeline",
        "status": "success",
        "chunks": [{
            "chunk_id": "CHUNK_001",
            "document_id": "DOC_001",
            "page_start": 1,
            "page_end": 1,
            "text": "Synthetic finding documented.",
        }],
    })
    dao.atomic_write_json(case_dir / "_conflict_ledger.json", {
        "ledger_version": "conflict_ledger.v0.3",
        "case_id": "CASE_9001",
        "conflicts": [],
        "history_boundary": dao.make_history_boundary([], mode="native"),
        "operations": [],
    })
    dao.atomic_write_json(case_dir / "case_type_result.json", {
        "case_id": "CASE_9001",
        "component": "claim-analysis",
        "status": "success",
        "case_type": "기타",
        "template_id": "기타형",
        "confidence": 1.0,
        "evidence_references": [{
            "document_id": "DOC_001",
            "page": 1,
            "quote": "Synthetic finding documented.",
        }],
        "review_required": False,
    })


def _enabled_config(tmp_path: Path) -> Path:
    config = json.loads((ROOT / "config" / "medical" / "medical_structuring_v0.1.json").read_text(encoding="utf-8"))
    config.update({
        "behavior_enabled": True,
        "approval": {
            "approved_by": "synthetic-test-owner",
            "authority_role": "test-fixture",
            "decision_record": "tests/test_dao_medical_variables.py",
            "approved_at": "2026-07-23T00:00:00+09:00",
            "scope": "Synthetic fixtures only",
        },
        "enabled_case_types": ["기타"],
        "enabled_document_roles": ["medical_record"],
        "variable_kinds": [{
            "code": "diagnosis_code",
            "domain_code": "diagnosis",
            "label": "Synthetic diagnosis code",
        }],
    })
    path = tmp_path / "medical_config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _enabled_projection_config(tmp_path: Path) -> Path:
    config = {
        "schema_version": "medical_projection_config.v0.1",
        "config_version": "medical_projection.v0.1",
        "projection_enabled": True,
        "approval": {
            "approved_by": "synthetic-test",
            "authority_role": "test",
            "decision_record": "synthetic-only",
            "approved_at": "2026-07-23T00:00:00+09:00",
            "scope": "synthetic fixture",
        },
        "field_rules": [{
            "rule_id": "synthetic_kcd_code",
            "field_name": "kcd_code",
            "variable_kind": "diagnosis_code",
            "value_type": "coded",
            "value_path": "coded_value.code",
            "selection_strategy": "single_distinct_or_fail",
            "compatibility_confidence": 1.0,
            "review_required": False,
        }],
        "primary_diagnosis_rule": {
            "rule_id": "legacy_document_character_priority",
            "enabled": False,
            "reason_disabled": "Synthetic test does not approve precedence policy.",
        },
    }
    path = tmp_path / "medical_projection_config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_write_medical_variables_publishes_canonical_immutable_revision(
    isolated_dao, tmp_path, monkeypatch, make_args, capsys
):
    case_dir = dao.case_dir("CASE_9001")
    _write_dependencies(case_dir)
    monkeypatch.setattr(dao, "MEDICAL_STRUCTURING_CONFIG", _enabled_config(tmp_path), raising=False)
    monkeypatch.setattr(dao, "MEDICAL_PROJECTION_CONFIG", _enabled_projection_config(tmp_path), raising=False)

    result = dao.cmd_write_medical_variables(make_args(
        case_id="CASE_9001",
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    ))

    assert result == 0, capsys.readouterr().out
    current = case_dir / "medical_variables.json"
    canonical = current.read_bytes()
    assert not canonical.endswith(b"\n")
    assert canonical == json.dumps(json.loads(canonical), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    revision = case_dir / "_medical_variable_revisions" / f"{digest}.json"
    assert revision.read_bytes() == canonical
    ledger = dao.load_medical_review_ledger("CASE_9001")
    assert ledger["review_items"] == []
    assert dao.cmd_check_medical_reviews_clear(make_args(case_id="CASE_9001")) == 1
    assert "missing:MCI_0001" in capsys.readouterr().out
    projection = dao.load_json(case_dir / "extracted_claim_fields.json")
    assert projection["fields"]["kcd_code"]["value"] == "SYN-001"
    assert projection["fields"]["kcd_code"]["source_variable_ids"] == ["MV_0001"]
    assert projection["medical_revision_sha"] == digest

    (case_dir / "_medical_review_ledger.json").write_text("{", encoding="utf-8")
    assert dao.cmd_write_medical_variables(make_args(
        case_id="CASE_9001", data_file=str(FIXTURE),
        held_by="claim-analysis", run_id="RUN_20260723_001",
    )) == 1

    revision_namespace = case_dir / "_medical_variable_revisions"
    relocated_namespace = case_dir / "relocated-medical-revisions"
    revision_namespace.rename(relocated_namespace)
    revision_namespace.symlink_to(
        relocated_namespace.name,
        target_is_directory=True,
    )
    loaded_revision, revision_error = dao._load_medical_revision(
        "CASE_9001",
        digest,
    )
    assert loaded_revision is None
    assert "revision namespace" in revision_error


def test_changed_publication_preserves_old_revision(isolated_dao, tmp_path, monkeypatch, make_args, capsys):
    case_dir = dao.case_dir("CASE_9001")
    _write_dependencies(case_dir)
    monkeypatch.setattr(dao, "MEDICAL_STRUCTURING_CONFIG", _enabled_config(tmp_path), raising=False)
    monkeypatch.setattr(dao, "MEDICAL_PROJECTION_CONFIG", _enabled_projection_config(tmp_path), raising=False)
    args = make_args(case_id="CASE_9001", data_file=str(FIXTURE), held_by="claim-analysis", run_id="RUN_20260723_001")
    assert dao.cmd_write_medical_variables(args) == 0
    first = (case_dir / "medical_variables.json").read_bytes()

    changed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    changed["variables"][0]["observations"][0]["coded_value"]["display"] = "Changed synthetic finding"
    changed["variables"][0]["observations"][0]["evidence"][0]["quote"] = "Changed synthetic finding documented."
    chunks = json.loads((case_dir / "page_chunks.json").read_text(encoding="utf-8"))
    chunks["chunks"][0]["text"] = "Changed synthetic finding documented."
    dao.atomic_write_json(case_dir / "page_chunks.json", chunks)
    candidate = tmp_path / "changed.json"
    candidate.write_text(json.dumps(changed), encoding="utf-8")
    args.data_file = str(candidate)

    ledger_owner = dao.sys.modules["medical_review_ledger"]
    real_load_ledger = ledger_owner.load_ledger
    monkeypatch.setattr(
        ledger_owner,
        "load_ledger",
        lambda *_args, **_kwargs: {
            "review_items": [{"state": "decision_pending"}]
        },
    )
    assert dao.cmd_write_medical_variables(args) == 1
    assert (case_dir / "medical_variables.json").read_bytes() == first
    monkeypatch.setattr(ledger_owner, "load_ledger", real_load_ledger)
    assert dao.cmd_write_medical_variables(args) == 0, capsys.readouterr().out

    second = (case_dir / "medical_variables.json").read_bytes()
    assert first != second
    revisions = list((case_dir / "_medical_variable_revisions").glob("*.json"))
    assert len(revisions) == 2
    assert any(path.read_bytes() == first for path in revisions)
    assert any(path.read_bytes() == second for path in revisions)


def test_initial_publication_rejects_dangling_revision_symlink_before_ledger_init(
    isolated_dao, tmp_path, monkeypatch, make_args
):
    case_id = "CASE_9001"
    case_dir = dao.case_dir(case_id)
    _write_dependencies(case_dir)
    monkeypatch.setattr(
        dao,
        "MEDICAL_STRUCTURING_CONFIG",
        _enabled_config(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )
    repository = dao.sys.modules["medical_repository"]
    candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))
    digest = hashlib.sha256(repository.canonical_json_bytes(candidate)).hexdigest()
    revision = repository.revisions_dir(dao, case_id) / f"{digest}.json"
    revision.parent.mkdir(parents=True, exist_ok=True)
    revision.symlink_to("missing-revision-target.json")

    result = dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    ))

    assert result == 1
    assert not dao.medical_variables_path(case_id).exists()
    assert not dao.medical_review_ledger_path(case_id).exists()


@pytest.mark.parametrize(
    "namespace_kind",
    ["regular_file", "dangling_symlink", "case_directory_symlink"],
)
def test_initial_publication_rejects_unsafe_revision_namespace_before_ledger_init(
    isolated_dao,
    tmp_path,
    monkeypatch,
    make_args,
    namespace_kind,
):
    case_id = "CASE_9001"
    case_dir = dao.case_dir(case_id)
    _write_dependencies(case_dir)
    monkeypatch.setattr(
        dao,
        "MEDICAL_STRUCTURING_CONFIG",
        _enabled_config(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )
    repository = dao.sys.modules["medical_repository"]
    candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))
    digest = hashlib.sha256(repository.canonical_json_bytes(candidate)).hexdigest()
    namespace = repository.revisions_dir(dao, case_id)
    if namespace_kind == "regular_file":
        namespace.write_text("not a directory", encoding="utf-8")
    elif namespace_kind == "dangling_symlink":
        namespace.symlink_to("missing-revision-directory", target_is_directory=True)
    else:
        namespace.symlink_to(".", target_is_directory=True)

    result = dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    ))

    assert result == 1
    assert not dao.medical_variables_path(case_id).exists()
    assert not dao.medical_review_ledger_path(case_id).exists()
    assert not (case_dir / f"{digest}.json").exists()


@pytest.mark.parametrize(
    "failure_point",
    ["namespace_recheck", "revision_create", "projection_write", "canonical_write"],
)
def test_initial_publication_rolls_back_ledger_after_revision_namespace_failure(
    isolated_dao,
    tmp_path,
    monkeypatch,
    make_args,
    failure_point,
):
    case_id = "CASE_9001"
    case_dir = dao.case_dir(case_id)
    _write_dependencies(case_dir)
    monkeypatch.setattr(
        dao,
        "MEDICAL_STRUCTURING_CONFIG",
        _enabled_config(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )
    repository = dao.sys.modules["medical_repository"]
    real_namespace_check = repository.revision_namespace_error
    real_revision_create = dao.atomic_create_bytes_in_directory
    real_atomic_write_json = dao.atomic_write_json
    real_atomic_write_bytes = dao.atomic_write_bytes
    if failure_point == "namespace_recheck":
        checks = 0

        def fail_second_namespace_check(path):
            nonlocal checks
            checks += 1
            return None if checks == 1 else "synthetic unsafe revision namespace"

        monkeypatch.setattr(
            repository,
            "revision_namespace_error",
            fail_second_namespace_check,
        )
    elif failure_point == "revision_create":
        def fail_revision_create(directory, filename, content):
            raise OSError("synthetic immutable revision creation failure")

        monkeypatch.setattr(
            dao,
            "atomic_create_bytes_in_directory",
            fail_revision_create,
        )
    elif failure_point == "projection_write":
        def fail_projection_write(path, content):
            if path.name == "extracted_claim_fields.json":
                raise OSError("synthetic projection publication failure")
            return real_atomic_write_json(path, content)

        monkeypatch.setattr(dao, "atomic_write_json", fail_projection_write)
    else:
        def fail_canonical_write(path, content):
            if path == dao.medical_variables_path(case_id):
                raise OSError("synthetic canonical publication failure")
            return real_atomic_write_bytes(path, content)

        monkeypatch.setattr(dao, "atomic_write_bytes", fail_canonical_write)

    result = dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    ))

    assert result == 1
    assert not dao.medical_variables_path(case_id).exists()
    assert not dao.medical_review_ledger_path(case_id).exists()
    revision_directory = repository.revisions_dir(dao, case_id)
    assert not list(revision_directory.glob("*.json"))
    monkeypatch.setattr(repository, "revision_namespace_error", real_namespace_check)
    monkeypatch.setattr(dao, "atomic_create_bytes_in_directory", real_revision_create)
    monkeypatch.setattr(dao, "atomic_write_json", real_atomic_write_json)
    monkeypatch.setattr(dao, "atomic_write_bytes", real_atomic_write_bytes)
    assert dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )) == 0


def test_initial_canonical_post_replace_failure_preserves_retryable_publication(
    isolated_dao,
    tmp_path,
    monkeypatch,
    make_args,
    capsys,
):
    case_id = "CASE_9001"
    case_dir = dao.case_dir(case_id)
    _write_dependencies(case_dir)
    monkeypatch.setattr(
        dao,
        "MEDICAL_STRUCTURING_CONFIG",
        _enabled_config(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )
    target = dao.medical_variables_path(case_id)
    real_open = dao.os.open
    injected = False

    def fail_canonical_parent_open_after_replace(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal injected
        if path == target.parent and target.exists() and not injected:
            injected = True
            raise OSError("synthetic post-replace canonical directory failure")
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(dao.os, "open", fail_canonical_parent_open_after_replace)
    args = make_args(
        case_id=case_id,
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )
    assert dao.cmd_write_medical_variables(args) == 1
    assert injected
    assert target.exists()
    assert dao.medical_review_ledger_path(case_id).exists()
    revision_directory = dao.sys.modules["medical_repository"].revisions_dir(
        dao,
        case_id,
    )
    assert len(list(revision_directory.glob("*.json"))) == 1

    monkeypatch.setattr(dao.os, "open", real_open)
    assert dao.cmd_write_medical_variables(args) == 0, capsys.readouterr().out


def test_table_only_evidence_publishes_without_unrepresentable_legacy_field(
    isolated_dao,
    tmp_path,
    monkeypatch,
    make_args,
):
    case_id = "CASE_9001"
    case_dir = dao.case_dir(case_id)
    _write_dependencies(case_dir)
    monkeypatch.setattr(
        dao,
        "MEDICAL_STRUCTURING_CONFIG",
        _enabled_config(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )
    candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))
    locator = candidate["variables"][0]["observations"][0]["evidence"][0]
    locator.pop("quote")
    locator["table_location"] = {
        "table": "Diagnosis table",
        "row": "2",
        "column": "diagnosis",
        "value": "Synthetic finding documented.",
    }
    candidate_path = tmp_path / "table-only-medical-variables.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    assert dao.cmd_write_medical_variables(make_args(
        case_id=case_id,
        data_file=str(candidate_path),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )) == 0

    canonical = dao.load_json(dao.medical_variables_path(case_id))
    published_locator = canonical["variables"][0]["observations"][0]["evidence"][0]
    assert published_locator["table_location"] == locator["table_location"]
    projection = dao.load_json(case_dir / "extracted_claim_fields.json")
    assert "kcd_code" not in projection["fields"]


def test_existing_publication_propagates_committed_error_and_remains_retryable(
    isolated_dao,
    tmp_path,
    monkeypatch,
    make_args,
):
    case_id = "CASE_9001"
    case_dir = dao.case_dir(case_id)
    _write_dependencies(case_dir)
    monkeypatch.setattr(
        dao,
        "MEDICAL_STRUCTURING_CONFIG",
        _enabled_config(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )
    initial_args = make_args(
        case_id=case_id,
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )
    assert dao.cmd_write_medical_variables(initial_args) == 0

    revised = json.loads(FIXTURE.read_text(encoding="utf-8"))
    observation = revised["variables"][0]["observations"][0]
    observation["coded_value"]["display"] = "Synthetic committed revision"
    observation["evidence"][0]["quote"] = "Synthetic committed revision documented."
    page_chunks = dao.load_json(case_dir / "page_chunks.json")
    page_chunks["chunks"][0]["text"] = "Synthetic committed revision documented."
    dao.atomic_write_json(case_dir / "page_chunks.json", page_chunks)
    revised_path = tmp_path / "committed-revision.json"
    revised_path.write_text(json.dumps(revised), encoding="utf-8")
    revised_args = make_args(
        case_id=case_id,
        data_file=str(revised_path),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )
    target = dao.medical_variables_path(case_id)
    real_atomic_write_bytes = dao.atomic_write_bytes

    def fail_after_canonical_commit(path, content):
        real_atomic_write_bytes(path, content)
        if path == target:
            raise dao.AtomicWriteCommittedError("synthetic committed-state failure")

    monkeypatch.setattr(dao, "atomic_write_bytes", fail_after_canonical_commit)
    with pytest.raises(dao.AtomicWriteCommittedError):
        dao.cmd_write_medical_variables(revised_args)
    assert dao.load_json(target) == revised
    assert dao.medical_review_ledger_path(case_id).exists()

    monkeypatch.setattr(dao, "atomic_write_bytes", real_atomic_write_bytes)
    assert dao.cmd_write_medical_variables(revised_args) == 0


@pytest.mark.parametrize("race_kind", ["namespace_swap", "digest_collision"])
def test_initial_publication_descriptor_anchors_revision_creation_and_recovers(
    isolated_dao,
    tmp_path,
    monkeypatch,
    make_args,
    capsys,
    race_kind,
):
    case_id = "CASE_9001"
    case_dir = dao.case_dir(case_id)
    _write_dependencies(case_dir)
    monkeypatch.setattr(
        dao,
        "MEDICAL_STRUCTURING_CONFIG",
        _enabled_config(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )
    repository = dao.sys.modules["medical_repository"]
    candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))
    digest = hashlib.sha256(repository.canonical_json_bytes(candidate)).hexdigest()
    revision_name = f"{digest}.json"
    namespace = repository.revisions_dir(dao, case_id)
    namespace.mkdir()
    relocated = case_dir / "relocated-racing-revisions"
    real_open = dao.os.open
    raced = False

    def race_before_descriptor_relative_create(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if path == revision_name and dir_fd is not None and not raced:
            raced = True
            if race_kind == "namespace_swap":
                namespace.rename(relocated)
                namespace.symlink_to(".", target_is_directory=True)
            else:
                collision_fd = real_open(
                    revision_name,
                    dao.os.O_WRONLY | dao.os.O_CREAT | dao.os.O_EXCL,
                    0o600,
                    dir_fd=dir_fd,
                )
                with dao.os.fdopen(collision_fd, "wb") as stream:
                    stream.write(b"synthetic digest collision")
            return real_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(dao.os, "open", race_before_descriptor_relative_create)
    args = make_args(
        case_id=case_id,
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )
    assert dao.cmd_write_medical_variables(args) == 1
    assert raced
    assert not dao.medical_variables_path(case_id).exists()
    assert not dao.medical_review_ledger_path(case_id).exists()
    assert not (case_dir / revision_name).exists()

    monkeypatch.setattr(dao.os, "open", real_open)
    if race_kind == "namespace_swap":
        namespace.unlink()
        relocated.rename(namespace)
    else:
        (namespace / revision_name).unlink()
    assert dao.cmd_write_medical_variables(args) == 0, capsys.readouterr().out


def test_descriptor_create_cleanup_preserves_replacement_inode(
    isolated_dao,
    monkeypatch,
):
    directory = isolated_dao / "descriptor-cleanup"
    directory.mkdir()
    relocated = isolated_dao / "descriptor-cleanup-relocated"
    filename = "revision.json"
    replacement = b"adversary replacement"
    real_lstat = Path.lstat
    swapped = False

    def swap_before_identity_result(path):
        nonlocal swapped
        metadata = real_lstat(path)
        if path == directory and not swapped:
            swapped = True
            child = directory / filename
            child.unlink()
            child.write_bytes(replacement)
            directory.rename(relocated)
            directory.symlink_to(".", target_is_directory=True)
            return real_lstat(directory)
        return metadata

    monkeypatch.setattr(Path, "lstat", swap_before_identity_result)
    with pytest.raises(ValueError, match="identity changed"):
        dao.atomic_create_bytes_in_directory(directory, filename, b"created bytes")

    assert swapped
    assert (relocated / filename).read_bytes() == replacement


def test_descriptor_create_rejects_swap_after_lstat_snapshot(
    isolated_dao,
    monkeypatch,
):
    directory = isolated_dao / "descriptor-lstat-race"
    directory.mkdir()
    relocated = isolated_dao / "descriptor-lstat-race-relocated"
    filename = "revision.json"
    real_lstat = Path.lstat
    swapped = False

    def swap_after_snapshot(path):
        nonlocal swapped
        metadata = real_lstat(path)
        if path == directory and not swapped:
            swapped = True
            directory.rename(relocated)
            directory.symlink_to(".", target_is_directory=True)
        return metadata

    monkeypatch.setattr(Path, "lstat", swap_after_snapshot)
    with pytest.raises(ValueError, match="identity changed"):
        dao.atomic_create_bytes_in_directory(directory, filename, b"created bytes")

    assert swapped
    assert not (isolated_dao / filename).exists()


def test_descriptor_create_rejects_fifo_collision_without_blocking(isolated_dao):
    import multiprocessing

    directory = isolated_dao / "descriptor-fifo"
    directory.mkdir()
    filename = "revision.json"
    os.mkfifo(directory / filename)
    context = multiprocessing.get_context("fork")
    results = context.Queue()

    def attempt_create():
        try:
            dao.atomic_create_bytes_in_directory(directory, filename, b"content")
        except Exception as exc:
            results.put(type(exc).__name__)
        else:
            results.put("unexpected-success")

    process = context.Process(target=attempt_create)
    process.start()
    process.join(1.5)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("descriptor create blocked on a FIFO collision")
    assert results.get(timeout=1) == "ValueError"


def test_interrupted_projection_publication_fails_closed_and_retry_recovers(
    isolated_dao, tmp_path, monkeypatch, make_args, capsys
):
    case_dir = dao.case_dir("CASE_9001")
    _write_dependencies(case_dir)
    monkeypatch.setattr(dao, "MEDICAL_STRUCTURING_CONFIG", _enabled_config(tmp_path), raising=False)
    monkeypatch.setattr(dao, "MEDICAL_PROJECTION_CONFIG", _enabled_projection_config(tmp_path), raising=False)
    args = make_args(
        case_id="CASE_9001", data_file=str(FIXTURE), held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )
    assert dao.cmd_write_medical_variables(args) == 0

    changed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    changed["variables"][0]["observations"][0]["coded_value"]["code"] = "SYN-002"
    candidate = tmp_path / "interrupted.json"
    candidate.write_text(json.dumps(changed), encoding="utf-8")
    args.data_file = str(candidate)
    real_atomic_write_bytes = dao.atomic_write_bytes

    def fail_current(path, content):
        if path == dao.medical_variables_path("CASE_9001"):
            raise OSError("synthetic interruption before commit point")
        return real_atomic_write_bytes(path, content)

    monkeypatch.setattr(dao, "atomic_write_bytes", fail_current)
    with pytest.raises(OSError, match="synthetic interruption"):
        dao.cmd_write_medical_variables(args)
    with pytest.raises(ValueError, match="does not match"):
        dao.read_contract_data("CASE_9001", "extracted_claim_fields.json")
    (case_dir / "projection-alias.json").symlink_to("extracted_claim_fields.json")
    with pytest.raises(ValueError, match="does not match"):
        dao.read_contract_data("CASE_9001", "projection-alias.json")

    monkeypatch.setattr(dao, "atomic_write_bytes", real_atomic_write_bytes)
    assert dao.cmd_write_medical_variables(args) == 0, capsys.readouterr().out
    recovered = dao.read_contract_data("CASE_9001", "extracted_claim_fields.json")
    assert recovered["fields"]["kcd_code"]["value"] == "SYN-002"


@pytest.mark.parametrize(
    "replacement",
    [
        {"case_id": "OTHER", "conflicts": []},
        {"case_id": "CASE_9001", "conflicts": "not-an-array"},
    ],
)
def test_publication_requires_valid_canonical_conflict_ledger(
    isolated_dao, tmp_path, monkeypatch, make_args, replacement
):
    case_dir = dao.case_dir("CASE_9001")
    _write_dependencies(case_dir)
    conflict_path = case_dir / "_conflict_ledger.json"
    if replacement is None:
        conflict_path.unlink()
    else:
        dao.atomic_write_json(conflict_path, replacement)
    monkeypatch.setattr(
        dao, "MEDICAL_STRUCTURING_CONFIG", _enabled_config(tmp_path), raising=False
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )

    result = dao.cmd_write_medical_variables(make_args(
        case_id="CASE_9001",
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    ))

    assert result == 1
    assert not dao.medical_variables_path("CASE_9001").exists()


def test_publication_initializes_missing_zero_conflict_ledger_under_dao_control(
    isolated_dao, tmp_path, monkeypatch, make_args
):
    case_dir = dao.case_dir("CASE_9001")
    _write_dependencies(case_dir)
    (case_dir / "_conflict_ledger.json").unlink()
    monkeypatch.setattr(
        dao, "MEDICAL_STRUCTURING_CONFIG", _enabled_config(tmp_path), raising=False
    )
    monkeypatch.setattr(
        dao, "MEDICAL_PROJECTION_CONFIG", _enabled_projection_config(tmp_path),
        raising=False,
    )

    assert dao.cmd_write_medical_variables(make_args(
        case_id="CASE_9001",
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )) == 0
    ledger = dao.validated_conflict_ledger("CASE_9001")
    assert ledger["conflicts"] == []
    assert ledger["history_boundary"]["mode"] == "native"
    assert dao.validated_run_state("CASE_9001")["medical_review_adopted"] is True


def test_publication_persists_adoption_before_the_medical_commit_point(
    isolated_dao, monkeypatch, make_args
):
    monkeypatch.setattr(medical_repository, "_publish_owned", lambda _dao, _args: 1)

    assert dao.cmd_write_medical_variables(make_args(
        case_id="CASE_9001",
        data_file=str(FIXTURE),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )) == 1
    state = dao.validated_run_state("CASE_9001")
    assert state["medical_review_adopted"] is True


def test_no_issue_publication_clears_but_missing_or_malformed_ledger_blocks(
    isolated_dao, tmp_path, monkeypatch, make_args, capsys
):
    case_dir = dao.case_dir("CASE_9001")
    _write_dependencies(case_dir)
    monkeypatch.setattr(
        dao,
        "MEDICAL_STRUCTURING_CONFIG",
        _enabled_config(tmp_path),
        raising=False,
    )
    monkeypatch.setattr(
        dao,
        "MEDICAL_PROJECTION_CONFIG",
        _enabled_projection_config(tmp_path),
        raising=False,
    )
    candidate = json.loads(FIXTURE.read_text(encoding="utf-8"))
    candidate["medical_issues"] = []
    candidate["importance_assignments"] = []
    candidate_path = tmp_path / "no-issue-medical-variables.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    args = make_args(
        case_id="CASE_9001",
        data_file=str(candidate_path),
        held_by="claim-analysis",
        run_id="RUN_20260723_001",
    )

    assert dao.cmd_write_medical_variables(args) == 0, capsys.readouterr().out
    assert dao.cmd_check_medical_reviews_clear(
        make_args(case_id="CASE_9001")
    ) == 0

    ledger_path = dao.medical_review_ledger_path("CASE_9001")
    ledger_path.unlink()
    assert dao.cmd_check_medical_reviews_clear(
        make_args(case_id="CASE_9001")
    ) == 1

    dao.atomic_write_json(
        ledger_path,
        medical_review_ledger.empty_ledger(dao, "CASE_9001"),
    )
    ledger_path.write_text("{", encoding="utf-8")
    assert dao.cmd_check_medical_reviews_clear(
        make_args(case_id="CASE_9001")
    ) == 1


@pytest.mark.parametrize(
    "filename",
    [
        "medical_variables.json",
        "extracted_claim_fields.json",
        "_medical_review_ledger.json",
        "_medical_variable_revisions/" + "a" * 64 + ".json",
    ],
)
def test_generic_text_writer_cannot_replace_medical_owned_contracts(
    isolated_dao, tmp_path, make_args, filename
):
    target = dao.case_dir("CASE_9001") / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("canonical-bytes", encoding="utf-8")
    candidate = tmp_path / "replacement.txt"
    candidate.write_text("unvalidated-replacement", encoding="utf-8")

    result = dao.cmd_write_text(make_args(
        case_id="CASE_9001",
        filename=filename,
        text_file=str(candidate),
        held_by="synthetic-test",
        run_id="RUN_20260723_001",
        purpose=None,
    ))

    assert result == 1
    assert target.read_text(encoding="utf-8") == "canonical-bytes"


@pytest.mark.parametrize(
    "filename",
    [
        "_medical_review_ledger.json",
        "_medical_variable_revisions/" + "a" * 64 + ".json",
    ],
)
@pytest.mark.parametrize("exists", [False, True])
def test_generic_contract_read_cannot_probe_protected_medical_state(
    isolated_dao, make_args, filename, exists, capsys
):
    target = dao.case_dir("CASE_9001") / filename
    if exists:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")

    result = dao.cmd_read_contract(make_args(
        case_id="CASE_9001",
        filename=filename,
    ))

    assert result == 1
    assert "purpose-built medical DAO command" in capsys.readouterr().out
    with pytest.raises(ValueError, match="purpose-built medical DAO command"):
        dao.read_contract_data("CASE_9001", filename)


def test_generic_contract_read_rejects_hardlink_to_private_medical_ledger(
    isolated_dao, make_args, capsys
):
    case_directory = dao.case_dir("CASE_9001")
    case_directory.mkdir(parents=True, exist_ok=True)
    ledger_path = dao.medical_review_ledger_path("CASE_9001")
    ledger_path.write_text(
        json.dumps({"private_marker": "SYNTHETIC_PRIVATE_MEDICAL_LEDGER"}),
        encoding="utf-8",
    )
    alias = case_directory / "ordinary-contract.json"
    alias.hardlink_to(ledger_path)

    result = dao.cmd_read_contract(make_args(
        case_id="CASE_9001",
        filename=alias.name,
    ))

    assert result == 1
    assert "SYNTHETIC_PRIVATE_MEDICAL_LEDGER" not in capsys.readouterr().out
    with pytest.raises(ValueError, match="purpose-built medical DAO command"):
        dao.read_contract_data("CASE_9001", alias.name)


def test_projection_read_requires_explicit_legacy_mode(isolated_dao):
    case_id = "CASE_9001"
    directory = dao.case_dir(case_id)
    directory.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(
        directory / "extracted_claim_fields.json",
        _legacy_projection(case_id, None),
    )

    projection, error = medical_repository.load_projection(dao, case_id)

    assert projection is None
    assert "projection_mode" in error


def test_explicit_legacy_projection_is_readable_only_before_medical_adoption(
    isolated_dao,
):
    case_id = "CASE_9001"
    directory = dao.case_dir(case_id)
    directory.mkdir(parents=True, exist_ok=True)
    legacy = _legacy_projection(case_id, "legacy_pre_medical")
    dao.atomic_write_json(directory / "extracted_claim_fields.json", legacy)

    projection, error = medical_repository.load_projection(dao, case_id)
    assert error is None
    assert projection == legacy

    dao.atomic_write_json(directory / "medical_variables.json", {"post": "adoption"})
    projection, error = medical_repository.load_projection(dao, case_id)
    assert projection is None
    assert "post-adoption" in error


def test_legacy_projection_rejects_partial_medical_authority(isolated_dao):
    case_id = "CASE_9001"
    directory = dao.case_dir(case_id)
    directory.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(
        directory / "extracted_claim_fields.json",
        _legacy_projection(case_id, "legacy_pre_medical"),
    )
    (directory / "_medical_variable_revisions").mkdir()

    projection, error = medical_repository.load_projection(dao, case_id)

    assert projection is None
    assert "post-adoption" in error
