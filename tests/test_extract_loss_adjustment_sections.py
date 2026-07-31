import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import fitz
import pytest

import extract_loss_adjustment_sections as extractor


def _write_bound_cache(
    study: Path, source: Path, text: str, document_id: str = "DOC_001"
) -> dict:
    cache_root = study / "_ocr_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / f"{document_id}.txt").write_text(text, encoding="utf-8")
    (cache_root / f"{document_id}.json").write_text(
        json.dumps(
            {
                "schema_version": "ocr_cache.v1",
                "extraction_contract_sha256": extractor.OCR_EXTRACTION_CONTRACT_SHA256,
                "document_id": document_id,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "sidecar_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "ocr_method": "embedded_text",
            }
        ),
        encoding="utf-8",
    )
    return {
        "ocr_method": "embedded_text",
        "ocr_cache_path": f"_ocr_cache/{document_id}.txt",
        "ocr_cache_metadata_path": f"_ocr_cache/{document_id}.json",
        "ocr_sidecar_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def test_ocr_extraction_contract_binds_implementation_and_tool_versions():
    contract = extractor.OCR_EXTRACTION_CONTRACT

    assert contract["extractor_implementation_sha256"] == hashlib.sha256(
        Path(cast(str, extractor.__file__)).read_bytes()
    ).hexdigest()
    assert set(contract["tool_versions"]) == {
        "ocrmypdf",
        "pymupdf",
        "tesseract",
    }
    assert all(contract["tool_versions"].values())
    assert extractor.OCR_EXTRACTION_CONTRACT_SHA256 == hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_command_version_is_bounded(monkeypatch: pytest.MonkeyPatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=10)

    monkeypatch.setattr(extractor.subprocess, "run", timeout)

    assert extractor._command_version(["hung", "--version"]) == "unavailable:timeout"


def test_run_ocr_command_supervises_group_and_inherits_only_its_study_lock(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 1234
        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            captured["timeout"] = timeout
            return "stdout", "stderr"

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(extractor.subprocess, "Popen", fake_popen)

    result = extractor._run_ocr_command(
        ["ocrmypdf", "input.pdf", "output.pdf"], study_lock_descriptor=17
    )

    assert result.returncode == 0
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["pass_fds"] == (17,)
    assert captured["timeout"] == extractor.OCR_PROCESS_TIMEOUT_SECONDS


def test_run_ocr_command_kills_process_group_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    signals: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 4321
        returncode: int | None = None
        calls = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls < 3:
                raise extractor.subprocess.TimeoutExpired("ocrmypdf", timeout)
            self.returncode = -9
            return "", "timed out"

        def poll(self) -> int | None:
            return self.returncode

    monkeypatch.setattr(extractor.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(
        extractor.os, "killpg", lambda group, signal: signals.append((group, signal))
    )

    with pytest.raises(RuntimeError, match="timed out"):
        extractor._run_ocr_command(["ocrmypdf", "input.pdf", "output.pdf"])

    assert signals == [(4321, extractor.signal.SIGTERM), (4321, extractor.signal.SIGKILL)]


def test_atomic_write_json_does_not_follow_predictable_temp_symlink(tmp_path: Path):
    target = tmp_path / "manifest.json"
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    target.with_suffix(".json.tmp").symlink_to(outside)

    extractor.atomic_write_json(target, {"safe": True})

    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {"safe": True}


def _write_verified_reviews(study: Path, manifest: dict) -> None:
    manifest["document_count"] = len(manifest["documents"])
    for record in manifest["documents"]:
        source_path = Path(record.get("source_path", ""))
        if source_path.is_file():
            record.setdefault("source_size_bytes", source_path.stat().st_size)
        record.setdefault("ocr_sidecar_sha256", "0" * 64)
    independent_path = study / "independent-boundary-review.json"
    independent = {
        "schema_version": "loss_adjustment_independent_boundary_review.v1",
        "reviewer_id": "independent-synthetic-reviewer",
        "review_generation_id": "independent-synthetic-generation",
        "status": "verified",
        "decision_count": len(manifest["documents"]),
        "mismatch_count": 0,
        "decisions": [
            {
                "document_id": record["document_id"],
                "source_relative_path": record["source_relative_path"],
                "source_sha256": record["source_sha256"],
                "source_page_count": record["source_page_count"],
                "ocr_sidecar_sha256": record["ocr_sidecar_sha256"],
                "contains_report": record["contains_report"],
                "page_start": record["page_start"],
                "page_end": record["page_end"],
                "confidence": "high",
            }
            for record in manifest["documents"]
        ],
    }
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    boundary_path = study / "boundary-review.json"
    boundary = {
        "schema_version": "loss_adjustment_boundary_review.v1",
        "primary_reviewer_id": "primary-synthetic-reviewer",
        "review_generation_id": "primary-synthetic-generation",
        "document_count": len(manifest["documents"]),
        "independent_review_status": "verified",
        "independent_review": {
            "path": "independent-boundary-review.json",
            "sha256": hashlib.sha256(independent_path.read_bytes()).hexdigest(),
            "document_count": len(manifest["documents"]),
            "mismatch_count": 0,
        },
        "decisions": [
            {
                "document_id": record["document_id"],
                "source_relative_path": record["source_relative_path"],
                "source_sha256": record["source_sha256"],
                "source_page_count": record["source_page_count"],
                "ocr_sidecar_sha256": record["ocr_sidecar_sha256"],
                "contains_report": record["contains_report"],
                "page_start": record["page_start"],
                "page_end": record["page_end"],
                "primary_review": "verified",
                "rationale": "synthetic reviewed boundary",
            }
            for record in manifest["documents"]
        ],
    }
    boundary_path.write_text(json.dumps(boundary), encoding="utf-8")
    manifest["boundary_review"] = {
        "path": "boundary-review.json",
        "sha256": hashlib.sha256(boundary_path.read_bytes()).hexdigest(),
        "document_count": len(manifest["documents"]),
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _build_reviewed_study(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "reviewed source page")
        document.save(source)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "reviewed source page")
    manifest = {
        "schema_version": "loss_adjustment_corpus_manifest.v0.1",
        "source_root": str(sources),
        "study_root": str(study),
        "document_count": 1,
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_size_bytes": source.stat().st_size,
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                **cache_fields,
            }
        ],
    }
    _write_verified_reviews(study, manifest)
    return study, source, manifest


def _complete_extract_recovery_journal(
    study: Path, staging: Path, journal: dict[str, Any]
) -> dict[str, Any]:
    journal.setdefault("phase", "prepared")
    journal.setdefault(
        "staging_object", extractor._publication_object_token(staging)
    )
    existing = {target["relative"] for target in journal["targets"]}
    for relative in {"sections", "no-report", "manifest.json"} - existing:
        final_path = study / relative
        candidate_path = staging / relative
        if relative == "manifest.json":
            final_path.write_text("old manifest", encoding="utf-8")
            candidate_path.write_text("new manifest", encoding="utf-8")
        else:
            final_path.mkdir(parents=True)
            candidate_path.mkdir(parents=True)
            (final_path / "value").write_text(f"old {relative}", encoding="utf-8")
            (candidate_path / "value").write_text(f"new {relative}", encoding="utf-8")
        journal["targets"].append(
            {
                "relative": relative,
                "candidate_relative": f"{journal['staging_relative']}/{relative}",
                "backup_relative": (
                    f"{journal['staging_relative']}/backup-{Path(relative).name}"
                ),
                "had_previous": True,
                "previous_sha256": extractor._publication_fingerprint(final_path),
            }
        )
    for target in journal["targets"]:
        candidate = study / target["candidate_relative"]
        final = study / target["relative"]
        backup = study / target["backup_relative"]
        if "candidate_sha256" not in target:
            target["candidate_sha256"] = extractor._publication_fingerprint(
                candidate if candidate.exists() else final
            )
        for location in (candidate, final, backup):
            if (
                location.exists()
                and extractor._publication_fingerprint(location)
                == target["candidate_sha256"]
            ):
                target["candidate_object"] = extractor._publication_object_token(
                    location
                )
                break
        if target["had_previous"]:
            for location in (backup, candidate, final):
                if (
                    location.exists()
                    and extractor._publication_fingerprint(location)
                    == target["previous_sha256"]
                ):
                    target["previous_object"] = extractor._publication_object_token(
                        location
                    )
                    break
        else:
            target["previous_object"] = None
    return journal

def _publication_generation(tmp_path: Path) -> tuple[Path, Path]:
    study = tmp_path / "study"
    staging = study / "_working/extract-candidate"
    for name in ("sections", "no-report"):
        (study / name).mkdir(parents=True)
        (study / name / "value").write_text("old", encoding="utf-8")
        (staging / name).mkdir(parents=True)
        (staging / name / "value").write_text("new", encoding="utf-8")
    (study / "manifest.json").write_text("old manifest", encoding="utf-8")
    (staging / "manifest.json").write_text("new manifest", encoding="utf-8")
    return study, staging


def test_validate_study_root_restricts_in_repository_studies_to_canonical_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    canonical = repository / "loss-adjustment-format-study"
    outside = tmp_path / "external-study"
    monkeypatch.setattr(extractor, "REPOSITORY_ROOT", repository)

    assert extractor._validate_study_root(canonical) == canonical
    assert extractor._validate_study_root(outside) == outside
    with pytest.raises(ValueError, match="canonical ignored location"):
        extractor._validate_study_root(repository / "other-study")


def test_detect_report_range_includes_cover_through_evidence_list():
    pages = [
        "損 害 查 定 바른길",
        "손해사정을 완료하여 보험금 사정서를 제출합니다",
        "CLAIM ADJUSTMENT",
        "I. 사정 요약 1. 사정 근거 2. 사정 금액 3. 사정 의견",
        "II. 위임 및 보험계약사항 책임사정사",
        "III. 사고 및 손해 발생",
        "IV. 관계법규 및 보상책임",
        "V. 보험금 사정 5. 사정 결과 및 의견",
        "VI. 증빙자료 1. 진단서 2. 의무기록 책임사정사",
        "진단서 질병분류기호 환자의 성명",
    ]

    result = extractor.detect_report_range(pages)

    assert result["contains_report"] is True
    assert result["page_start"] == 1
    assert result["page_end"] == 9
    assert result["confidence"] == "high"


def test_detect_report_range_rejects_attachment_only_document():
    pages = [
        "진단서 질병분류기호 환자의 성명",
        "의무기록 사본 검사 결과",
        "보험증권 계약자 피보험자",
    ]

    result = extractor.detect_report_range(pages)

    assert result["contains_report"] is False
    assert result["page_start"] is None
    assert result["page_end"] is None


def test_detect_report_range_does_not_include_unrelated_pages_before_page_four():
    pages = [
        "타사 지급내역",
        "보험금 지급 근거",
        "계약별 지급 금액",
        "손해사정서 1. 수임정보 2. 보험계약사항",
        "적용 관계 법규 및 약관 사정의견",
        "거래 내역",
    ]

    result = extractor.detect_report_range(pages)

    assert result["contains_report"] is True
    assert result["page_start"] == 4
    assert result["page_end"] == 5


def test_detect_report_range_accepts_short_form_report_with_one_title_signal():
    pages = [
        "보험금 사정 요약 손해보상금 사정금액 손해사정",
        "보상금 사정의 기초사실 보상금 사정 결론",
    ]

    result = extractor.detect_report_range(pages)

    assert result["contains_report"] is True
    assert result["page_start"] == 1
    assert result["page_end"] == 2


def test_split_sidecar_pages_preserves_declared_page_count():
    sidecar = "첫 페이지\f둘째 페이지\f셋째 페이지\n"

    assert extractor.split_sidecar_pages(sidecar, 3) == [
        "첫 페이지",
        "둘째 페이지",
        "셋째 페이지\n",
    ]


def test_ocr_document_rejects_cache_bound_to_different_source(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "new source")
        document.save(source)
    record = {
        "document_id": "DOC_001",
        "source_path": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_page_count": 1,
    }
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    stale_text = "stale text with the correct page count"
    (cache_root / "DOC_001.txt").write_text(stale_text, encoding="utf-8")
    (cache_root / "DOC_001.json").write_text(
        json.dumps(
            {
                "schema_version": "ocr_cache.v1",
                "extraction_contract_sha256": extractor.OCR_EXTRACTION_CONTRACT_SHA256,
                "document_id": "DOC_001",
                "source_sha256": "0" * 64,
                "source_page_count": 1,
                "sidecar_sha256": hashlib.sha256(stale_text.encode()).hexdigest(),
                "ocr_method": "embedded_text",
            }
        ),
        encoding="utf-8",
    )
    fresh_text = "fresh embedded text " * 4
    monkeypatch.setattr(extractor, "_embedded_pages", lambda _: [fresh_text])

    result = extractor._ocr_document(
        record, cache_root, tmp_path / "working", ocr_jobs=1
    )

    assert result["pages"] == [fresh_text]
    assert result["ocr_method"] == "embedded_text"
    metadata = json.loads((cache_root / "DOC_001.json").read_text(encoding="utf-8"))
    assert metadata["source_sha256"] == record["source_sha256"]
    assert metadata["sidecar_sha256"] == hashlib.sha256(
        fresh_text.encode()
    ).hexdigest()


def test_refresh_cache_provenance_rebuilds_legacy_cache(
    tmp_path: Path, monkeypatch
):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    embedded_text = "fresh embedded source text " * 4
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), embedded_text)
        document.save(source)
    study = tmp_path / "study"
    cache_root = study / "_ocr_cache"
    cache_root.mkdir(parents=True)
    (cache_root / "DOC_001.txt").write_text("legacy stale text", encoding="utf-8")
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "document_count": 1,
        "boundary_review": {
            "path": "boundary-review.json",
            "sha256": "0" * 64,
            "document_count": 1,
        },
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_size_bytes": source.stat().st_size,
                "source_page_count": 1,
                "ocr_method": "cached",
                "ocr_cache_path": "_ocr_cache/DOC_001.txt",
                "ocr_sidecar_sha256": hashlib.sha256(
                    b"legacy stale text"
                ).hexdigest(),
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                "review_rationale": "reviewed stale OCR",
            }
        ],
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(extractor, "_embedded_pages", lambda _: [embedded_text])

    refreshed = extractor.refresh_cache_provenance(
        study, document_jobs=1, ocr_jobs=1
    )

    record = refreshed["documents"][0]
    assert record["ocr_method"] == "embedded_text"
    assert record["ocr_cache_reused"] is False
    assert record["ocr_cache_metadata_path"] == "_ocr_cache/DOC_001.json"
    assert record["ocr_sidecar_sha256"] == hashlib.sha256(
        embedded_text.encode()
    ).hexdigest()
    assert record["review_status"] == "pending"
    assert "review_rationale" not in record
    assert "boundary_review" not in refreshed
    assert (cache_root / "DOC_001.json").is_file()
    assert "fresh embedded source text" in (cache_root / "DOC_001.txt").read_text(
        encoding="utf-8"
    )


