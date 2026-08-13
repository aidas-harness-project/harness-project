import hashlib
import json
from pathlib import Path

import fitz
import pytest

import extract_loss_adjustment_sections as extractor


def _write_bound_cache(study: Path, source: Path, text: str) -> dict:
    cache_root = study / "_ocr_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / "DOC_001.txt").write_text(text, encoding="utf-8")
    (cache_root / "DOC_001.json").write_text(
        json.dumps(
            {
                "schema_version": "ocr_cache.v1",
                "document_id": "DOC_001",
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
        "ocr_cache_path": "_ocr_cache/DOC_001.txt",
        "ocr_cache_metadata_path": "_ocr_cache/DOC_001.json",
    }


def _write_verified_reviews(study: Path, manifest: dict) -> None:
    independent_path = study / "independent-boundary-review.json"
    independent = {
        "schema_version": "loss_adjustment_independent_boundary_review.v1",
        "status": "verified",
        "decision_count": len(manifest["documents"]),
        "mismatch_count": 0,
        "decisions": [
            {
                "document_id": record["document_id"],
                "source_relative_path": record["source_relative_path"],
                "source_sha256": record["source_sha256"],
                "source_page_count": record["source_page_count"],
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
        "schema_version": "loss_adjustment_boundary_review.v0.1",
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
    assert (cache_root / "DOC_001.json").is_file()
    assert "fresh embedded source text" in (cache_root / "DOC_001.txt").read_text(
        encoding="utf-8"
    )


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


def test_apply_boundary_review_verifies_every_manifest_document(tmp_path, monkeypatch):
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
    reviewed_bytes = review_path.read_bytes()

    def replace_review_before_hash(path: Path) -> str:
        replacement = json.loads(reviewed_bytes)
        replacement["decisions"][0]["rationale"] = "foreign replacement"
        review_path.write_text(json.dumps(replacement), encoding="utf-8")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(extractor, "sha256_file", replace_review_before_hash)

    result = extractor.apply_boundary_review(manifest_path, review_path)

    assert result["documents"][0]["page_start"] == 2
    assert result["documents"][0]["page_end"] == 6
    assert result["documents"][0]["review_status"] == "verified"
    assert result["documents"][1]["contains_report"] is False
    assert result["documents"][1]["review_status"] == "verified"
    assert result["boundary_review"]["sha256"] == hashlib.sha256(reviewed_bytes).hexdigest()


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

    with pytest.raises(ValueError, match="approved sources tree"):
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

    with pytest.raises(ValueError, match="unsafe source PDF path|symlink component"):
        extractor._source_inventory(sources)


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

    with pytest.raises(ValueError, match="governed harness state"):
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

    with pytest.raises(ValueError, match="governed harness state"):
        extractor.extract_sections(study)
    with pytest.raises(ValueError, match="governed harness state"):
        extractor.adopt_reviewed_cache_provenance(study)
    validation = extractor.validate_study(study)
    assert any("governed harness state" in error for error in validation["errors"])


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
            "contains_report": True,
            "page_start": 1,
            "page_end": 1,
        },
        {
            "document_id": "DOC_002",
            "source_relative_path": "second.pdf",
            "source_sha256": "2" * 64,
            "source_page_count": 1,
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

    duplicate_inventory = json.loads(json.dumps(manifest))
    duplicate_inventory["documents"][1]["document_id"] = "DOC_001"
    duplicate_errors = extractor._validate_review_artifacts(
        study, duplicate_inventory, duplicate_inventory["documents"]
    )
    assert any(
        "manifest contains duplicate document IDs" in error
        for error in duplicate_errors
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


def test_adopt_reviewed_cache_provenance_binds_only_verified_study(tmp_path: Path):
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
    record = manifest["documents"][0]
    record.pop("ocr_cache_metadata_path")
    record["ocr_method"] = "cached"
    (study / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    adopted = extractor.adopt_reviewed_cache_provenance(study)

    adopted_record = adopted["documents"][0]
    assert adopted_record["ocr_cache_metadata_path"] == "_ocr_cache/DOC_001.json"
    metadata = json.loads(cache_metadata.read_text(encoding="utf-8"))
    assert metadata["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert metadata["binding_basis"] == "reviewed_legacy_sidecar"
    assert extractor.validate_study(study)["valid"] is True
