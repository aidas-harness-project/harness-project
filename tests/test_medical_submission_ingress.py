"""Private-ingress constraints for medical-review operator submissions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import medical_review_ledger


def _write_json(path: Path, value: dict | str) -> None:
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(json.dumps(value), encoding="utf-8")


def test_submission_loader_accepts_private_ingress_file(tmp_path: Path, monkeypatch):
    ingress = tmp_path / "ingress"
    ingress.mkdir(mode=0o700)
    submission = ingress / "submission.json"
    _write_json(submission, {"decision": "refer"})
    monkeypatch.setattr(
        medical_review_ledger, "MEDICAL_REVIEW_INGRESS_ROOT", ingress
    )

    assert medical_review_ledger._load_submission(
        str(submission), "synthetic submission"
    ) == {"decision": "refer"}


def test_submission_loader_rejects_path_outside_private_ingress(
    tmp_path: Path, monkeypatch
):
    ingress = tmp_path / "ingress"
    ingress.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    _write_json(outside, {"decision": "refer"})
    monkeypatch.setattr(
        medical_review_ledger, "MEDICAL_REVIEW_INGRESS_ROOT", ingress
    )

    with pytest.raises(ValueError, match="outside the private medical-review ingress"):
        medical_review_ledger._load_submission(str(outside), "synthetic submission")


def test_submission_loader_rejects_oversized_file(tmp_path: Path, monkeypatch):
    ingress = tmp_path / "ingress"
    ingress.mkdir(mode=0o700)
    submission = ingress / "oversized.json"
    _write_json(
        submission,
        '{"payload":"' + "x" * medical_review_ledger.MAX_SUBMISSION_BYTES + '"}',
    )
    monkeypatch.setattr(
        medical_review_ledger, "MEDICAL_REVIEW_INGRESS_ROOT", ingress
    )

    with pytest.raises(ValueError, match="exceeds the byte limit"):
        medical_review_ledger._load_submission(
            str(submission), "synthetic submission"
        )


def test_submission_loader_rejects_symlink(tmp_path: Path, monkeypatch):
    ingress = tmp_path / "ingress"
    ingress.mkdir(mode=0o700)
    target = ingress / "target.json"
    _write_json(target, {"decision": "refer"})
    alias = ingress / "alias.json"
    alias.symlink_to(target)
    monkeypatch.setattr(
        medical_review_ledger, "MEDICAL_REVIEW_INGRESS_ROOT", ingress
    )

    with pytest.raises(ValueError, match="unavailable"):
        medical_review_ledger._load_submission(str(alias), "synthetic submission")


def test_submission_loader_rejects_non_private_ingress_root(
    tmp_path: Path, monkeypatch
):
    ingress = tmp_path / "ingress"
    ingress.mkdir(mode=0o755)
    submission = ingress / "submission.json"
    _write_json(submission, {"decision": "refer"})
    monkeypatch.setattr(
        medical_review_ledger, "MEDICAL_REVIEW_INGRESS_ROOT", ingress
    )

    with pytest.raises(ValueError, match="ingress permissions are not private"):
        medical_review_ledger._load_submission(
            str(submission), "synthetic submission"
        )


def test_submission_loader_rejects_ancestor_swap_after_containment_check(
    tmp_path: Path, monkeypatch
):
    ingress = tmp_path / "ingress"
    ingress.mkdir(mode=0o700)
    nested = ingress / "nested"
    nested.mkdir()
    submission = nested / "submission.json"
    _write_json(submission, {"source": "inside"})
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_json(outside / submission.name, {"source": "outside"})
    monkeypatch.setattr(
        medical_review_ledger,
        "MEDICAL_REVIEW_INGRESS_ROOT",
        ingress,
    )
    original_open = medical_review_ledger.os.open
    swapped = False

    def swap_ancestor_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and (
            Path(path) == submission
            or path == nested.name
        ):
            original_nested = ingress / "nested-original"
            nested.rename(original_nested)
            nested.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        medical_review_ledger.os,
        "open",
        swap_ancestor_before_open,
    )

    with pytest.raises(ValueError, match="unavailable"):
        medical_review_ledger._load_submission(
            str(submission),
            "synthetic submission",
        )
    assert swapped is True