def test_refresh_cache_provenance_rolls_back_late_worker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sources = tmp_path / "sources"
    sources.mkdir()
    study = tmp_path / "study"
    cache_root = study / "_ocr_cache"
    cache_root.mkdir(parents=True)
    records = []
    for index, name in enumerate(("a.pdf", "b.pdf"), start=1):
        document_id = f"DOC_{index:03d}"
        source = sources / name
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), f"source {index}")
            document.save(source)
        (cache_root / f"{document_id}.txt").write_text(
            f"published {index}", encoding="utf-8"
        )
        records.append(
            {
                "document_id": document_id,
                "source_path": str(source),
                "source_relative_path": name,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_size_bytes": source.stat().st_size,
                "source_page_count": 1,
                "ocr_method": "cached",
                "ocr_cache_path": f"_ocr_cache/{document_id}.txt",
            }
        )
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "document_count": 2,
        "documents": records,
    }
    manifest_path = study / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_before = manifest_path.read_bytes()
    cache_before = {
        path.name: path.read_bytes() for path in cache_root.iterdir()
    }

    def fake_ocr(
        record: dict[str, Any],
        target_cache: Path,
        _working: Path,
        _jobs: int,
        _source_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        if record["document_id"] == "DOC_002":
            raise RuntimeError("late refresh failure")
        target_cache.mkdir(parents=True, exist_ok=True)
        text_path = target_cache / "DOC_001.txt"
        metadata_path = target_cache / "DOC_001.json"
        text_path.write_text("candidate", encoding="utf-8")
        metadata_path.write_text("{}", encoding="utf-8")
        return {
            "pages": ["candidate"],
            "ocr_method": "embedded_text",
            "cache_reused": False,
            "cache_path": text_path,
            "cache_metadata_path": metadata_path,
        }

    monkeypatch.setattr(extractor, "_ocr_document", fake_ocr)

    with pytest.raises(RuntimeError, match="late refresh failure"):
        extractor.refresh_cache_provenance(study, document_jobs=1, ocr_jobs=1)

    assert manifest_path.read_bytes() == manifest_before
    assert {path.name: path.read_bytes() for path in cache_root.iterdir()} == cache_before
    assert extractor._directory_entries(study / "_working") == set()


def test_extract_range_writes_exact_original_pages(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        for page_number in range(1, 5):
            page = document.new_page(width=200, height=200)
            page.insert_text((20, 40), f"page-{page_number}")
        document.save(source)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "section.pdf"

    result = extractor.extract_pdf_range(source, target, page_start=2, page_end=3)

    with fitz.open(target) as section:
        assert section.page_count == 2
        assert "page-2" in section[0].get_text()
        assert "page-3" in section[1].get_text()
    assert result["source_sha256"] == source_sha256
    assert result["output_page_count"] == 2
    assert result["page_start"] == 2
    assert result["page_end"] == 3
    assert result["output_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_write_section_text_uses_source_page_markers(tmp_path):
    target = tmp_path / "section.txt"

    extractor.write_section_text(target, ["cover", "body", "evidence"], 2, 3)

    assert target.read_text(encoding="utf-8") == (
        "===== SOURCE PAGE 2 =====\nbody\n\n"
        "===== SOURCE PAGE 3 =====\nevidence\n"
    )


def test_apply_boundary_review_verifies_every_manifest_document(tmp_path):
    source_root = tmp_path.parent / f"{tmp_path.name}-sources"
    source_root.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_root": str(source_root),
                "study_root": str(tmp_path),
                "document_count": 2,
                "documents": [
                    {
                        "document_id": "DOC_001",
                        "source_relative_path": "first.pdf",
                        "source_sha256": "1" * 64,
                        "source_page_count": 8,
                        "contains_report": True,
                        "page_start": 1,
                        "page_end": 7,
                        "review_status": "pending",
                    },
                    {
                        "document_id": "DOC_002",
                        "source_relative_path": "second.pdf",
                        "source_sha256": "2" * 64,
                        "source_page_count": 4,
                        "contains_report": None,
                        "page_start": None,
                        "page_end": None,
                        "review_status": "pending",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    review_path = tmp_path / "boundary-review.json"
    review_path.write_text(
        json.dumps(
            {
                "document_count": 2,
                "decisions": [
                    {
                        "document_id": "DOC_001",
                        "source_relative_path": "first.pdf",
                        "source_sha256": "1" * 64,
                        "source_page_count": 8,
                        "contains_report": True,
                        "page_start": 2,
                        "page_end": 6,
                        "primary_review": "verified",
                        "rationale": "reviewed report boundary",
                    },
                    {
                        "document_id": "DOC_002",
                        "source_relative_path": "second.pdf",
                        "source_sha256": "2" * 64,
                        "source_page_count": 4,
                        "contains_report": False,
                        "page_start": None,
                        "page_end": None,
                        "primary_review": "verified",
                        "rationale": "no report present",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = extractor.apply_boundary_review(manifest_path, review_path)

    assert result["documents"][0]["page_start"] == 2
    assert result["documents"][0]["page_end"] == 6
    assert result["documents"][0]["review_status"] == "verified"
    assert result["documents"][1]["contains_report"] is False
    assert result["documents"][1]["review_status"] == "verified"


def test_apply_boundary_review_rejects_incomplete_review(tmp_path):
    source_root = tmp_path.parent / f"{tmp_path.name}-sources"
    source_root.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_root": str(source_root),
                "study_root": str(tmp_path),
                "document_count": 2,
                "documents": [
                    {"document_id": "DOC_001", "source_page_count": 2},
                    {"document_id": "DOC_002", "source_page_count": 2},
                ],
            }
        ),
        encoding="utf-8",
    )
    review_path = tmp_path / "boundary-review.json"
    review_path.write_text(
        json.dumps(
            {
                "document_count": 1,
                "decisions": [
                    {
                        "document_id": "DOC_001",
                        "contains_report": False,
                        "page_start": None,
                        "page_end": None,
                        "primary_review": "verified",
                        "rationale": "none",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        extractor.apply_boundary_review(manifest_path, review_path)
    except ValueError as exc:
        assert "exactly cover the manifest" in str(exc)
    else:
        raise AssertionError("incomplete review must be rejected")


def test_extract_sections_ignores_untrusted_manifest_output_paths(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "loss adjustment report")
    document.save(source)
    document.close()

    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "loss adjustment report")
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                "output_pdf_path": str(tmp_path / "escaped.pdf"),
                "output_text_path": str(
                    study / "sections/DOC_001/loss-adjustment-report.txt"
                ),
                "metadata_path": str(study / "sections/DOC_001/metadata.json"),
                **cache_fields,
            }
        ]
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_verified_reviews(study, manifest)

    extractor.extract_sections(study)

    assert not (tmp_path / "escaped.pdf").exists()

    working_root = study / "_working"
    if working_root.exists():
        working_root.rmdir()
    outside_working = tmp_path / "outside-working"
    outside_working.mkdir()
    sentinel = outside_working / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    working_root.symlink_to(outside_working, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe working directory|working directory.*symlink"):
        extractor.extract_sections(study)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    working_root.unlink()

    metadata_path = study / "sections/DOC_001/metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["unexpected"] = "value"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    validation = extractor.validate_study(study)
    assert any("section metadata schema mismatch" in error for error in validation["errors"])
    assert (
        study / "sections/DOC_001/loss-adjustment-report.pdf"
    ).is_file()


def test_extract_sections_writes_explicit_no_report_metadata(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "claim attachment")
    document.save(source)
    document.close()

    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "claim attachment")
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "contains_report": False,
                "page_start": None,
                "page_end": None,
                "review_status": "verified",
                "review_rationale": "No report title, body, or terminal evidence index.",
                **cache_fields,
            }
        ]
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_verified_reviews(study, manifest)

    extractor.extract_sections(study)

    metadata_path = study / "no-report/DOC_001/metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["contains_report"] is False
    assert metadata["source_sha256"] == manifest["documents"][0]["source_sha256"]
    assert metadata["review_status"] == "verified"


def test_extract_sections_rejects_traversal_document_id(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "claim attachment")
        document.save(source)

    study = tmp_path / "study"
    study.mkdir()
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "../../escaped",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "contains_report": False,
                "page_start": None,
                "page_end": None,
                "review_status": "verified",
                "review_rationale": "Synthetic no-report decision.",
            }
        ]
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid document_id"):
        extractor.extract_sections(study)

    assert not (tmp_path / "escaped").exists()


def test_extract_sections_rejects_cache_path_outside_study(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "loss adjustment report")
        document.save(source)
    outside_cache = tmp_path / "outside.txt"
    outside_cache.write_text("loss adjustment report", encoding="utf-8")

    study = tmp_path / "study"
    study.mkdir()
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                "ocr_cache_path": "../outside.txt",
            }
        ]
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_verified_reviews(study, manifest)

    with pytest.raises(ValueError, match="unsafe ocr_cache_path"):
        extractor.extract_sections(study)

    assert not (study / "sections/DOC_001").exists()


def test_validate_roots_rejects_governed_harness_state(
    tmp_path: Path, monkeypatch
):
    repository = tmp_path / "repository"
    source_root = repository / "data"
    source_root.mkdir(parents=True)
    study_root = repository / "study"
    monkeypatch.setattr(extractor, "REPOSITORY_ROOT", repository)

    with pytest.raises(ValueError, match="approved sources tree|canonical ignored location"):
        extractor._validate_roots(source_root, study_root)


def test_source_inventory_includes_pdf_suffix_case_insensitively(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for name in ("lower.pdf", "upper.PDF"):
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), name)
            document.save(sources / name)

    inventory = extractor._source_inventory(sources)

    assert {record["source_relative_path"] for record in inventory} == {
        "lower.pdf",
        "upper.PDF",
    }


def test_source_inventory_rejects_symlinked_pdf(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    outside = tmp_path / "outside.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "outside")
        document.save(outside)
    (sources / "linked.pdf").symlink_to(outside)

    with pytest.raises(
        ValueError, match="source inventory must not contain symlinks|unsafe source PDF path|symlink component"
    ):
        extractor._source_inventory(sources)


def test_source_inventory_rejects_symlinked_root(tmp_path: Path):
    real_sources = tmp_path / "real-sources"
    real_sources.mkdir()
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "source")
        document.save(real_sources / "source.pdf")
    linked_sources = tmp_path / "linked-sources"
    linked_sources.symlink_to(real_sources, target_is_directory=True)

    with pytest.raises(ValueError, match="source root must not be a symlink"):
        extractor._source_inventory(linked_sources)


def test_apply_boundary_review_rejects_governed_study_root(
    tmp_path: Path, monkeypatch
):
    repository = tmp_path / "repository"
    data_root = repository / "data/study"
    source_root = repository / "sources"
    data_root.mkdir(parents=True)
    source_root.mkdir()
    monkeypatch.setattr(extractor, "REPOSITORY_ROOT", repository)
    manifest_path = data_root / "manifest.json"
    review_path = data_root / "boundary-review.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_root": str(source_root),
                "study_root": str(data_root),
                "documents": [
                    {
                        "document_id": "DOC_001",
                        "source_page_count": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    review_path.write_text(
        json.dumps(
            {
                "document_count": 1,
                "decisions": [
                    {
                        "document_id": "DOC_001",
                        "contains_report": False,
                        "page_start": None,
                        "page_end": None,
                        "primary_review": "verified",
                        "rationale": "Synthetic no-report decision.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="governed harness state|canonical ignored location"):
        extractor.apply_boundary_review(manifest_path, review_path)


def test_apply_boundary_review_rejects_outside_review_before_read(tmp_path: Path):
    study = tmp_path / "study"
    sources = tmp_path / "sources"
    study.mkdir()
    sources.mkdir()
    manifest_path = study / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_root": str(sources),
                "study_root": str(study),
                "documents": [],
            }
        ),
        encoding="utf-8",
    )
    outside_review = tmp_path / "outside-review.json"
    outside_review.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="boundary review file must be inside"):
        extractor.apply_boundary_review(manifest_path, outside_review)


def test_study_entrypoints_reject_governed_root_before_manifest_read(
    tmp_path: Path, monkeypatch
):
    repository = tmp_path / "repository"
    study = repository / "data/study"
    study.mkdir(parents=True)
    (study / "manifest.json").write_text("not json", encoding="utf-8")
    monkeypatch.setattr(extractor, "REPOSITORY_ROOT", repository)

    with pytest.raises(ValueError, match="governed harness state|canonical ignored location"):
        extractor.extract_sections(study)
    with pytest.raises(ValueError, match="governed harness state|canonical ignored location"):
        extractor.adopt_reviewed_cache_provenance(study)
    validation = extractor.validate_study(study)
    assert any(
        "governed harness state" in error or "canonical ignored location" in error
        for error in validation["errors"]
    )


def test_extract_cli_has_no_pending_review_bypass(tmp_path: Path):
    with pytest.raises(SystemExit):
        extractor._parser().parse_args(
            ["extract", str(tmp_path / "study"), "--allow-pending"]
        )


def test_extract_sections_requires_bound_primary_and_independent_reviews(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "loss adjustment report")
        document.save(source)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "loss adjustment report")
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                **cache_fields,
            }
        ],
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="boundary review reference missing"):
        extractor.extract_sections(study)

    assert not (study / "sections").exists()


def test_review_artifacts_bind_source_generation_and_unique_manifest_inventory(
    tmp_path: Path,
):
    study = tmp_path / "study"
    study.mkdir()
    records = [
        {
            "document_id": "DOC_001",
            "source_relative_path": "first.pdf",
            "source_sha256": "1" * 64,
            "source_page_count": 1,
            "ocr_sidecar_sha256": "a" * 64,
            "contains_report": True,
            "page_start": 1,
            "page_end": 1,
        },
        {
            "document_id": "DOC_002",
            "source_relative_path": "second.pdf",
            "source_sha256": "2" * 64,
            "source_page_count": 1,
            "ocr_sidecar_sha256": "b" * 64,
            "contains_report": False,
            "page_start": None,
            "page_end": None,
        },
    ]
    manifest = {"documents": records}
    _write_verified_reviews(study, manifest)

    changed_generation = json.loads(json.dumps(manifest))
    changed_generation["documents"][0]["source_sha256"] = "3" * 64
    generation_errors = extractor._validate_review_artifacts(
        study, changed_generation, changed_generation["documents"]
    )
    assert any(
        "boundary decision source generation mismatch" in error
        for error in generation_errors
    )

    changed_ocr_generation = json.loads(json.dumps(manifest))
    changed_ocr_generation["documents"][0]["ocr_sidecar_sha256"] = "c" * 64
    ocr_generation_errors = extractor._validate_review_artifacts(
        study, changed_ocr_generation, changed_ocr_generation["documents"]
    )
    assert any(
        "boundary decision source generation mismatch" in error
        for error in ocr_generation_errors
    )

    independent_path = study / "independent-boundary-review.json"
    independent = json.loads(independent_path.read_text(encoding="utf-8"))
    independent["decisions"][0]["ocr_sidecar_sha256"] = "c" * 64
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    boundary_path = study / "boundary-review.json"
    boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
    boundary["independent_review"]["sha256"] = hashlib.sha256(
        independent_path.read_bytes()
    ).hexdigest()
    boundary_path.write_text(json.dumps(boundary), encoding="utf-8")
    boundary_reference = cast(dict[str, Any], manifest["boundary_review"])
    boundary_reference["sha256"] = hashlib.sha256(
        boundary_path.read_bytes()
    ).hexdigest()
    independent_generation_errors = extractor._validate_review_artifacts(
        study, manifest, manifest["documents"]
    )
    assert any(
        "independent boundary decision source generation mismatch" in error
        for error in independent_generation_errors
    )

    duplicate_inventory = json.loads(json.dumps(manifest))
    duplicate_inventory["documents"][1]["document_id"] = "DOC_001"
    duplicate_errors = extractor._validate_review_artifacts(
        study, duplicate_inventory, duplicate_inventory["documents"]
    )
    assert any(
        "manifest contains duplicate document IDs" in error
        for error in duplicate_errors
    )


def test_review_artifacts_require_distinct_reviewer_identities(tmp_path: Path):
    study = tmp_path / "study"
    study.mkdir()
    manifest = {
        "documents": [
            {
                "document_id": "DOC_001",
                "source_relative_path": "source.pdf",
                "source_sha256": "1" * 64,
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
            }
        ]
    }
    _write_verified_reviews(study, manifest)
    independent_path = study / "independent-boundary-review.json"
    independent = json.loads(independent_path.read_text(encoding="utf-8"))
    independent["reviewer_id"] = "reviewer-a"
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    boundary_path = study / "boundary-review.json"
    boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
    boundary["primary_reviewer_id"] = "reviewer-a"
    boundary["independent_review"]["sha256"] = hashlib.sha256(
        independent_path.read_bytes()
    ).hexdigest()
    boundary_path.write_text(json.dumps(boundary), encoding="utf-8")
    boundary_reference = cast(dict[str, Any], manifest["boundary_review"])
    boundary_reference["sha256"] = hashlib.sha256(boundary_path.read_bytes()).hexdigest()

    errors = extractor._validate_review_artifacts(
        study, manifest, manifest["documents"]
    )
    assert any("reviewer identities must be nonempty and distinct" in error for error in errors)


def test_review_artifacts_require_distinct_review_generation_identities(
    tmp_path: Path,
):
    study = tmp_path / "study"
    study.mkdir()
    manifest = {
        "source_root": str(tmp_path / "sources"),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_relative_path": "source.pdf",
                "source_sha256": "a" * 64,
                "source_page_count": 1,
                "contains_report": False,
                "page_start": None,
                "page_end": None,
                "review_status": "verified",
            }
        ],
    }
    _write_verified_reviews(study, manifest)
    independent_path = study / "independent-boundary-review.json"
    independent = json.loads(independent_path.read_text(encoding="utf-8"))
    independent["review_generation_id"] = "shared-generation"
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    boundary_path = study / "boundary-review.json"
    boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
    boundary["review_generation_id"] = "shared-generation"
    boundary["independent_review"]["sha256"] = hashlib.sha256(
        independent_path.read_bytes()
    ).hexdigest()
    boundary_path.write_text(json.dumps(boundary), encoding="utf-8")
    boundary_reference = cast(dict[str, Any], manifest["boundary_review"])
    boundary_reference["sha256"] = hashlib.sha256(boundary_path.read_bytes()).hexdigest()

    errors = extractor._validate_review_artifacts(study, manifest, manifest["documents"])

    assert any(
        "review generation identities must be nonempty and distinct" in error
        for error in errors
    )


def test_extract_sections_removes_superseded_classification_output(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "loss adjustment report")
        document.save(source)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "loss adjustment report")
    manifest_path = study / "manifest.json"
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                "review_rationale": "Synthetic reviewed report range.",
                **cache_fields,
            }
        ],
    }
    _write_verified_reviews(study, manifest)
    extractor.extract_sections(study)
    assert (study / "sections/DOC_001").is_dir()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = manifest["documents"][0]
    record.update(
        {
            "contains_report": False,
            "page_start": None,
            "page_end": None,
            "review_rationale": "Synthetic reviewed no-report decision.",
        }
    )
    _write_verified_reviews(study, manifest)
    extractor.extract_sections(study)
    assert not (study / "sections/DOC_001").exists()
    assert (study / "no-report/DOC_001").is_dir()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = manifest["documents"][0]
    record.update(
        {
            "contains_report": True,
            "page_start": 1,
            "page_end": 1,
            "review_rationale": "Synthetic reviewed report range.",
        }
    )
    _write_verified_reviews(study, manifest)
    extractor.extract_sections(study)
    assert (study / "sections/DOC_001").is_dir()
    assert not (study / "no-report/DOC_001").exists()


def test_extract_sections_rejects_symlinked_output_directory(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "loss adjustment report")
        document.save(source)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "loss adjustment report")
    outside = tmp_path / "outside"
    outside.mkdir()
    (study / "sections").symlink_to(outside, target_is_directory=True)
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                **cache_fields,
            }
        ],
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_verified_reviews(study, manifest)

    with pytest.raises(ValueError, match="unsafe section output directory"):
        extractor.extract_sections(study)

    assert not (outside / "DOC_001").exists()


def test_extract_sections_rejects_source_outside_manifest_root(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    canonical_source = sources / "foreign.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "canonical source")
        document.save(canonical_source)
    foreign_source = tmp_path / "foreign.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "loss adjustment report")
        document.save(foreign_source)
    study = tmp_path / "study"
    study.mkdir()
    (study / "_ocr_cache").mkdir()
    (study / "_ocr_cache/DOC_001.txt").write_text(
        "loss adjustment report", encoding="utf-8"
    )
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(foreign_source),
                "source_relative_path": "foreign.pdf",
                "source_sha256": hashlib.sha256(foreign_source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                "ocr_cache_path": "_ocr_cache/DOC_001.txt",
            }
        ],
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="source path does not match manifest root"):
        extractor.extract_sections(study)

    assert not (study / "sections/DOC_001").exists()


def test_extract_sections_rejects_cache_tampered_after_scan(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "loss adjustment report")
        document.save(source)
    study = tmp_path / "study"
    cache_root = study / "_ocr_cache"
    cache_root.mkdir(parents=True)
    original_text = "loss adjustment report"
    cache_path = cache_root / "DOC_001.txt"
    cache_path.write_text(original_text, encoding="utf-8")
    metadata_path = cache_root / "DOC_001.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": "ocr_cache.v1",
                "extraction_contract_sha256": extractor.OCR_EXTRACTION_CONTRACT_SHA256,
                "document_id": "DOC_001",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_page_count": 1,
                "sidecar_sha256": hashlib.sha256(original_text.encode()).hexdigest(),
                "ocr_method": "embedded_text",
            }
        ),
        encoding="utf-8",
    )
    record = {
        "document_id": "DOC_001",
        "source_path": str(source),
        "source_relative_path": "source.pdf",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_page_count": 1,
        "ocr_method": "embedded_text",
        "ocr_cache_path": "_ocr_cache/DOC_001.txt",
        "ocr_cache_metadata_path": "_ocr_cache/DOC_001.json",
        "contains_report": True,
        "page_start": 1,
        "page_end": 1,
        "review_status": "verified",
    }
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [record],
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_verified_reviews(study, manifest)
    cache_path.write_text("tampered but still one page", encoding="utf-8")

    with pytest.raises(ValueError, match="OCR cache provenance mismatch"):
        extractor.extract_sections(study)

    assert not (study / "sections/DOC_001").exists()


def test_verified_cache_rejects_stale_extraction_contract(tmp_path: Path):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "source")
        document.save(source)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "source")
    metadata_path = study / cache_fields["ocr_cache_metadata_path"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["extraction_contract_sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    record = {
        "document_id": "DOC_001",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_page_count": 1,
        **cache_fields,
    }

    with pytest.raises(ValueError, match="OCR cache provenance mismatch"):
        extractor._load_verified_ocr_cache(study, record)


def test_validate_study_rejects_unrelated_pdf_with_updated_hash(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "reviewed source page")
        document.save(source)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "reviewed source page")
    manifest = {
        "schema_version": "loss_adjustment_corpus_manifest.v0.1",
        "source_root": str(sources),
        "study_root": str(study),
        "document_count": 1,
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_size_bytes": source.stat().st_size,
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                **cache_fields,
            }
        ],
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_verified_reviews(study, manifest)
    manifest = extractor.extract_sections(study)
    _write_verified_reviews(study, manifest)
    assert extractor.validate_study(study)["valid"] is True

    orphan = study / "sections/orphan.txt"
    orphan.write_text("orphan", encoding="utf-8")
    orphan_result = extractor.validate_study(study)
    assert orphan_result["valid"] is False
    assert any(
        "sections directory contains unexpected entries" in error
        for error in orphan_result["errors"]
    )
    orphan.unlink()

    nested_orphan = study / "sections/DOC_001/orphan-dir"
    nested_orphan.mkdir()
    (nested_orphan / "orphan.txt").write_text("orphan", encoding="utf-8")
    result = extractor.validate_study(study)
    assert result["valid"] is False
    assert any("unexpected report output files" in error for error in result["errors"])
    (nested_orphan / "orphan.txt").unlink()
    nested_orphan.rmdir()

    extra_cache = study / "_ocr_cache/EXTRA.txt"
    extra_cache.write_text("orphan cache", encoding="utf-8")
    cache_result = extractor.validate_study(study)
    assert cache_result["valid"] is False
    assert any(
        "OCR cache does not close over manifest documents" in error
        for error in cache_result["errors"]
    )
    extra_cache.unlink()

    text_path = study / "sections/DOC_001/loss-adjustment-report.txt"
    metadata_path = study / "sections/DOC_001/metadata.json"
    text_path.write_text("tampered but rehashed\n", encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["text_sha256"] = hashlib.sha256(text_path.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    text_result = extractor.validate_study(study)
    assert text_result["valid"] is False
    assert any(
        "section text does not match reviewed OCR slice" in error
        for error in text_result["errors"]
    ), text_result["errors"]

    extractor.extract_sections(study)
    extra_source = sources / "extra.pdf"
    with fitz.open() as extra:
        extra.new_page().insert_text((72, 72), "inventory drift")
        extra.save(extra_source)
    inventory_result = extractor.validate_study(study)
    assert inventory_result["valid"] is False
    assert any(
        "manifest does not exactly close over live source PDF inventory" in error
        for error in inventory_result["errors"]
    )
    extra_source.unlink()

    review_path = study / "boundary-review.json"
    original_review = review_path.read_text(encoding="utf-8")
    review = json.loads(original_review)
    review["reviewer"] = "tampered reviewer"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    review_result = extractor.validate_study(study)
    assert review_result["valid"] is False
    assert any("boundary review hash mismatch" in error for error in review_result["errors"])
    review_path.write_text(original_review, encoding="utf-8")

    output_pdf = study / "sections/DOC_001/loss-adjustment-report.pdf"
    with fitz.open() as unrelated:
        unrelated.new_page().insert_text((72, 72), "unrelated page")
        unrelated.save(output_pdf.with_suffix(".replacement.pdf"))
    output_pdf.with_suffix(".replacement.pdf").replace(output_pdf)
    metadata_path = study / "sections/DOC_001/metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["output_sha256"] = hashlib.sha256(output_pdf.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = extractor.validate_study(study)

    assert any("source page content mismatch" in error for error in result["errors"])


def test_extract_sections_rejects_unreviewed_live_pdf_before_output_mutation(
    tmp_path: Path,
):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "reviewed source page")
        document.save(source)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "reviewed source page")
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "document_count": 1,
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_size_bytes": source.stat().st_size,
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                **cache_fields,
            }
        ],
    }
    _write_verified_reviews(study, manifest)
    with fitz.open() as extra:
        extra.new_page().insert_text((72, 72), "unreviewed source page")
        extra.save(sources / "z.PDF")

    with pytest.raises(ValueError, match="exactly close over live source PDF inventory"):
        extractor.extract_sections(study)

    assert not (study / "sections").exists()
    assert not (study / "no-report").exists()


def test_extract_sections_preflights_all_caches_before_publication(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    study = tmp_path / "study"
    study.mkdir()
    records = []
    for index, name in enumerate(("a.pdf", "b.pdf"), start=1):
        document_id = f"DOC_{index:03d}"
        source = sources / name
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), f"source {index}")
            document.save(source)
        cache_fields = _write_bound_cache(
            study, source, f"source {index}", document_id=document_id
        )
        records.append(
            {
                "document_id": document_id,
                "source_path": str(source),
                "source_relative_path": name,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_size_bytes": source.stat().st_size,
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                **cache_fields,
            }
        )
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": records,
    }
    _write_verified_reviews(study, manifest)
    (study / "_ocr_cache/DOC_002.txt").write_text("tampered", encoding="utf-8")
    published_text = study / "sections/DOC_001/loss-adjustment-report.txt"
    published_text.parent.mkdir(parents=True)
    published_text.write_text("previously published\n", encoding="utf-8")

    with pytest.raises(ValueError, match="OCR cache provenance mismatch"):
        extractor.extract_sections(study)

    assert published_text.read_text(encoding="utf-8") == "previously published\n"


def test_extract_sections_cleans_staging_and_preserves_publication_on_generation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "source")
        document.save(source)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "source")
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_size_bytes": source.stat().st_size,
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                **cache_fields,
            }
        ],
    }
    _write_verified_reviews(study, manifest)
    published_text = study / "sections/DOC_001/loss-adjustment-report.txt"
    published_text.parent.mkdir(parents=True)
    published_text.write_text("previously published\n", encoding="utf-8")

    def fail_generation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("synthetic generation failure")

    monkeypatch.setattr(extractor, "extract_pdf_range", fail_generation)
    with pytest.raises(RuntimeError, match="synthetic generation failure"):
        extractor.extract_sections(study)

    assert published_text.read_text(encoding="utf-8") == "previously published\n"
    assert extractor._directory_entries(study / "_working") == set()


def test_recover_publication_rolls_back_mixed_generation_after_termination(
    tmp_path: Path,
):
    study = tmp_path / "study"
    staging = study / "_working/extract-interrupted"
    final_sections = study / "sections"
    final_no_report = study / "no-report"
    backup_sections = staging / "backup-sections"
    candidate_no_report = staging / "no-report"
    final_sections.mkdir(parents=True)
    final_no_report.mkdir()
    backup_sections.mkdir(parents=True)
    candidate_no_report.mkdir()
    (final_sections / "value").write_text("new", encoding="utf-8")
    (final_no_report / "value").write_text("old", encoding="utf-8")
    (backup_sections / "value").write_text("old", encoding="utf-8")
    journal = {
        "schema_version": "loss_adjustment_publication_journal.v1",
        "staging_relative": "_working/extract-interrupted",
        "targets": [
            {
                "relative": "sections",
                "candidate_relative": "_working/extract-interrupted/sections",
                "backup_relative": "_working/extract-interrupted/backup-sections",
                "had_previous": True,
                "previous_sha256": extractor._publication_fingerprint(
                    backup_sections
                ),
            },
            {
                "relative": "no-report",
                "candidate_relative": "_working/extract-interrupted/no-report",
                "backup_relative": "_working/extract-interrupted/backup-no-report",
                "had_previous": True,
                "previous_sha256": extractor._publication_fingerprint(
                    final_no_report
                ),
            },
        ],
    }
    _complete_extract_recovery_journal(study, staging, journal)
    (study / ".publication-journal.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    extractor._recover_publication(study)

    assert (final_sections / "value").read_text(encoding="utf-8") == "old"
    assert (final_no_report / "value").read_text(encoding="utf-8") == "old"
    assert not (study / ".publication-journal.json").exists()
    assert not staging.exists()


def test_recover_publication_preflights_all_backup_fingerprints_before_mutation(
    tmp_path: Path,
):
    study = tmp_path / "study"
    staging = study / "_working/extract-interrupted"
    final_sections = study / "sections"
    final_no_report = study / "no-report"
    final_manifest = study / "manifest.json"
    backup_sections = staging / "backup-sections"
    backup_no_report = staging / "backup-no-report"
    candidate_manifest = staging / "manifest.json"
    for directory in (
        final_sections,
        final_no_report,
        backup_sections,
        backup_no_report,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (final_sections / "value").write_text("new sections", encoding="utf-8")
    (final_no_report / "value").write_text("new no-report", encoding="utf-8")
    (backup_sections / "value").write_text("corrupt prior", encoding="utf-8")
    (backup_no_report / "value").write_text("old no-report", encoding="utf-8")
    final_manifest.write_text("old manifest", encoding="utf-8")
    candidate_manifest.write_text("new manifest", encoding="utf-8")
    expected_sections = tmp_path / "expected-sections"
    expected_sections.mkdir()
    (expected_sections / "value").write_text("old sections", encoding="utf-8")
    journal = {
        "schema_version": "loss_adjustment_publication_journal.v1",
        "staging_relative": "_working/extract-interrupted",
        "targets": [
            {
                "relative": "sections",
                "candidate_relative": "_working/extract-interrupted/sections",
                "backup_relative": "_working/extract-interrupted/backup-sections",
                "had_previous": True,
                "previous_sha256": extractor._publication_fingerprint(expected_sections),
                "previous_object": extractor._publication_object_token(
                    expected_sections
                ),
            },
            {
                "relative": "no-report",
                "candidate_relative": "_working/extract-interrupted/no-report",
                "backup_relative": "_working/extract-interrupted/backup-no-report",
                "had_previous": True,
                "previous_sha256": extractor._publication_fingerprint(backup_no_report),
            },
            {
                "relative": "manifest.json",
                "candidate_relative": "_working/extract-interrupted/manifest.json",
                "backup_relative": "_working/extract-interrupted/backup-manifest.json",
                "had_previous": True,
                "previous_sha256": extractor._publication_fingerprint(final_manifest),
            },
        ],
    }
    _complete_extract_recovery_journal(study, staging, journal)
    journal_path = study / ".publication-journal.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(RuntimeError, match="generation mismatch"):
        extractor._recover_publication(study)

    assert (final_sections / "value").read_text(encoding="utf-8") == "new sections"
    assert (final_no_report / "value").read_text(encoding="utf-8") == "new no-report"
    assert final_manifest.read_text(encoding="utf-8") == "old manifest"
    assert journal_path.exists()


def test_recover_publication_is_idempotent_after_restore_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study = tmp_path / "study"
    staging = study / "_working/extract-interrupted"
    final_sections = study / "sections"
    backup_sections = staging / "backup-sections"
    final_sections.mkdir(parents=True)
    backup_sections.mkdir(parents=True)
    (final_sections / "value").write_text("new", encoding="utf-8")
    (backup_sections / "value").write_text("old", encoding="utf-8")
    journal = {
        "schema_version": "loss_adjustment_publication_journal.v1",
        "staging_relative": "_working/extract-interrupted",
        "targets": [
            {
                "relative": "sections",
                "candidate_relative": "_working/extract-interrupted/sections",
                "backup_relative": "_working/extract-interrupted/backup-sections",
                "had_previous": True,
                "previous_sha256": extractor._publication_fingerprint(backup_sections),
            }
        ],
    }
    _complete_extract_recovery_journal(study, staging, journal)
    (study / ".publication-journal.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )
    original_renameat2 = extractor._renameat2

    def interrupt_after_restore(source: Path, target: Path, flags: int) -> None:
        original_renameat2(source, target, flags)
        if (
            source == final_sections
            and target == backup_sections
            and flags == extractor._RENAME_EXCHANGE
        ):
            raise KeyboardInterrupt("interrupt after restore")

    monkeypatch.setattr(extractor, "_renameat2", interrupt_after_restore)
    with pytest.raises(KeyboardInterrupt, match="interrupt after restore"):
        extractor._recover_publication(study)
    monkeypatch.setattr(extractor, "_renameat2", original_renameat2)

    extractor._recover_publication(study)

    assert (final_sections / "value").read_text(encoding="utf-8") == "old"
    assert not (study / ".publication-journal.json").exists()


def test_exclusive_study_lock_rejects_concurrent_mutation(tmp_path: Path):
    study = tmp_path / "study"
    sources = tmp_path / "sources"
    study.mkdir()
    sources.mkdir()
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "source")
        document.save(sources / "source.pdf")

    with extractor._exclusive_study_lock(study):
        assert len(extractor._active_study_lock_fds()) == 1
        with pytest.raises(RuntimeError, match="another study mutation is active"):
            extractor.scan_corpus(sources, study, document_jobs=1, ocr_jobs=1)
        with pytest.raises(RuntimeError, match="another study mutation is active"):
            extractor.validate_study(study)
    assert extractor._active_study_lock_fds() == ()


def test_study_root_replacement_before_lock_acquisition_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study = tmp_path / "study"
    moved = tmp_path / "moved-study"
    study.mkdir()
    original_open = extractor.os.open
    replaced = False

    def replace_before_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if Path(path) == study and flags & os.O_DIRECTORY and not replaced:
            replaced = True
            os.replace(study, moved)
            study.mkdir()
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(extractor.os, "open", replace_before_open)

    with pytest.raises(RuntimeError, match="changed before lock acquisition"):
        with extractor._exclusive_study_lock(study):
            pass

    assert moved.exists()
    assert not (study / ".study.lock").exists()


def test_locked_mutation_remains_bound_to_original_root_inode(tmp_path: Path):
    study = tmp_path / "study"
    moved = tmp_path / "moved-study"
    study.mkdir()

    @extractor._locked_study_mutation(0)
    def mutate(anchored_study: Path) -> None:
        os.replace(study, moved)
        study.mkdir()
        (study / "foreign").write_text("replacement", encoding="utf-8")
        (anchored_study / "owned").write_text("original", encoding="utf-8")

    mutate(study)

    assert (moved / "owned").read_text(encoding="utf-8") == "original"
    assert not (study / "owned").exists()
    assert (study / "foreign").read_text(encoding="utf-8") == "replacement"


def test_apply_review_remains_bound_to_locked_root_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study = tmp_path / "study"
    moved = tmp_path / "moved-study"
    study.mkdir()
    manifest = study / "manifest.json"
    review = study / "boundary-review.json"
    manifest.write_text("{}", encoding="utf-8")
    review.write_text("{}", encoding="utf-8")

    def apply_on_anchored_root(
        anchored_manifest: Path, anchored_review: Path
    ) -> dict[str, Any]:
        assert "/proc/self/fd/" in anchored_manifest.as_posix()
        assert anchored_review.parent == anchored_manifest.parent
        os.replace(study, moved)
        study.mkdir()
        (anchored_manifest.parent / "applied.marker").write_text(
            "locked", encoding="utf-8"
        )
        return {"applied": True}

    monkeypatch.setattr(
        extractor, "_apply_boundary_review_unlocked", apply_on_anchored_root
    )

    assert extractor.apply_boundary_review(manifest, review) == {"applied": True}
    assert (moved / "applied.marker").read_text(encoding="utf-8") == "locked"
    assert not (study / "applied.marker").exists()


def test_validate_remains_bound_to_locked_root_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study = tmp_path / "study"
    moved = tmp_path / "moved-study"
    study.mkdir()

    def validate_anchored(anchored_root: Path) -> dict[str, Any]:
        assert "/proc/self/fd/" in anchored_root.as_posix()
        os.replace(study, moved)
        study.mkdir()
        (anchored_root / "validated.marker").write_text("locked", encoding="utf-8")
        return {"valid": True}

    monkeypatch.setattr(extractor, "_validate_study_unlocked", validate_anchored)

    assert extractor.validate_study(study) == {"valid": True}
    assert (moved / "validated.marker").read_text(encoding="utf-8") == "locked"
    assert not (study / "validated.marker").exists()


def test_scan_publishes_canonical_cache_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study = tmp_path / "study"
    sources = tmp_path / "sources"
    study.mkdir()
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "source")
        document.save(source)

    def fake_ocr(record, cache_root, worker_root, ocr_jobs, source_pdf_bytes):
        fields = _write_bound_cache(cache_root.parent, source, "손해사정서")
        return {
            "pages": ["손해사정서"],
            "ocr_method": fields["ocr_method"],
            "ocr_sidecar_sha256": fields["ocr_sidecar_sha256"],
            "cache_reused": False,
            "cache_path": cache_root / "DOC_001.txt",
            "cache_metadata_path": cache_root / "DOC_001.json",
        }

    monkeypatch.setattr(extractor, "_ocr_document", fake_ocr)

    manifest = extractor.scan_corpus(sources, study, document_jobs=1, ocr_jobs=1)

    assert manifest["study_root"] == study.resolve().as_posix()
    record = manifest["documents"][0]
    assert record["ocr_cache_path"] == "_ocr_cache/DOC_001.txt"
    assert record["ocr_cache_metadata_path"] == "_ocr_cache/DOC_001.json"
    assert (study / record["ocr_cache_path"]).is_file()
    assert (study / record["ocr_cache_metadata_path"]).is_file()


def test_scan_worker_failure_preserves_prior_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study = tmp_path / "study"
    sources = tmp_path / "sources"
    study.mkdir()
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "source")
        document.save(source)
    (study / "_ocr_cache").mkdir()
    (study / "_ocr_cache/old.txt").write_text("old cache", encoding="utf-8")
    (study / "manifest.json").write_text('{"generation":"old"}\n', encoding="utf-8")
    previous_manifest = (study / "manifest.json").read_bytes()
    previous_cache = (study / "_ocr_cache/old.txt").read_bytes()

    def fail_ocr(*args, **kwargs):
        raise RuntimeError("synthetic OCR failure")

    monkeypatch.setattr(extractor, "_ocr_document", fail_ocr)

    with pytest.raises(RuntimeError, match="scan failed for 1 document"):
        extractor.scan_corpus(sources, study, document_jobs=1, ocr_jobs=1)

    assert (study / "manifest.json").read_bytes() == previous_manifest
    assert (study / "_ocr_cache/old.txt").read_bytes() == previous_cache
    assert not (study / ".publication-journal.json").exists()


def test_publish_generation_preserves_foreign_final_inserted_before_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study, staging = _publication_generation(tmp_path)
    final_sections = study / "sections"
    original_exchange = extractor._exchange_verified

    def insert_foreign_before_exchange(
        left: Path,
        left_hash: str,
        left_object: dict[str, int],
        right: Path,
        right_hash: str,
        right_object: dict[str, int],
    ) -> None:
        if left == final_sections:
            shutil.rmtree(left)
            left.mkdir()
            (left / "value").write_text("foreign", encoding="utf-8")
        original_exchange(
            left, left_hash, left_object, right, right_hash, right_object
        )

    monkeypatch.setattr(extractor, "_exchange_verified", insert_foreign_before_exchange)

    with pytest.raises(RuntimeError, match="durable rollback could not complete"):
        extractor._publish_generation(
            study, staging, ["sections", "no-report", "manifest.json"]
        )

    assert (final_sections / "value").read_text(encoding="utf-8") == "foreign"
    assert (study / ".publication-journal.json").exists()
    assert staging.exists()


def test_recovery_preserves_backup_replaced_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study = tmp_path / "study"
    staging = study / "_working/extract-interrupted"
    final_sections = study / "sections"
    backup_sections = staging / "backup-sections"
    final_sections.mkdir(parents=True)
    backup_sections.mkdir(parents=True)
    (final_sections / "value").write_text("new", encoding="utf-8")
    (backup_sections / "value").write_text("old", encoding="utf-8")
    journal = {
        "schema_version": "loss_adjustment_publication_journal.v1",
        "staging_relative": "_working/extract-interrupted",
        "targets": [
            {
                "relative": "sections",
                "candidate_relative": "_working/extract-interrupted/sections",
                "backup_relative": "_working/extract-interrupted/backup-sections",
                "had_previous": True,
                "previous_sha256": extractor._publication_fingerprint(backup_sections),
            }
        ],
    }
    _complete_extract_recovery_journal(study, staging, journal)
    journal_path = study / ".publication-journal.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    original_exchange = extractor._exchange_verified

    def replace_backup_before_exchange(
        left: Path,
        left_hash: str,
        left_object: dict[str, int],
        right: Path,
        right_hash: str,
        right_object: dict[str, int],
    ) -> None:
        if right == backup_sections:
            shutil.rmtree(right)
            right.mkdir()
            (right / "value").write_text("foreign", encoding="utf-8")
        original_exchange(
            left, left_hash, left_object, right, right_hash, right_object
        )

    monkeypatch.setattr(extractor, "_exchange_verified", replace_backup_before_exchange)

    with pytest.raises(RuntimeError, match="exchange preimage changed"):
        extractor._recover_publication(study)

    assert (final_sections / "value").read_text(encoding="utf-8") == "new"
    assert (backup_sections / "value").read_text(encoding="utf-8") == "foreign"
    assert journal_path.exists()


def test_publication_preserves_unknown_staging_entry(tmp_path: Path):
    study, staging = _publication_generation(tmp_path)
    foreign = staging / "foreign"
    foreign.write_text("preserve", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not transaction-closed"):
        extractor._publish_generation(
            study, staging, ["sections", "no-report", "manifest.json"]
        )

    assert foreign.read_text(encoding="utf-8") == "preserve"
    assert not (study / ".publication-journal.json").exists()


def test_publish_generation_rolls_back_keyboard_interrupt_between_swaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study = tmp_path / "study"
    staging = study / "_working/extract-candidate"
    for name in ("sections", "no-report"):
        (study / name).mkdir(parents=True)
        (study / name / "value").write_text("old", encoding="utf-8")
        (staging / name).mkdir(parents=True)
        (staging / name / "value").write_text("new", encoding="utf-8")
    (study / "manifest.json").write_text("old manifest", encoding="utf-8")
    (staging / "manifest.json").write_text("new manifest", encoding="utf-8")
    original_renameat2 = extractor._renameat2

    def interrupt_no_report(source: Path, target: Path, flags: int) -> None:
        if (
            source == study / "no-report"
            and target == staging / "no-report"
            and flags == extractor._RENAME_EXCHANGE
        ):
            raise KeyboardInterrupt("synthetic termination")
        original_renameat2(source, target, flags)

    monkeypatch.setattr(extractor, "_renameat2", interrupt_no_report)

    with pytest.raises(KeyboardInterrupt, match="synthetic termination"):
        extractor._publish_generation(
            study,
            staging,
            ["sections", "no-report", "manifest.json"],
        )

    assert (study / "sections/value").read_text(encoding="utf-8") == "old"
    assert (study / "no-report/value").read_text(encoding="utf-8") == "old"
    assert (study / "manifest.json").read_text(encoding="utf-8") == "old manifest"
    assert not (study / ".publication-journal.json").exists()


def test_publish_generation_rolls_back_when_commit_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study = tmp_path / "study"
    staging = study / "_working/extract-candidate"
    final_sections = study / "sections"
    candidate_sections = staging / "sections"
    final_sections.mkdir(parents=True)
    candidate_sections.mkdir(parents=True)
    (final_sections / "value").write_text("old", encoding="utf-8")
    (candidate_sections / "value").write_text("new", encoding="utf-8")
    (study / "no-report").mkdir()
    (staging / "no-report").mkdir()
    (study / "no-report/value").write_text("old", encoding="utf-8")
    (staging / "no-report/value").write_text("new", encoding="utf-8")
    (study / "manifest.json").write_text("old manifest", encoding="utf-8")
    (staging / "manifest.json").write_text("new manifest", encoding="utf-8")
    journal = study / ".publication-journal.json"
    original_fsync_directory = extractor._fsync_directory
    failed = False

    def fail_commit_fsync(path: Path) -> None:
        nonlocal failed
        if path == study and not journal.exists() and not failed:
            failed = True
            raise OSError("commit fsync failed")
        original_fsync_directory(path)

    monkeypatch.setattr(extractor, "_fsync_directory", fail_commit_fsync)

    with pytest.raises(OSError, match="commit fsync failed"):
        extractor._publish_generation(
            study, staging, ["sections", "no-report", "manifest.json"]
        )

    assert (final_sections / "value").read_text(encoding="utf-8") == "new"
    assert not journal.exists()


def test_recover_committed_publication_resumes_quarantined_cleanup(tmp_path: Path):
    study, staging = _publication_generation(tmp_path)
    targets = []
    for relative in ("sections", "no-report", "manifest.json"):
        final = study / relative
        candidate = staging / relative
        backup = staging / f"backup-{Path(relative).name}"
        target = {
            "relative": relative,
            "candidate_relative": f"_working/extract-candidate/{relative}",
            "backup_relative": (
                f"_working/extract-candidate/backup-{Path(relative).name}"
            ),
            "had_previous": True,
            "previous_sha256": extractor._publication_fingerprint(final),
            "previous_object": extractor._publication_object_token(final),
            "candidate_sha256": extractor._publication_fingerprint(candidate),
            "candidate_object": extractor._publication_object_token(candidate),
        }
        extractor._exchange_verified(
            final,
            target["previous_sha256"],
            target["previous_object"],
            candidate,
            target["candidate_sha256"],
            target["candidate_object"],
        )
        extractor._move_noreplace_verified(
            candidate,
            target["previous_sha256"],
            target["previous_object"],
            backup,
        )
        targets.append(target)
    journal = {
        "schema_version": "loss_adjustment_publication_journal.v1",
        "phase": "committed",
        "staging_relative": "_working/extract-candidate",
        "staging_object": extractor._publication_object_token(staging),
        "targets": targets,
    }
    journal_path = study / ".publication-journal.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    backup_sections = staging / "backup-sections"
    quarantined = staging / ".remove-backup-sections"
    extractor._move_noreplace_verified(
        backup_sections,
        targets[0]["previous_sha256"],
        targets[0]["previous_object"],
        quarantined,
    )

    extractor._recover_publication(study)

    assert (study / "sections/value").read_text(encoding="utf-8") == "new"
    assert not journal_path.exists()
    assert not staging.exists()


def test_exclusive_study_lock_preserves_unjournaled_orphan_staging(tmp_path: Path):
    study = tmp_path / "study"
    orphan = study / "_working/extract-committed-orphan"
    orphan.mkdir(parents=True)
    (orphan / "backup-sections").mkdir()

    with pytest.raises(RuntimeError, match="preserved for review"):
        with extractor._exclusive_study_lock(study):
            pass

    assert orphan.exists()


def test_extract_sections_rejects_source_replacement_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "original source")
        document.save(source)
    replacement = tmp_path / "replacement.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "replacement source")
        document.save(replacement)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "original source")
    manifest = {
        "source_root": str(sources),
        "study_root": str(study),
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_size_bytes": source.stat().st_size,
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                **cache_fields,
            }
        ],
    }
    _write_verified_reviews(study, manifest)
    published_text = study / "sections/DOC_001/loss-adjustment-report.txt"
    published_text.parent.mkdir(parents=True)
    published_text.write_text("previously published\n", encoding="utf-8")
    original_extract = extractor.extract_pdf_range

    def replace_source_then_extract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        os.replace(replacement, source)
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(extractor, "extract_pdf_range", replace_source_then_extract)

    with pytest.raises(ValueError, match="live source PDF inventory"):
        extractor.extract_sections(study)

    assert published_text.read_text(encoding="utf-8") == "previously published\n"
    assert extractor._directory_entries(study / "_working") == set()


def test_extract_sections_rejects_review_mutation_at_commit_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study, _, _ = _build_reviewed_study(tmp_path)
    published_text = study / "sections/DOC_001/loss-adjustment-report.txt"
    published_text.parent.mkdir(parents=True)
    published_text.write_text("previously published\n", encoding="utf-8")
    original_publish = extractor._publish_generation

    def mutate_review_then_publish(*args: Any, **kwargs: Any) -> None:
        review_path = study / "boundary-review.json"
        review_path.write_bytes(review_path.read_bytes() + b" ")
        original_publish(*args, **kwargs)

    monkeypatch.setattr(extractor, "_publish_generation", mutate_review_then_publish)

    with pytest.raises(ValueError, match="boundary review hash mismatch"):
        extractor.extract_sections(study)

    assert published_text.read_text(encoding="utf-8") == "previously published\n"
    assert extractor._directory_entries(study / "_working") == set()


def test_adopt_reviewed_cache_failure_preserves_whole_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study, _, manifest = _build_reviewed_study(tmp_path)
    manifest = extractor.extract_sections(study)
    _write_verified_reviews(study, manifest)
    manifest_before = (study / "manifest.json").read_bytes()
    metadata_before = (study / "_ocr_cache/DOC_001.json").read_bytes()

    def fail_publish(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic publication failure")

    monkeypatch.setattr(extractor, "_publish_generation", fail_publish)

    with pytest.raises(RuntimeError, match="synthetic publication failure"):
        extractor.adopt_reviewed_cache_provenance(study)

    assert (study / "manifest.json").read_bytes() == manifest_before
    assert (study / "_ocr_cache/DOC_001.json").read_bytes() == metadata_before
    assert extractor._directory_entries(study / "_working") == set()


def test_adopt_reviewed_cache_provenance_rejects_legacy_contract(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "source.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "reviewed source page")
        document.save(source)
    study = tmp_path / "study"
    study.mkdir()
    cache_fields = _write_bound_cache(study, source, "reviewed source page")
    manifest = {
        "schema_version": "loss_adjustment_corpus_manifest.v0.1",
        "source_root": str(sources),
        "study_root": str(study),
        "document_count": 1,
        "documents": [
            {
                "document_id": "DOC_001",
                "source_path": str(source),
                "source_relative_path": "source.pdf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_size_bytes": source.stat().st_size,
                "source_page_count": 1,
                "contains_report": True,
                "page_start": 1,
                "page_end": 1,
                "review_status": "verified",
                **cache_fields,
            }
        ],
    }
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_verified_reviews(study, manifest)
    manifest = extractor.extract_sections(study)
    _write_verified_reviews(study, manifest)

    cache_metadata = study / "_ocr_cache/DOC_001.json"
    cache_metadata.unlink()
    manifest = json.loads((study / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["study_root"] == study.resolve().as_posix()
    record = manifest["documents"][0]
    record.pop("ocr_cache_metadata_path")
    record["ocr_method"] = "cached"
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        ValueError, match="legacy cache lacks the current extraction contract"
    ):
        extractor.adopt_reviewed_cache_provenance(study)

    assert not cache_metadata.exists()
    unchanged = json.loads((study / "manifest.json").read_text(encoding="utf-8"))
    assert "ocr_cache_metadata_path" not in unchanged["documents"][0]


def test_publication_rejects_same_bytes_on_different_final_inode_before_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study, staging = _publication_generation(tmp_path)
    final_sections = study / "sections"
    original_exchange = extractor._exchange_verified

    def replace_with_same_bytes_before_exchange(
        left: Path,
        left_hash: str,
        left_object: dict[str, int],
        right: Path,
        right_hash: str,
        right_object: dict[str, int],
    ) -> None:
        if left == final_sections:
            value = (left / "value").read_bytes()
            shutil.rmtree(left)
            left.mkdir()
            (left / "value").write_bytes(value)
        original_exchange(
            left, left_hash, left_object, right, right_hash, right_object
        )

    monkeypatch.setattr(
        extractor, "_exchange_verified", replace_with_same_bytes_before_exchange
    )

    with pytest.raises(RuntimeError, match="durable rollback could not complete"):
        extractor._publish_generation(
            study, staging, ["sections", "no-report", "manifest.json"]
        )

    assert (final_sections / "value").read_text(encoding="utf-8") == "old"
    assert (study / ".publication-journal.json").exists()


def test_exchange_postcheck_preserves_newer_foreign_live_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    left = tmp_path / "live"
    right = tmp_path / "candidate"
    left.mkdir()
    right.mkdir()
    (left / "value").write_text("old", encoding="utf-8")
    (right / "value").write_text("candidate", encoding="utf-8")
    left_hash, left_object = extractor._capture_publication_generation(left)
    right_hash, right_object = extractor._capture_publication_generation(right)
    original_renameat2 = extractor._renameat2

    def replace_live_after_exchange(source: Path, target: Path, flags: int) -> None:
        original_renameat2(source, target, flags)
        if source == left and flags == extractor._RENAME_EXCHANGE:
            shutil.rmtree(source)
            source.mkdir()
            (source / "value").write_text("newer foreign", encoding="utf-8")

    monkeypatch.setattr(extractor, "_renameat2", replace_live_after_exchange)

    with pytest.raises(RuntimeError, match="exchange identity changed"):
        extractor._exchange_verified(
            left, left_hash, left_object, right, right_hash, right_object
        )

    assert (left / "value").read_text(encoding="utf-8") == "newer foreign"
    assert (right / "value").read_text(encoding="utf-8") == "old"


def test_cleanup_preserves_quarantine_replaced_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "value").write_text("owned", encoding="utf-8")
    expected_hash, expected_object = extractor._capture_publication_generation(backup)
    original_remove = extractor._remove_publication_path_verified

    def replace_quarantine_before_remove(
        path: Path, expected_token: dict[str, int]
    ) -> None:
        shutil.rmtree(path)
        path.mkdir()
        (path / "value").write_text("foreign", encoding="utf-8")
        original_remove(path, expected_token)

    monkeypatch.setattr(
        extractor, "_remove_publication_path_verified", replace_quarantine_before_remove
    )

    with pytest.raises(RuntimeError, match="cleanup object changed"):
        extractor._quarantine_and_remove_verified(
            backup, expected_hash, expected_object
        )

    quarantine = tmp_path / ".remove-backup"
    assert (quarantine / "value").read_text(encoding="utf-8") == "foreign"


def test_publication_journal_create_does_not_replace_foreign_race_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    study, staging = _publication_generation(tmp_path)
    journal_path = study / ".publication-journal.json"
    original_renameat2 = extractor._renameat2

    def insert_foreign_journal(source: Path, target: Path, flags: int) -> None:
        if target == journal_path and flags == extractor._RENAME_NOREPLACE:
            target.write_text("foreign journal", encoding="utf-8")
        original_renameat2(source, target, flags)

    monkeypatch.setattr(extractor, "_renameat2", insert_foreign_journal)

    with pytest.raises(OSError):
        extractor._publish_generation(
            study, staging, ["sections", "no-report", "manifest.json"]
        )

    assert journal_path.read_text(encoding="utf-8") == "foreign journal"
    assert (study / "sections/value").read_text(encoding="utf-8") == "old"
    assert (staging / "sections/value").read_text(encoding="utf-8") == "new"


def test_publication_reruns_commit_fence_after_all_target_cas(tmp_path: Path):
    study, staging = _publication_generation(tmp_path)
    checks = 0

    def late_fence() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("late fence changed")

    with pytest.raises(RuntimeError, match="late fence changed"):
        extractor._publish_generation(
            study,
            staging,
            ["sections", "no-report", "manifest.json"],
            pre_commit_check=late_fence,
        )

    assert checks == 2
    assert (study / "sections/value").read_text(encoding="utf-8") == "old"
    assert (study / "no-report/value").read_text(encoding="utf-8") == "old"
    assert (study / "manifest.json").read_text(encoding="utf-8") == "old manifest"
    assert not (study / ".publication-journal.json").exists()
